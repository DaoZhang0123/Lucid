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

- Status enums (`pass` / `fail` / `partial` / `timeout` / `error` / `no-data` /
  `ok` / `max_steps` / `cancelled` / `api_error`) and `severity` /
  `category_of_fix` values (`prompt` / `tool` / `config` / `new-region` /
  `new-app-spec` / `bug-fix` / `low` / `medium` / `high`) — needed for
  downstream grep / clustering.
- File paths, `thread_id`, command lines, JSON field names (`status`,
  `final_text`, `expect_files`, `goal_met`, etc.).
- Quoted `instruction` text — keep verbatim in whatever language it was
  written; do not translate.
- The subagent prompt's role + output contract block (the
  "You are a VS Code Copilot subagent…" paragraph and the trailing
  `severity:` / `category_of_fix:` two lines) — subagents reply more
  reliably in English to an English contract.

---

## Subagent fan-out is mandatory

**Every thread with a `thread_dir` must be judged by a subagent before
`report.md` is written.** No inline judgement, no "assumed pass", no
sampling, no cost-saving carve-outs. The only skip is `thread_dir ==
null` (verdict is `no-data`, no signal to mine).

Rationale: the goal layer needs `context.log` + screenshots to catch
claim-vs-reality mismatches; `final_text` alone is not enough. Cutting
corners here is exactly how a thread that says "task complete" but
actually clicked the wrong window slips through as `pass`.

If the user explicitly says they only want a partial audit (e.g.
"只看失败的" / "only check the failures"), confirm with them and add a
big caveat box at the top of `report.md`. Default behaviour is
fan-out-everything.

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
  "thread_id": "thread-...",
  "thread_dir": "C:\\Users\\...\\dev.lucid\\logs\\threads\\thread-...",
  "queued_ms": ..., "ended_ms": ...,
  "status": "ok|max_steps|error|api_error|cancelled",
  "final_text": "...",
  "expect_files": [   // optional; copied verbatim from queries.json
    { "path": "%USERPROFILE%\\Downloads\\Test\\lucid-e2e-B1.txt",
      "must_exist": true, "must_contain": "hello from lucid e2e B1" }
  ] }
