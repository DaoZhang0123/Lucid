# 截图（Screen Sensor）设计方案 — v2.1

> ✅ 本文档描述当前实现（v2，2026-05 落地，2026-05-10 精简）。
> 关联代码：[lucid/screen.py](../lucid/screen.py),
> [lucid/loop.py](../lucid/loop.py),
> [lucid/context_manager.py](../lucid/context_manager.py),
> [lucid/tools.py](../lucid/tools.py),
> [lucid/meta_tools.py](../lucid/meta_tools.py),
> [lucid/system_prompt.py](../lucid/system_prompt.py),
> [lucid/config.py](../lucid/config.py).

---

## 0. 设计原则

**图很贵，能不发就不发。** 由"系统已经知道什么"驱动，由模型按需主动加图。

| 原则 | 含义 |
|---|---|
| 默认无图 | post-step **不再自动补图**；模型没要图就不发图（launch_app 的副产物 L2 例外） |
| L2 是 launch 的副产物 | `launch_app` / `focus_window` 成功后顺手附一张 L2，**仅此一次**；不 pin、不维护 "map" 状态机 |
| L1 按需 | 模型显式 `screenshot(level="fullscreen")` 才拍；不再起手就抓 |
| L3 是点击锐化器 | UIA 拿到最小元素 → 拍小 tile；UIA 失败 → **回退 L2**（不再回退到固定 200×200） |
| L0 也按需 | 图标合集 atlas **不再自动注入** prelude；模型显式 `screenshot(level="icon_atlas")` 才发；system prompt 仅告知"有这张图可调用" |
| 系统提示必须教育模型 | system prompt 明确告诉模型："看不到图很正常；要看请主动 `screenshot()`" |
| 实时查窗口，不 pin | click 越界守卫、L2 范围都走实时 `GetForegroundWindow`，**不再** 缓存 `active_app_rect` |

---

## 1. 截图工具 schema

`computer` 工具的一个 action：

```jsonc
{ "name": "computer",
  "arguments": { "action": "screenshot",
                 "level": "fullscreen" | "active_window" | "cursor_local" | "icon_atlas" } }
```

`screenshot` 设置的 `last_capture` 是后续 `left_click` 的坐标系基准。**例外**：`icon_atlas` 的 `offset=None`，上层据此跳过 `last_capture = cap`，避免模型按 atlas 像素去点屏幕。

---

## 2. 四级金字塔

| 级别 | 名字 | 范围 | 默认下采样 | 谁触发 |
|------|------|------|------|------|
| **L0** | `icon_atlas` | 已安装应用图标合集（[`launcher_icons.build_atlas`](../lucid/launcher_icons.py)） | atlas 自身尺寸 | 仅模型主动 `screenshot(level="icon_atlas")` |
| **L1** | `fullscreen` | 整个虚拟桌面 | `l1_max_long_edge = 1568` | 仅模型主动 |
| **L2** | `active_window` | 当前前台窗口（**实时 `GetForegroundWindow`**） | `l2_max_long_edge = 1568` | 1) 模型主动；2) `launch_app` / `focus_window` 成功后系统附一次 |
| **L3** | `cursor_local` | UIA 最小元素 bbox（+`l3_smart_padding_px`，自动长大到 `min_w/min_h`）；**UIA 拿不到 → 回退 L2** | `l3_max_long_edge = 0`（不下采） | 1) 模型主动；2) click 两阶段预览（`safety.verify_click_target_before`） |

### 2.1 L2 一律实时查 `GetForegroundWindow`

旧版引入了 `tool.active_app_rect` —— 在 `launch_app` 时记下窗口矩形，之后每步 post 都按这个老矩形 crop。问题：用户拖窗、Win+方向贴边、模型自己最大化 → 矩形漂了。

新版每次需要 L2 时都现查 [`active_window()`](../lucid/window.py)：

```
_capture_active_window():
    win = active_window()                  # GetForegroundWindow + GetWindowRect
    if win is None or win 退化:
        raise NoForegroundWindowError
    return crop(win.rect)
```

### 2.2 L3 smart sizing：UIA 拿不到时回退 L2

```
_capture_cursor_local(cx, cy):
    rect = uia.element_rect_at(cx, cy)
    if rect is None:
        return _capture_active_window()    # 退到 L2
    if rect 太窄/太矮:
        以鼠标为中心拓到 l3_smart_min_w / l3_smart_min_h
    rect 四周外拓 l3_smart_padding_px，clamp 到屏幕边界
    return crop(rect)  # level=L3
```

