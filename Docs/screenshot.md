# 截图（Screen Sensor）逻辑总览

> 本文是 ctrlapp 当前截图链路（采集 → 编码 → 发送 → 历史压缩 → 落盘）的实现备忘。
> 目标读者：调试 / 二次开发本仓库的人。代码以 `python/ctrlapp/screen.py`、
> `python/ctrlapp/loop.py`、`python/ctrlapp/context_manager.py`、
> `python/ctrlapp/config.py` 为主。

---

## 0. 截图是不是独立 tool？—— 不是，是 `computer` 工具的一个 action

当前所有桌面交互（截图 / 鼠标 / 键盘）都收在**同一个 `computer` 工具**下，通过
`action` 字段区分。截图等价于：

```jsonc
// model → tool_call
{
  "name": "computer",
  "arguments": {
    "action": "screenshot",
    "level": "fullscreen" | "active_window" | "cursor_local"
  }
}
```

代码：[`tools.py` `ComputerTool.openai_tool_schema()`](../python/ctrlapp/tools.py) 暴露 schema，
[`_dispatch()` 里 `if a == "screenshot"` 分支](../python/ctrlapp/tools.py)
调用 `sensor.capture(level)` 并返回 `ToolResult(image_png=..., output="L?/...")`。

### 为什么不拆成独立 `screenshot` / `click` / `type` tool

