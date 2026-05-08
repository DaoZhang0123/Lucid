# OtterScope 🦦

> **为你的 Windows 桌面配上一双灵巧的爪子、一双不眨的眼睛。**
> 把要做的事说给 OtterScope，它会看屏幕、动鼠键；你不在的时候，它替你看消息、替你回话。
> **不依赖任何 MCP / 应用 API / 浏览器插件。** 仅靠 **Claude 的多模态视觉**指挥真实的鼠标和键盘。

> **名字从哪来？** 海獅是那种极少见的、会用工具的野生动物——胸口抱一块随身小石头，
> 付身浮在水面，两只爪子都腔贝壳。**Otter** 是动手的那一半，**Scope** 是看东西的那一半——
> 三级截图金字塔 + 任务栏监听，盘在桌面上一动不动地看。
> 两者合起来就是 **OtterScope = 看得见的眼 · 能动手的爪**。

**语言：** [English](README.md) · **简体中文** · [Français](README.fr-FR.md)

```
你：     "打开 Microsoft Teams，给我自己发一句 'Hello'"
          ↓
OtterScope：  *截一张屏*
          *看到桌面*
          → launch_app("Microsoft Teams")
          → click(和自己的对话)
          → type("Hello")  → key("enter")
          → "完成。"
```

OtterScope 是一个 Windows 桌面应用（`otterscope.exe` 引擎 + Tauri/WebView2 GUI）。下面是它已经能做的事，以及一大把可直接抄的指令例子。

---

## 为什么是 OtterScope

| | 传统 RPA / 走 API 的 bot | **OtterScope** |
| --- | --- | --- |
| 适配每个 App | 每个都要 SDK / 插件 / MCP server | **零适配。** 人能用，它就能用。 |
| 闭源/老旧软件（网银、ERP、游戏、微信…） | ❌ 通常不行 | ✅ 像素就是像素 |
| 上手成本 | 几小时胶水代码 | 装好 → 选 LLM → 一句话 |
| App 一更新 API 就崩 | 经常 | 只在 UI 视觉变了之后崩 |
| 成本 | 厂商锁定 | 自己挑 LLM（Anthropic / Copilot / 代理） |

---

## OtterScope 现在能做什么（`v0.3.0`）

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
- **自动回复**带硬编码的 **AUTO-REPLY SAFETY POLICY**（已嵌进 system prompt 层）：不泄露个人信息 / 验证码，不点付款 / 同意 / 安装，不接收文件 / 好友请求 / 屏幕共享，遇到模糊情况立刻 escalate-and-stop。

### 计划任务 / 模板
- **计划任务** —— cron 风格 + 单次 + visual_notify 三种模式。暂停 / 启用 / "立即执行"。
- **模板** —— 把常用指令存下来，一键发送。
- **同 thread context 持久化** —— 同一对话里下一句会自动续上之前的消息（旧图压缩）。

### 越用越聪明
- **`memory.md`** —— 长期记忆，自动并入 system prompt；OtterScope 可以调 `remember(text)` 主动写，你也可以在记忆页手编。
- **`tools.md`** —— 进化中的"操作技巧"库；任务结束后 OtterScope 会用 `learn_tip(text)` 把成功路径或失败教训记下来。
- **每个 App 单文件**（`apps/<slug>.py`）—— drop-a-file = 教 OtterScope 一个新 App，包含自定义启动方式 + 技巧。
- **打盹学习** —— 你 5 分钟没动作时 OtterScope 会安静地反思已结束的 thread，挖掘技巧 + *icon proposals*（它从截图里裁出来的小图标候选；你在打盹页接受后它就学会了"这个图标 = 这个 App"）。
- **自检** —— 显示器 / DPI / Win+R 别名 / 点击坐标偏移。

### 对自己诚实
- 每次运行落盘 `%LOCALAPPDATA%\dev.otterscope\logs\threads\<thread>\` —— `events.jsonl`、`messages.json`、所有截图、完整的 LLM context dump。
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

> *"`C:\Users\me\Downloads` 里最大的 5 个文件是哪几个？"*（这种 OtterScope 会用 `run_shell`，不会点来点去。）

### 🎮 游戏 / 小众软件

> *"在《文明 6》里走一回合：研究陶艺，造一个工人，结束回合。"*

> *"FL Studio 里把第 3 轨静音，把项目导出到 `D:\music\demo.wav`。"*

（游戏 UI 比较特别，第一次跑建议用 `confirm_each`，可以单步盯着。）

### 🧪 不动鼠键的 sanity check

> *"截一张全屏，告诉我有几个明显的窗口。"*

> *"读 `C:\Users\me\AppData\Local\dev.otterscope\config.toml`，告诉我现在用的是哪个 LLM provider。"*（走 `read_file` meta tool，不动 GUI。）

### 🔁 值得存下来的模板

| 名字 | 指令 |
| --- | --- |
| **Daily standup 草稿** | "打开我的 Daily Standup OneNote 页，把昨天的 commit 和今天的日程各总结成 3 条粘进页面。" |
| **截活动窗口到剪贴板** | "活动窗口截屏复制到剪贴板，告诉我 'done'。" |
| **静默时段自动回复** | （visual_notify 计划）"19:00–次日 08:00 之间，微信 / Teams 来消息就回 '我现在不在键盘前，明天再回'，然后结束。" |

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
│  Python sidecar (otterscope.exe)                       │
│  ReAct · 调度器 · 任务栏监听 · 打盹 · 记忆          │
│        ↓ mss 截图          ↓ pyautogui 注入         │
│        ↓ HTTP                                       │
│   Anthropic API   ·   GitHub Copilot   ·   代理     │
└─────────────────────────────────────────────────────┘
```

