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
- [x] 流式事件：`run_start / step_start / assistant_text / tool_call / tool_result / step_image / final / error` 通过 stdout 推到前端
- [x] 任务取消：`cancel_event` 在两步之间生效；CancelledError 收尾
- [x] Rust 侧：以 sidecar 拉起 `ctrlapp.exe`（PyInstaller 打包），管理生命周期
- [x] 进程崩溃自动重启 + 错误展示（`supervise()` 1s 重连、`ctrlapp://sidecar` 事件流）

### 1.3 安全 / 体验
- [x] 三档自动度（`full / confirm_critical / confirm_each`）UI 切换并实时下发
- [x] 全局急停热键（`Ctrl+Alt+Esc`，design.md §4.7）
- [ ] 鼠标移到屏幕左上角的 PyAutoGUI fail-safe 在 UI 上提示出来
- [x] 任务进行中显示当前动作类型 + 即时取消按钮
- [ ] 隐私白名单：用户可标记某进程/窗口标题为"截图前需告警/最小化"

### 1.4 截图与历史回放
- [x] 把每次运行的 `logs/<run>/step-*.png` 序列做成时间线视图（点缩略图看大图 + 当步 assistant 文本 + tool_call）
- [ ] "重放此任务"按钮（仅文本重放，不重新驱动鼠键）
- [ ] 历史搜索（任务 instruction 全文）

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
- [ ] 用户可保存的"任务模板"（如"周报流程"）
- [ ] 隐私沙箱：敏感区域自动模糊（密码框 / 网银 / 私聊）
- [ ] 模型可插拔（Claude / GPT-4o / Qwen-VL）通过同一 OpenAI 兼容代理切换
- [ ] **长期记忆 `memory.md` + 心跳 (heartbeat)**：
  - 仓库根 / 用户目录维护一份 `memory.md`，每次任务启动注入 system prompt 末尾，让模型知道用户偏好、常用路径、习惯动作
  - 主动写入：用户在聊天里说"记住我喜欢…" → Agent 通过 `memory.write` 工具追加一条带时间戳的记录
  - 被动写入：心跳定时（如每 N 步 / 每 X 分钟）触发一次"反思"调用，让模型从最近会话里抽取值得长期保留的偏好/约束并去重写入
  - 心跳同时承担"无任务时的小巡检"：可选地周期截屏 + 触发"发现异常 / 通知 / 长时间无响应"等启发式事件
  - 配置：`[memory] enabled / path / max_entries / heartbeat_interval_sec`，独立开关
- [ ] **桌面通知监听**：
  - 监听 Windows ToastNotification / Action Center（`Windows.UI.Notifications.Management.UserNotificationListener`，需用户授权）拿到微信、Teams、Outlook、Slack 等推送
  - Agent 主动汇总"过去 X 分钟内你收到了哪些消息、是否需要回复"，避免漏掉
  - 可配置过滤：白名单应用、关键字（如自己名字 / @ mention）才升级到打扰级
  - 配置：`[notify] enabled / poll_interval_sec / app_whitelist / urgent_keywords`

---

## Phase 3 — 平台化（远期）

- [ ] 任务市场 / 模板分享
- [ ] 多 Agent 协同（一个看屏一个写代码）
- [ ] 企业版：审计、SSO、策略中心

---

## 横向 / 工程债

- [ ] 单元测试：`_prune_old_images` / `_split_segments` / `_norm_key` / `RunLogger` 轮转 / `_parse_level`
- [ ] CI：Windows runner 跑 lint + 单测（不动鼠键的部分）
- [ ] L3 鼠标近屏幕边缘时区域裁剪到虚拟屏幕边界（防 mss 越界问题）
- [ ] OSWorld / WindowsAgentArena 子集跑分（design.md §3.2）