- **对齐 Anthropic computer-use 官方规范**（[`computer_20250124`](https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/computer-use-tool)，
  另见 [API reference](https://docs.anthropic.com/en/api/agents-and-tools/computer-use)）。
  该规范本身就把 screenshot/mouse/key 全收在同一个 tool 里，方便模型用同一坐标系一并调度。
  我们走 LiteLLM 代理时也复用同一个动作集，未来直连 Claude 原生 API 不用再转一层。
- **共享坐标系**：`screenshot` 设置的 `last_capture` 直接被随后的 `left_click` 拿来
  做 `model_to_screen` 反算，拆开后这条链路要么得全局共享状态（更脏），要么得让
  模型自己来回传 offset/scale（更易出错）。
- **简化 `_maybe_pre_click_verify` / 落点 L3 取证**：这两个 hook 都是按 "tool name +
  action" 决策，单一 tool 让 hook 实现成同一个分支判断，不需要按多个 tool 名分别注册。

### 拆开能拿到什么收益（暂不做）

- 弱模型偶尔把 `coordinate` 漏给 `screenshot`、`text` 漏给 `left_click` 之类的混用。
  独立 tool 后 schema 更紧，模型选错的概率下降。
- screenshot 可以加专属参数（如自定义 `region=[x,y,w,h]`），不必塞进通用 `coordinate`。

### 折中（已计划，详见 [todo.md](todo.md)）

- 在 `tools._dispatch` 里加严格参数校验：`screenshot` 不接 `coordinate/text`、点击类
  必须有 `coordinate`，否则直接 `ToolResult(error=...)` 让模型立刻看到错误（避免现在
  "缺参数静默把鼠标当前位坐标拿去点"的暗坑）。
- screenshot 支持 `region=[cx,cy,r]` 任意框 L3，不只能在鼠标位置。

---

## 1. 三级金字塔（L1 / L2 / L3）

`ScreenLevel` 三档（`screen.py`）：

| 级别 | 名字 | 范围 | 默认下采样上限 (`config.ScreenshotConfig`) | 典型场景 |
|------|------|------|--------|--------|
| **L1** | `fullscreen` | 整个虚拟桌面（`mss.monitors[0]`，跨多显示器） | `l1_max_long_edge = 1568` | 起手识局、找窗口 |
| **L2** | `active_window` | 当前前台窗口的客户区 | `l2_max_long_edge = 1568` | 在 App 内部操作 |
| **L3** | `cursor_local` | 鼠标周围 `2*l3_radius_px` 见方的小窗（默认 200×200） | `l3_max_long_edge = 0`（不缩放） | 看小按钮 / 小图标 / 文字 |

> L0 是逻辑上的"教学图"层，**不是 ScreenSensor 拍的**；它是 `icon_memory.py` 拼出来
> 的图标合集（"icon atlas"），永久跟随 system prompt 注入，不参与 L1/L2/L3 的采样。

每张 `Capture` 对象统一携带：

- `image: PIL.Image`（已下采样）
- `raw_size`（物理像素）/ `sent_size`（实际发给 LLM 的像素）
- `offset`（在虚拟屏幕坐标系里的左上角）
- `phash`（dHash，用于变化检测、相似度判定）
- `model_to_screen(x, y)` —— LLM 给的图内坐标 → 真实屏幕坐标

LLM 任何时候给 `coordinate` 都必须用 **当前最近一张截图的 sent_size 坐标系**；系统按
`offset + scale` 反算回物理坐标后再驱动鼠标。

## 2. 何时拍：自动 vs. 模型主动

### 2.1 起手 L1
`Agent._run` 一启动就抓一张 L1（`first = self.sensor.capture(L1)`），落盘为
`step-000-init.png` 并随首条 user message 一起发给 LLM。

### 2.2 模型主动 `screenshot`
模型可以调 `computer({action: "screenshot", level: "fullscreen|active_window|cursor_local"})`
任意级别。`tool.last_capture` 会被更新为这一张，loop 的"动作后回送"阶段会优先回送它。

### 2.3 动作后自动补图（post-step）
每一步 dispatch 完所有 tool_call 后，loop 都会回送一张视觉输入（`loop.py` ~L1019）：

- 若本步出现过 `screenshot`：**复用**那张 `tool.last_capture`，不重复采集；
- 否则：新拍一张 L1 当作"动作后变化校验图"。

### 2.4 落点 L3 取证（click verify）
`safety.verify_click_with_l3 = True` 时，本步若出现了点击类动作（`left/right/middle/double/triple_click` 或 `left_click_drag`），且 post 主图本身不是 L3，则**额外**抓一张鼠标周围 L3，跟主图一起回送，方便模型核对"我刚刚点中了什么"。

### 2.5 两阶段点击预览（preview L3）
`safety.verify_click_target_before = True` 时，每个含 `coordinate` 的点击动作走"先预览再确认"协议（`_maybe_pre_click_verify`）：

1. 模型第一次提交点击 **不带 `confirmed`**：系统不真正点，而是用
   `sensor.capture_around(sx, sy, radius_screen)` 在目标坐标周围抓一张 L3 tile，回送给模型；
2. 模型核对那张 tile 后，重发同一动作并带 `confirmed=true` 才真正落点。

## 3. 怎么发：JPEG / PNG 选择

> 这是上一轮修掉 408 timeout 的关键。

`Capture.encoded_for_send(prefer_jpeg, quality)` 给两条路径：

- **L1 / L2 大图** → JPEG（默认 `send_jpeg_quality = 80`），约 80–150 KB；
- **L3 小图 + 图标合集** → PNG（无损），通常本就只有几 KB。

由 `Agent._capture_image_part(cap)` 统一封装，根据 `cap.level` 自动选编码。
开关：`config.toml` 的 `[screenshot] send_jpeg_for_l1_l2 = true/false`、`send_jpeg_quality = 80`。

> ⚠️ 落盘到 `logs/<thread>/step-NNN-*.png` 的版本**始终是 PNG**（人查看用），
> 只有"上传到 LLM 的字节"会按上面规则切到 JPEG。所以本地 `step-005-post-fullscreen.png`
> 的体积 ≠ 实际请求 body 里那张图的体积。

## 4. 历史压缩：`ContextManager`

`ContextManager.compress_old_images`（`context_manager.py`）在每一步发请求**前**对历史
消息列表做就地裁剪，避免上下文越滚越大：

- 永不动 L0（图标合集）和 prelude（system + 起手 user/assistant 引导对）。
- **每个 level 保留最近 K 张**：`screenshot.keep_recent_l1 = 1` / `keep_recent_l2 = 1` / `keep_recent_l3 = 2`。
- **再叠加：全局最近 N 张** `keep_recent_global` 一并保留。
- 其余更老的图：
  - 若 `image_recompress_enabled = True`（默认 True，q35、长边 720）：**重新解码 → 缩小 → JPEG q35** 写回原位；
  - 若关闭，或重压后比原来还大：替换成纯文字占位，例如
    `[旧截图已省略以控制请求大小; level=L1; file=step-007-post-fullscreen.png; path=...]`，
    模型/调试者可按文件名去 `logs/<thread>/` 找原图。

## 5. 自动总结：`maybe_summarize`

每步发请求前 `ContextManager.maybe_summarize` 还会做一次"超长保护"：

- 估算总 token（文本 chars/4 + 每张图按 base64 长度/4，单图截顶 2400）；
- 超过 `target_ratio * model_context_tokens`（默认 0.7 × 200 000）时：
  保留 prelude + 最近 `keep_recent_messages = 12` 条原文不动，把更早的对话整段送给 LLM
  自己生成一段 ≤600 词的 `## Conversation summary so far …`，替换原历史。
- 总结调用走 `Agent._summarize_segment`（同 model，`tools=[]`，`max_tokens = 1500`）。
- 关闭：`[context] auto_compress_enabled = false`。

## 6. 落盘 / 调试可视化

每张截图无论是否被发给 LLM，都会落盘：

- 路径 `%LOCALAPPDATA%\dev.ctrlapp\logs\<thread-id>\step-NNN-*.png`
- 同时在 `events.jsonl` 写一条 `step_image`：`{step, level, width, height, file, path, phase: init|post}`。
- `Agent._record_img` 维护一个 `md5(发送字节) → 文件名` 的 dict，
  这样 `_prune_old_images_dispatch` 把图替换成占位文字时能写出准确的 `file=`。
- `_sanitize_for_log` 在写 `context.log`（每步发给 LLM 的 messages 全文）时，
  把 base64 数据块替换成 `[image: <文件名>]`，只保留文件引用，避免 log 里出现兆级 base64。

## 7. 模型坐标系怎么跟着图走

回送图的同一条 user message 文本里会显式写：

```
[level=L2] 模型请求的截图 (active_window)：发送尺寸 1568x910（原始 2560x1488）；
在屏幕坐标系中位于 (left=0, top=0, right=2560, bottom=1488)；
图片内 (px,py) → 屏幕坐标 (0+px*1.633, 0+py*1.635)。
```

模型据此知道"我下一步给 coordinate=[x,y]，会被换算成屏幕的哪个像素"。
切级别后必须立刻按新公式给坐标，loop 不会做"上一张图坐标系延用到下一张"的兼容处理。

## 7.5 端到端时序：一次任务里截图都在什么时候发生

下面是单次 `Agent._run` 的截图相关时间线（穿插了模型决策、tool 调度、历史压缩、
落盘）。同一个 step 内可能有多张图同时回送给模型，每张图都按 §3 编码、按 §6 落盘。

```
启动任务
  └─ [拍] sensor.capture(L1)              → first  ── 落盘 step-000-init.png
     └─ 拼 system + (icon atlas L0) + user("任务：…" + first 图) → 发首条请求

──── 进入主循环 (step = 1..max_steps) ────

step N 开始
  ├─ [压] context_mgr.compress_old_images(messages)
  │      旧图 → JPEG q35/720px 重压 或 替换为占位文字
  ├─ [压] context_mgr.maybe_summarize(messages)
  │      估算 token > 0.7×ctx_tokens？ 是 → 调 LLM 生成 ≤600 词摘要替换老历史
  │
  ├─ [发] llm.chat(messages, tools=[computer, …])
  │      └─ 模型回 tool_calls (可能多个) 或者纯文本
  │
  ├─ for each tool_call in tool_calls:
  │     ├─ if action 是点击 且没带 confirmed:
  │     │      [拍] sensor.capture_around(sx, sy, r)   ← 两阶段预览 (§2.5)
  │     │      不真正点击，把这张 L3 tile 当作 ToolResult 回送
  │     │      step_preview_pngs.append(tile)
  │     ├─ elif action == "screenshot":
  │     │      [拍] sensor.capture(level)              ← 模型主动 (§2.2)
  │     │      tool.last_capture = cap;  had_screenshot = True
  │     │      ToolResult(image_png=cap.png_bytes(), output=...)
  │     └─ else (mouse/key/type/...):
  │            driver.<action>(...)                    ← 真正驱动鼠标/键盘
  │            (没有截图)
  │
  ├─ 动作后回送图 (§2.3)
  │     if had_screenshot: post = tool.last_capture            (复用)
  │     else:               [拍] post = sensor.capture(L1)     (兜底)
  │     落盘 step-NNN-post-<level>.png
  │
  ├─ 落点 L3 取证 (§2.4)
  │     if 本步出现过点击 且 post 不是 L3:
  │           [拍] verify = sensor.capture(L3)
  │           落盘 step-NNN-verify-cursor_local.png
  │
  ├─ 组装回送 user message:
  │     · text 描述 post 图坐标系 (§7) + post 图 (JPEG/PNG by §3)
  │     · 可选: text "落点取证" + verify 图
  │     · 可选: 每张 step_preview_pngs 各配一段 "Pre-click preview #N" text + 图
  │
  └─ messages.append(...) → 进入 step N+1

──── 出循环 ────
  · 模型说 "task complete:" / "task failed:"  → 正常结束
  · 步数达上限                                  → 强制结束
  · 任何异常 (api_error / cancel)              → 落盘 messages.json 备续接
```

> 一句话：**所有截图都通过 `ScreenSensor` 单一入口产出 `Capture`，再经
> `Agent._capture_image_part(cap)` 统一编码进 user message**。无论是模型主动调
> screenshot、还是 loop 自动补 post / verify / preview，路径都是这一条。

## 8. 相关配置一览（`config.toml`）

```toml
[screenshot]
l1_max_long_edge = 1568
l2_max_long_edge = 1568
l3_max_long_edge = 0          # L3 不缩放
l3_radius_px     = 100        # L3 视野 = 200x200
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

## 9. 已知坑 / 待改进

- **越界坐标静默换算**：模型在 L3 200×200 截图后给了一个 y=366 的 coordinate，loop 仍按当前 offset+scale 算成了一个屏幕坐标。建议在 `tools.py` 的坐标转换里加越界硬校验，强制模型先重新截图。详见 `Docs/todo.md`。
- **第一张 post 图永远是新鲜大图**：当前历史压缩规则保护"最新一张"，所以最新的 L1 永远是高码率版本。L1/L2 改 JPEG 已经把单图体积压到 100 KB 量级，问题不再尖锐；如果未来仍有边界 case，可以让 `compress_old_images` 把 L1 也强制经过一次 JPEG 重编码（即使是最近一张）。
- **锁屏时 BitBlt 拒绝访问**：Winlogon secure desktop 不允许任何用户态进程截屏，`mss.grab` 会抛 `ScreenShotError`。当前没法绕过（这是 OS 安全边界），只能在首次运行引导里关掉锁屏 / 睡眠 / 屏保。详见 `Docs/todo.md`。

---

## 10. 让模型自主控制截图 —— 改进方案与取舍（待决策）

> 目标：把"是否截图 / 截哪 / 截多大 / 编码多狠"这些决定权下放给模型，
> 同时不破坏现有的安全网（post 自动补图、点击两阶段预览、L3 取证）。
> 下面每个方案独立可选，可单选也可叠加。

### 现状回顾（决定权归属）

| 维度 | 谁决定 | 备注 |
|------|--------|------|
| 是否拍 | 模型 + loop 强制补图 | post 图模型挡不掉 |
| 范围 | 模型选 L1/L2/L3 三档 | L3 永远围着鼠标 |
| 大小 | config 全局 | `l?_max_long_edge` |
| 编码 | level 自动 | L1/L2=JPEG, L3=PNG |
| 质量 | config 全局 | `send_jpeg_quality=80` |

### 方案 A：自定义 region（任意矩形 L4）

**做什么**：`screenshot` 多一个 `region: [x, y, w, h]` 参数（屏幕物理坐标系）。
传了就走 `_grab(region) → _shrink → JPEG/PNG`，不传就退化到当前 L1/L2/L3。

- 改动点：`screen.py` 抽 `_capture_region(left, top, w, h, max_long_edge)`；
  `tools.py` schema 加字段；`_dispatch` 加 4 行分支。新增 `ScreenLevel.L4 = "region"`
  或干脆给 region 截图打 `L2` 标签复用历史压缩规则。
- 新增配置：`region_max_pixels = 4_000_000`（防止模型不小心要 4K 大图）。
- 代码量：~30 行。

**优点**

- 模型能精确围观一个对话框/按钮，不再被"鼠标必须先 mouse_move 过去"绑死。
- App 区域化坐标库（todo 第 4 项）天然复用：`region(app, "send_button")` 内部翻译为 region 截图。
- 解决 §9 第 1 条的"L3 只能在鼠标周围"约束。

**缺点**

- 模型可能传错坐标系（屏幕坐标 vs. 上一张图坐标），需要 schema description 写清楚 + 越界硬校验。
- 多一种 level 后历史压缩 / 落点取证的判定逻辑要相应扩展（轻微）。
- 模型选择空间变大 → 更可能"过度截图"刷 token。

### 方案 B：自定义 max_long_edge（控制大小）

**做什么**：`screenshot` 多一个可选 `max_long_edge: int`（0=不缩放）。
传了就覆盖该图的 `_shrink` 上限，不传走 config 默认。

- 改动点：3 行（schema + dispatch 转发参数）。
- 上限 clamp：`min(model_value, hard_cap=2400)`，防止模型传 99999。

**优点**

- 模型可以"我就要看一张超清的活动窗口"，临时拉高上限；也可以"我只要个缩略图导航"，主动缩小。
- 调试期省 token：用户可以在某些任务里让模型显式压成 800px。

**缺点**

- 大多数模型不会主动用这个参数，只有少数提示工程后才会 — ROI 偏低。
- 需要写一段 prompt 教模型何时调大/调小，否则就是死参数。

### 方案 C：自定义 quality（控制编码强度）

**做什么**：`screenshot` 多一个可选 `quality: int`（1-100，100=PNG 无损）。
默认走 config，传了就覆盖。

- 改动点：~5 行。`_capture_image_part` 接受 `quality_override`。

**优点**

- 模型读小字 / 验证视觉哈希时可以临时拉满 PNG，平时用 q60 省带宽。
- 实现成本极低，向后兼容。

**缺点**

- 跟方案 B 同理，模型主动用的概率低；需要 prompt 教学。
- 跟方案 B 加一起后参数空间变大，弱模型更容易写错 schema。

### 方案 D：模型可"否决"自动 post 补图

**做什么**：模型在 `screenshot` / 任何 action 里多一个可选 `skip_post_screenshot: true`，
本步告诉 loop 不要再补 L1。或者反过来：默认不补，模型不要图就什么也不做。

两个子方案：

- **D1（白名单）**：默认行为不变（补图），新增显式开关让模型跳过；
- **D2（黑名单）**：默认不补，loop 只在模型说"我看不到"或"动作疑似失败"时补。

**优点**

- 节省非常多 token：连续 5 步 type/key 不需要 5 张截图；模型一次 screenshot 后就能心算后续状态。
- 模型自主感更强，更接近"我就是个开发者，自己决定何时刷新"。

**缺点**

- D2 风险高：弱模型经常忘记看图就乱点，post 补图本来就是兜底；去掉后误操作率会升高。
- D1 实现简单但模型多半不知道这开关存在；除非系统 prompt 反复强调。
- 跟"两阶段点击预览"配合时要小心：preview tile 是用户必看的，不能被 skip。

**推荐**：先做 D1（保守），观察一段时间再考虑 D2。或者引入 config 开关
`auto_post_screenshot = "always" | "on_action_only" | "off"`，由用户而非模型决定全局策略。

### 方案 E：一次截多张（multi-region）

**做什么**：`screenshot` 接受 `regions: [[x,y,w,h], ...]`，一次返回多张图。

**优点**

- 同一步骤可以同时围观"输入框 + 发送按钮 + 状态栏"三个不相邻区域，省 round-trip。
- 拼一张大图不可行（坐标系混乱），多张分别给最干净。

**缺点**

- ToolResult 现在只支持单图返回；要么改 ToolResult 支持 list，要么把多图塞进随后的 user message（破坏"工具调用→工具结果"语义对称）。
- 模型对多图响应的能力参差，弱模型容易只看第一张。
- 跟方案 A 强相关，应在 A 之后再做。

### 方案 F：返回时附带"屏幕全局缩略图" + 你点的那块高清图

**做什么**：模型每次拍 region/L3，loop 自动在结果里**额外**附一张极小（200×200）的全屏 thumbnail，
让模型在"局部细节图 + 全局位置感"之间不丢失上下文。

**优点**

- 解决 L3/region 截图导致的"我在哪儿"迷失感；连续多张 L3 后模型容易忘记屏幕全貌。
- 缩略图极小（~5 KB），代价微乎其微。

**缺点**

- 多发了一张图，对当前 token 估算 / 历史压缩规则要更新（缩略图打 `L0?` 还是 `L1?`）。
- 模型可能误以为缩略图能精确定位，错把缩略图坐标当主图坐标用 → 必须在 text 里反复强调"仅供方位参考"。

### 方案 G：把"截图"拆成独立 tool

**做什么**：`screenshot` 变成一个独立 function，参数空间紧凑：`{level, region?, max_long_edge?, quality?}`。

**优点**

- schema 极简，模型选错的概率最低。
- 配合方案 A/B/C，参数都集中在一个 tool 里，可读性最好。

**缺点**

- 偏离 Anthropic computer-use 官方规范（详见 §0），未来直连 Claude 原生 API 要做转换层。
- 现有 hook（pre-click verify / post 补图判定）都是按 action 字段做的，拆 tool 后要重写分发逻辑。

### 方案 H（可选叠加）：`screenshot` 不消耗一个"动作位"

**做什么**：当前每步如果调了 `screenshot`，loop 就把它算成一个 tool_call，post 补图阶段会用它"复用"。
改成允许"模型可以在同一个 step 内既调 screenshot 又调 click" — 现有架构其实已经支持
parallel tool_calls，但 dispatch 顺序里 click 可能先于 screenshot 执行。

**优点**

- 模型可以一步内"看完→点完"，节省一个完整 round-trip。
- 跟两阶段点击预览可以叠加：第一次"screenshot region + left_click(no confirmed)" → 第二次"left_click(confirmed)"。

**缺点**

- 顺序很重要：click 必须在 screenshot 之后执行；当前 dispatch 是按模型给的 tool_call 顺序遍历，需要保证模型遵守。
- 单 step 多动作让 events / log 可视化更复杂。

### 方案对比矩阵

| 方案 | 代码量 | 风险 | 收益 | 推荐度 |
|------|--------|------|------|--------|
| A. 自定义 region | ~30 行 | 低 | 高（解锁 App 区域库 + 任意围观） | ⭐⭐⭐⭐⭐ |
| B. 自定义 max_long_edge | ~3 行 | 极低 | 中（需要 prompt 配合） | ⭐⭐⭐ |
| C. 自定义 quality | ~5 行 | 极低 | 中（需要 prompt 配合） | ⭐⭐⭐ |
| D1. 模型可跳过 post | ~10 行 | 中 | 高（省 token 30~50%） | ⭐⭐⭐⭐ |
| D2. 默认不补 post | ~10 行 | 高 | 极高 | ⭐⭐ |
| E. 一次多 region | ~50 行 | 中 | 中 | ⭐⭐ |
| F. 缩略图陪伴 | ~20 行 | 中 | 低-中 | ⭐⭐ |
| G. 拆独立 tool | ~80 行 | 中（破坏 Anthropic 规范对齐） | 中 | ⭐ |
| H. 单 step 多动作并行 | ~15 行 | 中 | 中-高 | ⭐⭐⭐ |

### 推荐组合（按工程经济性排序）

1. **MVP 套餐**：A + B + C（一次扩 schema 全做掉，~40 行，全部可选参数向后兼容）。
2. **MVP+ 套餐**：1 + D1（再加一个 `skip_post_screenshot` 开关，模型不要图就不补）。
3. **激进套餐**：1 + 2 + H（允许 screenshot+click 同步，配合两阶段预览拿到最少 round-trip）。
4. **暂不做**：E（等 A 落地再考虑）、F（先看 A 用得怎么样）、G（破坏 Anthropic 对齐，未来直连 Claude 时再说）。

### 决策提示

- 如果你最关心**"App 区域库"那个 TODO**：先做 A（必经之路）。
- 如果你最关心**省 token / 长任务不爆 context**：D1 优先，再叠 A。
- 如果你最关心**模型能精细决策**（reasoning model）：B + C 一起做，并在 prompt 里教模型何时调高/调低。
- 如果你**想先观察一下不动**：什么都不改也合理，当前已经能完成大部分任务，只是模型自由度小一些。

---

## 11. 针对"快 + 准"的二次评估（决策聚焦）

> 你的两个核心目标：(1) 加快速度（全屏截图太慢、轮数太多）；(2) 提高点击准确性。
> 重新审视上面 8 个方案，按这两个维度打分，并指出几个 §10 没列但其实更对症的改动。

### 当前耗时分布（从 thread-220024 / 211143 两个日志反推）

| 阶段 | 单次耗时 | 备注 |
|------|----------|------|
| L1 全屏 JPEG q80 上传 | 100~150 KB → 实测 1~3s（Copilot 代理） | 网络抖动会放大到 30s+ |
| LLM 推理（claude opus 4.6） | 15~60s | 单步主要瓶颈 |
| 自动 post 补图 + 编码 | <0.5s | 体积已经压下来了 |
| 鼠标/键盘动作 | <0.2s | 可忽略 |
| **每步真实墙钟** | **20~80s** | 单步 LLM 推理占 70%+ |
| 任务总轮数 | 6~20 步 | 含 nudge / preview confirm 等"虚步" |

**结论**：模型推理 + round-trip 次数才是真瓶颈，单图体积已经不是大头。
所以"快"的关键是**减少轮数**，而不是把图压更小。

### 准确性瓶颈

- **L1 1568×656 上的 16-32px 小图标 / 系统托盘**：模型必失误。日志里好几步都是
  "L1 看一眼 → L3 围观 → 还是错 → 两阶段 preview → 再来" 4 个 round-trip 才点中。
- **没有"应用语义"**：模型只看像素，看不懂"这是 WeChat 的发送按钮"。
- **坐标系切换**：L1 → L3 → 切回 L1 时偶发坐标系混淆。

### 重排：对你的目标真正有用的方案

| 方案 | 加速？ | 提准？ | 工程成本 | 综合推荐 |
|------|--------|--------|----------|----------|
| **A. region 任意框** | ★★★（一次性看到目标，省 2~3 个 L3 round-trip） | ★★★★★（精细像素 + 围观目标周边） | ~30 行 | ⭐⭐⭐⭐⭐ |
| **H. 单 step 内 screenshot+click 并行** | ★★★★★（1 步顶 2~3 步） | ★★（依赖 A 的精细图） | ~15 行 | ⭐⭐⭐⭐⭐ |
| **D1. 模型可跳 post 补图** | ★★★（type/key 链路省图） | ☆ | ~10 行 | ⭐⭐⭐⭐ |
| **App 区域库**（TODO #4） | ★★★★（对常用 App 直接给坐标，零截图） | ★★★★★（彻底跳过视觉识别） | ~150 行 | ⭐⭐⭐⭐⭐（中长期最优解） |
| **B. 缩小 L1 默认尺寸** | ★★（每图省 50KB；轮数不变） | ★（更糊） | ~3 行 + config | ⭐⭐⭐ |
| **C. 自定义 quality** | ★ | ★ | ~5 行 | ⭐⭐ |
| **E/F/G** | 略 | 略 | — | 暂不做 |

### §10 没列但对你更对症的几个方案

#### I. 两阶段 preview 改为"模型可选"，不再强制
**现状**：每个含 coordinate 的点击都强制走 preview → confirm 两个 round-trip。
对小按钮（托盘图标）很有用，但对**大目标**（窗口正中的发送按钮 100×40px）纯属浪费。

**做法**：模型在 click 时可以直接传 `confirmed=true` 跳过 preview，但**前提条件**：
本 step 内或上一 step 内**必须有一张包含目标坐标的高分辨率截图**（L3 或 region）。
如果模型在 L1 直接 confirmed=true 点小图标，loop 警告 + 强制走 preview。

- 加速：★★★★（信心足时省一半 round-trip）
- 提准：★（基本不变，仍有兜底）
- 成本：~20 行（loop 里加"信心评估"逻辑）

#### J. 取消 nudge，把"无 tool_call"也当作允许
**现状**：模型回纯文本（narrate）时，loop 会塞一条 "Please don't just narrate..." 然后**重启一轮**。日志里 6 步任务里有 4 步是 nudge 浪费。

**做法**：
- **J1（柔性）**：不发 nudge，直接当成 step 完成；下一步自动追加 L1 + 提示"上一轮你只想了想，这一步请采取行动"，仍然消耗一个 step 但不浪费一次"思考→nudge→再思考"两轮。
- **J2（激进）**：直接惩罚式 — assistant text 不带 tool_call 立刻判任务失败。

J1 是甜点：加速 ★★★★，零风险。

#### K. 一次性"看大图 + 框小图"组合（A 的延伸）
**做法**：region 截图返回时，loop **同时附带**该 region 在 L1 缩略图里的红框标注，让模型既看到细节又不丢方位（这是 §10 方案 F 的更聪明版本）。

- 加速：☆
- 提准：★★★（解决 L3 / region 后的"我在哪"迷失）
- 成本：~30 行（PIL 画框）

#### L. 给 SYSTEM_PROMPT 瘦身 + 把"操作 tips"按 App 触发式注入
**现状**：SYSTEM_PROMPT ~12 KB，含 18 条 seed tips 全量注入。每步都重发一次。
对 LLM 推理速度影响不大（缓存友好），但对 input token 计费有累积影响。

**做法**：把 tips 拆成核心 5 条 + App 特定 tips（按当前活动窗口 / 任务关键词触发注入）。

- 加速：★（节省每步 ~3KB input）
- 提准：☆
- 成本：~50 行

### 推荐落地顺序（你目前的最佳路径）

> 三步走，每步独立可见效果。

**第 1 步（立即做，~1 小时）**：J1（取消 nudge）+ I（preview 可跳过）
- 立竿见影砍掉 30~50% 的虚步。
- 风险极低，只动 loop.py 单文件。
- 用现成的 thread 跑一次 "发微信给老婆 + 三句话"，对比改前改后步数。

**第 2 步（~半天）**：A + H（region 任意框 + 单 step 内 screenshot+click 并行）
- 模型能"region 围观 → 同 step 直接 click confirmed"一气呵成。
- 配合第 1 步，从"找按钮 → 点按钮"的典型 4 步压缩到 1 步。
- A 同时是 App 区域库的前置条件。

**第 3 步（~1 天）**：App 区域库（TODO #4）
- 对最高频的 5 个 App（VS Code / 微信 / Outlook / Chrome / 资源管理器）做一次性校准。
- 之后这些 App 内的常用按钮**完全不需要截图**，模型直接拿坐标点。
- 这是真正的"快 + 准"终极解，但需要前面两步铺路。

**先不做**：B（缩 L1 默认尺寸 — 改了影响 L1 文字识别）、C（quality 调节 — ROI 太低）、D2（默认不补 post — 风险高）、E/F/G。

### 一句话

> **最大瓶颈是 round-trip 次数，不是图大小。** 先用 J1 + I 砍虚步，再用 A + H 让"看 + 点"合一，最后用 App 区域库把熟练 App 变成"零截图盲点操作"。三步下来，典型任务从 15+ 步降到 3~5 步，速度和准确率同时翻倍。

---

## 12. 视觉之外：用 Windows 原生接口启动 / 切换 App

> 视觉系统真正擅长的是 "**App 内部**" 的操作（找按钮、读文本、识别状态）。
> "**调出哪个 App**" 这个外层问题在 Windows 上几乎全有非视觉路径，强行用截图反而是
> 最慢、最不准的方案。下面按可靠性从高到低列。

### 12.1 直接命令行 / 协议唤起（最快、最可靠）

#### A. 已知绝对路径 → `subprocess.Popen`

```python
# 微信新版：%LOCALAPPDATA%\Tencent\WeChat\WeChat.exe
# 旧版：    %ProgramFiles%\Tencent\WeChat\WeChat.exe
subprocess.Popen([r"C:\Program Files\Tencent\WeChat\WeChat.exe"])
```

- ✅ 100% 可靠；❌ 路径要预存 / 安装位置可能变。

#### B. 注册表 App Paths（推荐入口）

`HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\<exe>` 由安装程序自己注册。
能直接在 `Win+R` / `cmd` 里打 `wechat`、`code`、`outlook`、`chrome`、`notepad` 之类的别名启动。

```python
subprocess.Popen("wechat", shell=True)   # 走 Win+R 同款解析
subprocess.Popen("code .", shell=True)   # VS Code
subprocess.Popen("outlook", shell=True)
```

- ✅ 无需知道路径，用户视角的"别名"=程序员视角；❌ 没注册的 App 会失败。

#### C. UWP / 微软商店 App → AUMID 或协议 URI

```powershell
# 列出所有 UWP App + AUMID
Get-StartApps | Where-Object Name -Match WeChat
# Name        AppID
# 微信        Tencent.WeChat_xxx!App

start shell:AppsFolder\Tencent.WeChat_xxx!App
```

很多 App 还注册了 URI 协议（最舒服）：

```python
subprocess.Popen("start weixin://", shell=True)              # 微信
subprocess.Popen("start mailto:lvmin@x.com", shell=True)     # 默认邮件 + 收件人
subprocess.Popen("start ms-outlook://compose?to=...&subject=...&body=...", shell=True)
subprocess.Popen("start vscode://file/D:/Project/foo", shell=True)
subprocess.Popen("start ms-settings:network", shell=True)    # 直接打开"设置→网络"
```

- ✅ 跨用户位置都能用；✅ 协议 URI 还能直接传参（比 GUI 快无数倍）。

### 12.2 用户已有的快捷键 / 系统设施

#### D. 微信 / QQ 自带全局热键

设置→快捷键里能配 `Ctrl+Alt+W` 唤出主窗口；之后：

```python
import pyautogui; pyautogui.hotkey("ctrl", "alt", "w")
```

- ✅ 微信的"已经在跑就唤出"一步到位、无重复实例。

#### E. Win+1..9：钉在任务栏的 App 快捷键

引导用户初次设置时把高频 App 钉到任务栏，之后 `Win+3` 就能起第 3 个钉的 App。

#### F. PowerToys Run / 系统搜索

`Win` → 输入 "wec" → Enter，是 Windows 搜索的"模糊启动"：

```python
pyautogui.press("win"); time.sleep(0.3)
pyautogui.typewrite("wechat", interval=0.02); time.sleep(0.4)
pyautogui.press("enter")
```

- ✅ 没注册任何路径也能起；❌ 慢（要等索引）；❌ 偶尔搜出来是网页结果。

### 12.3 进程 / 窗口枚举（先判断"是不是已经在跑"，再决定起不起）

> 这是**整个 launch_app 链路的第 0 步**，也是最容易被忽略的一步。
> 大多数桌面 App（微信 / VS Code / Outlook / Chrome）都是单实例或托盘常驻 ——
> 如果不先检测就直接 `Popen`，要么会弹"已在运行"提示框，要么会起一个新窗口
> 抢焦点反而打断用户。

#### A. 命令行一行查（人 / Agent 都能用）

```powershell
# PowerShell：按进程名
Get-Process WeChat -ErrorAction SilentlyContinue
Get-Process Code, chrome, OUTLOOK -ErrorAction SilentlyContinue |
    Select-Object Id, ProcessName, MainWindowTitle

# 按窗口标题模糊匹配（找出所有可见窗口）
Get-Process | Where-Object { $_.MainWindowTitle -like "*微信*" }
```

```cmd
:: cmd / 任何 shell：tasklist
tasklist /FI "IMAGENAME eq WeChat.exe"
tasklist /V /FI "IMAGENAME eq Code.exe" | findstr /I "Visual Studio Code"
```

```powershell
# 列出所有有可见窗口的进程（Agent 排查"现在桌面上开着啥"）
Get-Process | Where-Object MainWindowHandle -ne 0 |
    Select-Object Id, ProcessName, MainWindowTitle |
    Sort-Object ProcessName
```

#### B. Python 检测（sidecar 内置）

```python
import psutil, pygetwindow as gw, win32gui

def is_running(process_name: str) -> bool:
    """大小写不敏感地判断进程是否存在。"""
    name = process_name.lower()
    return any(p.info["name"].lower() == name
               for p in psutil.process_iter(["name"]))

def find_window(title_substring: str) -> int | None:
    """按窗口标题子串找第一个可见 hwnd；返回 None 表示没开。"""
    for w in gw.getAllWindows():
        if w.visible and title_substring in w.title:
            return w._hWnd
    return None

# 更精细：按 className + title 双条件（防止误命中 IME/通知中心子窗口）
def find_window_strict(class_name: str | None, title_re: str) -> int | None:
    import re
    pattern = re.compile(title_re)
    found = []
    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd): return True
        if class_name and win32gui.GetClassName(hwnd) != class_name: return True
        if pattern.search(win32gui.GetWindowText(hwnd)):
            found.append(hwnd)
        return True
    win32gui.EnumWindows(cb, None)
    return found[0] if found else None
