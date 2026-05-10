# <img src="app/src-tauri/icons/128x128.png" width="32" alt="Lucid icon" /> Lucid

一个真正“像人操作电脑”的 AI 助手：无需 MCP，直接控制你的 Windows 应用，并在你不在时持续自动回复。

> **为你的 Windows 桌面配上一双澄澈、不眨的眼睛——Windows 视觉智能体。**
> 把要做的事说给 Lucid，它会看屏幕、动鼠键；你不在的时候，它替你看消息、替你回话。
> **不依赖任何 MCP / 应用 API / 浏览器插件。** 仅靠 **Claude 的多模态视觉**指挥真实的鼠标和键盘。
> **不同于官方 bot（微信等）——Lucid 直接控制你的真实客户端**，所以能看任何消息、读到完整上下文、以你的身份回话，还有状态持久化、无需审核。

> **名字从哪来？** **明眸**——出自“明眸善睞”，明亮、洞察、看得清。Logo 中央那个十字瞒准框就是这双眼睛：看见什么就做什么。三级截图金字塔加上不眨眼的任务栏监听，让感知足够锐利；一个简单的 ReAct 主循环，让动作足够老实。**Lucid = 看得见的眼 · 动得了的手。**
> *小彩蛋：盯一眼启动 splash——一只小蟹会悄悄爬进瞒准框中央。*

**语言：** [English](README.md) · **简体中文** · [Français](README.fr-FR.md)

```
你：     "打开 Microsoft Teams，给我自己发一句 'Hello'"
          ↓
Lucid：  *截一张屏*
          *看到桌面*
          → launch_app("Microsoft Teams")
          → click(和自己的对话)
          → type("Hello")  → key("enter")
          → "完成。"
```

Lucid 是一个 Windows 桌面应用（`lucid.exe` 引擎 + Tauri/WebView2 GUI）。下面是它已经能做的事，以及一大把可直接抄的指令例子。

---

## 为什么是 Lucid

| | 传统 RPA / 走 API 的 bot | **Lucid** |
| --- | --- | --- |
| 适配每个 App | 每个都要 SDK / 插件 / MCP server | **零适配。** 人能用，它就能用。 |
| 闭源/老旧软件（网银、ERP、游戏、微信…） | ❌ 通常不行 | ✅ 像素就是像素 |
| 给你自动回复消息 | 官方 bot 只能；需要审核；无状态；看不到完整历史 | ✅ **驱动你真实的客户端。** 能看任何消息、读完整历史、以你的身份回话、有状态持久化。 |
| 上手成本 | 几小时胶水代码 | 装好 → 选 LLM → 一句话 |
| App 一更新 API 就崩 | 经常 | 只在 UI 视觉变了之后崩 |
| 成本 | 厂商锁定 | 自己挑 LLM（Anthropic / Copilot / 代理） |

---

## Lucid 现在能做什么

### 跟你聊天
- 对话式聊天壳（Tauri 2 + SvelteKit + WebView2），系统托盘，全局急停热键 `Ctrl+Alt+Esc`。
- 三语 UI —— **English / 简体中文 / Français**（svelte-i18n），设置页切换。
- 三个 LLM 后端一键切换：**Anthropic** 直连 · **GitHub Copilot** OAuth · **OpenAI 兼容代理**（LiteLLM、OpenClaw…）。

### 看屏，看得聪明
- **Per-Monitor V2 DPI** + 多屏虚拟坐标。
- **三级截图金字塔**让模型自己挑：L1 全屏 / L2 活动窗口 / L3 鼠标周边（基于 UIA 智能贴边——不再是死板的 200×200 方框，而是吸到光标下那个 UI 元素的真实边界）。
- 智能 context 管理：每级保留窗口 + 旧截图 JPEG 重压缩 + 超出模型上限自动总结。
- **空起手模式：** 起手不一定真要喂 L1，告诉模型桌面尺寸即可，让它自己决定要不要看。

