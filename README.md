# ctrlapp

> Windows 桌面助理 — 不依赖任何 MCP / 应用 API，仅靠 **Claude 多模态视觉**指挥鼠标键盘完成对话式任务。
> 本仓 = [design.md](design.md) 中 **Phase 0（Spike）** 的可运行最小实现。

```
你说："打开记事本，输入 hello"
       ↓
    Claude 看截图 → 输出 tool_use(key="win+r") → 输出 type("notepad") → ...
       ↓
    本进程在你的真实 Windows 桌面上点点点
```

## 现状（Phase 0）

- ✅ Windows 原生 Python 进程（不走 Docker）
- ✅ Per-Monitor V2 DPI Aware + 多屏虚拟坐标
- ✅ 三级金字塔截图骨架（L1/L2/L3，当前 ReAct 主用 L1）
- ✅ `computer` 工具完整动作派发（点击 / 拖拽 / 滚轮 / 中文 type / 快捷键）4个
- ✅ 中文输入走剪贴板 + Ctrl+V
- ✅ Safety Layer：危险词命中 / `confirm_each` 档位的终端 y/n 确认
- ✅ CLI: `python -m ctrlapp "..."`
- ✅ **LLM 后端走本地 LiteLLM 代理**（OpenAI 兼容协议）——一套 GitHub Copilot 订阅调通 Claude/GPT/Gemini
- ⏳ Tauri + WebView2 UI（Phase 1）

## 架构

```
ctrlapp (Python)  ──HTTP──>  http://localhost:4000  ───>  GitHub Copilot 后端
   │                       (litellm-ghc-proxy-lite)              │
   │                                                              ↓
   └─ mss截图 / pyautogui驱动                          claude-opus-4.6 等
```

本仓不再依赖 Anthropic 官方 SDK；LLM 调用走 `openai` SDK 打代理的 OpenAI 兼容端点。
`computer` 工具是本仓自定义的 OpenAI function tool（action / coordinate / text / scroll_* / duration），
不依赖任何厂商专有的 computer-use 特性。

## 前置

