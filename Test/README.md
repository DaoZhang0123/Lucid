# Lucid E2E regression test

> One Python entry point ([run.py](run.py)) + one skill file ([SKILL.md](SKILL.md))
> + one query set ([queries.json](queries.json)). Each iteration runs the
> same set of queries against Lucid, then VS Code Copilot uses the multi-
> subagent flow described in `SKILL.md` to turn the raw run into a
> "completion + duration + fixes" iteration plan.

```
queries.json   →  run.py  →  runs/<ts>/manifest.json + sidecar.stderr.log
                                      ↓
                           VS Code Copilot reads SKILL.md, then:
                                      ↓
                          report.md + iteration-plan.md
                          + subagent-prompts/<id>.md
```

> **Output language**: the user-facing artifacts (`report.md`,
> `iteration-plan.md`, the chat hand-off) are written in **Simplified
> Chinese** per `SKILL.md`'s output-language clause; this README and the
> skill file itself are in English.

---

## 0. What we evaluate

In order of priority:

1. **Correctness** — was the query actually completed? Positive signal:
   `task_close.status == "ok"` AND `final_text` contains `expect_signal`
   (or, as a soft fallback when `expect_signal == ""`, contains
   `task complete:` / `任务完成`). Negative: `max_steps` / `error` /
   `api_error` / `cancelled`, or `final_text` containing `task failed:` /
   `无法完成`.
