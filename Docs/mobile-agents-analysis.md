# 移动端 GUI Agent 横向分析（5 个项目）

> 调研对象：[MobiAgent](https://github.com/IPADS-SAI/MobiAgent)、
> [MobileAgent](https://github.com/X-PLUG/MobileAgent)（X-PLUG 系列：v1/v2/v3/v3.5 + PC-Agent + Mobile-Agent-E + GUI-Critic-R1 + UI-S1）、
> [AppAgent](https://github.com/TencentQQGYLab/AppAgent)（CHI 2025）、
> [mobile-use](https://github.com/minitap-ai/mobile-use)、
> [mobilerun](https://github.com/droidrun/mobilerun)。
>
> 本仓库 [OtterScope](../README.md) 是 Windows 桌面端 GUI Agent。本文目的不是
> "评 5 个移动 Agent 谁最好"，而是**抽出能搬到 OtterScope 的设计模式与可复用模块**，
> 同时把它们也作为竞品视野记录（与 [competitor-analysis.md](competitor-analysis.md) 互为补充）。

---

## 1. 项目一句话画像

| # | 项目 | 团队 | 平台 | 模型策略 | 核心卖点 |
|---|------|------|------|----------|----------|
| **1** | **MobiAgent** | 上海交大 IPADS-SAI | Android（含 on-device 4B/7B 推理） | 自研 MobiMind（Decider + Grounder 双模型，可选 mixed 单模型） | **Record & Replay 加速 (AgentRR)** + 自带 milestone DAG 评测 (MobiFlow) + user profile memory（Mem0 + GraphRAG） |
| **2** | **MobileAgent (X-PLUG)** | 阿里通义实验室 | Android / Windows / Mac | 自研 GUI-Owl-1.5（2B/4B/8B/32B/235B，Instruct + Think） | **多 Agent 协作 + 多代版本** (v1/v2/v3/v3.5/PC-Agent/Mobile-Agent-E)，已商用云手机 |
| **3** | **AppAgent** | 腾讯 QQGYLab + NTU | Android | GPT-4V / Qwen-VL-Max | **两阶段：Exploration → Deployment**；探索期写元素文档，部署期查文档 |
| **4** | **mobile-use** | minitap.ai (开源初创) | Android / iOS | LLM 可插拔（OpenAI/Anthropic/Gemini/Vertex） | **LangGraph 多节点架构**（cortex/executor/planner/hopper/contextor/outputter/summarizer）；100% AndroidWorld benchmark |
| **5** | **mobilerun** | droidrun (Berlin) | Android / iOS | LLM 可插拔（OpenAI/Anthropic/Gemini/Ollama/DeepSeek） | **CLI 优先 + Python SDK + MCP server**；app_cards 与 macro 模块；Arize Phoenix 执行 tracing |

## 2. 架构对比矩阵

| 维度 | MobiAgent | MobileAgent (v3) | AppAgent | mobile-use | mobilerun |
|------|-----------|------------------|----------|------------|-----------|
| **Agent 数** | 1 (Decider+Grounder 双模型) | 多 (Manager/Operator/Reflector/Notetaker) | 1（深度依赖元素文档） | 7 节点 LangGraph 子图 | 1 + planning 子代理 |
| **感知** | 截图 + 可选 OmniParser + Tesseract | 截图 + GUI-Owl 自带 grounding | 截图 + UIA tree 编号 (Set-of-Mark) | 截图 + accessibility tree | 截图 + UIA |
| **Grounding** | 独立 Grounder 模型 | 基模直出绝对/相对坐标 | 数字标注（数字标签或网格） | 模型 + tree 双源 | 模型 |
| **输入** | ADB Keyboard | ADB Keyboard / ADB tap | ADB | ADB / fb-idb | ADB / iOS bridge |
| **记忆** | Mem0 vector + Neo4j GraphRAG (user profile) + experience embed | notetaker tool 写笔记 | 探索期生成的"app/<name>/docs.json" | summarizer 节点定期总结 | telemetry / trajectory 持久化 |
| **加速** | **AgentRR** 录-放复用上次轨迹 | speculative multi-action（v3.5）；多 agent 并行 | 文档命中后直接套用（≈ 录放） | 节点级缓存 | macro 录制 |
| **评测** | **MobiFlow** (milestone DAG) | OSWorld / AndroidWorld 自家成绩 | 自带 testset.md | AndroidWorld 100% | 自家 91.4% benchmark |
| **观测** | 自带轨迹 viewer | 任务回放 | 截图日志 | 节点 trace | **Arize Phoenix** |
| **打包形态** | Python + Android APK + on-device runner | Python + 在线 demo + 阿里云 API | Python CLI | Python + Docker + npm-style script | `pip install mobilerun` + CLI |
| **License** | (见 repo) | (见 repo) | MIT | Apache 2.0 | MIT |

## 3. 各项目深度解读

### 3.1 MobiAgent（IPADS-SAI）—— 最学术、最完整的"系统化框架"

**模型设计**：把决策（"下一步该做什么"）和定位（"按钮在哪里"）拆成两个模型——
**Decider** 负责高层动作 + reasoning，**Grounder** 负责把"点搜索框"翻译成精确像素坐标。
也提供 mixed 单模型版（MobiMind-Mixed-7B / 4B）。所有模型基于 Qwen3-VL 微调，开源可下载。

**AgentRR（Agent Record & Replay）**：第一次完成任务后自动录下轨迹，下次接到相似任务
**直接回放**，跳过 LLM 调用。这是对"延迟"问题最直接的攻击——Demo 视频里第二次同任务
**几乎瞬时**完成。

**MobiFlow 评测**：定义任务完成的"milestone DAG"，按通过的关键节点而非完整轨迹计分；
比单纯"任务成功率"更细粒度，能定位"在哪一步掉链子"。

**User Profile Memory**：用 Mem0 (Milvus 向量库) + Neo4j (GraphRAG) 持久化"用户偏好"
（"我喜欢晚上十点后看 B 站""我的招行卡用账号 X 登录"），下次同一用户的同类任务能秒拿
context。

**给 OtterScope 的可借鉴**：
1. **AgentRR 思路 → OtterScope templates / skills 升级**：现在的 templates 是手写一句 instruction，
   理想是录一次任务自动转成"参数化轨迹模板"，下次匹配到就回放（详见
   [todo.md](todo.md) Phase 2 "Skills 系统"和"App 区域化坐标库"）。
2. **MobiFlow milestone DAG → OtterScope 任务可观测性**：当前 events.jsonl 是平铺事件，
   可加一层"用户/模型预声明的 milestones"，UI 上显示"5/7 关卡已过"。
3. **Decider+Grounder 双模型 → OtterScope 未来本地 grounding fallback**：当云端 Claude
   点不准小图标时，本地跑 UI-TARS-7B / OS-Atlas / GUI-Owl-7B 做 grounding。

### 3.2 MobileAgent（X-PLUG / 通义实验室）—— 商业化最深、版本最丰富

X-PLUG 在 2024–2026 三年里出了 7 代相关项目，每代主题不同：

| 版本 | 主题 | 关键创新 |
|------|------|----------|
| Mobile-Agent-v1 (ICLR 2024 W) | Single-agent baseline | 朴素 ReAct + screenshot |
| Mobile-Agent-v2 (NeurIPS 2024) | **多 Agent 协作** | Planning + Decision + Reflection 三角 |
| Mobile-Agent-v3 (2025.8) | **多模态多平台** | GUI-Owl-7B/32B 自研基模；Manager+Operator+Reflector+Notetaker；可跑 OSWorld + AndroidWorld |
| Mobile-Agent-v3.5 (2026.2) | **跨平台 + 长时记忆** | GUI-Owl-1.5 (2B–235B)；Instruct/Think 双版本；MCP tool calling |
| **PC-Agent** (ICLR 2025 W) | **PC 端**多 agent | 主动感知模块 (active perception) for dense PC UI；任务分解 + reflection |
| Mobile-Agent-E | **自进化** | Long-horizon tasks，agent 之间互教 |
| GUI-Critic-R1 (NeurIPS 2025) | **预操作错误诊断** | 在执行前用 critic 模型预判"这一步会不会出错"，省掉错-改的 round-trip |
| UI-S1 (ACL 2026) | **半在线 RL** | semi-online RL 训练 GUI agent |

**给 OtterScope 最对症的两个**：

- **PC-Agent 的"主动感知模块"**：PC 桌面元素密集（密密麻麻的菜单、工具栏），
  靠模型一次截图扫不全。PC-Agent 的做法是**Agent 主动指定"我要看这个区域"**，
  对应 OtterScope 的 [Docs/screenshot.md §10 方案 A](screenshot.md#方案-a自定义-region任意矩形-l4) "region 任意框"——这是 PC 端必须做的，移动端可以不做。
- **GUI-Critic-R1 的"预操作 critic"**：在 click 之前**单独跑一个轻量 critic**
  判断"这一步合不合理"，比 OtterScope 当前的"两阶段 click preview"省一次截图、省一次模型推理。
  虽然 critic 也是模型调用，但可以用更小的 critic 模型（7B 而不是 Opus），延迟更低。
  对应 OtterScope [todo.md](todo.md) Phase 2 "**多 Agent 设计：planner / checker / executor 三角**" 中的 checker 角色。

### 3.3 AppAgent（腾讯 QQGYLab，CHI 2025）—— "探索-部署"两阶段范式

**核心设计**：把 Agent 的生命周期切成两段：

1. **Exploration phase** (`learn.py`)：
   - **自主探索** 或 **看用户演示一遍**
   - 每次跟某个 UI 元素交互后，模型生成一段"这个元素是干啥的"文档，落到 `app/<name>/docs.json`
   - 形成"App 元素字典"（按 ID + 截图位置 + 文字描述）
2. **Deployment phase** (`run.py`)：
   - 接到任务后，先查"我有这个 App 的文档吗"，有就**用文档回答"我现在该点哪个 ID"**
   - 没文档也能跑（success rate not guaranteed）

**Grounding 方案**：用 SoM (Set-of-Mark) 把 UI 元素打上数字标签 (`1`/`2`/`3`...) 给模型选；
点不准时还有"网格覆盖"兜底（屏幕铺 10×10 网格，模型说"点 row=4 col=7"）。

**给 OtterScope 的可借鉴**：
1. **两阶段范式 = OtterScope 的 "App 区域库 + 学习反馈"**：
   - Phase A 类比 OtterScope [todo.md "App 区域化坐标库"](todo.md) 的"首次校准"
   - Phase B 类比 OtterScope [todo.md "launch_app meta tool"](todo.md) + region 查表
   - 已经能把"打开 X、点 X 的 Y 按钮"从 4-6 步压到 1-2 步
2. **元素文档 = OtterScope 的 tooltips / icon_memory 升级版**：
   - 当前 OtterScope 只学"图标外观→功能"（icon_memory.py）
   - AppAgent 学"元素 ID + 上下文 + 操作后果"，更结构化
   - 可以让 OtterScope 的 `learn_tip` 进化成 `learn_element(app, region, description, action_outcome)`
3. **Set-of-Mark / 网格 = grounding 兜底**：当 Claude 给的坐标偏 ±20px 时，
   OtterScope 可以画一张"红色十字 + 数字 1-9"覆盖在目标周围，让模型选数字（替代 region 截图）。
   实现成本极低（PIL 画框），值得加进 [Docs/screenshot.md §10 方案 K "缩略图陪伴"](screenshot.md#方案-f返回时附带屏幕全局缩略图--你点的那块高清图) 之后。

### 3.4 mobile-use（minitap.ai）—— LangGraph 工程化最优雅

**架构**：完全用 LangGraph 拼成一张 7 节点的子图：

```
planner ──┐
          ├──► cortex ──► executor ──► (loop)
contextor ┘                    │
                               ▼
hopper ◄──────────── outputter ──► summarizer
```

- **planner**：把高层任务拆成 sub-goals
- **cortex**：核心 ReAct，决定下一动作
- **executor**：调 controller 真打鼠标键盘
- **hopper**：sub-goal 切换 / 跳步
- **contextor**：组装上下文（截图 + tree）
- **outputter**：结构化输出（如 JSON 抽取）
- **summarizer**：定期总结历史

**LLM 配置**：每个节点可指定不同 model（cortex 用强模型、summarizer 用便宜模型），
落在 `llm-config.override.jsonc`。**这就是节省 token / 钱的核心招数**。

**Skills 目录**：`skills/` 下放可复用动作模板（用 yaml/markdown 定义参数化步骤），
对应 OtterScope [todo.md "Skills 系统"](todo.md) 设想——mobile-use 已经做了。

**100% AndroidWorld**：是首个跑通 AndroidWorld 全部任务的开源 agent，工程上确实很扎实。

**给 OtterScope 的可借鉴**：
1. **LangGraph 节点拆分 → OtterScope 多 Agent 三角的实现底座**：
   - 当前 OtterScope 是单 ReAct 循环
   - [todo.md "多 Agent 设计：planner / checker / executor 三角"](todo.md) 完全可以用 LangGraph
     落地，省掉自己造状态机
2. **每节点独立 LLM 配置**：summarizer / checker 用 Haiku 或本地 7B 模型，
   主推理用 Opus，**单任务成本可降一个量级**
3. **Skills 目录 → templates 升级**：
   - 当前 OtterScope templates 是单条 instruction
   - 可以改成 skills 目录 (`%LOCALAPPDATA%\dev.otterscope\skills\<slug>.yaml`)，
     每个 skill 含 params + steps 数组，模型用 `run_skill(name, params)` 调用
4. **Docker 一键启动**：mobile-use 提供 Docker 镜像，用户不需要装 Python；
   OtterScope 已经走 Tauri 安装包路线，但 Python sidecar 仍需 PyInstaller 打包——可以借鉴
   mobile-use 把"启动 sidecar 之前的环境检查"做得更友好

### 3.5 mobilerun（droidrun）—— SDK + CLI + MCP 三件套

**形态最像 OtterScope 的开发哲学**：`pip install mobilerun` → `mobilerun setup` → `mobilerun configure` → `mobilerun run "..."`
三步上路，CLI/SDK/MCP-server 三种用法。

**模块切分**（`mobilerun/agent/`）：

- `droid/` —— Android driver
- `executor/` —— 动作执行
- `oneflows/` —— 单步 ReAct loop
- `manager/` —— 任务管理
- `providers/` —— LLM 适配（OpenAI/Anthropic/Gemini/Ollama/DeepSeek）
- `trajectory/` —— 轨迹持久化
- `fast_agent/` —— 快速路径（疑似缓存命中走这条）
- `tool_registry.py` —— tool 注册中心

`app_cards/` 单独建模 "对每个 App 我知道什么"——和 OtterScope 的 launchers.json + 区域库
**几乎一对一映射**。

**Telemetry**：用 [Arize Phoenix](https://github.com/Arize-ai/phoenix) 做分布式 tracing，
把每次 LLM 调用 + tool_call + 时间线可视化。比 OtterScope 当前的 `events.jsonl` + 手翻 log 更舒服。

**给 OtterScope 的可借鉴**：
1. **app_cards 几乎是 launchers.json 的同义词**——证明这条路是行业共识
2. **Arize Phoenix 集成 → OtterScope 调试可视化升级**：
   - 当前 OtterScope 的 events.jsonl 只能在前端聊天流里看
   - 加一层 Phoenix 适配（OpenInference 协议，~50 行）就能在 Phoenix UI 里看到
     每步的 token 消耗、延迟、tool 调用树、prompt diff
   - 对调试 "为什么这次比上次慢/贵" 这种问题非常有用
3. **fast_agent 路径**：明确把"快路径"（缓存/规则/模板命中）和"慢路径"（LLM ReAct）
   写成两个模块，避免在一个函数里 if/else 横飞——值得在 OtterScope loop.py 早期 split

## 4. 移动端 vs OtterScope（桌面端）的关键差异

| 维度 | 移动端常见做法 | OtterScope（桌面端）做法 / 取舍 |
|------|----------------|---------------------------|
| **窗口切换** | App 全屏独占；切应用 = 回 home + 点 icon | 多窗口并存；可拖动重叠 → OtterScope 需要 `launch_app` + 窗口枚举 (§12.3) |
| **元素定位** | accessibility tree 信息丰富，UIA 几乎可用 | 微信/QQ 自绘，UIA 树空 → OtterScope 退回纯视觉 (§12.4) |
| **截图** | 屏幕小、单 DPI、单显示器 | 多显示器 + DPI 缩放 + 大屏密集 → OtterScope 三级金字塔 + region |
| **输入** | ADB Keyboard 标准 IME 通路 | Win32 SendInput / 剪贴板粘贴 / IME 处理 |
| **延迟容忍** | 用户预期低（点-反应感） | 用户预期更高（鼠标-反应感）→ OtterScope 必须更激进减 round-trip |
| **任务复杂度** | 短任务多（"打开微信发条消息"） | 长任务多（"读 PDF 写邮件" / 多窗口协作）→ OtterScope 必须做 context 压缩 (§4-5) |
| **HITL** | 移动端罕见（移动操作快） | OtterScope 必备（误操作代价大）→ 两阶段 click preview |
| **变现** | 多走云手机/SaaS | OtterScope 走桌面安装包 + 自带模型 → 离线、隐私、可控 |

## 5. 优先级清单：OtterScope 应该立即吸收哪些

按 ROI 排序：

| # | 借鉴对象 | 来自 | 工程成本 | 预期收益 | OtterScope 对应位置 |
|---|----------|------|---------|---------|----------------|
| **1** | **每节点独立 LLM 配置** | mobile-use | ~30 行 | 单任务成本降 50%+ | [llm.py](../python/otterscope/llm.py) 加 model_for_role 参数 |
| **2** | **app_cards / launchers.json** | mobilerun, AppAgent | ~100 行 | "打开 X" 4 步→1 步 | [todo.md "launch_app meta tool"](todo.md) |
| **3** | **GUI-Critic 风格 pre-action checker** | MobileAgent v3 | ~80 行 + 一个小模型调用 | 减少错-改 round-trip | [todo.md "多 Agent 三角"](todo.md) checker 角色 |
| **4** | **AgentRR 录放** | MobiAgent | ~150 行 | 重复任务秒级回放 | 与 templates / skills 合并设计 |
| **5** | **Arize Phoenix tracing** | mobilerun | ~50 行 | 调试可视化质变 | events.jsonl 旁加 OpenInference exporter |
| **6** | **AppAgent 元素文档** | AppAgent | ~120 行 | learn_element 进化 | 升级 icon_memory.py + tooltips.py |
| **7** | **Set-of-Mark / 网格 grounding 兜底** | AppAgent | ~30 行 (PIL) | 小图标点击 +20% 命中 | [Docs/screenshot.md §10](screenshot.md#方案-a自定义-region任意矩形-l4) 之后追加 |
| **8** | **MobiFlow milestone DAG** | MobiAgent | ~200 行 | 任务可观测质变 | 用户/模型预声明 milestones |
| **9** | **PC-Agent 主动感知** | MobileAgent | 已规划 | 桌面密集 UI 提准 | = [Docs/screenshot.md §10 方案 A region](screenshot.md#方案-a自定义-region任意矩形-l4) |
| **10** | **Skills 目录** | mobile-use | ~100 行 | templates 进化 | [todo.md "Skills 系统"](todo.md) |

## 6. 一句话总结

> **移动端 GUI Agent 在 2024–2026 已基本走通**：多 Agent 协作、双模型 grounding、
> 录放加速、user memory、节点化 + 可观测——这些模式都被验证有效。**OtterScope 作为
> 桌面端项目，应该有意识地"借力"，而不是从 0 重发明**。
> 最优先吸收三件事：(1) **launch_app + app_cards**（mobilerun/AppAgent 的 app_cards），
> (2) **每节点独立 LLM 配置**（mobile-use 的 LangGraph），(3) **AgentRR 风格录放**
> （MobiAgent 的轨迹复用）。这三件做完，OtterScope 在"速度 + 准确率 + 成本"三个维度
> 都能立刻拉开和当前架构的差距。
