# lucid · TODO

按 [design.md §6 路线图](design.md) 拆分。已完成项打勾，进行中项 `[~]`，未开始 `[ ]`。

---

## Phase 0 — Spike（命令行验证可行性）✅ 已完成

### 兜底加固（在 Phase 0 基础上叠加）
- [x] 架构层兜底：点击类动作后额外抓一张 L3 鼠标周边取证图回送（`[safety].verify_click_with_l3`，可关）
- [x] 行为层兜底：保存/打开对话框 sidebar guard 状态机——侦测保存触发键 → armed 6 步窗口内若有"左侧 25% 区域点击"则注入"请改用文件名框/地址栏 type 路径"提示（`[safety].save_dialog_sidebar_guard`，可关）


- [x] Per-Monitor V2 DPI Aware + 多屏虚拟坐标
- [x] 三级金字塔截图接入 ReAct（L1 全屏 / L2 活动窗口 / L3 鼠标周边；screenshot 动作接受 `level` 参数）
- [x] 自定义 OpenAI function tool `computer`（点击 / 拖拽 / 滚轮 / type / 快捷键 / wait / cursor_position）
- [x] 中文 / 任意文本 type 走剪贴板 + Ctrl+V，规避输入法干扰
- [x] Safety Layer：危险词命中 + `confirm_each` 档位的终端 y/n 确认
- [x] CLI：`python -m lucid "..."` + `--smoke-test`
- [x] LLM 通过本地 LiteLLM 代理（OpenAI 兼容协议），支持 GitHub Copilot 后端
- [x] 5xx 重试（`proxy_client.chat_once`）
- [x] 对话历史滑窗（`[llm].keep_recent_screenshots`）防 413；裁剪会保证**每级 L1/L2/L3 都保留最新一张**再叠加全局最近 N 张
- [x] 主循环鲁棒性：System prompt 强制中间步骤调工具；旁白时 nudge；"任务完成:/任务失败:" 终态
- [x] 本地运行日志：`logs/<时间戳>-<slug>/{run.log, messages.jsonl, step-*.png}`，文本与图像独立等级
- [x] 端到端用例：用 Win+R 打开记事本 → type 文本 → Ctrl+S → 在文件名框 type 完整路径 → 回车保存；PowerShell 核对文件存在 + 内容正确（可复现）

---

## Phase 1 — MVP（个人可用）

目标：把 CLI 包装成"普通 Windows 用户能装、能聊、能急停"的小工具。

### 1.1 Tauri 壳 + WebView2 聊天窗
- [x] Rust workspace 脚手架（Tauri 2.x + WebView2 + 单 main window + 系统托盘）
- [x] 选择前端栈（SvelteKit + Vite SPA，单页聊天 UI）
- [x] 聊天窗 UI：输入框 / 消息流 / 状态条（当前自动度 / 步数预算 / 急停按钮）
- [x] 托盘菜单：显示/隐藏窗口、急停（取消任务）、退出
- [x] 窗口最小化到托盘而非任务栏（CloseRequested → hide + prevent_close）

### 1.2 Tauri ↔ Python 守护进程
- [x] Python 侧：自写 stdio JSON-RPC 暴露 `ping / start_task / cancel / get_status / shutdown`（NDJSON 帧、stdout 协议 / stderr 日志）
- [x] 流式事件：`run_start / step_start / assistant_text / tool_call / tool_result / step_image / final / error / thread_changed` 通过 stdout 推到前端
- [x] Thread 级 RPC：`thread_new / thread_list / thread_read / thread_set_active / thread_delete / thread_read_image`（按对话而非按任务汇总）
- [x] 任务取消：`cancel_event` 在两步之间生效；CancelledError 收尾
- [x] Rust 侧：以 sidecar 拉起 `lucid.exe`（PyInstaller 打包），管理生命周期
- [x] 进程崩溃自动重启 + 错误展示（`supervise()` 1s 重连、`lucid://sidecar` 事件流）

### 1.3 安全 / 体验
- [x] 三档自动度（`full / confirm_critical / confirm_each`）UI 切换并实时下发
- [x] 全局急停热键（`Ctrl+Alt+Esc`，design.md §4.7）
- [ ] 鼠标移到屏幕左上角的 PyAutoGUI fail-safe 在 UI 上提示出来
- [x] 任务进行中显示当前动作类型 + 即时取消按钮
- [ ] 隐私白名单：用户可标记某进程/窗口标题为"截图前需告警/最小化"

