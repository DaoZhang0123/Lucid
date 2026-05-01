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
- [ ] Speculative multi-action（一次给多步预案，本地按需执行）
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

---

## Phase 3 — 平台化（远期）

- [ ] 任务市场 / 模板分享
- [ ] 多 Agent 协同（一个看屏一个写代码）
- [ ] 企业版：审计、SSO、策略中心

---

## 横向 / 工程债

- [ ] **首次运行引导：把 Windows 任务栏按钮"从不合并"**：在欢迎页 / 设置自检里加一项检测和一键引导——打开「设置 → 个性化 → 任务栏 → 任务栏行为 → 合并任务栏按钮并隐藏标签」改为「从不」。这样任务栏每个窗口都带文字标签，模型靠 OCR 就能知道哪些 App / 窗口已开，不必再依赖图标识别。可在引导里直接 `start ms-settings:taskbar` 跳到对应页面，并给出截图示意。
- [ ] **Context Manager / 自适应压缩**：当本次发给 LLM 的 messages 估算 token 数（或字节数）逼近模型 context window 阈值（如 70%）时，自动触发"摘要压缩"——把最早的若干 user/assistant/tool 段（除 prelude 外）调用一次廉价模型生成"## 上文摘要"段塞回去，丢掉原始消息（只保留最近 N 步原文 + 最近若干截图）。并入 F3 持久化（saved tail 也走压缩）。配置：`[context] auto_compress_enabled / target_ratio / summary_model / keep_recent_steps`。
- [ ] 单元测试：`_prune_old_images` / `_split_segments` / `_norm_key` / `RunLogger` 轮转 / `_parse_level`
- [ ] CI：Windows runner 跑 lint + 单测（不动鼠键的部分）
- [ ] L3 鼠标近屏幕边缘时区域裁剪到虚拟屏幕边界（防 mss 越界问题）
- [ ] OSWorld / WindowsAgentArena 子集跑分（design.md §3.2）