```

#### C. 用进程名 + 窗口标题双重判定（推荐）

单看进程不够（微信关到托盘后进程仍在，但没主窗口）；单看窗口也不够（窗口偶尔
被最小化到任务栏不在 `getAllWindows` 里）。**两者结合**最稳：

| 状态 | `is_running` | `find_window` | 应该做什么 |
|------|:---:|:---:|------|
| 完全没开 | False | None | 走 launch（shortcut / uri / exe） |
| 跑着 + 有窗口 | True | hwnd | `SetForegroundWindow(hwnd)` 直接切焦点 |
| 跑着 + 托盘 / 最小化 | True | None | 发全局热键（如 `Ctrl+Alt+W`）唤出主窗 |
| 跑着 + 多窗口（Chrome） | True | [hwnd...] | 选标题最近匹配的；或全部列给模型挑 |

#### D. SetForegroundWindow 的坑

Windows 有"焦点偷窃保护"，纯后台进程调用 `SetForegroundWindow` 可能被拒（窗口
只闪任务栏不前置）。常见 workaround：

```python
import win32gui, win32con, win32process, win32api

def force_foreground(hwnd):
    # 1) 先恢复（如果最小化了）
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    # 2) AttachThreadInput 把"输入队列"挂到目标线程，绕开保护
    fg = win32gui.GetForegroundWindow()
    cur_tid = win32api.GetCurrentThreadId()
    fg_tid, _ = win32process.GetWindowThreadProcessId(fg)
    if fg_tid != cur_tid:
        win32process.AttachThreadInput(cur_tid, fg_tid, True)
        try:
            win32gui.SetForegroundWindow(hwnd)
        finally:
            win32process.AttachThreadInput(cur_tid, fg_tid, False)
    else:
        win32gui.SetForegroundWindow(hwnd)