UIA 偶尔会"偷懒"返回顶层窗口的 rect。原本有一条 `l3_smart_max_ratio` 拒绝过大 rect 的检查，2026-05 删掉了：拒绝之后的 fallback 也是整窗 L2，等价但多花一次截屏，不划算。

---

## 3. 何时拍：默认无图，按需主动

### 3.1 起手：**不拍图，也不发 atlas**

`Agent._run` 起手只发 system prompt + 真正的任务 user message。**没有截图，没有 atlas**。

system prompt 教育模型：

```
By default you receive NO screenshot. Screenshots are expensive
(80–150 KB JPEG each). Request one only when you actually need to
see the screen:

- screenshot(level="active_window") — current foreground app
- screenshot(level="cursor_local") — small tile around the mouse
- screenshot(level="fullscreen")   — whole desktop
- screenshot(level="icon_atlas")   — collage of all installed app
                                      icons; NOT a screen, no clicks

launch_app / focus_window already give you one L2 for free.
After that, no further auto-screenshots — ask if you need to see.
```

### 3.2 模型主动 `screenshot`

模型可以调 `computer({action:"screenshot", level:...})` 任意级别。`tool.last_capture` 被这一张更新；下一步若模型给 `coordinate` 就按这张图反算。`icon_atlas` 不更新 `last_capture`。

### 3.3 动作后自动补图（post-step）—— 极简两条规则

```
本步是否调用了 screenshot 或 launch/focus（attached L2）？
├─ 是 → 复用那张图作为 post 图回送（带坐标系文本）
└─ 否 → 不发 post 图。回送只含 tool result 文本。
```

不再有：L1 兜底、L2 map 状态机、L3 cursor-local 自动补、`active_app_rect` pin 维护。

### 3.4 click 落点 pixel-diff 校验 —— **已下线**（2026-05-10）

旧 R3 路径每次点击额外抓 2 张 L3（pre/post），算像素差比例，低于阈值就附 "may have missed" 提示。下线原因：

1. 与 "图很贵默认不发" 原则冲突 —— 每次点击都额外 2 张 L3。
2. 阈值 0.5% 调不准：抗锯齿、字体渲染、动画的差异天然在 0.1%–2% 之间，误报和漏报都多。
3. hint 信息含混：模型若需要核对落点，自己 `screenshot(level="cursor_local")` 更直接。

相关删除：`screenshot.click_verify_*` 4 个字段、`tools._click_with_verify`、`screen.ScreenSensor.pixel_diff_ratio`。

### 3.5 两阶段点击预览（preview）

`safety.verify_click_target_before = true`（默认 false）时：模型第一次提交 click 不带 `confirmed=true` → 系统抓一张 `capture_around(sx, sy, r)` tile 回送 → 模型确认后重发带 `confirmed=true` 才真正落点。

这张 tile 走 §2.2 的回退规则（UIA 拿到 → L3；拿不到 → L2）。

### 3.6 L0 icon_atlas

模型调 `screenshot(level="icon_atlas")` 时：

```
_capture_icon_atlas():
    atlas = launcher_icons.build_atlas(cfg_root)   # 已缓存
    if atlas is None or atlas 是空的:
        return ToolResult(text="icon atlas is empty (no apps scanned yet)",
                          image=None)
    return Capture(level=L0,
                   image=atlas.png_bytes,
                   raw_size=atlas.size,
                   sent_size=atlas.size,
                   offset=None,                   # 不是屏幕坐标系
                   text_index=atlas.captions)
```

回送 user message 文本：

```
[level=L0] icon_atlas: N apps, image is a labelled grid '[N] App name'.
THIS IS NOT THE SCREEN — coordinates here are meaningless for clicks.
Text index:
[1] WeChat
[2] QQ
...
```

`offset=None` 让上层跳过 `last_capture = cap`。

### 3.7 launch_app 副产物 L2

`launch_app` 成功后：等新窗口可见（最长 `launch_wait_max_ms`，每 `launch_wait_poll_ms` 轮询一次 `GetClientRect`），然后按 client rect 裁一张 L2 附在 ToolResult 里。这张 L2 同步设到 `last_capture`，作为模型下一步操作的坐标系基准。

