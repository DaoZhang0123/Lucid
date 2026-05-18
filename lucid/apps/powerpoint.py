"""Microsoft PowerPoint."""

SLUG = "powerpoint"
TITLE = "Microsoft PowerPoint"

TIPS = """\
- [seed · launch] Use `launch_app(name='powerpoint')` — resolves the `powerpnt` App Paths alias via `start powerpnt`. Do NOT `where powerpnt` / `Get-ChildItem -Recurse`.
- [seed · cold-start] Office cold start shows a splash for 3–8s before the start screen renders. After `launch_app` returns, the screenshot you take in the next step is the canonical "PPT is ready" check. Do NOT issue extra `launch_app` retries when the title bar already contains "PowerPoint".
- [seed · start-screen] On launch PowerPoint shows the start screen. **Press `Enter` to open a Blank Presentation — do NOT click the "Blank Presentation" thumbnail.** The thumbnail's hit region drifts with window size / theme / template-row scroll position; pixel-clicks miss repeatedly (E2E 20260517 O3 — 393s). Enter activates the same "new blank deck" path regardless of layout. The empty deck has the title-bar substring "演示文稿1 - PowerPoint" / "Presentation1 - PowerPoint".
- [seed · title-placeholder] On slide 1, the "Click to add title" placeholder is centred near the top half. Click it once to enter edit mode, then `type` the title text. To exit text mode press Esc.
- [seed · close] Ctrl+W closes the deck; if a Save prompt appears use `key text='alt+s'` for Save / `alt+n` for Don't Save.
"""

LAUNCHER = {
    "name": "Microsoft PowerPoint",
    "description": "Microsoft PowerPoint desktop app. Started via the `powerpnt` App Paths alias (no recursive search).",
    "exe": "powerpnt",
    "process": "POWERPNT.EXE",
    "window_title_re": r"PowerPoint",
    "launch_timeout_s": 8.0,
}
