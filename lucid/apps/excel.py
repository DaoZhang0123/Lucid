"""Microsoft Excel."""

SLUG = "excel"
TITLE = "Microsoft Excel"

TIPS = """\
- [seed · launch] Use `launch_app(name='excel')` — resolves the `excel` App Paths alias via `start excel`. Do NOT `where excel` / `Get-ChildItem -Recurse`.
- [seed · cold-start-budget] Office cold start (first launch after boot, slow SSDs, or many add-ins) can take **30–90s** to surface a window — not the 3–8s warm path. `launch_app` waits up to ~20s for the window, then the outer 25s watchdog fires. **If `launch_app('excel')` returns a timeout error, do NOT retry `launch_app` and do NOT immediately retry `focus_window` — Excel is still mid-init and the second `launch_app` will often wedge the Win32 enum call for another 25s (observed E2E 20260520 O2 — 85s wasted on 3 retries).** Fallback is one shot: `run_shell({'command':'start excel', 'shell':'cmd', 'timeout_s':10})` — fire-and-forget, no window deadline. Then `screenshot(level='active_window')` once every step; the title bar substring "Excel" appearing in `active_window` is the canonical "Excel is ready" signal.
- [seed · cold-start-warm] Once Excel is launched once in a session it stays warm; subsequent `launch_app` calls return in <1s.
- [seed · start-screen] On launch Excel shows the start screen. **Press `Enter` to open a Blank workbook — do NOT click the "Blank workbook" thumbnail.** The thumbnail's hit region drifts with the window size, theme, and which template row is currently scrolled into view; pixel-clicks miss 3–4 times in a row even with two-phase preview (E2E 20260518 P3 — 767s burned trying to click the thumbnail). The Enter key is bound to the same "create blank doc" action regardless of layout. The empty workbook has the title-bar substring "工作簿1 - Excel" / "Book1 - Excel".
- [seed · enter-formula] Click the target cell, then `type` the formula text (starting with `=`) and press Enter. The cell value updates. To re-read the computed value, click the same cell again — the formula bar (or the cell itself if you take a `screenshot(level='active_window')`) will show it.
- [seed · close] Ctrl+W closes the workbook; if a Save prompt appears use `key text='alt+s'` for Save / `alt+n` for Don't Save.
- [seed · key-Delete-pitfall] After dismissing a save / paste-options popup, do NOT immediately fire `key Delete` blind — the underlying input layer can wedge if focus is briefly on a transient toolbar overlay. Take a fresh `screenshot(level='active_window')` first so the watchdog observes the real focused control. (See E2E run 20260512-182234 O2 wedge.)
- [seed · prefer-skill-for-authoring] **Excel UI is great for tweaking cells, fixing one formula, or showing the user a live sheet** — it is NOT a good way to build a workbook from scratch (multi-sheet model, lots of rows, conditional formatting). For "make me an xlsx with this data / model" tasks: first call `search_skills(query="create excel xlsx")` — if the user enabled the `anthropics/skills` repo it returns a real `document-skills/xlsx` SKILL.md that authors the file programmatically (openpyxl). Install with `install_repo_skill`, then `read_skill` and follow the body. UI remains the right tool for *editing* an existing sheet the user opens.
"""

LAUNCHER = {
    "name": "Microsoft Excel",
    "description": "Microsoft Excel desktop app. Started via the `excel` App Paths alias (no recursive search).",
    "exe": "excel",
    "process": "EXCEL.EXE",
    "window_title_re": r"Excel",
    # Bumped from 8.0 → 20.0: Office cold-start (first launch after boot,
    # slow SSDs, or add-in churn) commonly needs 15-20s before the window
    # is enumerable. The outer launch_app watchdog is 25s so 20s leaves a
    # small safety margin. See E2E 20260520 O2 — 8.0s timed out on cold
    # start; subsequent retries wedged the Win32 enum call.
    "launch_timeout_s": 20.0,
}
