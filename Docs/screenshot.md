# 截图（Screen Sensor）逻辑总览

> OtterScope 当前截图链路（采集 → 编码 → 发送 → 历史压缩 → 落盘）的实现备忘。
> 主要代码：[python/otterscope/screen.py](../python/otterscope/screen.py),
> [python/otterscope/loop.py](../python/otterscope/loop.py),
> [python/otterscope/context_manager.py](../python/otterscope/context_manager.py),
> [python/otterscope/tools.py](../python/otterscope/tools.py),
> [python/otterscope/config.py](../python/otterscope/config.py).

---

## 1. 截图是 `computer` 工具的一个 action，不是独立 tool

所有桌面交互（截图 / 鼠标 / 键盘）都收在同一个 `computer` 工具下，按 `action` 字段区分：

```jsonc
{
  "name": "computer",
  "arguments": {
    "action": "screenshot",
    "level": "fullscreen" | "active_window" | "cursor_local"
  }
}
```

为什么不拆：

- **对齐 Anthropic computer-use 规范**（`computer_20250124`），未来直连 Claude 原生 API 不用转换层。
- **共享坐标系**：`screenshot` 设置的 `last_capture` 直接被随后的 `left_click` 用来反算屏幕坐标。
- **Hook 实现简单**：pre-click verify、落点 L3 取证都按 "tool name + action" 单一分支判断。

---

## 2. 三级金字塔（L1 / L2 / L3）

`ScreenLevel` 三档（`screen.py`）：

| 级别 | 名字 | 范围 | 默认下采样 (`config.ScreenshotConfig`) | 典型场景 |
|------|------|------|--------|--------|
| **L1** | `fullscreen` | 整个虚拟桌面（跨多显示器） | `l1_max_long_edge = 1568` | 起手识局、找窗口 |
| **L2** | `active_window` | 当前前台窗口的客户区 | `l2_max_long_edge = 1568` | 在 App 内部操作 |
| **L3** | `cursor_local` | 默认 = UIA 拿到的鼠标下最小 UI 元素的 bounding rect（加 16px padding）；退化为鼠标周围 `2*l3_radius_px` 见方（默认 200×200） | `l3_max_long_edge = 0`（不缩放） | 看小按钮 / 文字 |

> L0 是逻辑上的"教学图"层，**不是 ScreenSensor 拍的**；它是 `icon_memory.py` 拼出来
> 的图标合集（"icon atlas"），永久跟随 system prompt 注入。

### 2.1 L3 smart sizing（UIA-驱动的动态 crop）

默认开启（`[screenshot] l3_smart_enabled = true`）。L3 不再是固定 200×200：

```
_capture_cursor_local(cx, cy):
    rect = uia.element_rect_at(cx, cy)        # 调 IUIAutomation::ElementFromPoint
    if rect is None:                           # UIA 失败 / 远程桌面 / 受保护进程
        → 退化 fixed 200×200
    if rect 太大 (超屏幕 l3_smart_max_ratio = 40%):
        → 退化 fixed                          # 命中了顶层窗口 / 容器
    if rect.w < l3_smart_min_w (=160):
        以鼠标为中心拓到 min_w                 # 防像素级 element
    if rect.h < l3_smart_min_h (=80):
        拓到 min_h
    rect 四周外拓 l3_smart_padding_px (=16)
    clamp 到屏幕边界
```

实现：[python/otterscope/uia.py](../python/otterscope/uia.py) 是一个纯 `ctypes` COM 包装（~150 行，不引入新依赖），第一次调用 ~60ms（COM 初始化），后续 <5ms。

为什么引入这个：原固定 200×200 遇到计算器显示区这种细长控件（约 280×60）会拍不全上下文；遇到侧边栏这种细高窓口也一样。参考 Snipaste “鼠标隐居 UI 元素 bbox” 的交互，UIA 取到的 rect 往往是“语义上最小有意义的 widget”，拍下来后上下文完整。这不破坏“纯视觉”原则——L3 **给模型的仍然只是像素**，UIA 只决定 crop 矩形。

