# ctrlapp 竞品 / 现有方案调研

> 本文从 [design.md §3](design.md) 抽出独立维护，专注横向对标。
> 移动端 GUI Agent 的深入分析另见 [mobile-agents-analysis.md](mobile-agents-analysis.md)。

聚焦在**与本项目同一路线（LLM 驱动 + 纯视觉 / 截图 + 控制鼠标键盘）**的开源项目。商业大厂产品作为背景了解，单独列在末尾即可。

## 1. 开源 GitHub 项目（重点对标）

> 按"距离本项目的相似度"由近到远排列。

| # | 项目 | Stars 量级 | 形态 / 技术栈 | 视觉路线 | 与本项目差异 / 可借鉴点 |
| --- | --- | --- | --- | --- | --- |
| 1 | **[corbt/agent.exe](https://github.com/corbt/agent.exe)** | ~3.5k | Electron + TS + Claude Computer Use | 纯视觉 | **路线最接近**：Claude + 桌面 + 简单 UI。但作者明确写"6 小时写的 PoC，不维护"，仅主屏，UX 粗糙。**直接对标**——我们要做的就是它的工程化版本。 |
| 2 | **[OthersideAI/self-operating-computer](https://github.com/OthersideAI/self-operating-computer)** | ~10k | Python CLI，跨平台，多模型（GPT-4o/Claude/Gemini） | 纯视觉 + Set-of-Mark | 老牌项目，CLI 形态，无原生 UI，无 HITL，无 IME 处理。可借鉴其多模型抽象层与 SoM 实现。 |
| 3 | **[showlab/computer_use_ootb](https://github.com/showlab/computer_use_ootb)** | ~1.9k | Python，"开箱即用" Win/macOS GUI Agent | 纯视觉 | Win/Mac 原生跑（不用 Docker），但仍是 Python 脚本 + 简易 UI。**最接近的"原生跨平台 demo"**，工程化未完成。 |
| 4 | **[trycua/cua](https://github.com/trycua/cua)** | ~14k | 基础设施级，沙箱 + SDK + 评测 | 纯视觉 + 沙箱 | 重在"给 Agent 一个安全沙箱"（VM 而非主机）；定位偏底座，不直接服务终端用户。可借鉴其 SDK / 评测套件。 |
| 5 | **[simular-ai/Agent-S](https://github.com/simular-ai/Agent-S)** | ~10.9k | Python 框架（S1/S2/S3） | 纯视觉 + 独立 Grounding 模型 | **当前 OSWorld 上首个超人类水平（72.6%）**。架构亮点：主推理模型 + UI-TARS-7B 做坐标 grounding。本项目可学习其"双模型"策略提升点击精度。 |
| 6 | **[microsoft/UFO / UFO² / UFO³](https://github.com/microsoft/UFO)** | ~8.5k | Python，Win 原生，UIA + COM + MCP + 视觉 | 混合（非纯视觉） | 学术 SOTA，**重度依赖 UIA / COM / MCP**，与"三个不"原则正相反。可借鉴其"speculative multi-action"（51% LLM 调用降幅）。 |
| 7 | **[OpenInterpreter/open-interpreter](https://github.com/OpenInterpreter/open-interpreter)** | ~60k+ | Python，"OS Mode"为可选 | 偏代码执行 | 主路线是"让 LLM 写代码自己跑"；OS 控制是副线。本项目相反：**主线就是模拟人手**。 |
| 8 | **[OpenAdaptAI/OpenAdapt](https://github.com/OpenAdaptAI/OpenAdapt)** | ~1.2k | Python，"录制 + 回放 + 泛化"思路 | 纯视觉 + demonstration | 强调用户演示数据训练个性化 Agent。可借鉴其"录制即模板"机制。 |
| 9 | **[BAAI-Agents/Cradle](https://github.com/BAAI-Agents/Cradle)** | ~2k | Python，"通用计算机控制"框架 | 纯视觉 | 因玩 RDR2 出名，强调任意软件 / 游戏；学术性强，工程化弱。可借鉴其"长时序记忆 + 反思"模块。 |
| 10 | **[GAIR-NLP/PC-Agent](https://github.com/GAIR-NLP/PC-Agent)** | ~1k | Python，PC 版"夜间打工" Agent | 视觉 + 认知架构 | 国内学术项目，分层认知；可参考其规划器拆分。 |
| 11 | **[showlab/ShowUI / ShowUI-Aloha](https://github.com/showlab/ShowUI)** | ~数 k | Vision-Language-Action 模型 | 纯视觉 | 提供专门的 GUI 基础模型（可作为本地 grounding 备选，替代云端 Claude 做坐标定位）。 |
| 12 | **[OS-Copilot/OS-Atlas](https://github.com/OS-Copilot/OS-Atlas)** | ~1k+ | GUI Agent 基础动作模型 + 数据集 | 纯视觉 | 同上，开源 grounding 模型候选。 |
| 13 | **[mediar-ai/terminator](https://github.com/mediar-ai/terminator)** | ~1.4k | Rust，"Windows 版 Playwright" | UIA / 选择器（**非纯视觉**） | 反路线但有用：可作为 fallback 工具，处理纯视觉点不准的"魔鬼细节" UI。 |
| 14 | **[asweigart/pyautogui](https://github.com/asweigart/pyautogui)** / **[nut-tree/nut.js](https://github.com/nut-tree/nut.js)** | 高 | 跨平台输入注入库 | 工具库 | 候选输入层依赖（Python / Node 各一）。 |
| 15 | **[OpenGVLab/ScaleCUA](https://github.com/OpenGVLab/ScaleCUA)** | ~1.1k | 跨平台 CUA + 数据 | 纯视觉 | ICLR 2026 Oral，开源数据 + 模型，可做模型评测对照。 |
| 16 | **[onuratakan/gpt-computer-assistant](https://github.com/onuratakan/gpt-computer-assistant)** | 较高 | Python 桌面助手 | 视觉 | OpenAI 路线版本，UX 借鉴。 |
| 17 | **[anthropics/claude-quickstarts/computer-use-demo](https://github.com/anthropics/claude-quickstarts/tree/main/computer-use-demo)** | 高 | Docker + Linux + Streamlit | 纯视觉 | **Anthropic 官方参考**。本项目=它的 Windows 原生工程化版本。 |

## 2. 移动端 GUI Agent（横向参考）

虽然 ctrlapp 聚焦 Windows 桌面，移动端 GUI Agent 在过去两年的进展（多 Agent 协作、
on-device 推理、Record-Replay 加速、user profile memory）非常值得平移到桌面侧。
独立深度分析见 [mobile-agents-analysis.md](mobile-agents-analysis.md)，简表：

| # | 项目 | 平台 | 技术亮点 | ctrlapp 可借鉴 |
| --- | --- | --- | --- | --- |
| M1 | [X-PLUG/MobileAgent](https://github.com/X-PLUG/MobileAgent)（含 v1–v3.5 / PC-Agent / Mobile-Agent-E） | Android / Win / Mac | 多 Agent 协作（planner+decision+reflection+memory）；GUI-Owl 1.5 自研基模；PC-Agent 主动感知 | **多 Agent 拆分** 和 **PC-Agent 的主动感知模块**最值得移植到 ctrlapp 的 ReAct 循环 |
| M2 | [IPADS-SAI/MobiAgent](https://github.com/IPADS-SAI/MobiAgent) | Android（含 on-device） | Decider/Grounder 双模型；AgentRR 录-放加速；MobiFlow milestone DAG 评测；user profile (Mem0+GraphRAG) | **AgentRR 录放思想** 与 **user profile memory** 直接对应 ctrlapp 的 templates / memory 路线 |
| M3 | [TencentQQGYLab/AppAgent](https://github.com/TencentQQGYLab/AppAgent) (CHI 2025) | Android | 两阶段：Exploration 期生成"元素文档"，Deployment 期查文档；网格覆盖兜底 grounding | **两阶段：先探索写文档、再上岗"** 等价于 ctrlapp 未来的 "App 区域库 + 学习反馈" 闭环 |
| M4 | [minitap-ai/mobile-use](https://github.com/minitap-ai/mobile-use) | Android / iOS | LangGraph 多节点（cortex / executor / planner / hopper / contextor / outputter / summarizer）；100% AndroidWorld | **LangGraph 节点化** 对应 ctrlapp 未来的 planner/executor/checker 三角 |
| M5 | [droidrun/mobilerun](https://github.com/droidrun/mobilerun) | Android / iOS | 多 LLM provider；CLI 优先；Arize Phoenix tracing；macro / app_cards 模块 | **app_cards** 与 ctrlapp 的 "App 区域库 + launchers.json" 思路高度一致；**Phoenix tracing** 可作为 events.jsonl 的可视化升级 |

## 3. 学术 / 评测基准（用来给项目"打分"）

- **OSWorld** ([xlang-ai](https://github.com/xlang-ai/OSWorld))：跨应用桌面任务集，业界主基准。Agent S3 已达 72.6%（超人类）。
- **WindowsAgentArena** ([microsoft](https://github.com/microsoft/WindowsAgentArena))：Windows 专属，和本项目目标平台对齐。
- **ScreenSpot-Pro / ScreenSpot**：GUI grounding 精度评测。
- **AndroidWorld**：移动端，仅作参考。mobile-use 已 100% 通过、mobilerun 91.4%。

> 本项目 MVP 出炉后应跑这两个桌面基准至少一遍，证明"通用性 + 准确率"在可接受区间。

## 4. 商业 / 大厂产品（背景）

仅列名以避免混淆领域全貌——这些都不是本项目的直接竞品（要么平台不同，要么走 API 路线，要么仅在云端浏览器）：

- **国外**：Anthropic Computer Use（API/Demo）、OpenAI Operator / ChatGPT Agent（云端浏览器）、Microsoft Copilot Vision / Recall + Actions（深度绑 Windows）、Hyperwrite（浏览器扩展）、Adept ACT-1（已停摆）。
- **国内**：智谱 AutoGLM / GLM-PC、华为小艺 / 荣耀 YOYO（手机 Accessibility）、影刀 / 来也 / UiBot 等传统 RPA（脚本式）、钉钉 / 飞书 AI 助理（SaaS 内）。

## 5. 调研结论

- **直接对标** [agent.exe](https://github.com/corbt/agent.exe)：路线一致但停留在 PoC，存在明显工程化空白。
- **OSS 标杆** [Agent-S](https://github.com/simular-ai/Agent-S)、[UFO²](https://github.com/microsoft/UFO)、[self-operating-computer](https://github.com/OthersideAI/self-operating-computer)：路线接近但形态都偏 Python 框架 / 学术 demo，**没有人做"Windows 原生托盘聊天客户端"**。
- **移动端经验沉淀** 见 [mobile-agents-analysis.md](mobile-agents-analysis.md)，可平移的关键模式：
  - 多 Agent 拆分（Mobile-Agent-v2/v3、mobile-use 的 LangGraph 子图）
  - Decider+Grounder 双模型（MobiAgent、Agent-S）
  - 探索-部署两阶段 + UI 元素文档（AppAgent）
  - Record & Replay 加速重复任务（MobiAgent AgentRR、OpenAdapt）
  - User profile memory（MobiAgent Mem0+GraphRAG）
  - app_cards / launchers.json（mobilerun、ctrlapp planned）
- **可复用的开源积木**：
  - Grounding：UI-TARS-7B / OS-Atlas / ShowUI / GUI-Owl-1.5（可作为本地 grounding 备选，替代云端 Claude 做坐标定位）。
  - 输入：pyautogui / nut.js / mediar-ai/terminator（fallback）。
  - 评测：OSWorld、WindowsAgentArena 直接接入。
  - 截图 / 录制：OpenAdapt 录制思路、Cradle 长时序记忆、AgentRR 轨迹回放。
  - 可观测性：Arize Phoenix（mobilerun 同款）。
- **差异化护城河**：
  1. **Windows 原生客户端体验**（Tauri 安装包、托盘、热键、IME、DPI、多屏）——开源同行普遍缺失。
  2. **中文办公场景优化**（IME 输入、中文 UI 训练样本、本土 SaaS 适配）。
  3. **三级金字塔截屏 + 安全沙箱 + HITL**——把"通用视觉 Agent"做成"个人用户敢日常使用"的产品。
