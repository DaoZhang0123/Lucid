# ctrlapp · TODO

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
- [x] CLI：`python -m ctrlapp "..."` + `--smoke-test`
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
- [x] Python 侧：自写 stdio JSON-RPC 暴露 `ping / start_task / cancel / get_status / set_autonomy / shutdown`（NDJSON 帧、stdout 协议 / stderr 日志）
- [x] 流式事件：`run_start / step_start / assistant_text / tool_call / tool_result / step_image / final / error / thread_changed` 通过 stdout 推到前端
- [x] Thread 级 RPC：`thread_new / thread_list / thread_read / thread_set_active / thread_delete / thread_read_image`（按对话而非按任务汇总）
- [x] 任务取消：`cancel_event` 在两步之间生效；CancelledError 收尾
- [x] Rust 侧：以 sidecar 拉起 `ctrlapp.exe`（PyInstaller 打包），管理生命周期
- [x] 进程崩溃自动重启 + 错误展示（`supervise()` 1s 重连、`ctrlapp://sidecar` 事件流）

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

### 1.5 适配与体验细节
- [x] 多屏布局变化时重新探测 `[screenshot].l1_max_long_edge`（`ctrlapp.selfcheck monitors` + 设置页一键自检）
- [x] HiDPI 标度差异下的坐标自检（`ctrlapp.selfcheck click` 用 dHash 比对点击前后变化）
- [x] 非英文系统下“win+r”等组合键 alias 自检（`ctrlapp.selfcheck winr`）

### 1.6 5 个示例场景跑通
- [x] 记事本：写文本并保存到指定路径（Phase 0 已端到端通；`ctrlapp.examples run notepad`）
- [~] 浏览器：打开 URL → 截图描述页面（指令已编入 `ctrlapp.examples`，待真跑验证）
- [~] Excel：在 A1 输入“周报”并保存到桌面（指令已编入，待真跑验证）
- [~] 微信：找到指定联系人 → 发送一句消息（HITL 强确认，已编入并强制 `confirm_each`）
- [~] 文件管理器：在指定文件夹新建子文件夹并重命名（指令已编入，待真跑验证）

### 1.7 打包与发布
- [x] PyInstaller 单目录打包 `ctrlapp.exe`（`packaging/ctrlapp.spec`，含 mss / pyautogui / pyperclip / openai SDK）
- [x] Tauri `cargo tauri build` → `.msi`（已配 bundle.targets=msi+nsis，bundle.resources=dist/ctrlapp）
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
  - `python/ctrlapp/memory.py`：`memory_for_prompt` 在每次起手注入 system prompt 末尾
  - 主动写入：模型用 `remember(text)` 工具调用追加；`/memory` 页面可手编/清空
  - 路径：`%LOCALAPPDATA%\dev.ctrlapp\memory.md`；`[memory]` 配置 enabled / max_entries / max_chars
- [x] **操作技巧 `tools.md`（可演化的提示库）**：
  - `python/ctrlapp/tooltips.py`：`tools_for_prompt` 在每次起手注入 system prompt 末尾
  - 首次启动 seed：自动写入"键盘优先 / 不覆盖用户内容 / 先 alt+tab 看是否已开 / 保存对话框直接 type 绝对路径"等通用技法（原 SYSTEM_PROMPT 中的"常用技巧"段）
  - 主动写入：模型用 `learn_tip(text, kind)` 把任务中总结的成功路径或失败教训追加进文件；`/tools` 页面可手编 / 一键追加 / 重置 seed
  - 路径：`%LOCALAPPDATA%\dev.ctrlapp\tools.md`；`[tools]` 配置 enabled / max_entries / max_chars
- [ ] **桌面通知监听**：
  - 监听 Windows ToastNotification / Action Center（`Windows.UI.Notifications.Management.UserNotificationListener`，需用户授权）拿到微信、Teams、Outlook、Slack 等推送
  - Agent 主动汇总"过去 X 分钟内你收到了哪些消息、是否需要回复"，避免漏掉
  - 可配置过滤：白名单应用、关键字（如自己名字 / @ mention）才升级到打扰级
  - 配置：`[notify] enabled / poll_interval_sec / app_whitelist / urgent_keywords`