用户数据：`%LOCALAPPDATA%\dev.otterscope\`（配置、日志、计划、记忆、图标缓存、Copilot token）。

---

## 安装（终端用户）

去 release 下载 `otterscope_<版本>_x64-setup.exe`，跑安装包，从开始菜单启动 **OtterScope**。

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
cd D:\Project\OtterScope
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .

pip install pyinstaller
pyinstaller packaging\otterscope.spec
# → dist\otterscope.exe
```

### 2）Tauri 应用

```powershell
cd app
npm install
npm run tauri build
# → app\src-tauri\target\release\bundle\nsis\otterscope_<版本>_x64-setup.exe
```

Rust 壳期望 `otterscope.exe` 就在它旁边（或装在 `%LOCALAPPDATA%\otterscope\` 下）；本地开发跑前先把 PyInstaller 输出拷过去。

---

## CLI 用法（不带 GUI）

最初的 CLI 仍能用，做烟雾测试最快：

```powershell
# 连通性烟雾测试（单轮，不动鼠键）
python -m otterscope --smoke-test "你是谁？一句话。"

# 谨慎模式：每步 y/n
python -m otterscope --max-steps 4 --autonomy confirm_each `
    "截一张全屏图，告诉我屏幕上有几个明显的窗口"

# 换模型
python -m otterscope --model claude-sonnet-4.5 "打开记事本，输入 hello"

# 全自动（只在虚拟机 / 干净桌面里跑）
python -m otterscope --autonomy full "打开记事本，输入 hello world，保存到桌面"
```

`Ctrl+C` 中断；把鼠标快速甩到屏幕**左上角**会触发 PyAutoGUI 的 fail-safe。

---

## 配置

默认模板在仓库根 [config.toml](config.toml)。**真正生效**的用户配置在 `%LOCALAPPDATA%\dev.otterscope\config.toml`，要改就改这个（仓里那份升级会被覆盖）。

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

## 常见问题

- **`HTTP 500 … Connection error`** —— 上游 Copilot 抖动；客户端已自动重试 5xx，再跑一次大概率就好。
- **`HTTP 413 Request Entity Too Large`** —— 累计截图过多。调小 `[llm].keep_recent_screenshots`、`[screenshot].l1_max_long_edge`、`--max-steps`，或把 `[logging].image_format` 切成 `"jpg"`。
- **`AuthenticationError: Failed to refresh API key`** —— Copilot 设备登录 token 过期，去设置页重新登录。
- **`No such model …`** —— 代理没启用该 model_name 或填错了。设置页换一个。
- **`BitBlt: 拒绝访问`** —— Windows 当前在锁屏 / Winlogon 安全桌面。解锁；或用「熄屏」（`nircmd monitor off`）替代「锁屏」，截图仍可工作。
- **中文 type 乱码** —— 确认 `[input].chinese_input = "clipboard"`（默认），它直接走粘贴绕开输入法。
- **多屏点击偏位** —— 所有屏保持同样缩放；必要时调 `[screenshot].l1_max_long_edge`，避免被过度缩小。

---

## 风险提醒

- 模型会**完全接管你的鼠标键盘**。请在不重要的桌面 / 虚拟机里跑。
- 截图会被上传到你选择的 LLM 后端（Anthropic / GitHub Copilot 上游 / 你的代理）。
  **敏感窗口（密码框、网银、私聊）请提前关闭或最小化。**
- 任务栏自动回复在 system prompt 层带硬编码安全策略（不泄露验证码 / 地址、不点付款 / 同意、模糊就停），但你仍然要留意自己往白名单里勾了哪些 App。

---

## Stargazers · 对标 OpenAdapt

[![GitHub stars](https://img.shields.io/github/stars/codetrek/OtterScope?style=social)](https://github.com/codetrek/OtterScope/stargazers)

我们和同赛道的老前辈 [OpenAdaptAI/OpenAdapt](https://github.com/OpenAdaptAI/OpenAdapt) 做月度对照（同样定位"通用 computer-use agent"，比我们早开始），看自己的增长曲线在哪个相对位置：

| 日期 | OtterScope ★ | OpenAdapt ★ | 备注 |
| ---: | ---: | ---: | --- |
| 2026-05-01 | _tbd_ | ~1566 | OpenAdapt 同期 233 forks |
| 2026-06-01 |  |  |  |

刷新脚本：

```powershell
gh api repos/OpenAdaptAI/OpenAdapt --jq '.stargazers_count'
gh api repos/codetrek/OtterScope --jq '.stargazers_count'
```
