"""Microsoft Paint (mspaint)."""

SLUG = "paint"
TITLE = "Paint"

TIPS = """\
- [seed · launch] `launch_app(name='paint')` resolves the `mspaint` App Paths alias. Title bar substring "Paint" / "画图" = ready. **Do NOT re-launch Paint if a Paint window is already open** — `launch_app` returns `method=focus_existing_window` and reuses it; firing `launch_app` again from inside a stroke-failure loop just opens a SECOND blank Paint and the previous window's state is now hidden behind it (E2E thread-20260520-104022: 3 Paint windows opened back-to-back).
- [seed · cold-start] First launch may take 1–3s for the canvas to render; the system already attaches one L2 after launch_app.
- [seed · canvas-vs-workspace] **CRITICAL — the Paint "canvas" is a small WHITE rectangle, NOT the surrounding gray workspace.** A fresh Paint window has two visually distinct drawable-looking areas:
  - **The canvas** (where strokes actually land): a **pure white** rectangle, typically only ~600×400 px at default zoom, sitting in the centre / upper-left of the workspace. It has **8 small dark square handles** drawn around its perimeter (4 corners + 4 edge midpoints). **These handles are CANVAS RESIZE handles** — they look almost identical to the Select-tool selection handles, but they are part of the canvas itself, ALWAYS present, and have nothing to do with the current tool. Do not interpret them as "leftover selection". On a 2K screen the canvas can occupy as little as 20-30% of the window area.
  - **The workspace** (background, ignores all input): the much larger **light gray / off-white** area surrounding the canvas. It fills the rest of the Paint window between the ribbon (top) and the status bar (bottom). **Dragging here does literally nothing** — no stroke, no selection, no error. The post-drag screenshot looks identical to the pre-drag screenshot, which is the canonical "my drag failed silently" symptom.
  Distinguish them by: (a) **colour** — canvas is #FFFFFF pure white, workspace is a slightly warmer / cooler off-white; in a low-DPI screenshot they can look almost identical, in which case use (b); (b) **the 8 handles** — the rectangle bounded by those 8 small squares IS the canvas, everything outside is workspace.
- [seed · canvas-locate] **Before any `left_click_drag`, find the canvas rectangle on the current L2 and pick coordinates strictly INSIDE it.** Procedure:
  1. From the most recent `active_window` L2 of Paint, visually find the small white rectangle with 8 handles (see canvas-vs-workspace).
  2. Read the canvas's **top-left handle coordinate** and **bottom-right handle coordinate** off the colour gridline labels (rule 7). Call these `(cx0, cy0)` and `(cx1, cy1)`.
  3. Pick stroke endpoints with at least 20 px margin inside those bounds, e.g. `start = (cx0+30, cy0+30)`, `end = (cx0+80, cy0+60)` for a short diagonal.
  4. If `cx1-cx0 < 100` or `cy1-cy0 < 100` (canvas is tiny), maximise Paint first (`key("Win+Up")` or click the maximise box) so subsequent screenshots show a bigger canvas before measuring again.
  **Do NOT pick a "nice round" coordinate like (700, 600) by gut feel** — that almost always lands in the gray workspace on Win11 Paint at default window size (E2E thread-20260520-104022: every drag at (700,600) / (550,500) silently no-op'd because they were on workspace, not canvas; model misread "no change" as "still in Select mode" and pressed P repeatedly — Pencil was already selected the whole time).
- [seed · stroke-recipe] **End-to-end "draw one short stroke on canvas" — recipe:**
  1. (Optional, defensive) `key(text="P")` — switch to Pencil. Cheap insurance even if you think it's already selected.
  2. `screenshot(level="active_window")` if you don't already have a fresh L2 of Paint — you need this to locate the canvas.
  3. From the L2, locate the canvas's 8 handles per canvas-locate; derive `(start_x, start_y)` and `(end_x, end_y)` strictly inside the white area.
  4. `mouse_move(coordinate=[start_x, start_y])` then `left_click_drag(coordinate=[end_x, end_y])` — drag starts from current cursor position.
  5. `screenshot(level="active_window")` to verify a thin black squiggle appeared inside the canvas (NOT just "the canvas still looks blank" — the post-drag image must literally show new black pixels where the cursor traversed).
  6. `task complete: paint stroke ok`.
- [seed · tools-keyboard] **Two failed clicks on a toolbar tool → switch to single-letter keys** (rule 19). Pencil = `P`, Brush = `B`, Eraser = `E`, Fill = `K`, Text = `T`, Line = `L`, Rectangle = `R`. Ctrl+Z = undo. These NEVER miss — clicking the tiny ribbon icons does.
- [seed · save-as] Ctrl+S on a fresh untitled doc opens Save As. Type the absolute path directly into the filename field; press Enter; verify with `run_shell Test-Path` BEFORE `task complete`. See save-dialog tips for filename-box pitfalls.
"""

LAUNCHER = {
    "name": "Paint",
    "description": "Microsoft Paint. Started via the `mspaint` App Paths alias (no recursive search).",
    "exe": "mspaint",
    "process": "mspaint.exe",
    "window_title_re": r"Paint|画图",
    "launch_timeout_s": 5.0,
}
