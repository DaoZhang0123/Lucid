# ctrlAppWithoutMCP — 设计文档

> 一款运行在 Windows 上的"通用桌面助理"。它**不依赖任何 MCP / 应用插件 / 应用 API**，仅靠 **Claude Opus 4.6 的多模态（视觉 + 推理）能力**，以"模拟人类操作"的方式：截图 → 理解 → 决策 → 操作鼠标键盘，完成用户用自然语言下达的任意桌面任务。

---

## 1. 产品定位

### 1.1 一句话定义
**"会看屏幕、会动鼠标键盘的 Claude"**——把整个 Windows 桌面变成 LLM 的工作台，用对话替代点点点。

### 1.2 核心理念（三个"不"）
| 原则 | 含义 |
| --- | --- |
| **不依赖 MCP** | 不需要每个 App 提供 MCP Server / 插件 / SDK，避免生态绑定。 |
| **不依赖应用 API** | 不调用 Office COM、浏览器扩展、Win32 UIA 树等"作弊"接口；纯视觉理解 + 人类级输入。 |
| **不区分应用** | 任何能在屏幕上显示、能用鼠标键盘操作的 App（含国产软件、老旧 ERP、远程桌面、游戏内 UI）都能驱动。 |

### 1.3 目标用户
- **办公白领**：处理 Excel/WPS、钉钉/飞书、网银、报销系统等"非标"应用。
- **运维 / 测试**：本地小型 RPA、UI 回归。
- **轻度残障 / 老年用户**：用自然语言代替复杂操作。
- **开发者**：作为 Agent 框架做二次开发。

### 1.4 典型场景
1. "把桌面右下角企业微信里张三今天发的文件下载下来，重命名为 `周报-0425.docx`，放到 D:\\汇报\\。"
2. "打开浏览器，登录我的招行（密码我手动输），把最近一个月支出按类别整理成 Excel。"
3. "现在屏幕上是一份 PDF，帮我总结要点并发邮件给老板。"
4. "玩这局《文明 6》的回合：先研究农业，再造工人，回合结束。"

---

## 2. 可行性论证

### 2.1 关键能力是否就绪？

| 能力 | 现状 | 结论 |
| --- | --- | --- |
| 多模态视觉理解屏幕截图 | Claude Opus 4.x 已具备像素级 UI 理解、能给出近似坐标 | ✅ 可行 |
| 输出结构化 tool_use（点击 / 输入 / 截图） | Anthropic 官方已开放 `computer_use` beta tool | ✅ 可行 |
| Windows 截图 | `mss` / GDI / DXGI Desktop Duplication，毫秒级 | ✅ 成熟 |
| Windows 鼠标键盘注入 | `pyautogui` / `pynput` / SendInput Win32 API | ✅ 成熟 |
| 多显示器、DPI 缩放 | Win32 `SetProcessDpiAwarenessContext` + 物理坐标换算 | ⚠️ 需谨慎处理 |
| 中文 IME 输入 | 直接走剪贴板粘贴 / SendInput Unicode 最稳 | ✅ 可解 |
| UAC、管理员窗口 | 受 Windows 安全模型限制，普通进程无法操作高权限窗口 | ⚠️ 需以管理员启动；明确告知用户 |

### 2.2 模型能力边界（基于公开评测）
- **OSWorld / WindowsAgentArena** 基准上，Claude Sonnet/Opus 4.x 系列是当前 SOTA 之一，单任务成功率 ~40–60%。
- **强项**：文字密集 UI、表单填写、浏览器、Office。
- **弱项**：高密度小图标、像素级拖拽、动画转场、需要长时序记忆的复杂工作流。
- **结论**：MVP 聚焦"中等复杂度白领任务"完全可行，复杂任务靠**人类介入 (HITL)** 兜底。

### 2.3 为什么"不依赖 MCP"是合理的取舍？

| 维度 | 走 MCP / API | 走纯视觉（本项目） |
| --- | --- | --- |
| 接入成本 | 每个 App 都要适配 | 0，开箱即用 |
| 生态依赖 | 强 | 无 |
| 速度 | 快 | 慢（每步要截图 + 大模型推理 1–3s） |
| 准确率 | 高 | 中 |
| 通用性 | 弱 | **极强**（含一切私有软件 / 远程桌面 / VDI） |
| 用户隐私 | 取决于 App | 截图全程经过 LLM，需重点设计脱敏 |