### 1.4 对话 / Thread / 截图
- [x] **按 thread 而非按 task 归档**：同一 thread 的多次任务共享 `thread-<ts>-<slug>/` 目录（`meta.json` + `events.jsonl` + `step-*.png`）
- [x] 左侧可折叠 thread 列表（点击切换、悬停 ✕ 删除、顶部 + 新建）替换独立 `/history` 页
- [x] 主聊天窗嵌入截图缩略图（点击 lightbox 放大），包含起手 init 图
- [x] 切换路由不丢失聊天状态（模块级 `$state` 单例 store）
- [x] **多模态输入：粘贴截图 / 拖拽文件 / 📎 对话框**（2026-05-09）：图片粘贴/拖拽自动入 `%LOCALAPPDATA%\dev.lucid\inbox\`；JS `fileRefs` ↔ Rust `file_refs` 通过 sidecar `start_task` 透传给 `Agent`，作为 `[Attached files]` 块拼到首条 user message（默认是"载荷不读内容"，verb 启发式判定何时改读 —— [loop.py](lucid/loop.py) L613）。`load_local_images`（当时名 `load_screenshot`）白名单扩到 `logs / inbox / LUCID_CWD\inbox`。前端 chip 缩略图走 `read_attachment_b64` Rust 命令（asset:// 默认未启用）；用户图片右对齐 (`fromUser`)。新事件 `user_attachments` 持久化到 `events.jsonl`，切换/重开 thread 也能复现 chip。

### 1.5 适配与体验细节
- [x] 多屏布局变化时重新探测 `[screenshot].l1_max_long_edge`（`lucid.selfcheck monitors` + 设置页一键自检）
- [x] HiDPI 标度差异下的坐标自检（`lucid.selfcheck click` 用 dHash 比对点击前后变化）
- [x] 非英文系统下“win+r”等组合键 alias 自检（`lucid.selfcheck winr`）

### 1.6 5 个示例场景跑通
- [x] 记事本：写文本并保存到指定路径（Phase 0 已端到端通；`lucid.examples run notepad`）
- [~] 浏览器：打开 URL → 截图描述页面（指令已编入 `lucid.examples`，待真跑验证）
- [~] Excel：在 A1 输入“周报”并保存到桌面（指令已编入，待真跑验证）
- [~] 微信：找到指定联系人 → 发送一句消息（HITL 强确认，已编入并强制 `confirm_each`）
- [~] 文件管理器：在指定文件夹新建子文件夹并重命名（指令已编入，待真跑验证）

### 1.7 打包与发布
- [x] PyInstaller 单目录打包 `lucid.exe`（`packaging/lucid.spec`，含 mss / pyautogui / pyperclip / openai SDK）
- [x] Tauri `cargo tauri build` → `.msi`（已配 bundle.targets=msi+nsis，bundle.resources=dist/lucid）
- [x] 安装包内嵌 `config.toml` 默认值；首次启动引导用户填代理 base_url / api_key（`/settings` 页 + `read_settings`/`write_settings` 命令）
- [ ] 自动更新（Tauri updater + GitHub Release）（updater plugin 已在 tauri.conf.json 占位但 active=false）
- [ ] 签名（可后置，先 SmartScreen 警告也能用）

---

## Phase 2 — 准生产（暂不展开，待 Phase 1 验证后细化）

- [ ] Set-of-Mark 增强模式（候选可点击区域编号叠加在截图上）
- [x] 用户可保存的"任务模板"（如"周报流程"）—— `templates.json` + `/templates` 页面，可一键发送
- [ ] 隐私沙箱：敏感区域自动模糊（密码框 / 网银 / 私聊）
- [x] 模型可插拔（Claude / GPT-4o / Qwen-VL）通过同一 OpenAI 兼容代理切换 —— 设置页 provider 切换 + `reload_config` 热生效
- [x] **长期记忆 `memory.md` + 心跳 (heartbeat)**（heartbeat 留接口未启用）：
  - `lucid/memory.py`：`memory_for_prompt` 在每次起手注入 system prompt 末尾
  - 主动写入：模型用 `remember(text)` 工具调用追加；`/memory` 页面可手编/清空
  - 路径：`%LOCALAPPDATA%\dev.lucid\memory.md`；`[memory]` 配置 enabled / max_entries / max_chars
- [x] **操作技巧 `tools.md`（可演化的提示库）**：
  - `lucid/tooltips.py`：`tools_for_prompt` 在每次起手注入 system prompt 末尾
  - 首次启动 seed：自动写入"键盘优先 / 不覆盖用户内容 / 先 alt+tab 看是否已开 / 保存对话框直接 type 绝对路径"等通用技法（原 SYSTEM_PROMPT 中的"常用技巧"段）
  - 主动写入：模型用 `learn_tip(text, kind)` 把任务中总结的成功路径或失败教训追加进文件；`/tools` 页面可手编 / 一键追加 / 重置 seed
  - 路径：`%LOCALAPPDATA%\dev.lucid\tools.md`；`[tools]` 配置 enabled / max_entries / max_chars
- [~] **桌面通知监听（改视觉方案）**：
  - 结论更新：Teams / WeChat 在当前环境均无法稳定进入 Action Center；改为识别桌面底部中间任务栏区域的应用状态（图标徽标、小红点、任务栏预览提示）。
  - v1 路线：周期性抓取任务栏中部窄带（约底部 120px × 居中 40%-60% 宽），用颜色阈值 + 小区域连通域检测红点/徽标变化；只做事件记录，不自动点击。
  - v2 路线：为 Teams / WeChat 建立图标锚点和徽标模板，按图标邻域做定向检测，减少误报。
  - v3 路线：检测到疑似新消息后，再触发一次 L3/L2 精细截图，让主 Agent判断“是否需要打开 App 查看并回复”。
  - 详细方案见：`Docs/visual-notify-taskbar.md`。
- [x] **Cron 定时任务**：
  - `lucid/scheduler.py`：minute-tick `Scheduler` 后台线程随 sidecar 启动；支持 interval / daily / weekly 三种触发
  - 持久化：`schedules.json`（`%LOCALAPPDATA%\dev.lucid\`），含 `next_ms` / `last_run_ms` / `enabled`
  - `/schedules` 页面 CRUD + 暂停/启用；触发时自动 `thread_new + start_task`，运行中冲突自动跳过（`schedule_skipped` 事件）
  - 仍未做：cron 表达式语法（5 字段）、heartbeat 反思、桌面通知联动
- [x] **同 thread 多轮 run 的 context 持久化（F3）**：每次 `_run` 结束把 messages 去掉 prelude 后落盘到 `thread/messages.json`；下次同 thread 起新 run 时载入 tail 拼到新 instruction 之前。prelude（system + atlas）每次重建以反映 memory.md / tools.md / icons 的更新。
- [x] Image 压缩放到 context manager 里（`lucid/context_manager.py` `compress_old_images`：未命中“最近 K 张/级 + 全局最近 N 张”保留集的旧截图，先尝试解码 → 等比缩放到 `[context].image_recompress_max_long_edge` → JPEG @ `image_recompress_quality` 重编码；若新字节反而更大或重压关闭则回落到原 `[old screenshot omitted]` 文本占位）
- [x] **Context Manager / 自适应压缩**：`lucid/context_manager.py` `ContextManager.maybe_summarize`：在每次发往 LLM 前 `estimate_tokens(messages)` 估算请求体规模（文本按 chars/4，单图按 base64 长度/4 上限 2400），超过 `[context].target_ratio * model_context_tokens` 即触发摘要——把 prelude 之外、最近 `keep_recent_messages` 之前的旧消息剥掉图片后丢给 `Agent._summarize_segment`（复用当前 LLMClient 但 tools=[] 且只跑一次），把返回文本塞回成单条 `## Conversation summary so far` 的 user message。配置：`[context] auto_compress_enabled / target_ratio / model_context_tokens / keep_recent_messages / summary_max_tokens / summary_model`（summary_model 字段已预留，目前复用主 client）。F3 持久化天然吃下结果（saved tail 已经是压缩后的 messages）。
- [x] **`launch_app(name)` meta tool —— 用 Windows 原生接口启动 / 切换 App，绕开视觉**：详见 [Docs/screenshot.md §12](screenshot.md#12-视觉之外用-windows-原生接口启动--切换-app)。已落地，外加每个 App 的 tips / launcher spec 都搬到了 `lucid/apps/<slug>.py` 单文件（hot-plug 注册表，drop-a-file = add an app），并暴露 `update_launcher` meta tool 让 Agent 把"shortcut 改了 / exe 路径变了"等学习结果持久化到 `<user data>/launchers.json`。
- [~] **截图链路重构：函数化 + 启动差分捕获 L2 + 点击前后 L3 校验**（详见 [Docs/screenshot.md §3.3 / §3.3.1 / §3.4](screenshot.md#3-何时拍自动-vs-模型主动)）：
  - **目标**：把"何时拍 / 拍什么 / 怎么校验"从模型决策里拿走一部分，用确定性算法 + 时序 hook 补位，进一步压缩 round-trip。
  - **R1. screenshot 拆成独立函数（不再藏在 `computer` action 里）** — ❌ 未做。当前仍是 `computer({action:"screenshot", level})`，理由：与 Anthropic computer-use schema 对齐 + 共享 `tool.last_capture` 坐标系（详见 [screenshot.md §1](screenshot.md#1-截图是-computer-工具的一个-action不是独立-tool)），暂不拆。如真要拆，保留旧 action 作 alias 即可。
  - **R2. 起手只 L1 一次，之后 launch_app 用 diff 算 L2** — ✅ 落地了**结果等价**的更稳路径，但**不用 diff**。`launch_app` 直接用 Windows API 拿 hwnd 的客户区当作 region，pin 进 `tool.active_app_rect`；起手 L1 已可关（`[screenshot] feed_initial_l1_to_llm = false` 时只查虚拟桌面尺寸不抓像素，模型要看再自己 `screenshot`）。post-step 状态机自动从这个 rect 拍 L2 当 "map"，不消耗一次 LLM round-trip 让模型 "看 L1 找 App 在哪"。dHash 差分方案最初的目的是不用 hwnd → 实际 hwnd-rect 路径既准又省，**diff 版本不再做**。
  - **R3. 点击前后 L3 + L2 双图校验** — ✅ 已落地等价方案。点击动作后：(a) `tool.dispatch` 内部前后各拍一张 L3 算 `pixel_change_ratio`，低于 `click_no_change_threshold`（默认 0.5%）时把那张 post-L3 当 "may have missed" follow-up 附进 ToolResult（详见 [screenshot.md §3.3.1](screenshot.md#331-例外click_verify-判定-miss-会附一张-l3-提醒)）；(b) `safety.verify_click_with_l3 = true` 时 loop 在 post 阶段还会**额外**抓一张 L3 落点取证图（[§3.4](screenshot.md#34-落点-l3-取证click-verify)）。两阶段 preview 已默认关闭（[system_prompt.py](lucid/system_prompt.py) 根据 `safety.verify_click_target_before` 自动选 single-phase / two-phase 文案），保留作为兜底开关。
  - **R4. 坐标系强校验** — ⚠️ 部分。当前 `tool.dispatch` 在 active-app pin 状态下会拒绝把点击坐标反算到 `active_app_rect` 之外（防焦点跳走后乱点），但还没有按 `last_capture.sent_size` 做硬越界校验（如 L3 200x200 截图后给 y=366 仍会按 offset+scale 静默换算 —— 详见 [screenshot.md §11](screenshot.md#11-已知坑--待改进) 第 1 条）。这条仍待做。
  - **配置**：`[screenshot] feed_initial_l1_to_llm` / `click_verify_enabled` / `click_no_change_threshold` / `post_step_use_l3` / `[safety] verify_click_with_l3` / `verify_click_target_before` —— 全部已暴露。
  - **剩余 ToDo**：(R1) 是否真要拆出独立 `screenshot` tool（取舍见上）；(R4) 给 `coordinate` 加越界硬校验，越界直接 `ToolResult(error=...)` 让模型重新截图。

- [x] **语音输入 / Push-to-Talk 建任务**：注册一个全局快捷键（默认 `Ctrl+Alt+Space`，`[ui].voice_hotkey` 可改），按住录音、松开停止，把录到的音频送给 ASR（首选本地 `faster-whisper` small/medium，`[voice].engine = whisper-local | azure | openai`，`[voice].model_size`、`[voice].language="auto"`），转出文本后：
  - 如果当前没有 active thread 或用户配置 `[voice].always_new_thread = true` → 自动 `thread_new` + `start_task`（前端 chat 视图自动滚到新 thread）；
  - 否则把文本作为 user message 续到当前 thread。
  - 录音/识别时托盘图标变红或主窗口 footer 显示波形 + 倒计时上限（`[voice].max_seconds=30`）；识别失败给出错音 + 前端 toast。
  - 注意权限：Tauri 需声明 `microphone` capability；Windows 隐私设置里需开过麦克风。
- [x] **Skills 系统（可复用动作蓝图）**：在 templates 之上做一层"参数化、可由 Agent 自己挑用"的 skill：
  - 数据：`.lucid\skills\<slug>.json`（含 `name` / `description` / `params: [{name,type,desc}]` / `steps: [<instruction template>...]` / `requires: [tool names]` / `examples`）；可由用户在 `/skills` 页面 CRUD，不需要模型用新 meta tool `learn_skill(name, description, params, steps)` 写入，因为假设用户不是很懂，只需要load skill就行。
  - 调用：模型用 `run_skill(name, params)` 触发；sidecar 把 steps 渲染成 instruction（Jinja2 风格的 `{{var}}` 替换）后逐条 inject 进 messages，相当于"批量 user nudges"。也允许用户在前端 `/skills` 列表里点"运行"直接发起。
  - 注入：起手 prompt 里只列 skill 的 `name`（紧凑摘要），有需要再继续load `description + params`,避免 prompt 膨胀；模型决定调用某 skill 时再把 steps 完整展开。
  - 配置：`[skills] enabled`。
  - 与 templates 的区别：templates 是"一句固定 instruction"，skills 是"参数化的多步剧本 + 可被 Agent 主动调度"。
  - 支持online search然后offline load，但是要在system prompt里指明这是网上下载的，如果违反安全原则则去掉
- [ ] **多 Agent 设计：planner / checker / executor 三角**：把现在单 Agent 的 ReAct 拆三个角色，三者共享 thread messages 与截图，但各有 system prompt：
  - **Planner**：拿到 user instruction + 当前屏幕，输出"高层步骤计划 + 验收标准"（不调 `computer`），写入 thread。
  - **Executor**：现有 ReAct，按 plan 调 `computer` 推进。
  - **Checker**：每 N 步（`[multiagent].check_every_steps=5`，可关）或 executor 自报"我以为我做完了"时，单独跑一轮：拿最新截图 + plan + 已执行 tool_calls 列表，判定"是否完成 / 偏离 / 需回滚"，结果以 `[checker]` system message 注回 messages，executor 下一步要把它当指令对待。
  - 触发模式：`[multiagent].mode = off | check_only | plan_check_execute`；前两种省 token，最后一种最贵但最稳。
  - 实现：复用现有 `LLMClient`，每个角色有独立 prompt 文件（`lucid/prompts/{planner,checker}.md`），共享 `thread.messages.json` 持久化。
  - UI：聊天流里给三种角色不同色块前缀（planner=蓝、checker=橙、executor=绿）。
- [ ] **App 区域化坐标库（initialization-time region calibration）**：避免每次都靠视觉找按钮，针对常用 App（VS Code / 微信 / Outlook / Excel / Chrome / 资源管理器）在首次运行/设置页一键自检里跑一遍"区域校准"：
  - 把每个 App 主窗口划分成固定区域（如 VS Code: `activity_bar / sidebar / editor_tabs / editor_body / status_bar / panel`；微信: `nav_bar / chat_list / chat_header / chat_body / input_area / send_button`），每区域记录"相对窗口左上角的归一化矩形 (x%, y%, w%,
   h%) + 该区域内若干锚点元素的描述"。
  - 校准方式：① 让 Agent 用 `Win+E` / `ctrl+alt+w` 等启动 App，截 L1 + L2，用 LLM 一次性识别六七个区域并落盘；② 用户也可在前端区域校准面板手动框选/拖动调整。
  - 数据：`%LOCALAPPDATA%\dev.lucid\regions\<app_id>.json`（`window_signature` 用于 startup 时验证窗口尺寸/版本是否变化、变化则提示重校准）。
  - Runtime：模型可用新 meta tool `region(app, region_name)` 拿到"屏幕坐标 + 描述"，省掉一次截图+识别+点击的 3 步。
  - 配置：`[regions] enabled / auto_recalibrate_on_resolution_change`。
  - **Teams / 微信 子任务（2026-05-15 验证：Teams 上发消息一条任务跑了 39 步，主要卡在反复点击「键入消息」输入框命中不准 + 每步都先 narrate 被 nudge 浪费一轮，详见 `thread-20260515-181215-8e6fec`）**：
    - **Teams** — 有完整 UIA 树（`Microsoft.Teams.WebView2` 暴露 `Edit` / `Document` 控件），用 `IUIAutomation.FindFirst(TreeScope_Subtree)` 按 `LocalizedControlType="编辑"` + `Name in {"键入消息","Type a message"}` 直接拿 `BoundingRectangle`，命中后写入 `regions/teams.json` 的 `input_box`。其余区域（左导航栏 / 对话列表 / 标题栏）也都有 `AutomationId`，一次性枚举即可。
    - **微信** — Win32 老控件 + Duilib 自绘，UIA 几乎拿不到子结构（最多见到一个空的 `Window`）。备选三选一：(a) **OCR 锚点**：`region(app="wechat", name="input_box")` 触发时抓 L2 → 用 PaddleOCR/RapidOCR 找"搜索"/"发送(S)"等已知文案 → 反推输入框矩形；(b) **模板匹配**：把"发送"按钮和输入框边框作为模板图（`<user data>/regions/wechat/templates/*.png`），OpenCV `matchTemplate` 灰度匹配，鲁棒性比 OCR 高且无依赖；(c) **像素特征**：微信输入框上沿是固定灰线 (#E7E7E7)，沿 y 轴扫一行像素差找到边界。建议优先 (b)，配合首次运行让用户在校准面板里框一次输入框作为模板源。
    - **Teams 专用强制 tip（独立 todo）**：写入 `apps/teams.py` seed `[seed · compose-keyboard-first]`：发消息任务先 `Ctrl+Shift+X` 聚焦 compose box（已是 app tip 但未升级为强制路径），失败再退化到 `region("teams","input_box")` 点击；禁止直接用截图坐标点底部边缘。
- [ ] **Query-augmented user message**：在每次 LLM call 前，根据当前 user instruction 与最近若干 assistant 文本做轻量检索（BM25 + 可选 sentence-transformers embedding，`[rag] backend = bm25 | embed`），从 `memory.md` / `tools.md` / 历史 thread 摘要里召回 Top-K 相关条目，**只把相关的**那几条以 `## Relevant memory / tools (auto-retrieved)` 块拼进当前 user message——不再在 system prompt 里全量倾注 memory.md / tools.md（现在的做法）。
  - 触发：每次 `_run` 起手 + 每隔 `[rag].refresh_every_steps` 步重新检索（捕获任务中途话题漂移）。
  - 索引：sidecar 启动时把 memory/tools 切成单条 → 倒排索引；条目变更时增量更新（监听 `/memory` `/tools` 页面写盘事件）。
  - 配置：`[rag] enabled / top_k=5 / backend / refresh_every_steps=10 / max_chars_per_entry=300`。
  - 收益：(1) 大幅减少每步发给 LLM 的 prompt 体积；(2) 给模型的 memory/tools 信噪比变高（不会被无关条目干扰）；(3) 为后面 Phase 3 的"用户多人 / 多角色记忆隔离"留接口。
- [~] **打盹学习**：5分钟内没有任务的时候，启用打盹功能，从执行过任务的 `events.jsonl` 等文件提取需要的信息，比如 icon 信息、成功或失败的点（以防执行任务的时候没有记录下来）等，由大模型来学习，任务等级最低。打盹过的文档记录到 `doze_processed.json`，避免重复学习。
  - **v0（已落地）**：[lucid/doze.py](lucid/doze.py) `DozeWorker` 后台 tick 线程，由 sidecar 在 `serve()` 启动时拉起；空闲条件 = 没有运行中 worker + 队列为空 + 距上次 user-driven RPC > `idle_threshold_sec`（默认 300s）。一次 pass 选一条**未处理**的最近 thread，把 `events.jsonl` 压缩成纯文本时间线 + 现有 tips/memory 摘要喂给 LLM，工具白名单仅 `learn_tip` / `remember` / `load_app_tips`（**不动鼠键，不发图**）。协作式取消：任意 user-driven RPC（`start_task` / `thread_new` / `cancel`...）都会 `bump_activity()` 并打断当前 pass。新 RPC：`doze_status` / `doze_run_now` / `doze_clear_processed`；新事件：`doze_idle_start` / `doze_pass_done`。配置在 `[doze]`（默认 `enabled = false`，需 `/settings` 显式开启）。详见 [Docs/doze.md](doze.md)。
  - **v1 / icon 通道（已落地）**：[lucid/icon_proposals.py](lucid/icon_proposals.py) 新模块（`<user data>/icon_proposals/{index.json, <id>.png}`）；[doze.py](lucid/doze.py) 系统提示新增 `propose_icon(image_filename, x, y, w, h, label, description?)` 工具白名单条目，并把 `step_image` 事件的文件名 / 宽高列入 user prompt 供模型挑选；`_dispatch_propose_icon` 负责安全校验文件名、用 PIL 裁剪图块并入队。Sidecar 增加 5 个 RPC：`doze_proposals_list` / `doze_proposal_read_png` / `doze_proposal_accept`(可改名/改描述，→ `icon_memory.add_icon`) / `doze_proposal_reject` / `doze_proposals_clear`，全部进入 `_NON_ACTIVITY_METHODS` 白名单不打断打盹。前端新增 [/doze 页](app/src/routes/doze/+page.svelte)（与 /memory /schedules 并排，header 加 `nav_doze` i18n），包含状态面板 / Run Now / Clear Processed / 提案卡片预览 + 接受/拒绝/全部清空。
  - **v1（计划）**：心跳——与打盹共享 `lucid/reflector.py`，差别仅在触发器（运行中每 N 步 vs 任务完成后 5 分钟）与上下文窗口。
- [x] **初始化**: 有一些电脑上的配置，比如查看任务栏之类的，会影响到taskbar notify的功能，最好用一个任务来代替用户完成配置。任务等级可以弄成最低
- [x] **scheduler**: 定时任务可以加一个按钮，启动测试
- [x] **theme**: light/dark theme（2026-05-09）：[lib/theme.ts](app/src/lib/theme.ts) 持久化 `localStorage["lucid.theme"]`，默认跟随 `prefers-color-scheme`；`+layout.svelte` 调 `setupTheme()` 在子页面渲染前同步落 `<html data-theme>`，并集中放 `:global(html[data-theme="dark"] ...)` 重涂常见表面（body / footer / .bubble.assistant / .row / .chip / textarea/input/select / .tool / final-* / code / .hint / scope-bar / ConfirmModal），各页面 `<style>` 不必逐个改。主页 header 加 🌙/☀️ 按钮（i18n key `header.theme_toggle` 三语齐）。
- [x] **蟹钳鼠标**: Lucid移动鼠标的时候鼠标变成一个蟹钳，提醒用户这是由Lucid操纵的（绿幕生图 → [packaging/chroma_key.py](packaging/chroma_key.py) 抠透明 → [packaging/make_crab_cursor.py](packaging/make_crab_cursor.py) 用 PIL+ctypes-free 自写 ICONDIR 输出 32/48/64/96/128 多尺寸 `.cur`，hotspot 落在咬合点 (40%, 30%)，open / closed 两套共享同一 hotspot 不会跳。运行时 [lucid/cursor_indicator.py](lucid/cursor_indicator.py) 用 `LoadCursorFromFileW` + `CopyIcon` + `SetSystemCursor` 在 `Agent.run()` 进入时把 14 种系统光标 (`OCR_NORMAL/HAND/IBEAM/WAIT/SIZE*/NO/APPSTARTING/HELP`) 全替换成 open 蟹钳；点击/拖拽时 `tools.dispatch` 调 `pulse_click()` 切到 closed 蟹钳，~120ms 最小可见时长保证短点击也能看到，再切回 open；run 结束 `SystemParametersInfoW(SPI_SETCURSORS)` 一行还原。atexit + SIGINT/SIGTERM 三重保险防止 sidecar 崩了用户留着蟹钳光标到注销。受 `[input].crab_cursor` 控制，默认开。资源 `lucid/assets/crab_claw{,_closed}.{png,cur}` 通过 `lucid.spec` 的 `datas` 打进 PyInstaller bundle。）
- [x] **设置里加联系方式**: https://github.com/DaoZhang0123/ zhangdao@buaa.edu.cn https://x.com/zhangdao439566（设置页新增「关于 / 联系作者」tab，含 GitHub Star 按钮 + Email + X 链接，三语 i18n 已补）
- [x] **auto reply**: auto reply设定prompt
- [x] **安装位置在 ~/.lucid**（用户数据目录从 `%LOCALAPPDATA%\dev.lucid\` 迁到 `C:\Users\<name>\.lucid\`；Python 侧统一走 `Path.home() / ".lucid"`，Tauri 侧 `lucid_home()` 走 `USERPROFILE/.lucid`；config / inbox / logs / templates / schedules / memory / tools / copilot.json / queue.json / regions / launchers 等全部迁过去）

---

## Phase 3 — 平台化（远期）

- [ ] 任务市场 / 模板分享
- [ ] 多 Agent 协同（一个看屏一个写代码）
- [ ] 企业版：审计、SSO、策略中心

---

## 横向 / 工程债

- [x] **Tips seed pristine-refresh + 精确坐标硬约束（2026-05-09）**：[tooltips.py](lucid/tooltips.py) `_ensure_global_seeded` 仿照 `_ensure_app_seeded`，当 `tools.md` 里所有条目仍是 `[seed ...]`（用户没自己加过）就用最新 `_SEED_BODY` 覆盖，已装机也能拿到新 seed 而不必手删文件。新增全局条目 `[seed · click-precise-coords]`：禁止"around y=142"这类凭印象坐标，必须从最新截图读出元素中心像素，否则改走键盘路径（Ctrl+F / Tab / 地址栏）；另在 [apps/wechat.py](lucid/apps/wechat.py) 加 `[seed · send-file-via-shell-copy]`：已知绝对路径时直接 `Set-Clipboard -Path '<abs>'` → 焦点微信 → Ctrl+V → Enter，零图标点击 / 零对话框导航，配合 multimodal 附件流自然链路。
- [x] **Sidebar 「Conversations」标题居中**（2026-05-09）：`.side-heading` 加 `text-align: center`。
- [x] **后期步骤变慢的诊断 + image budget 收紧（2026-05-06）**：长 thread（如 ⏰ 天气，43 步、context.log 2.1 MB）从 step 14 起每轮稳定带 6-8 张 `image_ref`，且全程 `omitted=0` —— 即 `compress_old_images` 在 `keep_recent_l2=3` 下根本没 demote 任何一张图。两处修复：(a) `config.toml` 默认 `keep_recent_l2 = 3 → 1`（基线每步 ≈ L1=1 + L2=1 + L3=2 = 4 张）；(b) 重写 `loop.py` `SYSTEM_PROMPT_HEAD` rule 8，从"建议 transcribe"升级为强制：明确告知"old screenshots WILL be replaced by `[旧截图已省略...]` 占位符"，要求模型在**同一轮**就把图中与任务相关的信息（按钮坐标、列表项、错误文案、OCR 小字、聊天/搜索结果、数值字段……）转写到 assistant 文本里作为持久工作记忆，并提示 "summarise / forward / report what you see" 类任务务必 early-extract。详见 [design.md §4.5.3](design.md#453-分级截屏策略带宽成本与精度的平衡) 附加规则与 [screenshot.md §10](screenshot.md#10-相关配置一览configtoml)。
- [x] **Built-in zero-GUI utilities (`read_file` / `write_file` / `run_shell`)**：避免 「launch_app('cmd') → type 'type X' → screenshot → OCR」 这种 4 步 + ~100KB 图象 token 去读个 50 字节文件的倒贴路径。三个 meta tool 在 `meta_tools.py`、受 `[fileio]` / `[shell]` 控制；路径支持 `%ENV%` / `$env:NAME` / `~`；shell 调用隐藏控制台窗口 (`CREATE_NO_WINDOW`)，默认 timeout 20s（硬顶 120s，超过请开真终端），输出被 `_truncate_text` 限到 16k 字符。系统 prompt 里只留了 4 行指路牌，具体决策调用时看 tool description。
- [x] **光标周边 L3 智能紧贴（UIA-driven smart L3）**：H3 尺寸不再用固定 200x200，而是调 IUIAutomation `ElementFromPoint` 拿到鼠标处 UI 元素的 BoundingRectangle，外拓 `l3_smart_padding_px=16`、限下限 160x80；占屏 >40% 返回 None 回落固定方。处理了计算器这种 280x60 小显屏被 L3 正方形覃茂丢了边缘的问题。`lucid/uia.py` 是纯 ctypes COM 包装（0 三方依赖），首调 ~60ms、后续 <5ms。详见 [Docs/screenshot.md §2.1](screenshot.md#21)。
- [~] **国际化（i18n）：仓库 + App 主语言切英文，附中文 / 法语 / 阿拉伯语 / 俄语 翻译**：
  - **App UI 层**：✅ 已落地。`app/src/lib/i18n/` 用 `svelte-i18n` 注册 `en` / `zh-CN` / `fr-FR` 三语，`SUPPORTED_LOCALES` + `LOCALE_LABELS` 从 `index.ts` 集中导出；语言选择器在 `/settings`，写入 `localStorage["lucid.locale"]` 并在 `setupI18n()` 启动时复用，避免冷启动闪烁。`+page.svelte` / `chatStore` / `/settings` / `/templates` / `/schedules` / `/memory` / `/tools` 全部硬编码已抽到 `messages/{en,zh-CN,fr-FR}.json`（含 28 个 tz 城市标签 `tz_utc_m12..p14`，`TZS` 数组用 `$derived` 重算以便切换语言后即时刷新）。
  - **Sidecar 层**：⚠️ 部分。`SYSTEM_PROMPT` / `meta_tools.py` schema 描述 / `tooltips.py` seed / `memory.py` header / `icon_memory.py` atlas 标题 / `loop.py` 的 atlas 注入与 nudge 文案已全部翻成英文（默认面向国际用户）；尚未实现按 `cfg.ui.locale` 在 sidecar 内切换语言（也就是说目前是“英文 only 给 LLM”，不会按用户 UI locale 给中/法版 prompt）。
  - **仓库层**：❌ 未做。`README.md` / `design.md` / `todo.md` / 代码注释主版本仍是中文，未拆 `docs/zh-CN/` `docs/fr-FR/` 目录，README 顶部也没加语言切换链接。
  - **后续**：仓库层文档英化 + 多语切换，sidecar 文案按 `[ui].locale` 切换（需要 `tooltips.py` 准备 zh-CN/fr-FR 三套 seed），可选追加 `ar-SA` / `ru-RU`。
  - **验收**：英文环境装一遍 NSIS 默认 UI 全英；切到中文 / 法语后整窗刷新无残留中文 / 英文。
- [x] **README star 数**：在 README 顶部加一个 "Stargazers" 小节，挂自己仓库的 shields.io star badge（`https://img.shields.io/github/stars/<owner>/<repo>?style=social`）
- [ ] **首次运行引导：禁用锁屏 + 睡眠 + 屏保（防 BitBlt 拒绝访问）**：锁屏后 Windows 切到 Winlogon 安全桌面，BitBlt 直接返回 “拒绝访    问”（前端会看到 `ScreenShotError: Windows graphics function failed: BitBlt: 拒绝访问`），睡眠 / 休眠则会冻住 sidecar 的定时任务和长跑任务。在欢迎页 / 设置自检里加一组检测 + 一键修复（含“恢复默认”按钮记住原值再回写）：
  - 屏保关闭：`reg add "HKCU\Control Panel\Desktop" /v ScreenSaveActive /t REG_SZ /d 0 /f`
  - 屏幕熄灭 / 睡眠永不（AC 与电池都设 0）：`powercfg /change monitor-timeout-ac 0; powercfg /change monitor-timeout-dc 0; powercfg /change standby-timeout-ac 0; powercfg /change standby-timeout-dc 0`
  - 控制台 / 锁屏超时：`powercfg /setacvalueindex SCHEME_CURRENT SUB_VIDEO VIDEOCONLOCK 0; powercfg /setactive SCHEME_CURRENT`
  - 替代锁屏建议：用 `nircmd monitor off` 或调 `SendMessage(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, 2)` 仅熄屏不切安全桌面；屏幕黑了但 BitBlt 仍可工作。
  - 兜底：跑任务时若仍检测到 BitBlt Access Denied，前端弹 “屏幕已锁，无法截图，请解锁或改用熄屏”，并暂停当前 run 而不是反复把锁屏期间的截图错误喂给模型。
- [ ] 单元测试：`compress_old_images` / `_split_segments` / `_norm_key` / `RunLogger` 轮转 / `_parse_level`
- [ ] CI：Windows runner 跑 lint + 单测（不动鼠键的部分）
- [ ] L3 鼠标近屏幕边缘时区域裁剪到虚拟屏幕边界（防 mss 越界问题）
- [ ] OSWorld / WindowsAgentArena 子集跑分（design.md §3.2）
- [ ] 增加自动化测试
- [ ] 打榜