```

更轻的兜底：先 `keybd_event(VK_MENU, 0, 0, 0)` 模拟一下 Alt 键（Windows 认为
"有用户输入"了，会放行 SetForegroundWindow）。

### 12.4 UI 自动化框架（深入 App 内部也不依赖视觉）

适合"完全不要视觉"的精确控制：

- **`pywinauto` (UIA backend)**：`Application(backend="uia").connect(title="微信").child_window(title="搜索", control_type="Edit").set_focus().type_keys("吕敏")`
- **`comtypes` + UI Automation**（NVDA 同款 accessibility API），能从任意 App 拿到"按钮列表 + 名字 + 坐标"。
- **WinAppDriver / Appium**：Microsoft 官方 UI 测试驱动，跨 Win32/UWP/WPF。

⚠️ **微信 / QQ 等大量自绘控件**，UIA 树几乎是空的（这就是为什么各家 RPA 工具
对这些 App 也只能截图）。所以这条路对**生产力 App**（VS Code / Office / Chrome /
资源管理器）非常香，对**社交 App** 仍然要回退视觉。

### 12.5 对应到 ctrlapp 的设计：`launch_app` meta tool

最契合现状的"启动 App 不用截图"协议是新增 meta tool `launch_app(name)`，
内部按 12.1–12.3 的优先级依次尝试：

```python
# python/ctrlapp/launchers.py（草案）
LAUNCHERS = {
    "wechat":   {"shortcut": "ctrl+alt+w", "uri": "weixin://", "exe": "wechat",
                 "process": "WeChat.exe", "window_title": "微信"},
    "vscode":   {"uri": "vscode://", "exe": "code", "process": "Code.exe",
                 "window_title_re": r"Visual Studio Code"},
    "outlook":  {"uri": "ms-outlook://", "exe": "outlook", "process": "OUTLOOK.EXE"},
    "chrome":   {"exe": "chrome", "process": "chrome.exe"},
    "explorer": {"shortcut": "win+e", "exe": "explorer", "process": "explorer.exe"},
    "notepad":  {"shortcut": ["win+r", "notepad", "enter"], "exe": "notepad"},
    "settings": {"uri": "ms-settings:"},
    "run":      {"shortcut": "win+r"},
}

