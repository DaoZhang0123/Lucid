# 打盹学习 (Doze Learning)

> 状态：**v0 设计 + 文本通道落地**（icon 通道与心跳计划在 v1）。
> 配置入口：`[doze]`（默认 `enabled = false`，需要用户在 settings 中开启）。
> 代码：`lucid/doze.py` + `lucid/sidecar.py` 集成点。

---

## 1. 动机

任务跑完之后，`thread/` 目录里堆着大量原始素材：`events.jsonl`（每步 user / assistant /
tool_call / tool_result）、`context.log`（每次发往 LLM 的完整 messages，图像被替换成
本地 PNG 文件名）、`step-*.png`（截图）。**它们目前只用于回放与调试，没有被反馈到长期
记忆**。模型在任务里发现的成功路径 / 失败教训 / 新图标含义，如果当时没有显式 `learn_tip`
/ `remember` / `remember_icon`，就永远丢失了。

打盹学习的核心思路：**当 sidecar 空闲时（用户不在交互 + 没有排队任务 + 没有定时任务在跑），
拉一个低优先级的"反思 agent"，扫描最近未处理过的 thread，把可复用的知识写回
`tools.md` / `apps/<slug>/tips.md` / `memory.md` / `icons/`。**

这样：

- 用户主动用了一次"Win+R 输 `outlook` 打开 Outlook"——下次起任务时这条已经在 tips 里。
- 模型在某 thread 里反复点错某按钮、绕了一圈才发现快捷键——失败教训会被沉淀。
- 系统托盘里的某个未识别小图标在 thread 末尾被模型口头判定为"应该是网易云"——下次起手
  atlas 里就有它。

---

## 2. 何时打盹（idle detection）

打盹工作线程跟随 sidecar 启动（如果 `[doze].enabled = true`）。每 `tick_interval_sec`
（默认 60 秒）醒来检查：

1. **没有运行中的 worker**（`self._worker is None or not self._worker.is_alive()`）；
2. **任务队列为空**（`len(self._queue) == 0`）；
3. **距离最后一次"用户活动"超过 `idle_threshold_sec`**（默认 300 秒 / 5 分钟）；
4. **当前不在打盹中**（`self._doze_running == False`）。

四个条件同时满足 → 起一次 doze pass。

"用户活动"定义为以下事件被触发时 `_last_activity_ms` 被刷：

- `start_task` / `cancel` 等 RPC
- `thread_new` / `thread_set_active` / `thread_delete`
- 任务自然完成（`task_close` 事件）
- **不算**活动：`ping` / `get_status` / `thread_list` / `thread_read`（这些是 Tauri 前端
  打开窗口时的轮询，不应该重置打盹倒计时）

> **协作式取消**：打盹运行期间任何"用户活动"事件触发时，会 `set()` `_doze_cancel`
> 标志，doze worker 在每一轮 LLM 调用前后都会检查并提前退出。**用户绝不会等打盹**。

---

## 3. 一次 pass 做什么

### 3.1 选 thread

打盹 worker 维护 `<user data>/doze_processed.json`：

```jsonc
{
  "items": [
    {"thread_id": "thread-…-记事本写文案", "processed_ms": 1764..., "version": 1,
     "outcomes": {"tips": 2, "memories": 0, "icons": 0}},
    ...
  ]
}
```

候选规则：

- 必须存在 `events.jsonl` 且至少有一次 `task_close` 事件（即 thread 真的跑过任务）；
- `thread_id` 不在 `processed.json` 中（或 `version` 早于当前 doze prompt 版本，便于
  prompt 升级后重学）；
- 按 `updated_ms` 降序，最近的 thread 先学（最相关）；
- 单次 pass 最多处理 `max_threads_per_pass` 个（默认 1，避免占满 LLM 配额）。

### 3.2 拼 prompt

打盹**不调用 GUI 工具**——模型只读历史，不动鼠键。所以 prompt 体积可以非常紧凑：

