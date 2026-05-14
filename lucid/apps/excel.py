"""Microsoft Excel."""

SLUG = "excel"
TITLE = "Microsoft Excel"

TIPS = """\
- [seed · launch] Use `launch_app(name='excel')` — resolves the `excel` App Paths alias via `start excel`. Do NOT `where excel` / `Get-ChildItem -Recurse`.
- [seed · cold-start] Office cold start shows a splash screen for 3–8s before the start screen renders. After `launch_app` returns, wait for the next user message (the screenshot loop is enough) — do NOT immediately take a second screenshot and do NOT issue extra `launch_app` retries. The title bar substring "Excel" appearing in `active_window` is the canonical "Excel is ready" signal.
- [seed · start-screen] On launch Excel shows the start screen; press Enter (or click "Blank workbook") to get to an empty grid. The empty workbook has the title-bar substring "工作簿1 - Excel" / "Book1 - Excel".
- [seed · enter-formula] Click the target cell, then `type` the formula text (starting with `=`) and press Enter. The cell value updates. To re-read the computed value, click the same cell again — the formula bar (or the cell itself if you take a `screenshot(level='active_window')`) will show it.
- [seed · close] Ctrl+W closes the workbook; if a Save prompt appears use `key text='alt+s'` for Save / `alt+n` for Don't Save.
- [seed · key-Delete-pitfall] After dismissing a save / paste-options popup, do NOT immediately fire `key Delete` blind — the underlying input layer can wedge if focus is briefly on a transient toolbar overlay. Take a fresh `screenshot(level='active_window')` first so the watchdog observes the real focused control. (See E2E run 20260512-182234 O2 wedge.)
"""

LAUNCHER = {
    "name": "Microsoft Excel",
    "description": "Microsoft Excel desktop app. Started via the `excel` App Paths alias (no recursive search).",
    "exe": "excel",
    "process": "EXCEL.EXE",
    "window_title_re": r"Excel",
    "launch_timeout_s": 8.0,
}