> 旧版还有 R2 像素 diff（拍 launch 前后两张 L1，找差异 bbox 当作"新窗口位置"），2026-05 删掉。`GetClientRect` 简单可靠，diff 路径只在新窗口由别的进程绘制时（非常少见）才有用，性价比低。

---

## 4. 越界点击守卫：实时查窗口 rect

```python
def _validate_click_in_foreground(sx, sy):
    win = active_window()                      # GetForegroundWindow + GetWindowRect
    if win is None:
        return None                            # 锁屏等 → 不拦
    if not (win.left <= sx < win.right and win.top <= sy < win.bottom):
        return f"click ({sx},{sy}) is outside foreground window '{win.title}' ..."
    return None
```

旧版用 `active_app_rect` pin，窗口被拖走后老 rect 失效会误拦合法点击；新版每次实时查 `GetForegroundWindow`（< 1 ms），永远是当前真实状态。

---

## 5. 编码 / 历史压缩 / 总结

- `Capture.encoded_for_send`：L1/L2 → JPEG q80（~80–150 KB）；L3 / L0 atlas → PNG（无损）。
- `ContextManager.compress_old_images`：每级保留 `keep_recent_l1` / `_l2` / `_l3` 张最近图；旧图重压 q35/720px 或替换为占位文字。
- `min_per_l2_app`：跨 App 任务里给每个 active app 至少留 N 张最近 L2，防止新 App 把旧 App 的关键 L2 一次性挤掉。
- `ContextManager.maybe_summarize`：> 0.7 × ctx_tokens 时调 LLM 写 ≤1500 tokens 摘要。

---

## 6. 落盘 / 调试可视化

每张实际拍的图仍落 `~/.lucid/logs/<thread>/step-NNN-*.png`，并在 `events.jsonl` 写 `step_image`。`_sanitize_for_log` 仍把 base64 → `[image: <文件名>]`。

> 起手不拍 L1 后，`step-000-init.png` 不再产生。第一张落盘图就是模型主动要的（或 launch_app 附的 L2）。

---

## 7. 模型坐标系跟着图走

回送图的同一条 user message 文本里写：

```
[level=L2] active_window：发送尺寸 1568x910（原始 2560x1488）；
位于屏幕 (left=0, top=0, right=2560, bottom=1488)；
图内 (px,py) → 屏幕 (0+px*1.633, 0+py*1.635)。
```

规则一致：**最近一张发给模型的真实屏幕图就是坐标系基准**。L0 atlas 不更新基准。

---

## 8. 端到端时序

```
启动任务
  └─ 拼 system + user("任务：…")  → 发首条请求
     （没有 first 截图，没有 atlas）

──── 进入主循环 (step = 1..hard_cap) ────

step N:
  ├─ [压] context_mgr.compress_old_images(messages)
  ├─ [压] context_mgr.maybe_summarize(messages)
  ├─ [发] llm.chat(messages, tools=[computer, …])
  │
  ├─ for each tool_call:
  │     ├─ if 点击 且 verify_click_target_before 且没 confirmed:
  │     │     [拍] capture_around(sx, sy, r)（§3.5；UIA 拿不到 → L2）
  │     │     不真正点；tile 当 ToolResult 回送
  │     ├─ elif 点击落点超出 active_window().rect:
  │     │     拒绝（§4），返回错误文本，不点
  │     ├─ elif action == "screenshot":
  │     │     if level == "icon_atlas":
  │     │       [拼] launcher_icons.build_atlas(cfg)（§3.6）
  │     │       had_atlas = True; **不动 last_capture**
  │     │     else:
  │     │       [拍] sensor.capture(level)
  │     │       last_capture = cap; had_screenshot = True
  │     ├─ elif action == "launch_app" / "focus_window":
  │     │     do action; 成功后等窗口可见 → 按 GetClientRect 抓 L2
  │     │     attach 到 ToolResult; last_capture = L2; had_attached = True
  │     └─ else (mouse/key/type/...):
  │           driver.<action>(...)
  │
  ├─ 组装回送 user message:
  │     if had_screenshot or had_attached:
  │         · text(坐标系) + 该图（JPEG/PNG）
  │     elif had_atlas:
  │         · text("L0; not a screen, do not click") + atlas PNG
  │     else:
  │         · 只回 tool_result 文本（无图）
  │
  └─ messages.append(...) → step N+1

──── 出循环 ────
  · 模型说 "task complete:" / "task failed:"  → 正常结束
  · 步数达上限                                  → 强制结束
```