1. **System**: 一段固定的"反思官"角色定义（见 §4）。
2. **User**: 单条文本消息，结构化喂入：
   - thread 标题 + 最终 status
   - **现有 tips 摘要**（全局 `tools.md` + 已 load 的 per-app tips，仅前 K 行 + 哈希指纹），
     用于让模型显式去重
   - **现有 memory 摘要**（同上）
   - **events 时间线**（去图、压缩格式）：每条只保留：
     - `step`/`role`/`event` 类型
     - `assistant` 文本（截断到 `max_event_text_chars`）
     - `tool_call` 函数名 + arguments（截断）
     - `tool_result.output` 头/尾各 200 字符
   - **末尾 1–3 张关键截图的描述**（仅文件名 + 维度，不嵌入 base64；v1 才考虑发图给模型）
3. **Tools**: `learn_tip` + `remember` + `load_app_tips`（v0 不开 `remember_icon`，
   因为打盹路径下没有 `last_png_by_level` 上下文，crop 坐标也没意义）。

### 3.3 跑 ReAct

调 `LLMClient.chat(messages, tools, max_tokens)`：

- 模型可以发零到 `max_tool_calls_per_pass`（默认 6）个 tool_call；
- 每个 tool_call 走 `meta_tools.dispatch_meta_tool`（**复用现有 dispatch**——同样的
  幂等性 / 长度上限保护）；
- 文本 reply 被忽略（仅记录到 `doze.log`），doze 不持久化对话历史；
- 单 thread 最多 `max_rounds_per_thread` 轮（默认 2，保留模型"补一刀"机会但不发散）。

### 3.4 收尾

- 把本次 pass 的 `{calls, outcomes, errors}` append 到
  `<user data>/doze.log`（纯文本，便于人审）；
- 把 `thread_id` 写入 `processed.json`；
- 触发 `doze_pass_done` 事件（前端可选展示）。

---

## 4. 反思官 system prompt 草稿（v0）

```
You are lucid's "doze reflector": a low-priority background reviewer that runs while
the user is idle. Your job is to read ONE past task transcript and decide what
reusable knowledge (if any) should be promoted to long-term storage.

You CANNOT control the GUI. The only tools available are:
  - learn_tip(text, kind, app?) — append to tools.md / apps/<slug>/tips.md
  - remember(text)              — append to memory.md
  - load_app_tips(app)          — read existing per-app tips before writing

Strict rules:
  1. Be conservative. If unsure, write nothing. Bad tips are worse than missing tips.
  2. Deduplicate against the "Existing tips" / "Existing memory" digests in the user
     message. If something close already exists, skip it.
  3. Tips must be concrete and actionable: "<App>: <action> via <method>" or
     "<App>: avoid <pitfall> because <reason>". No vague advice.
  4. Memory entries must be user-stable facts (preferences, naming, paths). Do NOT
     record one-shot task facts.
  5. At most 3 tips and 1 memory entry per pass.
  6. After your tool_calls, reply ONE short sentence summarising what you wrote.
```

---

## 5. icon 通道 (v1, 计划)

v0 不动 icon。v1 要做的事：

- 在 prompt 末尾把每张 `step-*.png` 的元数据（步号 + 文件名 + dim）列出来；
- 加一个新 meta tool `propose_icon(label, description, thread_id, step, x, y, w, h)`
  让反思官指出"我在第 N 步的 L1 截图里发现一个未识别小图标在 (x,y,w,h)，建议
  起的标签是 <label>"；