**结论**：以"通用性"换"速度+准确率"，正是差异化卖点。可视为 **Claude 官方 Computer Use Demo 的 Windows 原生工程化版本**。

---

## 3. 竞品 / 现有方案调研

> 已抽出独立维护，详见：
>
> - **[competitor-analysis.md](competitor-analysis.md)** —— 桌面端 GUI Agent 横向对标 + 商业产品背景 + 评测基准
> - **[mobile-agents-analysis.md](mobile-agents-analysis.md)** —— 5 个移动端 GUI Agent（MobiAgent / MobileAgent / AppAgent / mobile-use / mobilerun）深度分析 + 可平移到 ctrlapp 的 10 项设计模式

**核心结论一句话**：
- **直接对标** [agent.exe](https://github.com/corbt/agent.exe)（PoC 路线一致）；OSS 标杆 Agent-S / UFO² / self-operating-computer 都偏 Python 框架——**没人做"Windows 原生托盘聊天客户端"**，是本项目的差异化护城河。
- **应优先吸收的移动端经验**：(1) `launch_app` + `app_cards`（mobilerun / AppAgent），(2) 每节点独立 LLM 配置（mobile-use 的 LangGraph），(3) AgentRR 录放（MobiAgent）。

---

## 4. 系统设计

### 4.1 交互形态：Windows 原生应用（Tauri + WebView2）

用户最终看到的是**一个 Windows 桌面应用**（安装包 .exe，开机可自启，托盘常驻，点击托盘图标弹出聊天窗）。**不是网页版**——浏览器沙箱无法控制其他应用的鼠标键盘，与本产品根本能力冲突。

采用 **"原生壳 + 网页内核"** 架构（Tauri 2 + WebView2）：

- **原生壳（Rust / Tauri）**：托盘图标、全局急停热键、窗口置顶、系统通知、自启动、DPAPI 凭证存储。
- **网页内核（WebView2 渲染 React/Vue）**：聊天窗、动作轨迹时间线、截图回放——UI 用 HTML/CSS 开发快、视觉好、易迭代。
- **后台守护（Python，无窗口子进程）**：Agent Orchestrator + Tool Runtime，由 Tauri 拉起，通过本地 IPC 通信。

这是 Cursor / Raycast / ChatGPT 桌面版同款打法：用户感知是一个原生 app，UI 实现是网页技术。

### 4.2 总体架构

```
┌──────────────────────────────────────────────────────────────┐
│  Windows 原生 App  (Tauri shell)                             │
│  ┌───────────────┐    ┌─────────────────────────────────┐    │
│  │ Tray + Hotkey │    │  WebView2  (React/Vue Chat UI)  │    │
│  │ + Autostart   │    │   ─ 对话                        │    │
│  │ + DPAPI 存储  │    │   ─ 动作时间线 / 截图回放       │    │
│  └───────┬───────┘    │   ─ HITL 确认 / 自动度档位      │    │
│          │            └────────────────┬────────────────┘    │
│          │  Tauri command / IPC        │ JSON-RPC over stdio │
│          └─────────────┬────────────────┘  (or named pipe)   │
└────────────────────────┼─────────────────────────────────────┘
                         ▼
┌──────────────────────────────────────────────────────────────┐
│           Agent Daemon  (Python 子进程，无窗口)              │
│   ┌──────────┐  ┌──────────────┐  ┌────────────────────┐     │
│   │ Planner  │→ │ ReAct Loop   │→ │ Action Verifier    │     │
│   └──────────┘  └──────┬───────┘  └────────────────────┘     │
└────────────────────────┼─────────────────────────────────────┘
                         │ tool_use / tool_result
            ┌────────────┴────────────┐
            ▼                         ▼
┌───────────────────────┐   ┌────────────────────────┐
│  Claude Opus 4.6 API  │   │  Local Tool Runtime    │
│  (vision + reasoning) │   │  ─ screenshot          │
└───────────────────────┘   │  ─ mouse / keyboard    │
                            │  ─ window mgmt         │
                            │  ─ clipboard / file    │
                            │  ─ shell (whitelisted) │
                            └────────────┬───────────┘
                                         ▼
                              ┌─────────────────────┐
                              │   Windows Desktop   │
                              └─────────────────────┘
```

**为什么不是纯网页**：浏览器 `getDisplayMedia` 截图需反复授权且帧率受限；浏览器**完全无法**控制其他进程的鼠标键盘；全局急停热键、托盘常驻、DPAPI 加密凭证都做不到。

**为什么不是纯原生控件（WPF / PySide6）**：UI 迭代慢、跨设计师协作差；本场景 UI 形态接近聊天产品，HTML/CSS 是更优解。

### 4.3 模块划分

| 模块 | 进程 | 职责 | 关键技术 |
| --- | --- | --- | --- |
| **Tauri Shell** | 原生 (Rust) | 托盘、全局急停热键、窗口管理、自启动、凭证加密、拉起守护进程 | Tauri 2, `tauri-plugin-global-shortcut`, `tauri-plugin-autostart`, Win32 DPAPI |
| **Chat UI** | WebView2 | 对话窗、动作时间线、截图回放、HITL 确认、设置 | React + Vite + TailwindCSS |
| **IPC Bridge** | 跨进程 | UI ↔ 守护进程消息总线（流式动作事件、用户中断） | JSON-RPC over stdio / 命名管道 |
| **Agent Orchestrator** | Python 守护 | 维护对话与 ReAct 循环；token / 截图 / 步数预算 | `anthropic` SDK |
| **LLM Client** | Python 守护 | Claude Opus 4.6 调用；prompt caching；图片缩放 | `anthropic` SDK |
| **Tool Runtime** | Python 守护 | `computer` / `bash` / `text_editor` 等 tool 的本地实现 | `mss`, `pywin32`, `pyautogui` |
| **Screen Sensor** | Python 守护 | 截图、多屏、DPI、可选 Set-of-Mark | `mss` + `Pillow` + 可选 `OmniParser` |
| **Input Driver** | Python 守护 | 鼠标 / 键盘 / 滚轮 / 拖拽 / 中文 IME 输入 | Win32 `SendInput`, 剪贴板兜底 |
| **Safety Layer** | Python 守护 | 危险动作拦截、白名单、HITL、审计日志 | 本地策略引擎 |
| **State Store** | Python 守护 | 对话历史、截图缓存、任务模板、用户偏好 | SQLite + 本地文件 |

### 4.4 核心循环（ReAct）

```
loop:
    1. 截全屏（或目标窗口） → resize 到 ~1280×800
    2. 把 [系统 prompt + 历史 + 用户目标 + 最近 N 张截图] 发给 Claude
    3. Claude 返回 tool_use:
         - screenshot
         - mouse_move(x,y) / left_click / double_click / right_click / drag
         - type(text) / key(combo)   ← 中文走剪贴板 paste
         - wait(ms)
         - finish(answer)
    4. Safety Layer 检查（黑名单坐标？危险按键？需 HITL？）
    5. Tool Runtime 执行 → 拿到新截图 → 作为 tool_result 回填
    6. 直到 finish 或超出步数预算
```

### 4.5 关键工程细节

#### 4.5.1 坐标与 DPI
- 进程声明 **Per-Monitor V2 DPI Aware**，避免 Windows 自动缩放截图。
- 截图记录原始分辨率 → 缩放给模型 → 模型返回坐标 → **按比例反算回物理坐标**再点。
- 多显示器：用虚拟屏幕坐标系（`GetSystemMetrics(SM_XVIRTUALSCREEN)` 等），允许模型选择"在哪个屏幕"。

#### 4.5.2 中文 / Unicode 输入
- `pyautogui.typewrite` 不支持中文。
- 方案：**剪贴板写入 + `Ctrl+V`**；或 `SendInput` 的 `KEYEVENTF_UNICODE`。
- 切换 IME：通过 `Ctrl+Space` / `Shift` 由用户预先设定，必要时 Agent 主动切到英文模式。

#### 4.5.3 分级截屏策略（带宽、成本与精度的平衡）

不同粒度的截图承担不同职责，**范围越小、频率越高、精度越高**；**范围越大、频率越低、用于全局态势感知**。三级金字塔：

| 级别 | 范围 | 默认频率 | 主要用途 | 信息量 | Token 成本 |
| --- | --- | --- | --- | --- | --- |
| **L1 全屏 / 多屏拼接** | 整个虚拟桌面 | **低**（如 60s 一次，或仅在任务起始 / 切换窗口 / 长时间无变化时触发） | 全局态势感知、找目标窗口、跨应用导航 | 最高 | 最高（缩到 ≤1568px 长边） |
| **L2 活动应用窗口** | 当前前台窗口的客户区 | **中**（如 5–10s 一次，或每个 GUI 动作前后） | 在已锁定的 App 内做表单 / 菜单 / 列表操作 | 中 | 中 |
| **L3 鼠标焦点局部** | 鼠标当前位置 ±100px（可配） | **高**（如 0.5–1s 一次，或每次 click/type 前自检） | 像素级精度校准、确认按钮命中、读 tooltip / 小图标 | 最低 | 最低（可不缩放） |

**调度规则**（由 Orchestrator 维护一个 `ScreenBudget` 状态机）：

1. **任务开始**：先拍 1 张 L1 用于规划。
2. **进入某 App 操作**：切到 L2 为主，仅在"找不到目标 / 怀疑窗口切换"时回退 L1。
3. **将要点击 / 输入**：动作前拍 1 张 L3 校准坐标；动作后拍 L3 验证；每 N 步再补一张 L2。
4. **长时间无变化**（如等待加载）：所有级别降频，只保留心跳级 L3 polling。
5. **全局事件**（用户中断、检测到弹窗）：立即升级到 L1 重新感知。

**附加规则**：
- 仅在"动作前后"或"频率到点"时截图，**不做每秒刷屏**。
- L1/L2 下采样到 **≤1568px 长边**（Claude vision 推荐）；L3 通常已经很小，**不缩放**以保留小字 / 小图标。
- 启用 **prompt caching**：系统 prompt + 任务描述缓存，每步只增量发新截图与 action。
- 滑动窗口：历史只保留最近 K 张截图（按级别独立 K 值），更早的用文字摘要替代。
- **变化检测**：若两次同级截图哈希高度相似，跳过本次 LLM 调用，直接复用上次决策。

**全部参数可配**（见 §5.x `config.toml` 示例）：

```toml
[screenshot]
# 单位：秒；0 表示按事件触发（默认）
l1_fullscreen_interval = 60
l2_activewindow_interval = 8
l3_cursor_interval = 0.8

l3_radius_px = 100        # 鼠标周边方块边长 = 2*radius
l1_max_long_edge = 1568   # 下采样长边，0 = 不缩放
l2_max_long_edge = 1568
l3_max_long_edge = 0

# 历史滑窗
keep_recent_l1 = 2
keep_recent_l2 = 4
keep_recent_l3 = 6

# 变化检测：两图相似度阈值（0~1，1=完全相同）
skip_if_similarity_above = 0.985
```

用户 / 任务模板可覆盖这些参数；未来可让 Agent 在 ReAct 中**自适应调整频率**（例如发现连续多次 L3 都没变化，就拉长间隔）。

#### 4.5.4 速度优化
- 单步耗时主要在 LLM 推理（1–3s）。
- **Speculative multi-action**：让模型在一次回复中输出 2–4 个连贯动作（参考 UFO² 的 51% LLM 调用降幅）。
- 本地 OCR/图标检测前置（OmniParser / EasyOCR）可作为辅助 hint，但保持"模型主导"。

#### 4.5.5 Set-of-Mark（可选增强）
- 用本地视觉模型（OmniParser）检测可点击元素 → 在截图上画编号框 → 模型只需输出"点 7 号"。
- 提高坐标精度，缺点是依赖本地模型，破坏"零依赖"原则——作为 **可选模式**。

### 4.6 安全与隐私设计

| 风险 | 缓解措施 |
| --- | --- |
| 截图外泄敏感信息 | 用户可定义"敏感窗口/区域"清单（如密码框、网银），自动**模糊**后再上送。 |
| 提示注入（屏幕上的恶意文字操控 Agent） | 系统 prompt 强约束；危险动作（转账、删除文件、发送邮件）**强制 HITL 二次确认**。 |
| 误操作 | 全程操作录屏 + 一键 **`Esc` 中断**（全局热键）+ 撤销日志。 |
| 权限提升 | 默认非管理员运行；遇 UAC 弹窗一律暂停由人确认。 |
| 凭证 | **永不让 Agent 读密码框**；登录环节切换"仅人类"模式。 |
| API key | 本地 DPAPI 加密存储。 |
| 审计 | 所有 LLM 请求 / 工具调用 / 截图保留 N 天，用户可导出/清除。 |

### 4.7 HITL（Human-in-the-Loop）UX
- **三档自动度**：全自动 / 关键步骤确认 / 每步确认。
- **全局热键**：`Ctrl+Alt+Esc` 立即停手并冻结鼠标键盘 1s。
- **悬浮面板**：右下角实时显示"当前在做什么、下一步要做什么、为什么"。
- **回放**：任务结束后可回放截图序列 + 模型推理。

---

## 5. 技术栈选型

| 层 | 选型 | 备注 |
| --- | --- | --- |
| 语言 | **Rust（UI 壳） + Python 3.11（Agent 守护） + TypeScript（前端）** | 多进程架构，各取所长 |
| **UI 框架** | **Tauri 2 + WebView2 + React + Vite + TailwindCSS** | 安装包小、启动快、UI 迭代强 |
| **跨进程 IPC** | JSON-RPC over stdio（Tauri sidecar） | Tauri 拉起 Python 子进程，流式输出 |
| LLM | **Claude Opus 4.6** via Anthropic API（默认） | 抽象层支持 Bedrock / Vertex / 本地代理 |
| 截图 | `mss`（快） + `windows-capture`（DXGI，最高帧率） | |
| 输入 | `pywin32` 直接调 `SendInput` | `pyautogui` 仅作兜底 |
| 窗口管理 | `pywinauto`（**仅查询窗口位置/标题**，不读 UIA 树以保持纯视觉精神） | |
| 托盘 / 热键 / 自启动 | `tauri-plugin-tray` / `tauri-plugin-global-shortcut` / `tauri-plugin-autostart` | |
| 凭证存储 | Win32 DPAPI（经 Tauri Rust 层封装） | |
| 打包 | **Tauri 官方 bundler（.msi / .exe）** + PyInstaller 生成 Python sidecar | 单安装包 |
| 日志/存储 | SQLite + 本地文件 | DPAPI 加密敏感字段 |

---

## 6. 路线图

### Phase 0 — Spike（验证可行性）
- 命令行版：`python agent.py "打开记事本，输入 hello"`。
- 复刻 Anthropic Computer Use 的 `computer` tool，但运行在 Windows 原生进程而非 Docker。
- 跑通：截图 → Claude → click/type → 回到截图。

### Phase 1 — MVP（个人可用）
- **Tauri 壳 + WebView2 聊天窗 + 托盘**。
- Tauri 以 sidecar 方式拉起 Python Agent 守护进程，stdio JSON-RPC 通信。
- 三档自动度 + 全局急停热键。
- 中文输入、多屏、DPI 适配。
- 任务历史与回放（截图序列时间线）。
- 5 个示例场景跑通（记事本、Excel、浏览器、微信、文件管理）。
- 一键 .msi 安装包发布。

### Phase 2 — 准生产
- Set-of-Mark 增强模式。
- Speculative multi-action。
- 任务模板（用户可保存"周报流程"等）。
- 隐私沙箱：敏感区域自动模糊。
- 模型可插拔（Claude / GPT-4o / Qwen-VL）。

### Phase 3 — 平台化
- 任务市场 / 模板分享。
- 多 Agent 协同（一个看屏一个写代码）。
- 企业版：审计、SSO、策略中心。

---

## 7. 风险与开放问题

1. **API 成本**：每步 1 张截图 + 历史，长任务可能数美元/次。需要 prompt caching + 截图压缩 + 步数预算硬上限。
2. **延迟**：1–3s/步 对用户耐心是挑战；需要良好的"过程可见"UX 来掩盖。
3. **稳定性**：模型偶尔点错坐标 ±20px。需要"点完后再截图自检"与回退机制。
4. **Windows 安全限制**：高权限窗口、受保护内容（部分浏览器 DRM 截图为黑屏）、Win+L 锁屏后无法操作。
5. **法务 / ToS**：部分网站禁止自动化访问；需在用户协议中明确风险与责任划分。
6. **模型版本漂移**：Anthropic Computer Use 仍是 beta，工具 schema 会变；需做适配层。
7. **可达性**：极少数老旧软件用 GDI 自绘，OCR 识别率低 → 需要 fallback（提示用户介入）。

---

## 8. 一句话总结

> 我们要做的，是把 **Claude Opus 4.6 的"看 + 想"**，与 **Windows 的"手 + 眼"** 缝合在一起的最小、最通用、最尊重用户隐私的 **桌面 Agent 客户端**——它不知道任何 App 的"内部"，但它能像人一样**看着屏幕，把事办了**。