def launch(name: str) -> str:
    spec = LAUNCHERS[name]
    # 1) 已经在跑 → 找窗口激活（零截图、零启动）
    if hwnd := find_window(spec):
        win32gui.SetForegroundWindow(hwnd)
        return f"activated existing window of {name}"
    # 2) 用户配置过的全局快捷键
    if "shortcut" in spec:
        send_hotkey(spec["shortcut"])
        return f"launched {name} via shortcut"
    # 3) 协议 URI 优先（不影响其他实例 / 可带参数）
    if "uri" in spec:
        subprocess.Popen(f"start {spec['uri']}", shell=True)
        return f"launched {name} via uri {spec['uri']}"
    # 4) 别名 / 绝对路径兜底
    subprocess.Popen(spec["exe"], shell=True)
    return f"launched {name} via exe {spec['exe']}"
```

模型从此可以：

```jsonc
{"name": "launch_app", "arguments": {"name": "wechat"}}
// → ToolResult: "activated existing window of WeChat (hwnd=0x12340)"
```

之后视觉只负责"在微信窗口里找吕敏 / 点输入框 / 输入 / 发送"，**省掉任务前 2~4 步**
（找托盘 → 点托盘 → 等窗口 → 验证）。

#### 配套设施

- **首次扫描**：sidecar 启动时跑一次 `Get-StartApps` + `Get-ChildItem shell:AppsFolder` +
  注册表 App Paths，把所有候选 App 写入 `%LOCALAPPDATA%\dev.ctrlapp\launchers.json`。
  模型可以 `list_apps()` 查询。
- **UI 编辑器**：用户可在 `/launchers` 页面手动校准（"我电脑上的微信路径是 D:\..."、
  "我设置的快捷键是 Ctrl+Shift+W"），或快速测试某个 launcher 是否真能起 App。
- **学习反馈**：模型某次成功用 `Ctrl+Alt+W` 起了微信，自动 `learn_tip` 一条
  "WeChat hotkey works on this machine"，下次直接用。

### 12.6 跟 §10 / §11 / TODO #4 区域库的关系

| 模块 | 解决什么 | 对接关系 |
|------|----------|----------|
| **`launch_app`**（本节） | 启动 / 切换 App，**App 边界以外** | 独立、最低成本，单文件 ~100 行 |
| **§10 A region 任意框** | App 启动后任意精细围观，**App 内部** | 与 launch_app 互补 |
| **§11 J1 取消 nudge** | 砍虚步 | 正交、独立做 |
| **TODO #4 App 区域库** | App 内部按区域查坐标，**完全跳过视觉** | launch_app 是它的兄弟模块；先有 launch_app 才能稳定校准 region |

### 12.7 优先级建议

`launch_app` 比 §10 / §11 任何视觉改进都**优先**——它能直接让"打开 X"这种典型任务
**从 4–6 步降到 1 步**，且实现成本低（~100 行 Python + 一个 JSON）。

**先支持 5 个高频 App**：微信 / VS Code / Outlook / Chrome / 资源管理器；
其余按用户需求增量加。

---

## 13. 截图链路重构：函数化 + 启动差分捕获 L2 + 点击前后 L3 校验

> 本节是对 §10 / §11 的整合落地方案：把"模型该不该截图、截哪、截完怎么验"
> 这套决策从 prompt 工程移到**确定性的代码 hook**里，让 LLM 把宝贵的 reasoning
> 步数花在"做什么"而不是"看哪"。设计前提：[`launch_app`](todo.md) 已落地。

### 13.1 现状的三个痛点

1. **screenshot 藏在 `computer.action` 里** —— schema 大、参数耦合（`coordinate` /
   `text` / `level` 共用一个对象），弱模型经常把"截图"当"点一下"调；
   独立加一个新参数（如 §10A 的 `region`）就要扩 `computer` 整个 schema。
2. **每步都自动补 L1** —— 真正最贵的瓶颈不是 token，而是"Anthropic / Copilot 的
   单图 vision pass + 模型对全屏 1568×~700 的低分辨率扫描"。launch_app 之后，
   App 主窗口的位置 / 尺寸完全已知，**根本不需要再让模型看一遍 L1 才知道窗口在哪**。
3. **点击校验是模型自检** —— 现在的两阶段 preview + post L3 取证靠模型"自己看图自己
   判断"，多 1 个 round-trip。其实"鼠标周围 200×200 的像素哈希在点击前后是否变化"
   是个**纯算法**问题，不该上 LLM。

### 13.2 R1 — `screenshot` 拆成独立 function tool

新建 `python/ctrlapp/screenshot_tool.py`，暴露顶层 `screenshot` 工具：

```jsonc
{
  "name": "screenshot",
  "parameters": {
    "level":          {"enum": ["fullscreen", "active_window", "cursor_local", "region"]},
    "region":         {"type": "array", "items": "number", "description": "[x, y, w, h] in screen px (level=region only)"},
    "max_long_edge":  {"type": "integer", "description": "0 = no shrink; clamped to 2400"},
    "quality":        {"type": "integer", "description": "1-100; 100 = PNG; default = config"}
  }
}
```

- 共享坐标系：仍写回到 `tool_state.last_capture`，`computer` 里所有点击动作从同一处
  读 offset / scale，与现状一致。
- **保留兼容别名**：`computer.action="screenshot"` 继续可用，内部直接转发到新 tool；
  这样 prompt / fixture / 历史 thread 不需要迁移即可回滚。
- 把 §10 方案 A / B / C（region / max_long_edge / quality）一次性收进新 schema。
- `_dispatch` / `_maybe_pre_click_verify` / 落点 L3 取证里所有"按 action 名分支"的 hook
  改成"按 tool name 分支"——`tool_name in ("screenshot", "computer")` 二选一。

### 13.3 R2 — 起手只 L1 一次；之后 launch_app 用差分给出 L2

#### 时序

```
任务起手
  ├─ [拍] L1 init → step-000-init.png    (唯一一次主动 L1)
  └─ 与首条 user message 一并发出