- [x] **Cron 定时任务**：
  - `python/ctrlapp/scheduler.py`：minute-tick `Scheduler` 后台线程随 sidecar 启动；支持 interval / daily / weekly 三种触发
  - 持久化：`schedules.json`（`%LOCALAPPDATA%\dev.ctrlapp\`），含 `next_ms` / `last_run_ms` / `enabled`
  - `/schedules` 页面 CRUD + 暂停/启用；触发时自动 `thread_new + start_task`，运行中冲突自动跳过（`schedule_skipped` 事件）
  - 仍未做：cron 表达式语法（5 字段）、heartbeat 反思、桌面通知联动
- [x] **同 thread 多轮 run 的 context 持久化（F3）**：每次 `_run` 结束把 messages 去掉 prelude 后落盘到 `thread/messages.json`；下次同 thread 起新 run 时载入 tail 拼到新 instruction 之前。prelude（system + atlas）每次重建以反映 memory.md / tools.md / icons 的更新。
- [x] Image 压缩放到 context manager 里（`python/ctrlapp/context_manager.py` `compress_old_images`：未命中“最近 K 张/级 + 全局最近 N 张”保留集的旧截图，先尝试解码 → 等比缩放到 `[context].image_recompress_max_long_edge` → JPEG @ `image_recompress_quality` 重编码；若新字节反而更大或重压关闭则回落到原 `[old screenshot omitted]` 文本占位）
- [x] **Context Manager / 自适应压缩**：`python/ctrlapp/context_manager.py` `ContextManager.maybe_summarize`：在每次发往 LLM 前 `estimate_tokens(messages)` 估算请求体规模（文本按 chars/4，单图按 base64 长度/4 上限 2400），超过 `[context].target_ratio * model_context_tokens` 即触发摘要——把 prelude 之外、最近 `keep_recent_messages` 之前的旧消息剥掉图片后丢给 `Agent._summarize_segment`（复用当前 LLMClient 但 tools=[] 且只跑一次），把返回文本塞回成单条 `## Conversation summary so far` 的 user message。配置：`[context] auto_compress_enabled / target_ratio / model_context_tokens / keep_recent_messages / summary_max_tokens / summary_model`（summary_model 字段已预留，目前复用主 client）。F3 持久化天然吃下结果（saved tail 已经是压缩后的 messages）。
- [x] **`launch_app(name)` meta tool —— 用 Windows 原生接口启动 / 切换 App，绕开视觉**：详见 [Docs/screenshot.md §12](screenshot.md#12-视觉之外用-windows-原生接口启动--切换-app)。已落地，外加每个 App 的 tips / launcher spec 都搬到了 `python/ctrlapp/apps/<slug>.py` 单文件（hot-plug 注册表，drop-a-file = add an app），并暴露 `update_launcher` meta tool 让 Agent 把"shortcut 改了 / exe 路径变了"等学习结果持久化到 `<user data>/launchers.json`。
- [ ] **截图链路重构：函数化 + 启动差分捕获 L2 + 点击前后 L3 校验**（详见 [Docs/screenshot.md §13](screenshot.md#13-截图链路重构函数化--启动差分捕获-l2--点击前后-l3-校验)）：
  - **目标**：把"何时拍 / 拍什么 / 怎么校验"从模型决策里拿走一部分，用确定性算法 + 时序 hook 补位，进一步压缩 round-trip。
  - **R1. screenshot 拆成独立函数（不再藏在 `computer` action 里）**：新建独立 `screenshot` tool（参数 `level` / `region` / `max_long_edge` / `quality`），与 `computer`（鼠键）解耦；坐标系仍统一在 `tool.last_capture` 里维护。同时保留 `computer.action=screenshot` 作为兼容别名，方便回滚。
  - **R2. 起手只 L1 一次，之后 launch_app 用 diff 算 L2**：首张 L1 后，`launch_app` 内部在调用前后各拍一张 L1，用 dHash / 像素差（`PIL.ImageChops.difference` + 连通域 bbox）算出"最大变化矩形"，断定为新 App 主窗口；把该矩形 + 截图直接以 `## launch_app result (visual)` 的 user message 主动推给模型，**不消耗一次 LLM round-trip 来"看 L1 找 App 在哪"**。返回给模型的 metadata：`{app, hwnd, region:[x,y,w,h], confidence}`。差分失败 / 多个候选区域时回退到 `_grab(window_rect_of_hwnd(hwnd))` —— 用 Windows API 拿 hwnd 的客户区作为 L2。
  - **R3. 点击前后 L3 + L2 双图校验（取代两阶段 preview）**：在 `_dispatch` 点击类动作时，**自动**前后各抓一张 L3（鼠标 ±100px），并在 post 阶段补一张 L2（活动窗口）；用 dHash 比对前后 L3，若相似度过高（>0.97）→ 直接报 `[click verify] no visual change near cursor` 让模型立刻重试 / 改坐标，**不必等模型自己看图发现**。两阶段 preview 改为兜底（仅 L1 直接点小图标时触发），由 §11 I 那条触发条件控制。
  - **R4. 坐标系强校验**：`computer` 里所有带 `coordinate` 的 action，对照 `tool.last_capture.sent_size` 做越界检查，越界直接 `ToolResult(error=...)` 让模型重新截图。
  - **配置**：`[screenshot] launch_diff_enabled / launch_diff_min_area_ratio=0.05 / click_verify_dhash_threshold=0.97 / first_l1_only=true`。
  - **与已落地的 `launch_app` 关系**：本条是"launch_app 之后视觉怎么接管"的下一步——launch_app 给出 hwnd / region，screenshot 模块把 region 翻译成 L2，模型完全跳过"找 App 在屏幕哪儿"。
- [ ] **语音输入 / Push-to-Talk 建任务**：注册一个全局快捷键（默认 `Ctrl+Alt+Space`，`[ui].voice_hotkey` 可改），按住录音、松开停止，把录到的音频送给 ASR（首选本地 `faster-whisper` small/medium，`[voice].engine = whisper-local | azure | openai`，`[voice].model_size`、`[voice].language="auto"`），转出文本后：
  - 如果当前没有 active thread 或用户配置 `[voice].always_new_thread = true` → 自动 `thread_new` + `start_task`（前端 chat 视图自动滚到新 thread）；
  - 否则把文本作为 user message 续到当前 thread。
  - 录音/识别时托盘图标变红或主窗口 footer 显示波形 + 倒计时上限（`[voice].max_seconds=30`）；识别失败给出错音 + 前端 toast。
  - 注意权限：Tauri 需声明 `microphone` capability；Windows 隐私设置里需开过麦克风。
- [ ] **Skills 系统（可复用动作蓝图）**：在 templates 之上做一层"参数化、可由 Agent 自己挑用"的 skill：
  - 数据：`%LOCALAPPDATA%\dev.ctrlapp\skills\<slug>.json`（含 `name` / `description` / `params: [{name,type,desc}]` / `steps: [<instruction template>...]` / `requires: [tool names]` / `examples`）；可由用户在 `/skills` 页面 CRUD，也可由模型用新 meta tool `learn_skill(name, description, params, steps)` 写入。
  - 调用：模型用 `run_skill(name, params)` 触发；sidecar 把 steps 渲染成 instruction（Jinja2 风格的 `{{var}}` 替换）后逐条 inject 进 messages，相当于"批量 user nudges"。也允许用户在前端 `/skills` 列表里点"运行"直接发起。
  - 注入：起手 prompt 里只列 skill 的 `name + description + params`（紧凑摘要），避免 prompt 膨胀；模型决定调用某 skill 时再把 steps 完整展开。
  - 配置：`[skills] enabled / max_skills / max_steps_per_skill`。
  - 与 templates 的区别：templates 是"一句固定 instruction"，skills 是"参数化的多步剧本 + 可被 Agent 主动调度"。
- [ ] **多 Agent 设计：planner / checker / executor 三角**：把现在单 Agent 的 ReAct 拆三个角色，三者共享 thread messages 与截图，但各有 system prompt：
  - **Planner**：拿到 user instruction + 当前屏幕，输出"高层步骤计划 + 验收标准"（不调 `computer`），写入 thread。
  - **Executor**：现有 ReAct，按 plan 调 `computer` 推进。
  - **Checker**：每 N 步（`[multiagent].check_every_steps=5`，可关）或 executor 自报"我以为我做完了"时，单独跑一轮：拿最新截图 + plan + 已执行 tool_calls 列表，判定"是否完成 / 偏离 / 需回滚"，结果以 `[checker]` system message 注回 messages，executor 下一步要把它当指令对待。
  - 触发模式：`[multiagent].mode = off | check_only | plan_check_execute`；前两种省 token，最后一种最贵但最稳。
  - 实现：复用现有 `LLMClient`，每个角色有独立 prompt 文件（`python/ctrlapp/prompts/{planner,checker}.md`），共享 `thread.messages.json` 持久化。
  - UI：聊天流里给三种角色不同色块前缀（planner=蓝、checker=橙、executor=绿）。
- [ ] **App 区域化坐标库（initialization-time region calibration）**：避免每次都靠视觉找按钮，针对常用 App（VS Code / 微信 / Outlook / Excel / Chrome / 资源管理器）在首次运行/设置页一键自检里跑一遍"区域校准"：
  - 把每个 App 主窗口划分成固定区域（如 VS Code: `activity_bar / sidebar / editor_tabs / editor_body / status_bar / panel`；微信: `nav_bar / chat_list / chat_header / chat_body / input_area / send_button`），每区域记录"相对窗口左上角的归一化矩形 (x%, y%, w%,
   h%) + 该区域内若干锚点元素的描述"。
  - 校准方式：① 让 Agent 用 `Win+E` / `ctrl+alt+w` 等启动 App，截 L1 + L2，用 LLM 一次性识别六七个区域并落盘；② 用户也可在前端区域校准面板手动框选/拖动调整。
  - 数据：`%LOCALAPPDATA%\dev.ctrlapp\regions\<app_id>.json`（`window_signature` 用于 startup 时验证窗口尺寸/版本是否变化、变化则提示重校准）。
  - Runtime：模型可用新 meta tool `region(app, region_name)` 拿到"屏幕坐标 + 描述"，省掉一次截图+识别+点击的 3 步。
  - 配置：`[regions] enabled / auto_recalibrate_on_resolution_change`。
- [ ] **Query-augmented user message**：在每次 LLM call 前，根据当前 user instruction 与最近若干 assistant 文本做轻量检索（BM25 + 可选 sentence-transformers embedding，`[rag] backend = bm25 | embed`），从 `memory.md` / `tools.md` / 历史 thread 摘要里召回 Top-K 相关条目，**只把相关的**那几条以 `## Relevant memory / tools (auto-retrieved)` 块拼进当前 user message——不再在 system prompt 里全量倾注 memory.md / tools.md（现在的做法）。
  - 触发：每次 `_run` 起手 + 每隔 `[rag].refresh_every_steps` 步重新检索（捕获任务中途话题漂移）。
  - 索引：sidecar 启动时把 memory/tools 切成单条 → 倒排索引；条目变更时增量更新（监听 `/memory` `/tools` 页面写盘事件）。
  - 配置：`[rag] enabled / top_k=5 / backend / refresh_every_steps=10 / max_chars_per_entry=300`。
  - 收益：(1) 大幅减少每步发给 LLM 的 prompt 体积；(2) 给模型的 memory/tools 信噪比变高（不会被无关条目干扰）；(3) 为后面 Phase 3 的"用户多人 / 多角色记忆隔离"留接口。
- [ ] **打盹**：5分钟内没有任务的时候，启用打盹功能，从执行过任务的context.md等文件提取需要的信息，比如icon信息，成功或者失败的点（以防执行任务的时候没有记录下来）等。

---

## Phase 3 — 平台化（远期）

- [ ] 任务市场 / 模板分享
- [ ] 多 Agent 协同（一个看屏一个写代码）
- [ ] 企业版：审计、SSO、策略中心

---

## 横向 / 工程债

- [~] **国际化（i18n）：仓库 + App 主语言切英文，附中文 / 法语 / 阿拉伯语 / 俄语 翻译**：
  - **App UI 层**：✅ 已落地。`app/src/lib/i18n/` 用 `svelte-i18n` 注册 `en` / `zh-CN` / `fr-FR` 三语，`SUPPORTED_LOCALES` + `LOCALE_LABELS` 从 `index.ts` 集中导出；语言选择器在 `/settings`，写入 `localStorage["ctrlapp.locale"]` 并在 `setupI18n()` 启动时复用，避免冷启动闪烁。`+page.svelte` / `chatStore` / `/settings` / `/templates` / `/schedules` / `/memory` / `/tools` 全部硬编码已抽到 `messages/{en,zh-CN,fr-FR}.json`（含 28 个 tz 城市标签 `tz_utc_m12..p14`，`TZS` 数组用 `$derived` 重算以便切换语言后即时刷新）。
  - **Sidecar 层**：⚠️ 部分。`SYSTEM_PROMPT` / `meta_tools.py` schema 描述 / `tooltips.py` seed / `memory.py` header / `icon_memory.py` atlas 标题 / `loop.py` 的 atlas 注入与 nudge 文案已全部翻成英文（默认面向国际用户）；尚未实现按 `cfg.ui.locale` 在 sidecar 内切换语言（也就是说目前是“英文 only 给 LLM”，不会按用户 UI locale 给中/法版 prompt）。
  - **仓库层**：❌ 未做。`README.md` / `design.md` / `todo.md` / 代码注释主版本仍是中文，未拆 `docs/zh-CN/` `docs/fr-FR/` 目录，README 顶部也没加语言切换链接。
  - **后续**：仓库层文档英化 + 多语切换，sidecar 文案按 `[ui].locale` 切换（需要 `tooltips.py` 准备 zh-CN/fr-FR 三套 seed），可选追加 `ar-SA` / `ru-RU`。
  - **验收**：英文环境装一遍 NSIS 默认 UI 全英；切到中文 / 法语后整窗刷新无残留中文 / 英文。
- [ ] **README star 数对标 OpenAdapt**：在 README 顶部加一个 "Stargazers" 小节，挂自己仓库的 shields.io star badge（`https://img.shields.io/github/stars/<owner>/<repo>?style=social`），并在脚注里记一组对标基线 —— 参考 [OpenAdaptAI/OpenAdapt](https://github.com/OpenAdaptAI/OpenAdapt)（2026-05-01 抓取：约 1566 stars、233 forks，定位 "Generative RPA / computer-use agent"，与本项目同赛道）。每月人工或脚本（`gh api repos/OpenAdaptAI/OpenAdapt --jq .stargazers_count`）刷一次写进 README 末尾的"对标"表，方便看自己的增长曲线相对位置。
- [ ] **首次运行引导：把 Windows 任务栏按钮"从不合并"**：在欢迎页 / 设置自检里加一项检测和一键引导——打开「设置 → 个性化 → 任务栏 → 任务栏行为 → 合并任务栏按钮并隐藏标签」改为「从不」。这样任务栏每个窗口都带文字标签，模型靠 OCR 就能知道哪些 App / 窗口已开，不必再依赖图标识别。可在引导里直接 `start ms-settings:taskbar` 跳到对应页面，并给出截图示意。
- [ ] **首次运行引导：禁用锁屏 + 睡眠 + 屏保（防 BitBlt 拒绝访问）**：锁屏后 Windows 切到 Winlogon 安全桌面，BitBlt 直接返回 “拒绝访    问”（前端会看到 `ScreenShotError: Windows graphics function failed: BitBlt: 拒绝访问`），睡眠 / 休眠则会冻住 sidecar 的定时任务和长跑任务。在欢迎页 / 设置自检里加一组检测 + 一键修复（含“恢复默认”按钮记住原值再回写）：
  - 屏保关闭：`reg add "HKCU\Control Panel\Desktop" /v ScreenSaveActive /t REG_SZ /d 0 /f`
  - 屏幕熄灭 / 睡眠永不（AC 与电池都设 0）：`powercfg /change monitor-timeout-ac 0; powercfg /change monitor-timeout-dc 0; powercfg /change standby-timeout-ac 0; powercfg /change standby-timeout-dc 0`
  - 控制台 / 锁屏超时：`powercfg /setacvalueindex SCHEME_CURRENT SUB_VIDEO VIDEOCONLOCK 0; powercfg /setactive SCHEME_CURRENT`
  - 替代锁屏建议：用 `nircmd monitor off` 或调 `SendMessage(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, 2)` 仅熄屏不切安全桌面；屏幕黑了但 BitBlt 仍可工作。
  - 兜底：跑任务时若仍检测到 BitBlt Access Denied，前端弹 “屏幕已锁，无法截图，请解锁或改用熄屏”，并暂停当前 run 而不是反复把锁屏期间的截图错误喂给模型。
- [ ] 单元测试：`_prune_old_images` / `_split_segments` / `_norm_key` / `RunLogger` 轮转 / `_parse_level`
- [ ] CI：Windows runner 跑 lint + 单测（不动鼠键的部分）
- [ ] L3 鼠标近屏幕边缘时区域裁剪到虚拟屏幕边界（防 mss 越界问题）
- [ ] OSWorld / WindowsAgentArena 子集跑分（design.md §3.2）
- [ ] 增加自动化测试
- [ ] 打榜
