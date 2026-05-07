# 任务栏中部视觉通知识别方案（两步法）

## 背景与目标

在当前环境中，Teams 和 WeChat 的提醒无法稳定通过 Windows 操作中心获取，因此采用纯视觉路径。

本方案遵循固定两步，不增加额外锚点定位代码：

1. 先做任务栏截图 diff（低成本）
2. 有 diff 后再调用大模型确认是否确实有新消息（高成本）

## 设计原则

- 不做每帧大模型调用，先用纯图像 diff 过滤。
- 不新增“单独锚点定位”主链路代码。
- 复用已有 icon 识别能力（icon memory / atlas）给大模型做参考。
- v1 先保证低成本和可用性，再做精细化。
- 触发用定时器，这个可以默认开启，每隔1分钟就检测

## 检测区域

仅截取 L1 全屏图中的底部中部窄带：

- 垂直范围：屏幕底部最后 120 像素。
- 水平范围：屏幕中间 50%（x 从 25% 到 75%）。

设计理由：

- 覆盖 Windows 11 默认居中的任务栏图标区。
- 把 diff 与模型分析都限制在小图区域，降低成本与误报。

## 两步流程

### Step 1：截图 diff（常态执行）

- 每隔 `poll_interval_sec` 截取一次任务栏窄带。
- 与上一帧做图像差异比对（dHash 或像素差异均可）。
- 如果差异低于阈值：直接结束本轮，不调用大模型。
- 如果差异超过阈值：进入 Step 2。

输出字段（内部）：

- `changed: bool`
- `diff_score: float`
- `strip_rect: {x,y,w,h}`

### Step 2：有 diff 才调用大模型确认（按需执行）

- 输入：当前任务栏窄带截图 + （可选）上一帧截图。
- 同时注入已有 icon atlas（来自 icon memory），让模型先识别任务栏中相关 App 图标。
- 模型只回答一件事：是否出现“新消息迹象”。

建议输出结构：

- `has_new_message: bool`
- `app_candidates: ["teams"|"wechat"|"unknown"]`
- `confidence: float`
- `reason: str`（简短）

## 严格版架构（Strict Mode）

为确保架构清晰性，diff 检测与任务队列完全分离：

### 检测层（Detection Layer）- 在队列外运行

1. Scheduler 每 2 秒触发一次 `visual_notify` 定时任务。
2. TaskbarMonitor 执行 Step 1（diff）+ Step 2（LLM 确认）。
3. **整个检测管道独立于队列系统，不占用任务工作线程，不受队列忙碌/排队影响。**
4. 检测结果落日志（事件流）：`taskbar_diff_detected`、`taskbar_notify_confirmed`、`taskbar_notify_rejected`。

### 入队层（Enqueueing Layer）- 只在确认时触发

当 LLM 返回 `has_new_message=true` 时：

1. 触发 `_on_taskbar_notify_confirmed(payload)` 回调，`payload` 已带
   `app_candidates`、`confidence`、`reason` 等 Step 2 输出。
2. 检查去重：是否已有相同 `_from_visual_notify=True` 的待处理任务在队列中。
3. 若有去重命中，直接跳过（避免多个视觉通知任务堆积）。
4. **App 注入**：把 `app_candidates`（去重 + 过滤 `unknown`）拼成一行追加到
   `auto_chat_instruction` 末尾，让 Agent 直接奔已识别 App，不再盲探。
5. **独立 thread**：用 `ThreadLog.create()` 单独建一条 thread（标题带 App
   名），通过 `_thread=` 传给 `_rpc_start_task`，避免空闲分支
   `_ensure_active_thread()` 复用用户当前打开的 thread。
6. 入队，优先级 `priority=2`（低于人工任务）。

### 去重与冷却

- **去重**：任何时刻队列中最多存在 1 个 `_from_visual_notify=True` 的任务。
- **冷却**：由 LLM Step 2 的 `llm_confirm_cooldown_sec` 控制，防止频繁重复触发 LLM。
- **优先级**：视觉通知的自动对话任务优先级固定为 2（最低），永不抢占人工任务或紧急任务。

### 状态转换图

```
┌─────────────────┐
│    LISTENING    │  Scheduler 2-sec tick, diff 轮询（队列外）
└────────┬────────┘
         │ [diff detected]
         ↓
┌─────────────────┐
│    DIFF_HIT     │  Step 1 命中，进入 Step 2 LLM 确认
└────────┬────────┘
         │ [LLM calling...]
         ├─→ [has_new_message=false] ─→ LISTENING
         │
         └─→ [has_new_message=true]
             ├─→ [duplicate in queue?] ──→ LISTENING（skip）
             │
             └─→ [unique, enqueue] ──→ HANDLE_MESSAGE
                                           │ (queue 处理)
                                           │ (等待工作线程)
                                           ↓
                                      (executing...
                                       auto reply...)
                                           │
                                           ↓
                                      BACK_TO_LISTENING
```

## 事件契约

向 sidecar 事件流/日志输出：

- `taskbar_diff_detected`（Step 1 命中）
- `taskbar_notify_confirmed`（Step 2 判定有新消息）
- `taskbar_notify_rejected`（Step 2 判定无新消息）

建议 payload：

- `ts_ms`
- `event`
- `screen`（`w`, `h`）
- `strip_rect`（`x`, `y`, `w`, `h`）
- `diff_score`（Step 1）
- `has_new_message`（Step 2）
- `app_candidates`（Step 2）
- `confidence`（Step 2）