### 真的开你的鼠键
- 完整 `computer` 工具：点击 / 拖拽 / 滚轮 / 快捷键 / 中文 type（剪贴板路径，绕开输入法）。
- 内置 zero-GUI 工具集，琐碎事不靠点击：`read_file` / `write_file` / `run_shell`（输出捕获、无控制台窗口、20s 超时）。
- 原生启动：`launch_app("VS Code")` 走 Windows API（开始菜单 + UWP MSIX manifest 扫描），自动 pin 活动窗口，省掉一轮"找图标"。

### 你不在的时候帮你盯着
- **任务栏视觉通知** —— 周期性 dHash diff 任务栏；可疑变化触发一次便宜的 LLM 二次确认，判断是不是真有新消息、是哪个 App。每条计划自带**应用白名单**，只动你允许的程序。
- **自动回复**带硬编码的 **AUTO-REPLY SAFETY POLICY**（已嵌进 system prompt 层）：不泄露个人信息 / 验证码，不点付款 / 同意 / 安装，不接收文件 / 好友请求 / 屏幕共享，遇到模糊情况立刻 escalate-and-stop。*不同于官方微信 bot（需要审核、无状态、无法控制出站消息），Lucid 直接驱动你的真实微信客户端 —— 所以你能得到完整的、自主的、有状态的自动回复。*

### 计划任务 / 模板
- **计划任务** —— cron 风格 + 单次 + visual_notify 三种模式。暂停 / 启用 / "立即执行"。
- **模板** —— 把常用指令存下来，一键发送。
- **同 thread context 持久化** —— 同一对话里下一句会自动续上之前的消息（旧图压缩）。

### 越用越聪明
- **`memory.md`** —— 长期记忆，自动并入 system prompt；Lucid 可以调 `remember(text)` 主动写，你也可以在记忆页手编。
- **`tools.md`** —— 进化中的"操作技巧"库；任务结束后 Lucid 会用 `learn_tip(text)` 把成功路径或失败教训记下来。
- **每个 App 单文件**（`apps/<slug>.py`）—— drop-a-file = 教 Lucid 一个新 App，包含自定义启动方式 + 技巧。
- **打盹学习** —— 你 5 分钟没动作时 Lucid 会安静地反思已结束的 thread，挖掘技巧 + *icon proposals*（它从截图里裁出来的小图标候选；你在打盹页接受后它就学会了"这个图标 = 这个 App"）。
- **自检** —— 显示器 / DPI / Win+R 别名 / 点击坐标偏移。