模型决定 launch_app("wechat")
  ├─ pre  = sensor.capture(L1)                       (loop 内拍，不发给模型)
  ├─ launchers.launch("wechat")                       (现有逻辑)
  ├─ 等待 ≤ 1.5s（轮询 GetForegroundWindow / process_iter）
  ├─ post = sensor.capture(L1)                       (loop 内拍)
  ├─ region = diff_bbox(pre, post)                    (确定性算法，见下)
  ├─ 若 region 命中 (面积比 > min_ratio):
  │     l2 = sensor.capture_region(region.x, region.y, region.w, region.h)
  │     落盘 step-NNN-launch_app-l2.png
  │     回送给模型一条 user message（注意：是 user 而不是 tool_result，
  │       因为这是 loop 主动追加的视觉提示，不算 launch_app 的"返回值"）：
  │       ## launch_app result (visual)
  │       app=wechat hwnd=0x1234 region=[x,y,w,h] confidence=0.92
  │       <l2 jpeg q80>
  └─ 若 diff 失败:
        l2 = sensor.capture_window(hwnd)             (用 Windows API 拿 hwnd 客户区)
        以同样格式回送，confidence=0.0 + reason="diff fallback"
```

#### diff 算法（约 30 行）

```python
from PIL import ImageChops, ImageFilter