```

Note: there is **no `expect_signal`** field. The skill no longer relies
on a brittle substring match against `final_text` — instead the **goal
layer** below is judged by reading the `instruction` + `final_text` and
asking "does the model's claim plausibly match the asked-for goal?".
For fast-pass rows the main agent does this inline; for non-pass / slow
rows a subagent does it with screenshots + context.log in hand. This
avoids paraphrase traps (e.g. the model says "in Scientific mode"
instead of the literal phrase "scientific mode confirmed") and
unavailable-fallback traps (e.g. the model says "excel unavailable" but
the prompt's literal expect signal was `3297`).

`status` and `final_text` are backfilled at end-of-run. If `status` is
`null`, the task did not produce a `task_close` event — likely killed by
the harness or never started; mark as `no-data`.

All file deliverables for this test set live under
`%USERPROFILE%\Downloads\Test\` (`C:\Users\<user>\Downloads\Test\`).
Expand the `%USERPROFILE%` env var when you check `expect_files` paths
on disk.

## Verdict rules (apply per query)

A query is `pass` only if **all three** layers agree:

1. **Run layer** (manifest, mechanical): `status == "ok"`.
2. **Goal layer** (AI judgement on `instruction` + `final_text`):
   reports `goal_met: pass | fail | partial`. For `status=="ok"` fast
   rows the main agent judges this inline (one cheap reasoning step per
   row); for non-pass and slow rows a subagent judges it with
   `context.log` + screenshots in hand. Replaces the old brittle
   `expect_signal` substring match.
3. **Deliverables layer** (`expect_files`, mechanical, when present):
   every entry passes its file check on disk.

```
verdict =
  pass         if status == "ok"
                  AND goal_met == "pass"
                  AND every expect_files entry passes (see below)
  fail         if status == "ok" AND goal_met == "pass"
                  but at least one expect_files entry FAILS its disk check
                  (mark as fail with a `交付文件不符` reason)
  fail         if status == "ok" but goal_met == "fail"
                  (record the subagent's one-line reason)
  partial      if status == "ok" AND goal_met == "partial"
                  (informational — counted separately, not as pass)
  timeout      if status == "max_steps"
  error        if status in ("error", "api_error", "cancelled")
  no-data      if status is null
```

**Goal-layer rubric** (applies whether you're judging inline or via
subagent):

- `goal_met: pass` — the `final_text` (cross-checked against
  `context.log` / step screenshots if anything looks suspicious)
  demonstrates that the task's goal was achieved. Paraphrases are fine
  ("in Scientific mode" ≡ "scientific mode confirmed"). The advertised
  *unavailable* fallback for messaging / office tasks (`<app>
  unavailable` when the app isn't installed / signed in) counts as
  `pass` — the task is supposed to gracefully bail in that case.
- `goal_met: partial` — the model did some of the work but not all
  (e.g. opened the app and located the right cell but never typed the
  formula; reached the WeChat input box but didn't paste the text).
- `goal_met: fail` — the model claims success in chat but cross-checks
  show it didn't actually do it (clicked wrong contact, typed in the
  wrong window, hallucinated a reading of the screen, etc.); or the
  model gave up / crashed without completing.

**Deliverables check** (per `expect_files` entry):

- **First, prefer `q["deliverables_snapshot"]` if present.** Since 2026-05-12
  the harness pins each task's expect_files state at task-close time
  (`exists` / `size` / `mtime_ms` / `contains_match` / `decode_failed`).
  Trust this over a fresh disk read — a later task in the same run may
  have deleted / overwritten the same path on purpose (B3 deletes the
  file B1 wrote; without the snapshot, B1 looks like a fail).
- Fall back to a fresh disk read (the rules below) only when
  `deliverables_snapshot` is absent (older runs).
- Expand `%USERPROFILE%` (use the harness host's value — `C:\Users\<user>`).
- `must_exist: true` → path must exist (file or dir).
- `must_exist: false` → path must NOT exist.
- `must_contain: "<sub>"` (only meaningful when `must_exist: true`) →
  read the file as UTF-8 / UTF-16 (BOM-aware; Notepad on Windows defaults
  to UTF-8 + BOM since Win11) and assert the substring is present
  case-insensitively. If decoding fails on every common encoding,
  treat as a check failure with `文件编码未识别` in the reason.
- For dry-run / no-side-effect tasks (no `expect_files` present), the
  deliverables layer is vacuously satisfied.

When a query downgrades from `pass` to `fail` purely because of the
deliverables layer, surface that explicitly in the report row's `reason`
column (e.g. `交付文件缺失: lucid-e2e-C1.txt` /
`交付内容不符: lucid-e2e-L1.txt 缺子串 "Lucid combo L1"`). The model can
claim "task complete" all it wants — if the file isn't there, it lied.

`duration_s = (ended_ms - queued_ms) / 1000` if both are present.

> ⚠️ The sidecar runs **a single worker, serially**. `queued_ms` is the
> enqueue timestamp, so `duration_s` includes queue-wait. Compute an
> additional `exec_s` column = `ended_ms` − previous query's `ended_ms`
> (use `queued_ms` for the very first query) — that's the actual time the
> task spent executing. Prefer `exec_s` in the report; show `wall_s` next
> to it for reference.

## Procedure

1. §1 — Read manifest, compute mechanical layers (run + deliverables),
   bucket rows.
2. §2 — Fan out one subagent **per thread with a `thread_dir`**.
   Each returns `goal_met` + hot-spot / wasted-work / one-fix analysis.
3. §3 — Combine the three layers into the final verdict, then write
   `report.md` and `iteration-plan.md`.

### Step 1 — Read overview (mechanical)

1. Read `<run_dir>/manifest.json`.
2. For each query compute `duration_s` + `exec_s`, the **run layer**
   (`status == "ok"`?) and the **deliverables layer** (every `expect_files`
   entry passes its disk check?). Capture the deliverables-failure reason
   string when applicable. Keep this in your scratch buffer — you cannot
   compute the final verdict yet because the goal layer hasn't run.
3. Provisional bucketing for **fan-out ordering** in Step 2:
   - `provisional_fail`: `status != "ok"` (timeout / error / no-data),
     OR deliverables layer failed.
   - `provisional_pass_slow`: `status == "ok"` AND deliverables OK AND
     `exec_s > p95` (over `status==ok` rows; cap p95 at 5 entries).
   - `provisional_pass_fast`: everything else.
4. (Optional) If `<baseline_dir>` was given, do the same for it and
   compute per-id `Δ duration_s`. Flag regressions where
   `Δduration > 30%`.

> Don't compute per-thread step / image / tool-call counts in §1 — those
> are subagent territory.

### Step 2 — Fan out one subagent per thread (ALL threads)

Fan out **one subagent per query that has a `thread_dir`** — including
fast passes. Rationale: every thread needs a goal-layer judgement
(otherwise the verdict can't be finalized), and fast passes can still
hide wasted screenshots / redundant tool calls / opportunities to add
an app-spec seed tip.

- Skip only queries with `thread_dir == null` (never started — verdict
  is `no-data`, no signal to mine).
- Order: `provisional_fail` first, then `provisional_pass_slow`, then
  `provisional_pass_fast`. The high-signal subagents finish first and
  the user sees actionable bullets in chat sooner.
- **Concurrency**: launch in batches of 4–6 to avoid swamping the IDE.
  For runs with > 30 threads, expect this step to dominate wall-time;
  proceed anyway — that's the point. **Do NOT short-circuit by
  judging fast passes inline.**

For each selected query call `runSubagent`:

- `description`: `"E2E analyze <id>"` (English).
- `prompt`: the template below, fully filled in.

> Also write the same prompt to `<run_dir>/subagent-prompts/<id>.md` so a
> human can re-trigger one subagent later without re-running this skill.

#### Per-thread prompt template

The "scene info" block is in Chinese (easier for the human to skim
on disk); the role + output contract is English so the subagent reliably
ends with the three machine-readable lines.

```markdown
# E2E 线程分析 —— <id>（<category>）

**任务指令**：<instruction>

**机械层初步判断**：status=`<status>` · exec_s=<exec_s> · wall_s=<wall_s>

**交付文件检查**：<无 / 依次列出每个 expect_files 项及 ✓✗ 与原因>

**final_text**：
```
<final_text, truncated to 600 chars>
```

**事件文件**：`<thread_dir>\events.jsonl`
**人类可读日志**：`<thread_dir>\context.log`（按步带 LLM 文字 + 工具调用 + 工具结果）
**步骤截图**：`<thread_dir>\step-*.png`（如 `step-001-post-active_window.png`）—— 至少抽看 2~3 张关键节点的图

---

You are a VS Code Copilot subagent. Investigate this single Lucid thread
and produce a short Chinese diagnosis **plus a goal-layer verdict**.

**Required reading** (use `read_file` with large line ranges; don't make
many small reads):

1. `events.jsonl` — machine-readable timeline (one JSON event per line).
   Look for `tool_call` / `tool_result` / `step_start` / `step_summary` /
   `assistant_text` / `task_close`.
2. `context.log` — the same timeline rendered for humans, includes the
   model's narration. Often clearer for spotting "model was confused".
3. **At least 2–3 `step-*.png` screenshots** via `view_image`. Pick:
   - the screenshot just before the first failure / longest step, AND
   - the final screenshot before `task_close`, AND
   - any screenshot the model's narration calls out as wrong / surprising.
   Even on a passing thread, glance at one mid-run screenshot to check
   whether the model wasted clicks on UI it could have skipped.

First, **judge the goal layer**: did the task achieve the goal stated
in the `instruction`? Use the rubric:

- `pass` — the deliverable / observation matches what the instruction
  asked for. Paraphrases are fine. The advertised `<app> unavailable`
  fallback (when the app isn't installed / signed in) counts as `pass`.
- `partial` — part of the multi-step instruction was done but not all.
- `fail` — the model claims success in chat but cross-checks (context.log
  + screenshots) show it didn't actually do it (clicked wrong target,
  typed in the wrong window, hallucinated a screen reading, etc.); or
  the model gave up / errored out without completing.

Then answer 3 questions in **Simplified Chinese**, **each ≤ 60 Chinese
characters**:

1. **热点 (Hot spot)** — which step burned the most time, and why?
   (clicked wrong / waited for screenshot / LLM thinking loop / tool retry /
   vision misread / real desktop interference). Cite the step number(s).
   For a fast pass, instead answer: "哪一步还能再省时间？"
2. **浪费 (Wasted work)** — any step we could have skipped? (repeated
   screenshots, should have used a meta tool instead of vision, unnecessary
   wait, redundant `active_window` after a click that already auto-attached
   one, etc.) Reference the screenshot(s) you viewed if the waste is
   visible there.
3. **一个修复 (One fix)** — a single concrete change Lucid could make.
   Pick one of: `prompt` / `tool` / `config` / `new-region` /
   `new-app-spec` / `bug-fix`, and say exactly what to change.

Finish with **three lines, exactly** (English, machine-readable):

```
goal_met: pass|partial|fail
severity: low|medium|high
category_of_fix: prompt|tool|config|new-region|new-app-spec|bug-fix
```

Do not write anything after those three lines. If you cannot determine
`goal_met` (e.g. the thread has no `task_close` / `final_text` is empty),
still emit the line as `goal_met: fail` and put the reason in the
一个修复 answer.
```

### Step 3 — Finalize verdicts + write `report.md`

Now combine the three layers per query:

```
final_verdict =
  no-data      if status is null
  timeout      if status == "max_steps"
  error        if status in ("error", "api_error", "cancelled")
  fail         if status == "ok" AND goal_met == "fail"
  partial      if status == "ok" AND goal_met == "partial"
  fail         if status == "ok" AND goal_met == "pass"
                  AND any expect_files entry FAILS its disk check
                  (reason: 交付文件缺失 / 交付内容不符 / 文件编码未识别)
  pass         if status == "ok" AND goal_met == "pass"
                  AND every expect_files entry passes
```

If a subagent's reply did NOT end with a parseable `goal_met:` line,
treat its `goal_met` as `fail` and put `subagent 未给出 goal_met` in the
`reason` column (this should be rare — the prompt is explicit).

Aggregate:

- `pass / partial / fail / timeout / error / no-data` counts
- `pass_rate` (pass only — partial does not count as pass)
- `p50 / p95 / max` of `exec_s` over `final_verdict == "pass"` rows only
- **Runtime totals (always required, do not skip)**:
  - `total_wall_s` = `max(ended_ms) - manifest.started_ms` (from
    `<run_dir>/manifest.json`; convert ms → s; also format as `Xm Ys`).
  - `threads_with_ended_ms` = count of queries with non-null `ended_ms`
    vs total (e.g. `32 / 48`). Mention any threads missing `ended_ms`
    here so the harness-liveness story is visible at a glance.
  - `ok_exec_sum_s` = sum of `exec_s` over `status == "ok"` rows.
  - `parallelism_ratio` = `ok_exec_sum_s / total_wall_s` (≈ 1.0 means
    fully serial; > 1 means real parallelism). Add a one-line note when
    `wall ≈ exec_sum + slow_outliers` so the reader sees obvious
    serialization.
  - `slow_tail_share` = `sum(exec_s of rows where exec_s > p95) / total_wall_s`
    as a percent. Flag when ≥ 30%.
- (Optional) baseline `Δ duration_s` table if `<baseline_dir>` was given

Write `<run_dir>/report.md` (Chinese; template in English for clarity):

```markdown
# E2E 报告 —— <run_dir name>

## §0 Runtime Stats（必填，源：manifest.json + _scratch/mech.json）
| 指标 | 值 |
|------|-----|
| run started | <YYYY-MM-DD HH:MM:SS from manifest.started_ms> |
| last thread ended | <max ended_ms> |
| **total wall-clock** | **<X> s ≈ <Xm Ys>** |
| 派发完成的线程 | <ended_count> / <total>（说明缺 ended_ms 的线程，如 harness 中途死亡） |
| ok 线程数（mech） | <N> |
| ok 线程 exec_s 总和 | <S> s ≈ <Xm Ys>（约占 wall 的 <pct>%） |
| ok 线程 exec_s 平均 | <avg> s |
| ok 线程 exec_s p50 / p95 / max | <p50> / <p95> / <max> s |
| max wall_s | <max wall_s>（哪条 id） |
| 慢尾合计 / 占 wall | <slow_sum>s / <pct>% |

观察：
- 串行/并行：当 `ok_exec_sum + slow_outliers ≈ wall` 时显式指出"完全串行，无并行收益"。
- harness liveness：若有线程缺 `ended_ms`，列出 id 段并给出最后存活的 id（与 §6 hand-off 呼应）。

## §1 汇总表
| id | category | verdict | status | exec_s | wall_s | goal | files | reason | events |
|---|---|---|---|---:|---:|---|---|---|---|
| A1 | cognitive | **pass**    | ok | 4.2  | 4.2  | pass    | —  | | [events](<thread_dir>/events.jsonl) |
| C1 | notepad   | **fail**    | ok | 38.1 | 41.0 | pass    | ✗  | 交付文件缺失 lucid-e2e-C1.txt | [events](...) |
| D2 | calculator| **partial** | ok | 12.0 | 12.4 | partial | —  | 切到 Scientific 但未输入 sin(30) | [events](...) |
| ... |

## §2 总量
- 总数：N，pass P，partial Pa，fail F，timeout T，error E，no-data D
- 通过率：PCT%（仅 pass 计入）
- 通过任务的 exec_s：p50 = ...，p95 = ...，max = ...
- 全 run wall-clock：<X> s（详见 §0）

## §3 基线对比（仅当用户提供 baseline 时）
| id | now | prev | Δ duration_s | regressed |
| ... |

## §4 失败 / 超时 / 出错 / partial 的线程
For each non-pass thread: id, final_verdict, status, goal_met,
instruction (truncated), the subagent's 一个修复 line,
link to events.jsonl. Do NOT inline events here.

## §5 慢尾（exec_s above p95）
通过任务里 exec_s 落在 p95 以上的列出来 —— 这些虽然过了但值得看是否能加速。

## §6 下一步
指向 `iteration-plan.md`。

## §7 下一轮重跑命令
把本轮 **非 pass**（fail / partial / timeout / error / no-data 且有
thread_dir）外加 **慢尾**（exec_s > p95，最多 5 条）的 id 拼成一条命令：

```powershell
.\.venv\Scripts\python.exe Test\run.py --only <id1>,<id2>,...
```

如果一条都没有（全 pass 且没有慢尾），写一行 `本轮全过且无慢尾，无需重跑`。
```

### Step 4 — Aggregate subagent replies for the iteration plan

1. Build a table: `id | final_verdict | goal_met | severity | category_of_fix | one-line fix`.
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

- Do not re-run `run.py`. Analysis is read-only on the source code
  outside `<run_dir>`. **All intermediate / scratch files** (one-off
  Python composer scripts, mechanical-layer JSON dumps, prompt
  generators, etc.) **must be written inside `<run_dir>` itself** —
  never under `Test/`, `lucid/`, or the workspace root. This way the
  whole run is self-contained: the user can keep, archive, or delete
  the run directory as a unit, and there is nothing to clean up
  afterwards. Suggested layout:

  ```
  <run_dir>/
    manifest.json              # written by run.py
    sidecar.stderr.log         # written by run.py
    subagent-prompts/<id>.md   # one per thread, written by §2
    _scratch/                  # any helper .py / .json this skill creates
    report.md                  # final, written by §3
    iteration-plan.md          # final, written by §5
  ```

  Do NOT delete `_scratch/` after composing — it is part of the run's
  audit trail and lets a human re-run a single composer step later.
- Do not delete failed runs even if they look broken — they may carry the
  most signal.
- If a subagent reply does not end with the three `goal_met:` /
  `severity:` / `category_of_fix:` lines, treat its `goal_met` as `fail`
  (per Step 3) and drop its severity / category_of_fix from the
  aggregate (don't guess).
- If `<run_dir>/iteration-plan.md` already exists, overwrite it (this skill
  is the source of truth for that file).