### 对自己诚实
- 每次运行落盘 `%LOCALAPPDATA%\dev.lucid\logs\threads\<thread>\` —— `events.jsonl`、`messages.json`、所有截图、完整的 LLM context dump。
- 三档自动度：`full` / `confirm_critical` / `confirm_each`。HITL 关键字列表（`删除` / `format` / `转账` / `确认付款` …）即使 `full` 也会拦下危险动作。

---

## 例子 —— 大家真的拿来干嘛

下面这些都是直接能粘进聊天框的一句话。路径 / 名字按需要改，自动度在底部切。

### 📝 Office 类

> *"打开记事本，把我刚才口述的会议纪要打进去，存成 `D:\notes\2026-05-08.txt`。"*

> *"打开桌面上 `expenses.xlsx`，滚到 C 列底，告诉我 C 列的总和。"*

> *"把 Edge 现在打开的 PDF 的执行摘要总结成 5 条要点，粘到一封新的 Outlook 草稿里发给 alice@…，主题写 `PDF 摘要`。"*

### 💬 离开座位时帮我回消息（配合计划任务）

新建 **计划 → 动作：visual_notify**，白名单勾上 `微信` + `Microsoft Teams`。指令模板：

> *"打开对应的聊天工具，看一眼最新未读，安全的话给一句简短自然的回复。"*

`AUTO-REPLY SAFETY POLICY`（在 system prompt 层）会强制：不泄露个人信息、不收文件 / 链接 / 验证码、不授权任何东西、聊天走偏立刻 escalate-and-stop。

### ⏰ 周期性任务（cron）

动作 `task` + 每天 / 每周 / interval 触发：

> *"工作日每天 9:00 —— 打开 Outlook，扫一眼未读收件箱，给我写 3 行总结弹个 toast。"*

> *"每周五 17:00 —— 打开 `D:\Reports\template.xlsx`，A1 填本周日期，另存为 `weekly-<YYYY-MM-DD>.xlsx` 到同目录。"*

> *"每 30 分钟 —— 看一眼 Visual Studio Code 的 git 状态栏；如果分支后面有 `*`（未保存），来一条提醒。"*

### 🌐 浏览器 / 检索

> *"打开 Chrome，搜 '2026 年最佳人体工学键盘'，前 3 个结果新标签打开，每个写一段总结。"*

> *"用我已经登录的 GitHub 标签页找到 `acme/foo` 仓库的 issue #142，把我接下来口述的评论粘进去，点 Comment。"*

### 🛠️ 文件 / 系统琐事

> *"`D:\Photos\unsorted` 里所有 `IMG_*.JPG`，按当前顺序重命名为 `2026-05-08-<NNNN>.jpg`。"*

> *"`C:\Users\me\Downloads` 里最大的 5 个文件是哪几个？"*（这种 Lucid 会用 `run_shell`，不会点来点去。）

### 🎮 游戏 / 小众软件

> *"在《文明 6》里走一回合：研究陶艺，造一个工人，结束回合。"*

> *"FL Studio 里把第 3 轨静音，把项目导出到 `D:\music\demo.wav`。"*

（游戏 UI 比较特别，第一次跑建议用 `confirm_each`，可以单步盯着。）

### 🧪 不动鼠键的 sanity check

> *"截一张全屏，告诉我有几个明显的窗口。"*

> *"读 `C:\Users\me\AppData\Local\dev.lucid\config.toml`，告诉我现在用的是哪个 LLM provider。"*（走 `read_file` meta tool，不动 GUI。）

---

## 架构（一图）

```
┌──────────── Tauri WebView (SvelteKit) ─────────────┐
│  聊天 │ 计划 │ 模板 │ 记忆 │ 打盹 │ ⚙              │
└──────────────────────┬─────────────────────────────┘
                       │ Tauri IPC
┌──────────────────────┴─────────────────────────────┐
│   Rust 壳 —— sidecar 生命周期 / 设置 / 系统托盘    │
└──────────────────────┬─────────────────────────────┘
                       │ JSON-RPC over stdio
┌──────────────────────┴─────────────────────────────┐
│  Python sidecar (lucid.exe)                       │
│  ReAct · 调度器 · 任务栏监听 · 打盹 · 记忆          │
│        ↓ mss 截图          ↓ pyautogui 注入         │
│        ↓ HTTP                                       │
│   Anthropic API   ·   GitHub Copilot   ·   代理     │
└─────────────────────────────────────────────────────┘
```

用户数据：`%LOCALAPPDATA%\dev.lucid\`（配置、日志、计划、记忆、图标缓存、Copilot token）。

---

## 安装（终端用户）

去 release 下载 `lucid_<版本>_x64-setup.exe`，跑安装包，从开始菜单启动 **Lucid**。

首次启动后进**设置**，挑一个 LLM 后端：

- **GitHub Copilot** —— 点 *Sign in to GitHub Copilot*，按提示走设备码流程。只要订阅了 Copilot 就能用。
- **Anthropic** —— 粘 `sk-ant-…` 密钥。
- **Proxy** —— 指向任意 OpenAI 兼容端点（例如 [litellm-ghc-proxy-lite](https://github.com/codetrek/litellm-ghc-proxy)）。

---

## 从源码构建

### 前置
- Windows 10 / 11
- Python 3.11+（验证过 3.14）
- Node.js 20+ 和 npm
- Rust 工具链（stable）+ **WebView2 运行时**（Win11 自带）

### 1）Python sidecar

```powershell
cd D:\Project\Lucid
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .

pip install pyinstaller
pyinstaller packaging\lucid.spec
# → dist\lucid.exe
```

### 2）Tauri 应用

```powershell
cd app
npm install
npm run tauri build
# → app\src-tauri\target\release\bundle\nsis\lucid_<版本>_x64-setup.exe
```

Rust 壳期望 `lucid.exe` 就在它旁边（或装在 `%LOCALAPPDATA%\lucid\` 下）；本地开发跑前先把 PyInstaller 输出拷过去。

---

## CLI 用法（不带 GUI）

请在仓库根目录（`D:\Project\Lucid`）运行。

如果当前 provider 需要 key，请先设置：

```powershell
# proxy provider
$env:LITELLM_MASTER_KEY = "your_proxy_key"

# anthropic provider
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

然后执行：

```powershell
cd D:\Project\Lucid

# 连通性烟雾测试（单轮，不动鼠键）
.venv\Scripts\python.exe -m lucid --smoke-test "你是谁？一句话。"

# 谨慎模式：每步 y/n
.venv\Scripts\python.exe -m lucid --max-steps 4 --autonomy confirm_each `
    "截一张全屏图，告诉我屏幕上有几个明显的窗口"

# 换模型
.venv\Scripts\python.exe -m lucid --model claude-sonnet-4.5 "打开记事本，输入 hello"

# 全自动（只在虚拟机 / 干净桌面里跑）
.venv\Scripts\python.exe -m lucid --autonomy full "打开记事本，输入 hello world，保存到桌面"
```

如果出现 `missing api_key (config .api_key or LITELLM_MASTER_KEY environment variable)`，请在 `%LOCALAPPDATA%\dev.lucid\config.toml` 里设置 `[llm.proxy].api_key`，或导出 `LITELLM_MASTER_KEY` 环境变量。

`Ctrl+C` 中断；把鼠标快速甩到屏幕**左上角**会触发 PyAutoGUI 的 fail-safe。

---

## 配置

默认模板在仓库根 [config.toml](config.toml)。**真正生效**的用户配置在 `%LOCALAPPDATA%\dev.lucid\config.toml`，要改就改这个（仓里那份升级会被覆盖）。

主要段落：

| 段 | 控什么 |
| --- | --- |
| `[llm]` | provider、最大步数、max_tokens、prompt-cache、temperature/top-p、截图保留策略 |
| `[llm.anthropic]` / `[llm.copilot]` / `[llm.proxy]` | 各 provider 的 model + 端点 + key |
| `[logging]` | 每次运行日志根目录、文本/图片等级（`DEBUG/INFO/WARNING/ERROR/OFF`）、`png/jpg`、轮转 |
| `[screenshot]` | 三级金字塔的频率、长边上限、每级保留张数、变化检测阈值 |
| `[safety]` | HITL 关键字、急停热键（`ctrl+alt+esc`）、默认自动度、落点取证、保存对话框防护 |
| `[input]` | `chinese_input = "clipboard"`（推荐）或 `unicode_sendinput`，动作间隔 |
| `[visual_notify]` | 任务栏轮询频率、dHash 阈值、LLM 二次确认冷却、auto-chat 指令 |
| `[doze]` | 打盹反思的各种上限 |
| `[memory]` / `[tools]` | 长期记忆 + 操作技巧的开关与上限 |
| `[fileio]` / `[shell]` | `read_file` / `write_file` / `run_shell` 的开关与沙箱 |

GUI 设置页保存后会热重载 sidecar。

---

## 风险提醒

- 模型会**完全接管你的鼠标键盘**。请在不重要的桌面 / 虚拟机里跑。
- 截图会被上传到你选择的 LLM 后端（Anthropic / GitHub Copilot 上游 / 你的代理）。
  **敏感窗口（密码框、网银、私聊）请提前关闭或最小化。**
- 任务栏自动回复在 system prompt 层带硬编码安全策略（不泄露验证码 / 地址、不点付款 / 同意、模糊就停），但你仍然要留意自己往白名单里勾了哪些 App。

---

## Stargazers

[![GitHub stars](https://img.shields.io/github/stars/codetrek/Lucid?style=social)](https://github.com/codetrek/Lucid/stargazers)