def diff_bbox(pre: Image, post: Image, min_area_ratio: float = 0.05) -> Box | None:
    """返回 (x, y, w, h)；None 表示差异太小 / 太散，无法判定。"""
    if pre.size != post.size:
        return None
    diff = ImageChops.difference(pre.convert("L"), post.convert("L"))
    diff = diff.point(lambda v: 255 if v > 25 else 0)   # 阈值二值化
    diff = diff.filter(ImageFilter.MaxFilter(5))         # 吞掉微抖动
    bbox = diff.getbbox()
    if not bbox:
        return None
    x0, y0, x1, y1 = bbox
    w, h = x1 - x0, y1 - y0
    if (w * h) / (pre.size[0] * pre.size[1]) < min_area_ratio:
        return None
    return (x0, y0, w, h)
```

> 算法用 dHash 也行，但 ImageChops.difference + getbbox 已经够了，且能直接给出
> bbox。多个不连通变化区域时取并集（getbbox 自带），再让 launch_app 的 hwnd
> 客户区做 sanity 校验：如果 diff bbox 与 GetWindowRect(hwnd) IoU < 0.3，
> 说明 diff 命中的可能不是新 App（可能是通知 / 时钟刷新），回退到 window rect。

#### 收益

- "起手 → launch_app → 模型看到新 App" 从 4 个 round-trip（init L1 → launch tool_call
  → L1 看一眼 → L3 围观）压到 **2 个**：init L1 + 含 L2 的 launch_app 推送消息。
- L2 是 App 客户区裁剪 + 局部高分辨率（不再把 4K 屏整张缩到 1568px），按钮文字识别准确率显著提升。
- 模型完全不需要"L1 找窗口在哪"的视觉步骤——hwnd 和 region 都是 Windows API 给的事实。

### 13.4 R3 — 点击前后 L3 + L2 双图，dHash 算法判定"有没有发生事"

替代当前的两阶段 preview + 模型自检 L3 取证：

```
模型: computer({action: "left_click", coordinate: [x, y]})
  ├─ pre_l3  = sensor.capture_around(x, y, 100)        (loop 内拍, 不发)
  ├─ driver.click(x, y)                                 (执行)
  ├─ time.sleep(0.15)                                   (等 UI 反应)
  ├─ post_l3 = sensor.capture_around(x, y, 100)
  ├─ post_l2 = sensor.capture(L2)                       (活动窗口)
  ├─ sim = dhash_similarity(pre_l3, post_l3)
  └─ 回送 ToolResult:
        若 sim > 0.97:
            output  = "click executed but no visual change near cursor (dHash sim=0.98); coordinate may be wrong or target unresponsive"
            image   = post_l2 (PNG)                    ← 让模型直接看活动窗口现状
            error   = None  (软警告，模型自行决策重试)
        否则:
            output  = f"click landed at ({x},{y}); pre/post diff sim={sim:.2f}"
            image   = post_l2