## 配置草案

```toml
[visual_notify]
enabled = true
poll_interval_sec = 2.0
strip_height_px = 120
strip_center_width_ratio = 0.50

# Step 1: diff 阈值
diff_method = "dhash"          # dhash | pixel
diff_threshold = 8.0            # 仅示例，需实测调参

# Step 2: 模型确认
llm_confirm_enabled = true
llm_confirm_on_diff_only = true
llm_confirm_cooldown_sec = 5.0  # 防抖，避免连续触发
```

## 与现有能力的关系

已存在的 icon 识别相关能力可直接复用，不需要新增锚点定位链路：

- `python/ctrlapp/icon_memory.py`：图标记忆与 atlas 构建
- `python/ctrlapp/loop.py`：会把 icon atlas 注入到模型上下文

本方案中它们的作用是：在 Step 2 里提升模型识别任务栏图标与消息迹象的准确性。

## 迭代计划

### v1（当前目标）

- 打通两步链路：`diff -> LLM确认`。
- 不做自动点击、不做自动拉起 App。
- 事件落日志，并在调试流可见。

验收标准：

- 无明显变化时，不触发模型调用。
- 有明显任务栏变化时，能触发模型确认并输出结构化结果。
- 端到端延迟 < 5 秒。

### v2（稳态优化）

- 调优 diff 阈值与 cooldown，降低抖动触发。
- 增加场景样本，优化模型提示词，降低误判。

### v3（接入决策链）

- `taskbar_notify_confirmed` 后，触发一次定向 L2/L3 截图。
- 由 Agent 决策是否打开 App 查看会话。

## 自动对话闭环（监听 -> 查看 -> 对话 -> 继续监听）

在两步法之上，推荐把自动流程做成状态机：

1. `LISTENING`
- Taskbar monitor 常驻轮询，仅执行 Step 1 diff。
- 无 diff 时保持监听，不调用 LLM。

2. `DIFF_HIT`
- 命中 `taskbar_diff_detected`。
- 进入 Step 2，调用 LLM 做“是否新消息”确认。

3. `CONFIRMED`
- 若返回 `has_new_message=true`，触发 `taskbar_notify_confirmed`。
- 若 `auto_chat_enabled=true` 且当前无运行任务/排队任务，自动下发一条任务指令。

4. `HANDLE_MESSAGE`
- Agent 执行固定动作链：
	- 打开对应 App（Teams/WeChat）
	- 查看最近未读
	- 生成并发送回复
	- 记录结果（成功/失败/原因）

5. `BACK_TO_LISTENING`
- 任务结束后自动回到监听态。
- 继续 Step 1 diff 轮询。

### 推荐自动任务指令模板

可放在 `visual_notify.auto_chat_instruction`：

```text
检测到任务栏可能有新消息。请先查看 Teams/WeChat 最近未读消息，基于上下文给出简短自然回复并发送。完成后返回继续监听。
```

> **App 注入（自动）**：sidecar 在入队前会把 Step 2 LLM 返回的 `app_candidates`
> （去重并过滤 `unknown`）拼成一行追加到上述模板末尾，例如：
>
> ```text
> [detector] 任务栏 diff + LLM 确认本次最可能产生新消息的 App: Teams / WeChat。请优先打开并查看这些 App 的最新未读。
> ```
>
> 这样 Agent 不必再"探每一个聊天 App"，直接奔具体目标。模板里**不需要**手写
> 占位符，留空即可，或干脆把硬编码的 "Teams/WeChat" 删掉，让 detector 行成
> 为唯一的 App 来源。

### 独立 thread

每次 `taskbar_notify_confirmed` 入队前，sidecar 都会用 `ThreadLog.create()`
新建一条独立 thread（标题形如 `🔔·Teams/WeChat <模板前 32 字符>`），并通过
`_thread=` 参数传给 `_rpc_start_task`。这样无论：

- sidecar 当前**正在跑**别的任务（走入队分支，本就会建独立 thread），还是
- sidecar **空闲**（曾经会 `_ensure_active_thread()` 复用用户当前打开的
  thread，把自动任务塞进去），

自动任务永远落在自己的 thread 里，不会污染用户正在浏览 / 编辑的会话。

### 关键防抖策略

- `llm_confirm_cooldown_sec`：防止短时间重复触发模型确认。
- 自动对话仅在“当前无运行任务且队列为空”时触发，避免并发打架。
- 任务失败不终止监听，仍返回 `LISTENING`。

## 风险与缓解

风险：任务栏动画或系统元素微小变化导致误触发 Step 2。
缓解：调高 diff 阈值 + 设置 `llm_confirm_cooldown_sec`。

风险：模型把非消息变化误判为新消息。
缓解：强化 Step 2 输出格式，要求给出 `reason` 与 `confidence`，并结合历史状态做二次过滤。

风险：icon atlas 不完整导致模型识别不稳定。
缓解：持续通过 icon memory 补齐常见图标样本。

## 为什么移除 WinRT 通知监听

基于当前观察：

- Teams/WeChat 提醒在该环境中无法稳定映射为操作中心可读数据。
- WinRT 监听增加依赖与维护成本，但未解决核心路径。

因此优先采用“任务栏视觉两步法”（diff 过滤 + 模型确认）。