---

## 9. 配置一览

```toml
[screenshot]
l1_max_long_edge = 1568
l2_max_long_edge = 1568
l3_max_long_edge = 0
send_jpeg_for_l1_l2 = true
send_jpeg_quality   = 80
keep_recent_l1   = 1
keep_recent_l2   = 1
keep_recent_l3   = 2
min_per_l2_app   = 1
launch_wait_max_ms  = 1500
launch_wait_poll_ms = 80
l3_smart_padding_px = 16
l3_smart_min_w   = 160
l3_smart_min_h   = 80

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
verify_click_target_before      = false  # 两阶段点击预览（默认关）
verify_click_target_radius_px   = 60
save_dialog_sidebar_guard       = true
```

**已删除字段**（防回流误填，列在这里）：
`l1_fullscreen_interval` / `l2_activewindow_interval` / `l3_cursor_interval` / `l3_radius_px` / `l3_smart_enabled` / `l3_smart_max_ratio` / `post_step_use_l3` / `feed_initial_l1_to_llm` / `skip_if_similarity_above` / `launch_diff_enabled` / `launch_diff_min_area_ratio` / `click_verify_enabled` / `click_no_change_threshold` / `click_verify_post_sleep_ms` / `click_verify_radius_px` / `[safety].verify_click_with_l3`。

---

## 10. 键盘焦点 ≠ 鼠标位置 ≠ L3 中心

L3 仍以 cursor 为中心（UIA 走 `ElementFromPoint(cursor)`），所以"键盘 type 后 L3 拍不到焦点控件"这个已知现象不变。建议解法 A/B/C：

- **A** 模型自己 `screenshot(level="active_window")` 校验（零代码，新版 system prompt 引导就够）
- **B** 在 [meta_tools.py](../lucid/meta_tools.py) 加 `read_text(automation_id=...)` 工具，UIA 直接读文本，不发图
- **C** 加 `cfg.l3_follow_focus`，L3 优先用 focused element rect

目前都未实现，A 在生产环境足够用。

---

## 11. 已知坑 / 待改进

- **越界坐标静默换算**：模型给的 coordinate 超出 last_capture 的 sent_size 时 loop 仍按 offset+scale 硬算。应该在 `tools.py` 的坐标转换里加越界硬校验。详见 `Docs/todo.md`。
- **锁屏时 BitBlt 拒绝访问**：Winlogon secure desktop 拒所有用户态截屏，`mss.grab` 抛 `ScreenShotError`；首次运行引导里关锁屏 / 睡眠 / 屏保。
- **多显示器 + DPI 混合**：当前 `active_window()` 走 `GetWindowRect`，per-monitor v2 DPI awareness 已在 [dpi.py](../lucid/dpi.py) 设过；rect 与 `mss.grab` 用同一坐标系，无需缩放。

---

## 12. 改动 summary（v1 → v2.1）

| 模块 | 旧（v1） | 新（v2.1） |
|---|---|---|
| 起手 | 拍 L1，落 step-000-init.png | 不拍 |
| 起手 atlas | 拼一对假 user/assistant turn 塞进 prelude | 删除；`screenshot(level="icon_atlas")` 按需 |
| post-step 默认 | L1 / L2-map / L3-cursor-local 三分支状态机 | 只在 had_screenshot / had_attached 时回送图 |
| `active_app_rect` pin | launch 时记一次，全程用 | 删除；改为实时 `GetForegroundWindow` |
| 越界点击守卫 | pin 的 active_app_rect | 实时 `GetWindowRect` |
| L3 UIA 失败回退 | 固定 200×200 方块 | 回退到 L2 |
| launch_app L2 来源 | R2 pre/post L1 像素 diff bbox | `GetClientRect(hwnd)` |
| click pixel-diff 校验（R3） | 默认开 | 删除（2026-05-10） |
| 落点 L3 取证（`verify_click_with_l3`） | 默认开 | 删除 |
| 配置项 | 旧字段保留 | 见 §9 "已删除字段" 列表 |
| system prompt | 默认期望模型每步都收到图 | 明确告诉模型"默认无图，要看请主动" |

净减代码 ~400 行；state 简化为 0 个 pin 状态位；`ScreenSensor.diff_bbox`、`ScreenSensor.pixel_diff_ratio` 等死代码已移除。
