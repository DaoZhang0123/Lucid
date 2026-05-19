"""Microsoft Excel."""

SLUG = "excel"
TITLE = "Microsoft Excel"

TIPS = """\
- [seed · launch] Use `launch_app(name='excel')` — resolves the `excel` App Paths alias via `start excel`. Do NOT `where excel` / `Get-ChildItem -Recurse`.
- [seed · cold-start] Office cold start shows a splash screen for 3–8s before the start screen renders. After `launch_app` returns, wait for the next user message (the screenshot loop is enough) — do NOT immediately take a second screenshot and do NOT issue extra `launch_app` retries. The title bar substring "Excel" appearing in `active_window` is the canonical "Excel is ready" signal.
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
    "launch_timeout_s": 8.0,
}
