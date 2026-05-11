# SKILL: Analyze a Lucid E2E run

> Use this skill when the user has just finished a Lucid E2E run (via
> [run.py](run.py)) and asks you to **analyze the results**, **compute pass
> rate**, **find regressions**, or **summarize what to fix next**.
> Inputs: a run directory under [runs/](runs/) containing `manifest.json`
> and `sidecar.stderr.log`. Outputs: `report.md`, `subagent-prompts/<id>.md`,
> `iteration-plan.md`, all written into the same run directory.

---

## Output language (mandatory)

The artifacts this skill produces — `report.md`, `iteration-plan.md`, any
prose you add inside `subagent-prompts/<id>.md`, and the final 5-bullet
hand-off in chat — **must be written in Simplified Chinese** (the user is
zh-CN). The skill itself is in English; only the *outputs* are Chinese.

Exceptions (keep these in English even inside Chinese prose):

- Status enums (`pass` / `fail` / `timeout` / `error` / `no-data` / `ok` /
  `max_steps` / `cancelled` / `api_error`) and `severity` / `category_of_fix`
  values (`prompt` / `tool` / `config` / `new-region` / `new-app-spec` /
  `bug-fix` / `low` / `medium` / `high`) — needed for downstream grep /
  clustering.
- File paths, `thread_id`, command lines, JSON field names (`status`,
  `final_text`, `expect_signal`, etc.).
- Quoted `instruction` text — keep verbatim in whatever language it was
  written; do not translate.
- The subagent prompt's role + output contract block (the
  "You are a VS Code Copilot subagent…" paragraph and the trailing
  `severity:` / `category_of_fix:` two lines) — subagents reply more
  reliably in English to an English contract.

---

## When to invoke

The user says one of:
- "analyze the latest E2E run"
- "跑完 E2E 了，看下结果"
- "compare this run to the previous baseline"
- "do the e2e analysis with subagents"
- "按 Test/SKILL.md 的流程，分析 Test/runs/<ts>/ 这一轮 E2E 结果"

If no run directory is given, list `Test/runs/` and pick the newest by
`mtime` (skip names beginning with `_`, those are smoke-tests).

## Required inputs

1. `<run_dir>` — the run directory, e.g. `Test/runs/20260511-142233/`.
2. Optionally `<baseline_dir>` — previous run for regression comparison.

If `<run_dir>/manifest.json` does not exist, stop and tell the user the run
is incomplete or the path is wrong.

## What `manifest.json` contains

`run.py` writes one entry per query. Trust these fields verbatim — do **not**
re-read events.jsonl just to recompute them:

```json
{ "id": "A1", "category": "cognitive", "instruction": "...",
  "expect_signal": "3297", "thread_id": "thread-...",
  "thread_dir": "C:\\Users\\...\\dev.lucid\\logs\\threads\\thread-...",
  "queued_ms": ..., "ended_ms": ...,
  "status": "ok|max_steps|error|api_error|cancelled",
  "final_text": "..." }
```

`status` and `final_text` are backfilled at end-of-run. If `status` is
`null`, the task did not produce a `task_close` event — likely killed by
the harness or never started; mark as `no-data`.

## Verdict rules (apply per query)

```
verdict =
  pass     if status == "ok"
              AND expect_signal (lowercase) is in final_text (lowercase)
              AND no negative marker in final_text
  fail     if status == "ok" but expect_signal missing OR negative marker present
  timeout  if status == "max_steps"
  error    if status in ("error", "api_error", "cancelled")
  no-data  if status is null
```

Negative markers: `task failed`, `任务失败`, `无法完成`, `cannot complete`.
Positive markers (only used as a soft fallback when `expect_signal == ""`):
`task complete`, `任务完成`.

`duration_s = (ended_ms - queued_ms) / 1000` if both are present.

> ⚠️ The sidecar runs **a single worker, serially**. `queued_ms` is the
> enqueue timestamp, so `duration_s` includes queue-wait. Compute an
> additional `exec_s` column = `ended_ms` − previous query's `ended_ms`
> (use `queued_ms` for the very first query) — that's the actual time the
> task spent executing. Prefer `exec_s` in the report; show `wall_s` next
> to it for reference.

## Procedure

### Step 1 — Read overview

1. Read `<run_dir>/manifest.json`.
2. For each query compute verdict + duration_s + `exec_s`. Keep the table
   in your scratch buffer.
3. Aggregate:
   - `pass / fail / timeout / error / no-data` counts
   - `pass_rate`
   - `p50 / p95 / max` of `exec_s` over **passing** queries only
4. (Optional) If `<baseline_dir>` was given, do the same for it and compute
   per-id `Δ duration_s`. Flag regressions where `Δduration > 30%`.

> Don't compute per-thread step / image / tool-call counts in §1 — those
> are subagent territory. Only do them if you have time.

### Step 2 — Write `report.md` (Chinese prose)

Write `<run_dir>/report.md` with these sections, in this order. Translate
the section labels below to Chinese in the actual file (template shown in
English here for clarity):