- **Windows 10/11 + Python 3.11+**（本仓验证过 3.14）
- 跑起一个本地 LiteLLM 代理，例如 [litellm-ghc-proxy-lite](https://github.com/codetrek/litellm-ghc-proxy)，默认监听 `http://localhost:4000`。
  进去完成 GitHub 设备认证后，`./test-proxy.sh` 能连上即可。

## 安装

```powershell
cd D:\Project\ctrlAppWithoutMCP
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .

# 从代理仓拿主密钥
$env:LITELLM_MASTER_KEY = (Get-Content D:\Project\litellm-ghc-proxy-lite\.env |
    Select-String '^LITELLM_MASTER_KEY=').ToString().Split('=')[1]
```

## 使用

```powershell
# 连通性烟雾测试（单轮，不动鼠键）
python -m ctrlapp --smoke-test "你是谁？一句话。"

# 谨慎模式：每个动作都问你 y/n、且先小步数试探
python -m ctrlapp --max-steps 4 --autonomy confirm_each \
    "截一张全屏图，告诉我屏幕上有几个明显的窗口"

# 换个模型（取决于代理 conf/copilot-config.yaml 里启用了哪些）
python -m ctrlapp --model claude-sonnet-4.5 "打开记事本，输入 hello"

# 代理不在 4000 端口时临时覆盖
python -m ctrlapp --base-url http://localhost:18000 "..."

# 全自动（不推荐，仅限安全完全可控的虚拟机/干净桌面）
python -m ctrlapp --autonomy full "打开记事本，输入 hello world，保存到桌面"
```

按 `Ctrl+C` 中断；把鼠标快速甩到屏幕**左上角**会触发 PyAutoGUI 的 fail-safe 立即停手。

### 实测输出

```
─── ctrlapp · smoke test ───
POST http://localhost:4000/chat/completions  model=claude-opus-4.6
Prompt: 你是谁？一句话。
─── Reply ───
我是Claude，由Anthropic开发的AI助手。
```

```
─── ctrlapp ───
任务: 截一张全屏图，用一句话告诉我屏幕上有几个明显的窗口
代理: http://localhost:4000  ·  模型: claude-opus-4.6  ·  ...
─── Step 1/2 ───
屏幕上有大约4个明显的窗口：3个PowerShell终端、右侧VS Code编辑器，以及任务栏多个图标。
```

```
─── ctrlapp ───
任务: 按下 Win+R，输入 notepad 然后回车，等记事本打开后输入 hello from ctrlapp
代理: http://localhost:4000  ·  模型: claude-opus-4.6  ·  自动度: full  ·  步数上限: 6
─── Step 1/6 ───
→ computer.key  {'text': 'win+r'}
─── Step 2/6 ───
→ computer.type {'text': 'notepad\n'}
─── Step 3/6 ───
→ computer.key  {'text': 'Return'}
─── Step 4/6 ───
→ computer.wait {'duration': 2}
─── Step 5/6 ───
→ computer.left_click {'coordinate': [780, 420]}
→ computer.type {'text': 'hello from ctrlapp'}
```

> 滑窗上限由 `[llm].keep_recent_screenshots` 控制（默认 4），超出后旧图在发出前被替换为占位文本，本地 `messages.jsonl` 和 `step-*.png` 仍会完整落盘。

#### 端到端：写文件并落盘

把"打字 + 保存到具体路径"作为闭环验证（用命令行核对结果，不依赖模型自述）：

```powershell
# 准备
Get-Process notepad -EA SilentlyContinue | Stop-Process -Force
Remove-Item D:\Project\ctrlAppWithoutMCP\ctrlapp-notepad-test.txt -Force -EA SilentlyContinue

# 跑
python -m ctrlapp --max-steps 40 --autonomy full @"
请把记事本作为目标程序完成下面动作：1) Win+R 启动 notepad；
2) 在编辑区点击聚焦后 type 文本：hello from ctrlapp；
3) Ctrl+S 触发保存对话框；4) 在文件名输入框 type 完整路径
D:\Project\ctrlAppWithoutMCP\ctrlapp-notepad-test.txt；5) 回车保存；
6) 标题栏不再有星号、变成 ctrlapp-notepad-test.txt 时，以 '任务完成:' 开头总结。
"@

# 核对（这才是真"测试通过"的判据）
Get-Content D:\Project\ctrlAppWithoutMCP\ctrlapp-notepad-test.txt -Encoding UTF8
# → hello from ctrlapp
```

实测 20 步内完成，文件 18 字节，内容与预期一致，记事本标题栏从 `*Untitled` 切到 `ctrlapp-notepad-test.txt`。

### 常见问题

- **`HTTP 500 ... Github_copilotException - Connection error`**
  代理本身没问题，是 GitHub Copilot 上游偏抖，重跑一次大概率就好（`proxy_client.chat_once` 已内置 5xx 重试）。持续报错可 `docker logs -f litellm-proxy-lite` 看上游。
- **`HTTP 413 Request Entity Too Large`**
  多步任务累计太多张截图，超出上游单请求体上限。本仓已用滑窗裁剪（[`llm`].`keep_recent_screenshots`，默认 4）只保留最近 N 张；如仍超限，可调小该值、减小 `--max-steps`、调小 `[screenshot].l1_max_long_edge`、或在 `[logging]` 里切到 `image_format="jpg"`。
- **`AuthenticationError: Failed to refresh API key`**
  代理那边 GitHub 设备登录 token 过期了，去 `litellm-ghc-proxy-lite` 重新跑一次设备认证。
- **`No such model claude-opus-4.6`** 或 4xx
  代理仓的 [conf/copilot-config.yaml](https://github.com/codetrek/litellm-ghc-proxy/blob/master/conf/copilot-config.yaml) 里没启用该 model_name。`./list-copilot-models.sh --enabled-only` 查，或用 `--model` 换个。
- **中文 type 乱码** / **type 出的字符被输入法吞了**
  确保 `[input].chinese_input = "clipboard"`（默认）——该模式下所有 `action="type"` 都走剪贴板粘贴，不受当前中/英输入法状态影响；`\n` / `\t` 会拆成“粘贴 + 按键”以保证换行/Tab 准确触发。
- **多屏点击偏位**
  检查所有屏缩放是否一致；需要时调 `[screenshot].l1_max_long_edge` 以免被过度缩小。

## 配置

所有可调参数在仓库根 [config.toml](config.toml)：

- `[llm]`：`max_tokens` / `max_steps` / `keep_recent_screenshots`
- `[llm.proxy]`：`base_url` / `model` / `api_key`（空则读 `LITELLM_MASTER_KEY`）
- `[logging]`：本地日志开关与等级（见下节）
- `[screenshot]`：三级金字塔的频率 / 长边上限 / 历史滑窗
- `[safety]`：HITL 关键字 / 默认自动度档位
- `[input]`：中文输入策略 / 动作间隔

命令行 `--model / --base-url / --max-steps / --autonomy / -c` 可临时覆盖。

### 本地日志

每次运行都会在 `logs/<时间戳>-<任务前缀>/` 下落盘：

```
logs/20260425-185158-用一句话告诉我屏幕上有几个明显的窗口/
├── run.log            # 文本流水（UTF-8）
├── messages.jsonl     # 每步 assistant 文本 + tool_call + 结果（不含图像）
├── step-000-init.png  # 任务开始时的截图
├── step-001-post.png  # 第 1 步动作后截图
└── ...
```

`config.toml` 里 `[logging]` 段：

| 字段 | 作用 |
| --- | --- |
| `enabled` | 总开关；false 时完全不写盘 |
| `text_level` | 文本等级 `DEBUG / INFO / WARNING / ERROR / OFF` |
| `image_level` | 图像等级 `DEBUG / INFO / WARNING / OFF`（**与文本独立**） |
| `image_format` | `png` 或 `jpg`（隐私 / 体积权衡） |
| `jpg_quality` | jpg 模式下的质量 |
| `keep_runs` | 历史轮转上限，超过后自动删最旧目录 |

常用搭配：

- **默认（推荐）**：`text_level=INFO` + `image_level=INFO` —— 步骤摘要 + 任务首张和每步动作后截图
- **极致排错**：`text_level=DEBUG` + `image_level=DEBUG` —— 还会保存模型主动 screenshot 的图
- **隐私优先**：`text_level=INFO` + `image_level=OFF` —— 只写文本，不留图
- **完全静默**：`enabled=false`

PowerShell 看 UTF-8 日志：`Get-Content path/run.log -Encoding UTF8`

## 目录结构

```
src/ctrlapp/
├── __main__.py        # CLI 入口
├── config.py          # config.toml 加载
├── dpi.py             # Per-Monitor V2 DPI + 虚拟屏幕矩形
├── window.py          # 前台窗口 / 鼠标位置（不读 UIA 树）
├── screen.py          # ScreenSensor：L1/L2/L3 截图、缩放、phash
├── input_driver.py    # 鼠标 / 键盘 / 中文剪贴板输入
├── tools.py           # `computer` function tool 动作派发
├── proxy_client.py    # smoke-test 用的最小 OpenAI 兼容客户端
├── safety.py          # HITL 拦截
├── runlog.py          # 每次运行的本地日志（文本 + 截图）
└── loop.py            # ReAct 主循环（openai SDK 走代理）
```

## 风险提醒

- 模型会**完全接管你的鼠标键盘**。请在不重要的桌面 / 虚拟机里跑。
- 截图会上传到本地代理 → 进而到 GitHub Copilot 上游（Anthropic / OpenAI / Google）。**敏感窗口（密码框、网银、私聊）请提前关闭或最小化**。
- 详见 [design.md §4.6 安全与隐私设计](design.md)。

## 路线

| Phase | 状态 | 内容 |
| --- | --- | --- |
| 0 Spike | ✅ 本仓 | CLI 跑通闭环 |
| 1 MVP | ⏳ | Tauri + WebView2 托盘聊天窗、全局急停热键、回放 |
| 2 准生产 | ⏳ | Set-of-Mark 增强、speculative multi-action、隐私沙箱 |
| 3 平台化 | ⏳ | 任务模板、企业策略 |

设计细节见 [design.md](design.md)。