```

- 把"明显没点中"这个判定从模型脑子搬到 8 行代码，**省掉一次完整 round-trip**。
- 仍然给模型 post L2，让它有视觉上下文做下一步决策；不再单独发 L3 取证图（信息量小）。
- 两阶段 preview 改为"信心评估触发"：仅当 (a) 模型在 L1 直接点 < 32px 区域，
  或 (b) 上一次同坐标点击命中过"no visual change" 警告时，才走 preview → confirmed
  双步流程。其它情况直接执行。

#### 配置

```toml
[screenshot]
launch_diff_enabled            = true
launch_diff_min_area_ratio     = 0.05    # 差异区域 < 5% 屏幕面积视为失败
launch_diff_window_iou         = 0.30    # diff bbox 与 hwnd 客户区 IoU 下限
click_verify_dhash_threshold   = 0.97    # >= 该阈值视为"没动"
click_verify_post_sleep_ms     = 150     # 点击后等多久再拍 post
first_l1_only                  = true    # 起手 L1 后只在显式请求时再发 L1
```

### 13.5 R4 — 坐标系强校验

每次 `computer` 收到带 `coordinate` 的 action：

```python
cap = tool_state.last_capture
if cap is None:
    return ToolResult(error="no screenshot taken yet; call screenshot() first")
ix, iy = args["coordinate"]
sw, sh = cap.sent_size
if not (0 <= ix < sw and 0 <= iy < sh):
    return ToolResult(error=(
        f"coordinate ({ix},{iy}) is outside the most recent screenshot "
        f"({sw}x{sh}, level={cap.level.value}). Call screenshot() again to refresh, "
        f"then re-issue the click with coordinates inside the new image."
    ))
```

把 §9 第 1 条"越界静默换算"的暗坑彻底堵上。

### 13.6 实现拆分（建议提交粒度）

| 提交 | 内容 | 行数估算 |
|------|------|----------|
| C1 | R4 坐标越界硬校验 + 单测 | ~30 |
| C2 | R1 抽出独立 `screenshot` tool（含兼容别名） | ~120 |
| C3 | R2 launch_app 接 diff + 自动注入 L2 user message | ~150 |
| C4 | R3 点击前后 dHash 校验，preview 降级为兜底 | ~80 |
| C5 | 配置 + 文档 + 端到端 thread 跑通微信发消息（验证步数从 ~12 降到 ~5） | ~30 |

C1 → C5 顺序解耦，每一步都可独立验证、独立回滚。

### 13.7 与 §10 / §11 / §12 的关系

| 已有方案 | 本节新增 | 关系 |
|----------|----------|------|
| §10 A region | R1 把 region 收进独立 screenshot tool | 实现层重叠，本节统一落地 |
| §11 I preview 可跳过 | R3 把 preview 降级为兜底 | 同向，更激进 |
| §11 J1 取消 nudge | — | 正交，仍要单独做 |
| §12 launch_app | R2 给 launch_app 配视觉返回 | 互补：原方案管"开"，本节管"开完后看到了什么" |
| TODO #4 区域库 | — | 区域库可以在 R2 落地后用 diff 算法**自动**校准（首次跑 launch_app 时把 diff bbox 写进 region.json） |

### 13.8 风险与回退

- **diff 误判**：通知弹窗、时钟刷新、桌面壁纸动效都会污染 diff。缓解：(a) 跟 hwnd
  rect 求 IoU 做 sanity 校验；(b) 给 launch_app 一个 `expected_window_size_ratio` 配置；
  (c) `launch_diff_enabled=false` 一键回退到"模型自己看 L1"。
- **dHash 阈值**：97% 是经验值，对动效多的 App（Chrome 标签切换有动画）可能误报"没动"。
  缓解：阈值可配置；首次出现误报时把"该 App 的 click_verify_threshold"作为 `learn_tip`
  写回，下次自动放宽。
- **兼容别名**：`computer.action="screenshot"` 保留 1~2 个版本后再移除，给历史 thread
  和外部 fixture 充分迁移期。




