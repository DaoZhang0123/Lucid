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

1. **Correctness** — was the query actually completed? Three layers must
   all agree (per `SKILL.md`'s verdict rules):
   - **Run layer** (mechanical): `task_close.status == "ok"`.
   - **Goal layer** (AI judgement): a per-thread subagent reads the
     `instruction` + `final_text` (and `context.log` / step screenshots
     if needed) and reports `goal_met: pass | partial | fail`. This
     replaces the old brittle `expect_signal` substring grep — the
     subagent tolerates paraphrases and recognizes the advertised
     `<app> unavailable` fallback as a graceful pass.
   - **Deliverables layer** (mechanical): every `expect_files` entry on
     the query passes its disk check (path exists / does not exist +
     optional `must_contain` substring). The model can claim "task
     complete" — if the file isn't on disk, it lied; the verdict is
     `fail`. All deliverables for this test set live under
     `%USERPROFILE%\Downloads\Test\`.
2. **Speed** — total `exec_s` (current task's `ended_ms` minus the previous
   task's `ended_ms`), plus, when a subagent reads `events.jsonl`, step
   count + tool-call count + screenshot count. Any significant regression
   on the same query between iterations counts.

We don't track LLM token usage or per-call retry counts at the harness
level; subagents pull them from `events.jsonl` if relevant.

---

## 1. The query set (57 queries)

File: [queries.json](queries.json). Each entry:

```json
{ "id": "B1",
  "category": "fileio",
  "instruction": "...",
  "expect_files": [               // optional; deliverables layer
    { "path": "%USERPROFILE%\\Downloads\\Test\\lucid-e2e-B1.txt",
      "must_exist": true,
      "must_contain": "hello from lucid e2e B1" }
  ],
  "max_steps": 6,                 // soft budget (informational only)
  "needs": []                     // optional preconditions
}
```

There is **no `expect_signal`** field. The goal layer is judged by the
per-thread subagent in Step 2 of `SKILL.md`, not by a string match.

**Category breakdown** (horizontal coverage; each query has a clear
"success signal"). App names in parentheses are the exact targets each
query touches — the agent uses `launch_app(name=...)` (or a hotkey /
start-menu fallback) for each:

| # | Category | Count | Apps / capabilities exercised |
|---|---|---|---|
| A | cognitive | 2 | Pure reasoning, `read_file` meta tool (no GUI) |
| B | fileio | 3 | `write_file` / `read_file` / `run_shell` trio |
| C | notepad | 2 | **Notepad** (notepad.exe): launch + keyboard input + Save-As dialog (both C1 and C2 save to disk) |
| D | calculator | 2 | **Calculator** (calc.exe): standard arithmetic via clicks (D1) + Scientific-mode switch (D2) |
| E | explorer | 2 | **File Explorer** (explorer.exe): GUI navigation + count subfolders (E1); `run_shell` create/delete cycle (E2) |
| F | browser | 3 | **Microsoft Edge** (msedge.exe): example.com load (F1) + Bing GUI search (F2) + `read_webpage` headless Bing search (F3) |
| G | vscode | 2 | `run_shell` line-count (G1) + `Select-String` grep (G2) — VS Code-shaped tasks done via shell |
| H | settings | 2 | **Settings** (Win+I): Display resolution (H1) + System > About (H2) — hardest vision class |
| I | multi-step | 2 | Cross-tool / cross-app: shell→write→read chain (I1); fullscreen + taskbar pinned-icon read (I2) |
| J | resilience | 1 | Deliberate "app not installed" branch — must stop, not loop |
| K | app | 11 | Single-app coverage: **Edge** (about:blank title, K1) / **Paint** (mspaint, draw stroke + save PNG, K2) / **Snipping Tool** (SnippingTool / ScreenSketch, K3) / **Task Manager** (taskmgr, top process row, K4) / **VS Code** (Code, title bar, K5) / **Clock / tray clock flyout** (K6) / Downloads sort via `run_shell` (K7) / **Sticky Notes** (StikyNot, K8) / **Windows Terminal** (wt.exe, with PowerShell fallback, K9) / **Photos** (Microsoft.Photos, K10) / **Mail** (Windows Mail, K11) |
| L | combo | 5 | **Multi-app chains**: Notepad+`read_file` verify (L1) / Edge→`write_file` persist (L2) / shell→Notepad timestamp (L3) / fullscreen→Notepad pinned-count (L4) / Notepad+Explorer visual+shell double-check (L5) |
| M | messaging | 4 | **WeChat (微信)** + **Microsoft Teams**: read-only probes do NOT send (M1 WeChat top-pinned chat name / M3 Teams left-rail tab + unread); self-chat probes DO send end-to-end since the recipient is yourself (M2 WeChat 文件传输助手 ping + Enter / M4 Teams self-chat ping + Enter); each has a not-installed / not-signed-in fallback |
| N | combo (with messaging) | 3 | shell→WeChat 文件传输助手 self-send (N1) / fullscreen foreground-detect→Notepad (N2) / Teams title→Notepad (N3) |
| O | office | 5 | **Microsoft Office desktop apps** — exercise the full save path: **Word** (winword, type + Ctrl+S → lucid-e2e-O1.docx) / **Excel** (excel, =137*24+9 in A1 + save lucid-e2e-O2.xlsx) / **PowerPoint** (powerpnt, blank deck + title + save lucid-e2e-O3.pptx) / **Outlook** (outlook, read-only top inbox subject, O4) / **OneNote** (onenote, type on a fresh page, auto-saves, O5); each has an unavailable fallback |
| P | combo (search + app) | 8 | **Bing search × app**, two paths per target app: HEADLESS (`read_webpage` on `bing.com/search?q=...`) and GUI (Edge GUI search box). Notepad save (P1 headless / P2 GUI) / Excel A1+B1 save (P3 headless / P4 GUI) / Teams self-chat send (P5 headless / P6 GUI) / WeChat 文件传输助手 self-send (P7 headless / P8 GUI). All Teams/WeChat targets are self-chats so Send is exercised end-to-end. |

> A single failure is not fatal — analysis handles them individually.
> L/N standardize the "open X, do Y, verify with Z" chains from the
> README examples (closest to real usage). M exercises the full input
> path; for self-chats (M2 / M4) Send/Enter is pressed end-to-end
> because the recipient is you. P exercises search×app integration
> across both the headless `read_webpage` path and the GUI Bing path,
> with the messaging targets (P5–P8) also self-sending. O queries save
> to disk so the Save dialog / Ctrl+S path is covered. If an Office
> app or a messaging client is not installed / not signed in, the
> `<app> unavailable` fallback scores as a (degraded) pass — see §4.

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
   files all live under `%USERPROFILE%\Downloads\Test\` and can be
   present or absent (B1/L1/L5/N2/N3 are all idempotent overwrites; the
   first task that needs the folder will `mkdir` it). Don't pre-create
   the folder either — the agent should learn to do that itself.

### Step B — Run the queries (30–80 min depending on model)

```powershell
cd d:\Project\Lucid
.\.venv\Scripts\python.exe Test\run.py
```

Optional flags:

```powershell
# Run only specific ids (debug / re-run failures from previous round)
.\.venv\Scripts\python.exe Test\run.py --only A1,B2,L1,M3

# Bigger global timeout (default 120 min — 57 queries serial typically take 75–95 min)
.\.venv\Scripts\python.exe Test\run.py --global-timeout 10800

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
3. `start_task` 57 times in order (300 ms apart). The sidecar's internal
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
[run.py] sidecar ready; enqueueing 57 queries
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
2. Fan out subagents (4–6 in parallel) for **every thread that has a
   `thread_dir`** — failures and slow tail first, fast passes last.
   Even green threads are mined for speed-up suggestions. Each subagent
   reads its own `events.jsonl` + `context.log` + 2–3 `step-*.png`
   screenshots and replies with hot spot / wasted work / one fix, ending
   with the machine-readable `severity:` / `category_of_fix:` two-line
   contract.
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

   Comma-separated, no spaces. Much faster than re-running all 57 while
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

- Sidecar is a single worker — 57 queries run **serially**. Concurrency
  needs a worker pool, plus a way to keep GUI-foreground tasks from
  fighting (only `run_shell` / `read_file` / `write_file` are safe to
  parallelize).
- Category M (WeChat / Teams) and category O (Word / Excel / PowerPoint /
  Outlook / OneNote) both depend on the client being installed and signed
  in. Not-installed / not-signed-in falls into the `<app> unavailable`
  branch which scores as pass — note that's a **degraded** pass.
- Category M / N / P self-chat queries (M2, M4, N1, P5–P8) press Send/Enter
  end-to-end — messages persist in your own 文件传输助手 / Teams self-chat
  transcript. That is intentional (the recipient is you) but expect the
  chat history to grow with each run.
- `run.py` deliberately doesn't talk to the Tauri app; running both
  simultaneously would spawn two sidecars.
- Baseline comparison currently only diffs `duration_s` (no step count —
  manifest doesn't store it; subagents would need to attach it).

---

## 5. File map

| File | Role |
|---|---|
| [queries.json](queries.json) | The 57 standard queries; append new ones, never remove old ids |
| [run.py](run.py) | Enqueuer + manifest writer; the only executable entry point |
| [SKILL.md](SKILL.md) | The skill VS Code Copilot uses to do analysis |
| [README.md](README.md) | This document |
| `runs/<ts>/manifest.json` | One run's manifest (query → thread_id → status) |
| `runs/<ts>/sidecar.stderr.log` | Raw sidecar stderr (debug aid) |
| `runs/<ts>/report.md` | Copilot-written overview (per SKILL §2) |
| `runs/<ts>/subagent-prompts/<id>.md` | Per-query subagent input (per SKILL §3) |
| `runs/<ts>/iteration-plan.md` | Copilot-written next-iteration plan (per SKILL §5) |