---
每张 `Capture` 携带：

- `image: PIL.Image`（已下采样）
- `raw_size`（物理像素）/ `sent_size`（实际发给 LLM 的像素）
- `offset`（在虚拟屏幕坐标系里的左上角）
- `phash`（dHash，用于变化检测）
- `model_to_screen(x, y)` —— LLM 给的图内坐标 → 真实屏幕坐标

LLM 给 `coordinate` 时必须用**当前 `last_capture` 的 sent_size 坐标系**；系统按 `offset + scale` 反算回物理坐标驱动鼠标。

---

## 3. 何时拍：自动 vs. 模型主动

### 3.1 起手 L1
`Agent._run` 一启动抓一张 L1（`first = self.sensor.capture(L1)`），落盘 `step-000-init.png`，随首条 user message 发出。

### 3.2 模型主动 `screenshot`
模型可以调 `computer({action: "screenshot", level: ...})` 任意级别。`tool.last_capture` 会被这一张更新，loop 的"动作后回送"阶段会优先复用它。

### 3.3 动作后自动补图（post-step）—— **L2 只补一次，之后默认 L3**

这是当前最关键的一条策略，由 `tool.active_app_rect` 和 `tool.active_app_l2_shown` 两个状态位驱动（[loop.py L1102-1150](../python/otterscope/loop.py#L1102-L1150)）：

```
本步是否调用了 screenshot？
├─ 是 → 复用 tool.last_capture（模型自己选了 level）
└─ 否 → 看 active_app_rect 是否被 pin：
        ├─ 未 pin（无 active app）：拍 L1 兜底
        └─ 已 pin（launch_app / focus_window 成功后）：
              ├─ active_app_l2_shown == False（第一次）：
              │     拍 App 矩形的 L2 当 "map"
              │     更新 last_capture（坐标系基准）
              │     active_app_l2_shown = True
              └─ active_app_l2_shown == True（后续每一步）：
                    检查鼠标是否还在 active_app_rect 内？
                    ├─ 在  → 拍 L3 cursor-local
                    │       **不覆盖 last_capture** —— L2 map 仍是坐标系基准
                    └─ 不在 → 跳过 L3，重拍 active_app_rect 的 L2
                              更新 last_capture（坐标系基准刷新）
                              tag 注明 "cursor outside app rect → L3 skipped"
```

要点：
- **同一个 pin 下，post-step 的 L2 通常只拍一次。** 之后每一步 post 默认是小尺寸 L3 cursor-local，省 token、省网络、省模型 vision pass。
- L3 拍完**不覆盖** `last_capture`，所以模型下一步给的坐标继续按 L2 map 的尺寸算。
- **鼠标越界守卫**（[loop.py L1283-1326](../python/otterscope/loop.py#L1283-L1326)）：拍 L3 之前会用 `window.cursor_pos()` 校验当前光标位置是否还在 `active_app_rect` 矩形内。如果不在（典型场景：点击意外把焦点切到别的 app、模态弹窗抢焦点、`focus_window` 把目标窗口移到新位置但鼠标仍停在旧坐标），L3 cursor-local 拍出来全是无关像素——这种时候直接降级到"重拍一张 active_app_rect 的 L2"，并刷新 `last_capture` 让坐标系重新对齐。tag 文本里会注明 `cursor outside app rect → L3 skipped; cursor=(cx,cy) rect=(...)`，方便事后排查。
- 想刷新 L2 map：模型显式 `screenshot(level="active_window")`，会同时把 `active_app_l2_shown` 置 True，下一步仍走 L3。
- 离开 App / 释放 pin：模型显式 `screenshot(level="fullscreen")`，`active_app_rect` 被清掉，恢复到无 pin 的 L1 默认。
- L2 / L3 两条路径任何一步异常都会回退到 L1（并清掉 pin），保证至少有图。
- **可选关闭 L3 降级**：`[screenshot] post_step_use_l3 = false` 时，pin 之后每一步都重新拍 active_app_rect 的 L2，不再降级到 L3 cursor-local。代价是每步多一张大图，收益是适合"点击常导致焦点跳到远离鼠标位置"的 App——典型例子 **微信点联系人**：点中后焦点跳到右下输入框，鼠标仍停在左侧联系人列表上，L3 cursor-local 只拍到联系人 hover 状态变化（<1%），跟"没点中"长得一模一样，模型容易卡在反复点同一个联系人。如果你的工作流里这种 App 占主要部分，可以打开这个开关。注意：上面那条"鼠标越界守卫"已经能解决"焦点完全切到另一个 app"的退化场景；`post_step_use_l3 = false` 解决的是"焦点没跑出 rect、但跳到 rect 内远离鼠标的位置"。

### 3.3.1 例外：click_verify 判定 miss 会附一张 L3 提醒

`screenshot.click_verify_enabled = True` 时，每次点击走"前后像素差校验"（[tools.py L260-310](../python/otterscope/tools.py#L260-L310)）：

```
[拍] pre_l3  = capture_around(x, y, r)
do_click(x, y)
sleep click_verify_post_sleep_ms
[拍] post_l3 = capture_around(x, y, r)
ratio = pixel_diff_ratio(pre_l3, post_l3)

if ratio < click_no_change_threshold (默认 0.5%):
    把 post_l3 当 follow-up image 附进 ToolResult
    **不动 last_capture，不动 active_app_rect**
    hint 文案告诉模型："可能没点中；坐标系没变；要重对齐请自己 screenshot"
else:
    只回 text，不附图
```

**关键：miss 兜底图是 L3，不是 L2。** 早期实现里这里会拍一张 `sensor.capture(L2)` 当 follow-up，但那张 L2 是按"当前前台窗口"重新查的，跟 pin 的 `active_app_rect` 不一定一致——尤其点击意外切了焦点时会拍到别的 app，把模型坐标系搞乱。改成 L3 后：(a) 体积小（PNG 几 KB），(b) 一定围绕鼠标位置（无歧义），(c) 不污染 `last_capture`。
- 总开关：`[screenshot] click_verify_enabled = true`。
- 阈值：`[screenshot] click_no_change_threshold = 0.005`。
- 这条路径**不重置** `active_app_l2_shown`、`active_app_rect`、`last_capture`，所以 post-step 主图分支仍按 §3.3 的状态机走 L3。新 L3 是**额外**的 follow-up，跟主 post 图一起进 user message。

### 3.4 落点 L3 取证（click verify）
`safety.verify_click_with_l3 = True` 时，本步若出现了点击类动作（`left/right/middle/double/triple_click` / `left_click_drag`），且 post 主图本身不是 L3，则**额外**抓一张鼠标周围 L3，跟主图一起回送，方便模型核对"我刚刚点中了什么"。

### 3.5 两阶段点击预览（preview L3）
`safety.verify_click_target_before = True` 时，每个含 `coordinate` 的点击走"先预览再确认"协议（[`tools.py::_maybe_pre_click_verify`](../python/otterscope/tools.py)）：

1. 模型第一次提交点击（不带 `confirmed`）：系统**不真正点**，用 `sensor.capture_around(sx, sy, radius_screen)` 在目标坐标周围抓一张 L3 tile 回送；
2. 模型核对 tile 后，重发同一动作并带 `confirmed=true` 才真正落点。

---

## 4. 怎么发：JPEG / PNG 选择

`Capture.encoded_for_send(prefer_jpeg, quality)` 给两条路径：

- **L1 / L2 大图** → JPEG（默认 `send_jpeg_quality = 80`），约 80–150 KB；
- **L3 小图 + 图标合集** → PNG（无损），通常只有几 KB。

由 `Agent._capture_image_part(cap)` 统一封装，按 `cap.level` 自动选编码。
开关：`config.toml` 的 `[screenshot] send_jpeg_for_l1_l2 = true/false`、`send_jpeg_quality = 80`。

> ⚠️ 落盘到 `logs/<thread>/step-NNN-*.png` 的版本**始终是 PNG**（人查看用），
> 只有"上传到 LLM 的字节"会按上面规则切到 JPEG。本地 png 体积 ≠ 实际请求 body 大小。

---

## 5. 历史压缩：`ContextManager`

`ContextManager.compress_old_images`（`context_manager.py`）在每一步发请求**前**对历史消息列表做就地裁剪：

- 永不动 L0（图标合集）和 prelude（system + 起手 user/assistant 引导对）。
- **每个 level 保留最近 K 张**：`screenshot.keep_recent_l1 = 1` / `keep_recent_l2 = 1` / `keep_recent_l3 = 2`。
- **再叠加：全局最近 N 张** `keep_recent_global` 一并保留。
- 其余更老的图：
  - 若 `image_recompress_enabled = True`（默认 True，q35、长边 720）：**重新解码 → 缩小 → JPEG q35** 写回原位；
  - 若关闭或重压后比原来还大：替换成纯文字占位，例如
    `[旧截图已省略以控制请求大小; level=L1; file=step-007-post-fullscreen.png; path=...]`。

---

## 6. 自动总结：`maybe_summarize`

每步发请求前 `ContextManager.maybe_summarize` 做一次"超长保护"：

- 估算总 token（文本 chars/4 + 每张图按 base64 长度/4，单图截顶 2400）；
- 超过 `target_ratio * model_context_tokens`（默认 0.7 × 200 000）时：保留 prelude + 最近 `keep_recent_messages = 12` 条原文不动，把更早的对话整段送给 LLM 自己生成一段 ≤600 词的 `## Conversation summary so far …`，替换原历史。
- 总结调用走 `Agent._summarize_segment`（同 model，`tools=[]`，`max_tokens = 1500`）。
- 关闭：`[context] auto_compress_enabled = false`。

---

## 7. 落盘 / 调试可视化

每张截图无论是否被发给 LLM，都会落盘：

- 路径 `%LOCALAPPDATA%\dev.otterscope\logs\<thread-id>\step-NNN-*.png`
- 同时在 `events.jsonl` 写一条 `step_image`：`{step, level, width, height, file, path, phase: init|post}`。
- `Agent._record_img` 维护 `md5(发送字节) → 文件名` dict，让 `context_manager.compress_old_images` 把图替换成占位文字时能写出准确的 `file=`。
- `_sanitize_for_log` 在写 `context.log`（每步发给 LLM 的 messages 全文）时把 base64 数据块替换成 `[image: <文件名>]`，避免 log 出现兆级 base64。

---

## 8. 模型坐标系怎么跟着图走

回送图的同一条 user message 文本里会显式写：

```
[level=L2] 模型请求的截图 (active_window)：发送尺寸 1568x910（原始 2560x1488）；
在屏幕坐标系中位于 (left=0, top=0, right=2560, bottom=1488)；
图片内 (px,py) → 屏幕坐标 (0+px*1.633, 0+py*1.635)。
```

模型据此知道"下一步给 coordinate=[x,y] 会被换算成屏幕的哪个像素"。
切级别后必须立刻按新公式给坐标，loop 不会做"上一张图坐标系延用到下一张"的兼容处理。

**例外**：active-app pin 状态下的 L3 post 图，文本里会注明 "coordinate frame still = L2 active app"——L3 只用来看变化，**坐标继续按上一张 L2 map 算**，因为 `last_capture` 没被 L3 覆盖。

---

## 9. 端到端时序：一次任务里截图的全链路

```
启动任务
  └─ [拍] sensor.capture(L1) → first  落盘 step-000-init.png
     └─ 拼 system + L0 icon atlas + user("任务：…" + first 图) → 发首条请求

──── 进入主循环 (step = 1..max_steps) ────

step N 开始
  ├─ [压] context_mgr.compress_old_images(messages)
  │      旧图 → JPEG q35/720px 重压 或 替换为占位文字
  ├─ [压] context_mgr.maybe_summarize(messages)
  │      估算 token > 0.7×ctx_tokens？ 是 → 调 LLM 生成 ≤600 词摘要替换老历史
  │
  ├─ [发] llm.chat(messages, tools=[computer, …])
  │      └─ 模型回 tool_calls (可能多个) 或纯文本
  │
  ├─ for each tool_call:
  │     ├─ if 点击 且没带 confirmed:
  │     │      [拍] sensor.capture_around(sx, sy, r)   ← 两阶段预览 (§3.5)
  │     │      不真正点击，把 L3 tile 当 ToolResult 回送
  │     ├─ elif action == "screenshot":
  │     │      [拍] sensor.capture(level)              ← 模型主动 (§3.2)
  │     │      tool.last_capture = cap;  had_screenshot = True
  │     ├─ elif action == "launch_app" / "focus_window":
  │     │      pin tool.active_app_rect = window rect
  │     │      tool.active_app_l2_shown = False
  │     └─ else (mouse/key/type/...):
  │            driver.<action>(...)
  │            ★ click_verify (§3.3.1)：算前后像素差占比；
  │               若 < 阈值 → 把 post_l3 当 follow-up 附进 ToolResult
  │               （**不动 last_capture / active_app_rect**；
  │                  让模型自行决定是否 screenshot 重对齐）
  │
  ├─ 动作后回送图 (§3.3)
  │     if had_screenshot:           post = tool.last_capture     (复用)
  │     elif active_app_rect pinned:
  │           if not l2_shown:       [拍] L2 of rect; last_capture=post; l2_shown=True
  │           else:
  │             if cursor in rect:   [拍] L3 cursor-local; **不覆盖 last_capture**
  │             else:                [拍] L2 of rect; last_capture=post   (越界守卫)
  │     else:                         [拍] L1                     (兜底)
  │     落盘 step-NNN-post-<level>.png
  │
  ├─ 落点 L3 取证 (§3.4)
  │     if 本步出现过点击 且 post 不是 L3:
  │           [拍] verify = sensor.capture(L3)
  │
  ├─ 组装回送 user message:
  │     · text 描述 post 图坐标系 (§8) + post 图 (JPEG/PNG by §4)
  │     · 可选: text "落点取证" + verify 图
  │     · 可选: 每张 step_preview_pngs 各配一段 "Pre-click preview #N" + 图
  │
  └─ messages.append(...) → 进入 step N+1

──── 出循环 ────
  · 模型说 "task complete:" / "task failed:"  → 正常结束
  · 步数达上限                                  → 强制结束
  · 任何异常                                    → 落盘 messages.json
```

---

## 10. 相关配置一览（`config.toml`）

```toml
[screenshot]
l1_max_long_edge = 1568
l2_max_long_edge = 1568
l3_max_long_edge = 0          # L3 不缩放
l3_radius_px     = 100        # 退化 fixed L3 时的视野 = 200x200
l3_smart_enabled = true       # UIA 驱动的动态 L3 crop（§2.1）
l3_smart_padding_px = 16
l3_smart_min_w   = 160
l3_smart_min_h   = 80
l3_smart_max_ratio = 0.4      # rect 超过 40% 屏幕 → 退化 fixed
keep_recent_l1   = 1
keep_recent_l2   = 1
keep_recent_l3   = 2
send_jpeg_for_l1_l2 = true    # 408 修复关键开关
send_jpeg_quality   = 80
skip_if_similarity_above = 0.985

[context]
image_recompress_enabled        = true
image_recompress_quality        = 35
image_recompress_max_long_edge  = 720
auto_compress_enabled           = true
target_ratio                    = 0.7
model_context_tokens            = 200000
keep_recent_messages            = 12
summary_max_tokens              = 1500

[safety]
verify_click_with_l3            = true   # 点击后自动补 L3 取证
verify_click_target_before      = true   # 两阶段点击预览
verify_click_target_radius_px   = 60
```

---

## 10.1 键盘焦点 ≠ 鼠标位置 ≠ L3 中心（重要）

经常遇到的疑问："`launch_app("calculator")` 后我没动鼠标，直接 `keyboard.type("1+1=")` 就能在显示框里出现 `1+1=`，那 L3 是不是也能拍到这个显示框？"

**不能。** 三件事是 Windows 里完全独立的状态：

| 状态 | 谁维护 | 怎么查 | 谁改它 |
|------|--------|--------|--------|
| **cursor position** | 显示驱动 | `GetCursorPos` / `window.cursor_pos()` | 物理鼠标移动 / `mouse_move` 动作 |
| **foreground window** | win32k | `GetForegroundWindow` | 用户点击 / `SetForegroundWindow` / 新进程显示主窗口 |
| **keyboard focus**（focused control） | 当前 foreground 进程内部 | `GetFocus`（仅同进程） / UIA `GetFocusedElement` | 进程自己（Tab 切换、控件 click、初始化默认焦点） |

所以：

- `launch_app("calculator")` 启动后，计算器**自己**把窗口推到 foreground，并把焦点放在显示框（默认行为）。键盘事件 `WM_KEYDOWN`/`WM_CHAR` 通过 foreground → focused control 路由，**整个过程不读 cursor**。
- 相应地 `keyboard.type("1+1=")` 注入的按键直接命中显示框，与鼠标在哪无关。
- **但 L3 = `_capture_cursor_local`，crop 中心是 `cursor_pos()`**，不是焦点控件。鼠标没动，L3 还停在原来的位置（很可能在另一个 App 的角落），跟键盘输入毫无关系。

想"看到 1+1= 的显示框"，三种思路：

- **A. 退回 L2（零代码）。** 计算器整窗 ~320×500，L2 active_window 完全够看清显示框。在 [calculator.py](../python/otterscope/apps/calculator.py) 的 tip 里加一条「输入完用 L2 校验结果」即可。当前 §3.3 的 post-step 状态机在第一次 pin 时拍的就是 L2，模型显式 `screenshot(level="active_window")` 也能强制刷新。
- **B. 用 UIA 直接读结果文本（不截图）。** [uia.py](../python/otterscope/uia.py) 已有 IUIAutomation 实例，扩展一个 `find_element_by_automation_id(hwnd, "CalculatorResults")` 就能把 Win11 计算器结果框的字符串原样取出来（Name 属性形如 `"显示为 2"`）。比让 LLM 看像素更准、更快、Token 更少。
- **C. 让 L3 中心 = 焦点控件中心。** 扩展 [uia.py](../python/otterscope/uia.py) 加 `focused_element_rect()`（`IUIAutomation::GetFocusedElement` vtable 第 8 槽 + `CurrentBoundingRectangle`），在 `_capture_cursor_local` 里加 `cfg.l3_follow_focus` 开关：开启时优先用 focused element 的 rect，没有再退回 cursor。这样 `keyboard.type` 后续步骤的 L3 自动落在显示框上。

目前**未实现**任何一种，留作后续可选改进。三个方向取舍：A 最省事，B 最准（适合数值/文本类 App，写进 [meta_tools.py](../python/otterscope/meta_tools.py) 当独立工具更合适），C 改动小但只解决"看清焦点控件"这一个 use case。

---

## 11. 已知坑 / 待改进

- **越界坐标静默换算**：模型在 L3 200×200 截图后给了一个 y=366 的 coordinate，loop 仍按当前 offset+scale 算成了一个屏幕坐标。建议在 `tools.py` 的坐标转换里加越界硬校验，强制模型先重新截图。详见 `Docs/todo.md`。
- **第一张 post 图永远是新鲜大图**：当前历史压缩规则保护"最新一张"，所以最新的 L1 永远是高码率版本。L1/L2 改 JPEG 已经把单图体积压到 100 KB 量级，问题不再尖锐。
- **锁屏时 BitBlt 拒绝访问**：Winlogon secure desktop 不允许任何用户态进程截屏，`mss.grab` 会抛 `ScreenShotError`。这是 OS 安全边界，绕不过；只能在首次运行引导里关掉锁屏 / 睡眠 / 屏保。