```markdown
# E2E 报告 —— <run_dir name>

## §1 汇总表
| id | category | verdict | status | exec_s | wall_s | expect | events |
|---|---|---|---|---:|---:|---|---|
| A1 | cognitive | **pass** | ok | 4.2 | 4.2 | ✓ | [events](<thread_dir>/events.jsonl) |
| ... |

## §2 总量
- 总数：N，pass P，fail F，timeout T，error E，no-data D
- 通过率：PCT%
- 通过任务的 exec_s：p50 = ...，p95 = ...，max = ...

## §3 基线对比（仅当用户提供 baseline 时）
| id | now | prev | Δ duration_s | regressed |
| ... |

## §4 失败 / 超时 / 出错的线程
For each non-pass thread: id, verdict, status, instruction (truncated),
link to events.jsonl. Do NOT inline events here — subagents read them.

## §5 慢尾（exec_s above p95）
List them so §3 fan-out covers slow passes too, not just failures.

## §6 下一步
"运行 Step 3（subagent fan-out）拿改进建议。"

## §7 下一轮重跑命令
把本轮 **非 pass**（fail / timeout / error / no-data 且有 thread_dir）
外加 **慢尾**（exec_s > p95，最多 5 条）的 id 拼成一条命令，方便用户直接
复制粘贴跑下一轮（可选地先把对应修复落地）：

```powershell
.\.venv\Scripts\python.exe Test\run.py --only <id1>,<id2>,...
```

如果一条都没有（全 pass 且没有慢尾），写一行 `本轮全过且无慢尾，无需重跑`。
```

### Step 3 — Fan out one subagent per relevant thread

Threads to fan out:
- **All** failed / timeout / error / no-data-with-thread-dir threads
  (highest signal).
- **Plus the slow tail**: passing threads with `exec_s > p95`, capped at 5
  to avoid swamping the IDE.
- Skip fast passes — the subagent will just say "nothing to fix".

For each selected query call `runSubagent`:

- `description`: `"E2E analyze <id>"` (English).
- `prompt`: the template below, fully filled in.

**Concurrency**: launch in batches of 4–6. Failures first, slow-tail last.

> Also write the same prompt to `<run_dir>/subagent-prompts/<id>.md` so a
> human can re-trigger one subagent later without re-running this skill.

#### Per-thread prompt template

The "scene info" block is in Chinese (easier for the human to skim
on disk); the role + output contract is English so the subagent reliably
ends with the two machine-readable lines.

```markdown
# E2E 线程分析 —— <id>（<category>）

**任务指令**：<instruction>

**结果**：`<verdict>` · status=`<status>` · exec_s=<exec_s> · wall_s=<wall_s>

**期望信号**：`<expect_signal>` —— <HIT|MISS>

**final_text**：
```
<final_text, truncated to 600 chars>
```

**事件文件**：`<thread_dir>\events.jsonl`

---

You are a VS Code Copilot subagent. Read the events.jsonl above (one JSON
event per line; use read_file with large line ranges). Then answer 3
questions in **Simplified Chinese**, **each ≤ 60 Chinese characters**:

1. **热点 (Hot spot)** — which step burned the most time, and why?
   (clicked wrong / waited for screenshot / LLM thinking loop / tool retry /
   vision misread / real desktop interference). Cite the step number(s).
2. **浪费 (Wasted work)** — any step we could have skipped? (repeated
   screenshots, should have used a meta tool instead of vision, unnecessary
   wait, etc.)
3. **一个修复 (One fix)** — a single concrete change Lucid could make.
   Pick one of: `prompt` / `tool` / `config` / `new-region` /
   `new-app-spec` / `bug-fix`, and say exactly what to change.

Finish with **two lines, exactly** (English, machine-readable):

```
severity: low|medium|high
category_of_fix: prompt|tool|config|new-region|new-app-spec|bug-fix
```

Do not write anything after those two lines.
```

### Step 4 — Aggregate subagent replies

1. Build a table: `id | verdict | severity | category_of_fix | one-line fix`.
2. Cluster by `category_of_fix` and count.
3. **High-leverage fixes**: any concrete fix proposed by ≥ 2 subagents
   (paraphrase liberally — "wait for window" and "add settle delay" should
   cluster).
4. **Single-thread blockers**: severity=high mentioned by exactly one.

### Step 5 — Write `iteration-plan.md` (Chinese prose)

Write `<run_dir>/iteration-plan.md` (Chinese; section labels translated):

```markdown
# E2E 迭代计划 —— <run_dir name>

## 概要
- 通过率：P/N（PCT%）
- Top 3 修复类别：...

## 高杠杆修复（≥ 2 个 subagent 一致）
- [ ] <fix>  —— 证据：threads <ids>
- [ ] ...

## 单点阻断（severity=high，独立）
- [ ] <fix>  —— thread <id>

## 较低优先级（每个只有单 subagent 提到）
- [ ] <fix>  —— thread <id>

## 按线程汇总
| id | verdict | severity | category_of_fix | 一句话修复 |
| ... |
```

### Step 6 — Hand off in chat (Chinese, ≤ 5 bullets)

In the chat reply give at most 6 bullets, in Chinese:

1. 通过率。
2. Top 1 高杠杆修复。
3. Top 2 高杠杆修复。
4. 最严重的单点阻断。
5. 指向 `iteration-plan.md` 的链接。
6. **下一轮重跑命令**：原样贴出 §7 算出的那条 `Test\run.py --only ...`
   命令（用代码块包起来），让用户一键复制。

**Do not modify Lucid source code in this skill** — the user decides which
fixes to land.

---

## Constraints

- Do not re-run `run.py`. Analysis is read-only on the run directory (you
  may *write* the three artifacts above into it, nothing else).
- Do not delete failed runs even if they look broken — they may carry the
  most signal.
- If a subagent reply does not end with the two `severity:` /
  `category_of_fix:` lines, drop it from the aggregate (don't guess).
- If `<run_dir>/iteration-plan.md` already exists, overwrite it (this skill
  is the source of truth for that file).