- doze worker 收到 propose_icon 后，**不直接写入 icons/**，而是：
  - 解码对应 `step-*.png`，按 (x,y,w,h) crop 出 PNG；
  - dHash 与现有 atlas 比对，距离 < 阈值则跳过（已存在）；
  - 否则 push 到 `<user data>/icon_proposals.json`（待用户在 `/icons` 页面一键确认 / 拒绝）。

这样既不会污染 atlas（只有用户点确认才入库），又能让模型在睡觉时帮忙做 icon 收集。

> **为什么不直接登记？** 现在的 `remember_icon` 强校验 crop 必须来自 `last_png_by_level`，
> 且需要模型亲眼看过那张图。打盹时模型只看到事件文本，crop 坐标完全没法验证；走"提案
> 池 + 用户审核"是更安全的兜底。

---

## 6. 心跳 (v1, 计划)

`memory.md` design 里早就预留了"心跳" hook（每 N 步反思一次）。打盹与心跳是同一思路在
不同时间窗的两个落点：

| 维度 | 心跳 | 打盹 |
| --- | --- | --- |
| 触发 | 任务运行中每 N 步 | 任务全部结束后空闲 5 分钟 |
| 输入 | 当前 messages 尾段 | 整个 thread `events.jsonl` |
| 优先级 | 与执行 agent 同 | 最低，可被任意 RPC 抢断 |
| 输出 | 注入回当前 thread 作为 `[reflection]` user msg | 直接写 `tools.md` / `memory.md` |
| 工具 | 仅文本反思（无 tool） | learn_tip / remember |

v1 会让打盹与心跳共享 `lucid/reflector.py`，差别只在触发器与上下文窗口。

---

## 7. 观测 / 调试

- `<user data>/doze.log`：每次 pass 的入参 / tool_call 列表 / 写入结果，文本格式可直接打开。
- `<user data>/doze_processed.json`：处理过的 thread 索引（人工删它即可让某 thread 重学）。
- 事件流（stdout NDJSON）：
  - `doze_idle_start` `{"idle_sec": …, "candidates": N}`
  - `doze_thread_picked` `{"thread_id": …, "title": …}`
  - `doze_pass_done` `{"thread_id": …, "outcomes": {…}, "elapsed_ms": …}`
  - `doze_interrupted` `{"reason": "user_activity"}`
  - `doze_error` `{"thread_id": …, "message": …}`
- 新 RPC：
  - `doze_status` → `{"enabled":bool, "running":bool, "last_pass_ms":..., "processed_count":N, "next_check_in_sec":...}`
  - `doze_run_now` → 强制立刻跑一次 pass（忽略 idle 阈值，但仍尊重 cancel）
  - `doze_clear_processed` → 清空 `processed.json`，下次 pass 重学全部

---

## 8. 安全边界

- **永远不会触发鼠键**——dispatch_meta_tool 只白名单 `learn_tip / remember / load_app_tips`；
  其它工具名直接 `unknown tool`。
- **不会发图**——v0 prompt 里没有 base64 图，token 成本可控（单 thread 估算 < 5k tokens
  输入 + < 1k tokens 输出）。
- **拿不到敏感字段**——打盹默认不读 `messages.json`（持久化的对话尾段，可能含密码）；
  只读 `events.jsonl`（已经过 sidecar 序列化时的脱敏，且不含 base64 图）。
- **可关**——`[doze].enabled = false` 是默认值。

---

## 9. 配置一览

```toml
[doze]
enabled = false                # 默认关，用户在 /settings 显式开启

# 何时打盹
idle_threshold_sec = 300       # 至少空闲 5 分钟才考虑
tick_interval_sec = 60         # idle 检查频率

# 单次 pass 限额
max_threads_per_pass = 1       # 一次只学一个 thread，慢慢来
max_rounds_per_thread = 2      # 单 thread 最多 2 轮 LLM 调用
max_tool_calls_per_pass = 6    # 全局 tool_call 上限
max_event_text_chars = 600     # events.jsonl 单条文本截断
max_tips_digest_lines = 30     # 喂给模型的"已有 tips"摘要行数
max_memory_digest_lines = 30   # 同上

# LLM 配额
max_tokens = 1500              # 反思 reply 最长 token
processed_path = "doze_processed.json"
log_path = "doze.log"
```

---

## 10. 与现有功能的关系

| 现有 | 关系 |
| --- | --- |
| `learn_tip` / `remember` / `remember_icon` | 复用 schema 与 dispatch；打盹是 caller 切换。 |
| `apps/<slug>/tips.md` | 反思官可以指定 `app=` 路由到 per-app tips。 |
| `icon_memory` atlas | v0 不写；v1 走 proposal 池 + 用户确认。 |
| `scheduler` | 不依赖 scheduler；打盹是独立的轻量 background tick。如果未来想做"白天空闲学，晚上不学"的窗口约束，可以把它折进 scheduler。 |
| `taskbar_monitor` | 完全独立。两者都是 background loop，互不干扰。 |
| `context_manager.compress_old_images` | 与之配合：thread 跑完后图片在前端是占位文本，但磁盘 PNG 仍在 `step-*.png`，给 v1 icon 通道用。 |
