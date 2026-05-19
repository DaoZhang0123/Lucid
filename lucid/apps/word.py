"""Microsoft Word (winword)."""

SLUG = "word"
TITLE = "Microsoft Word"

TIPS = """\
- [seed · launch] Use `launch_app(name='word')` — it goes through `start winword`, which resolves the App Paths alias without a recursive disk search. Do NOT `where winword` or `Get-ChildItem -Recurse` for the exe; that takes ~25s and the alias is enough.
- [seed · cold-start] Office cold start shows a splash screen for 3–8s before the start screen renders. After `launch_app` returns, the title-bar substring "Word" in `active_window` is the canonical "ready" signal — do NOT issue extra `launch_app` retries while the splash is up, and do NOT immediately take a second screenshot before the splash dismisses (one screenshot 2–3s after launch is enough).
- [seed · start-screen] On launch Word shows the start screen; press Enter (or click "Blank document") to get to an empty doc. The empty doc has the title-bar substring "文档1 - Word" / "Document1 - Word".
- [seed · title-bar-is-truth] When the task asks you to "read the title bar / report the document title", the value you get from `active_window` IS the ground truth — do NOT click anywhere just to "see the title better" and do NOT screenshot again before reporting it. One tool call (the `active_window` or the screenshot that contains it) is enough.
- [seed · save-as] Ctrl+S on a fresh doc opens Backstage "Save As"; press `Ctrl+S` then type the absolute path directly in the filename field of the Save dialog (works even if the sidebar shows OneDrive by default). Wait for the dialog to dismiss, then verify with `run_shell Test-Path`.
- [seed · close] Ctrl+W closes the document; if a Save prompt appears use `key text='alt+s'` for Save / `alt+n` for Don't Save.
- [seed · prefer-skill-for-authoring] **Word UI is fine for one-paragraph edits or showing the user what changed** — it is NOT a good way to generate a long, structured document (multi-page report, formatted resume, table-heavy spec). For "draft me a docx about X" tasks: first call `search_skills(query="create word docx")` — if the user enabled the `anthropics/skills` repo it returns a real `document-skills/docx` SKILL.md that builds the file programmatically (python-docx). Install with `install_repo_skill`, then `read_skill` and follow the body. Fall back to UI when the user explicitly asks you to type into Word, or when no skill is available.
"""

LAUNCHER = {
    "name": "Microsoft Word",
    "description": "Microsoft Word desktop app. Started via the `winword` App Paths alias (no recursive search).",
    "exe": "winword",
    "process": "WINWORD.EXE",
    "window_title_re": r"Word",
    "launch_timeout_s": 8.0,
}