2. **Speed** — total `exec_s` (current task's `ended_ms` minus the previous
   task's `ended_ms`), plus, when a subagent reads `events.jsonl`, step
   count + tool-call count + screenshot count. Any significant regression
   on the same query between iterations counts.

We don't track LLM token usage or per-call retry counts at the harness
level; subagents pull them from `events.jsonl` if relevant.

---

## 1. The query set (39 queries)

File: [queries.json](queries.json). Each entry:

```json
{ "id": "A1",
  "category": "cognitive",
  "instruction": "...",
  "expect_signal": "3297",   // grep this in final_text → pass
  "max_steps": 6,            // soft budget (informational only)
  "needs": []                // optional preconditions
}
```

**Category breakdown** (horizontal coverage; each query has a clear
"success signal"):

| # | Category | Count | Capabilities exercised |
|---|---|---|---|
| A | cognitive | 2 | Pure reasoning, `read_file` meta tool (no GUI) |
| B | fileio | 3 | `write_file` / `read_file` / `run_shell` trio |
| C | notepad | 2 | Launch + keyboard input + save dialog |
| D | calculator | 2 | Launch + simple clicks + UIA reads display |
| E | explorer | 2 | File-manager navigation + create / delete |
| F | browser | 2 | Edge launch + URL + screenshot description |
| G | vscode | 2 | VS Code launch + file tree + grep |
| H | settings | 2 | Win+I + locate setting (hardest vision class) |
| I | multi-step | 2 | Cross-tool / cross-app, validates thread state |
| J | resilience | 1 | Deliberate "app not installed" branch |
| K | app | 7 | Single-app coverage: Edge / Paint / Snipping Tool / Task Manager / VS Code / Clock / Downloads sort |
| L | combo | 5 | **Multi-app chains**: Notepad+read_file verify / Edge→write_file persist / shell→Notepad timestamp / screenshot→Notepad count / Notepad+Explorer visual+shell double-check |
| M | messaging | 4 | **WeChat + Microsoft Teams**, all dry-run (type but never send; Ctrl+A Delete after screenshot); includes not-installed / not-logged-in fallbacks |
| N | combo (with messaging) | 3 | shell+WeChat timestamp dry-run / screenshot foreground-detect+Notepad / Teams title+Notepad |

> A single failure is not fatal — analysis handles them individually.
> L/N standardize the "open X, do Y, verify with Z" chains from the
> README examples (closest to real usage). M only verifies "can we reach
> the input box and type"; it never produces an outgoing message.

---

## 2. Iteration loop (run this every iteration)

### Step A — Prep (5 min)

1. **Close the Lucid GUI** (the NSIS desktop app), so it doesn't open a
   second sidecar that fights for config.
2. Make sure `%LOCALAPPDATA%\dev.lucid\config.toml` has
   `autonomy = "full"` — HITL prompts would block the entire queue.
   **Danger words** (`delete file` / `format` / `transfer` / …) are still
   blocked, so `queries.json` deliberately avoids those phrasings; deletes
   go through `run_shell` + `Remove-Item` style.
3. Verify an LLM backend is reachable (Anthropic / Copilot / proxy).
4. **Do NOT pre-clean the desktop**. This is intentional — real
   environments have 50 windows, a packed taskbar, a cluttered desktop;
   the agent must work under that interference. The `lucid-e2e-*` temp
   files can be present or absent (B1/L1/L5/N2/N3 are all idempotent
   overwrites).

### Step B — Run the queries (25–60 min depending on model)

```powershell
cd d:\Project\Lucid
.\.venv\Scripts\python.exe Test\run.py
```

Optional flags:

```powershell
# Run only specific ids (debug / re-run failures from previous round)
.\.venv\Scripts\python.exe Test\run.py --only A1,B2,L1,M3

# Bigger global timeout (default 60 min)
.\.venv\Scripts\python.exe Test\run.py --global-timeout 7200

# Cancel the current task if its events.jsonl has not advanced for N
# seconds (default 1200). Cancelling moves on to the next task in the
# queue — it does NOT shut down the run.
.\.venv\Scripts\python.exe Test\run.py --per-task-cap 1800

# Whole-run wedge cap: if neither tid nor queue length nor events
# activity changes for N seconds, abort the whole run (default 600).
.\.venv\Scripts\python.exe Test\run.py --queue-idle-cap 1200
```

What `run.py` does (one paragraph):

1. `subprocess.Popen(["python", "-m", "lucid", "--sidecar"])`; stdin/stdout
   carry NDJSON JSON-RPC frames.
2. Wait for sidecar's `{"event":"ready"}`.
3. `start_task` 39 times in order (300 ms apart). The sidecar's internal
   priority queue runs them one at a time — `run.py` is **only an
   enqueuer**, not a scheduler.
4. Persist `manifest.json` to `Test/runs/<YYYYmmdd-HHMMSS>/`, one entry
   per query, rewritten on each enqueue.
5. Poll `get_status`. Track each running thread's `events.jsonl` mtime;
   if the per-task cap elapses with no growth, send `cancel` (only that
   one task — the queue keeps running). Exit when `running` is empty and
   queue length is 0.
6. **Backfill** each thread's `task_close.status` / `final_text` /
   `ended_ms` into `manifest.json` before exiting, so analysis doesn't
   need to re-read `events.jsonl` just for the verdict.

> Stop early: Ctrl+C — `run.py` sends a `shutdown` RPC for graceful exit.

You'll see something like:

```
[run.py] sidecar ready; enqueueing 39 queries
[run.py] enqueued  A1 → thread-... (running)
[run.py] enqueued  A2 → thread-... (pos=1)
...
[run.py] running=thread-... queue_len=37
[run.py] queue drained, no worker running — done
[run.py] done. manifest: Test\runs\20260511-142233\manifest.json
```

### Step C — Analysis with VS Code Copilot (a few minutes)

Open VS Code Copilot Chat (agent mode) and paste (substitute the run dir):

```
请按 Test/SKILL.md 的流程，分析 Test/runs/20260511-142233/ 这一轮 E2E 结果。
（如果要和上一轮对比：再加一句 "baseline 是 Test/runs/<上一个 ts>/"）
```

Copilot will, per `SKILL.md`:

1. Read `manifest.json`, compute pass / fail / timeout / `exec_s` p50/p95/max,
   write `report.md` (Chinese prose).
2. Fan out subagents (4–6 in parallel) for **every non-pass** thread plus
   the **slow tail** (`exec_s > p95`, capped at 5). Each subagent reads
   its own `events.jsonl` and replies with hot spot / wasted work / one
   fix, ending with the machine-readable `severity:` / `category_of_fix:`
   two-line contract.
3. Aggregate, write `iteration-plan.md` (Chinese):
   - **High-leverage fixes**: ≥ 2 subagents agreed.
   - **Single-thread blockers**: severity=high but unique.
   - **Per-thread roll-up**: full table.
4. Reply in chat with ≤ 6 bullets: pass rate, top two fixes, worst
   blocker, link to `iteration-plan.md`, **and a ready-to-paste re-run
   command for the next iteration** (see Step D).

> If Copilot doesn't auto-pick up the skill, just drag `Test/SKILL.md`
> into the chat and write "按这个 skill 分析 Test/runs/<ts>/".

### Step D — Land changes & queue the next iteration

1. Review `iteration-plan.md`.
2. **High-leverage fixes** → file under "工程债 / 横向" in `Docs/todo.md`.
3. **Single-thread blockers** → one GitHub issue each.
4. (Optional) rename the run dir `<ts>-<note>` for use as a baseline
   later.
5. **Re-run only the broken / slow ids next time** using the command the
   chat hand-off gave you (per `SKILL.md` §7):

   ```powershell
   .\.venv\Scripts\python.exe Test\run.py --only <ids the skill computed>
   ```

   Comma-separated, no spaces. Much faster than re-running all 39 while
   you're iterating on a fix.

---

## 3. Release criteria

Per-iteration minimum bar:

- **Required**: pass rate not lower than baseline.
- **Required**: median `exec_s` (passing queries) not regressed by more
  than 30%.
- **Recommended**: every high-severity item in `iteration-plan.md` lands
  in the next sprint.

---

## 4. Known limits / TODO

- Sidecar is a single worker — 39 queries run **serially**. Concurrency
  needs a worker pool, plus a way to keep GUI-foreground tasks from
  fighting (only `run_shell` / `read_file` / `write_file` are safe to
  parallelize).
- Category M (WeChat / Teams) depends on the client being installed and
  logged in. Not-logged-in falls into the "unavailable" branch which
  scores as pass — note that's a **degraded** pass.
- `run.py` deliberately doesn't talk to the Tauri app; running both
  simultaneously would spawn two sidecars.
- Baseline comparison currently only diffs `duration_s` (no step count —
  manifest doesn't store it; subagents would need to attach it).

---

## 5. File map

| File | Role |
|---|---|
| [queries.json](queries.json) | The 39 standard queries; append new ones, never remove old ids |
| [run.py](run.py) | Enqueuer + manifest writer; the only executable entry point |
| [SKILL.md](SKILL.md) | The skill VS Code Copilot uses to do analysis |
| [README.md](README.md) | This document |
| `runs/<ts>/manifest.json` | One run's manifest (query → thread_id → status) |
| `runs/<ts>/sidecar.stderr.log` | Raw sidecar stderr (debug aid) |
| `runs/<ts>/report.md` | Copilot-written overview (per SKILL §2) |
| `runs/<ts>/subagent-prompts/<id>.md` | Per-query subagent input (per SKILL §3) |
| `runs/<ts>/iteration-plan.md` | Copilot-written next-iteration plan (per SKILL §5) |
