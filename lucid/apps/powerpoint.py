"""Microsoft PowerPoint."""

SLUG = "powerpoint"
TITLE = "Microsoft PowerPoint"

TIPS = """\
- [seed · launch] Use `launch_app(name='powerpoint')` — resolves the `powerpnt` App Paths alias via `start powerpnt`. Do NOT `where powerpnt` / `Get-ChildItem -Recurse`.
- [seed · cold-start-budget] Office cold start can take **30–90s** on first launch after boot or with slow SSDs/many add-ins — not the 3–8s warm path. **If `launch_app('powerpoint')` returns a timeout error, do NOT retry `launch_app` and do NOT immediately retry `focus_window` — PowerPoint is still mid-init and the second `launch_app` will often wedge the Win32 enum call for another 25s.** Fallback: `run_shell({'command':'start powerpnt', 'shell':'cmd', 'timeout_s':10})` — fire-and-forget. Then take one `screenshot(level='active_window')` per step; the title-bar substring "PowerPoint" is the canonical "ready" signal.
- [seed · start-screen] On launch PowerPoint shows the start screen. **Press `Enter` to open a Blank Presentation — do NOT click the "Blank Presentation" thumbnail.** The thumbnail's hit region drifts with window size / theme / template-row scroll position; pixel-clicks miss repeatedly (E2E 20260517 O3 — 393s). Enter activates the same "new blank deck" path regardless of layout. The empty deck has the title-bar substring "演示文稿1 - PowerPoint" / "Presentation1 - PowerPoint".
- [seed · title-placeholder] On slide 1, the "Click to add title" placeholder is centred near the top half. Click it once to enter edit mode, then `type` the title text. To exit text mode press Esc.
- [seed · close] Ctrl+W closes the deck; if a Save prompt appears use `key text='alt+s'` for Save / `alt+n` for Don't Save.
- [seed · prefer-skill-for-authoring] **Driving PowerPoint via UI is excellent for editing one slide, hunting bugs, or showing the user what you did** — it is NOT a good way to author a multi-slide deck from scratch (click-typing every textbox burns tens of seconds per slide and pixel hits drift). For "make me a deck about X" tasks: first call `search_skills(query="create powerpoint pptx")` — if the user enabled the `anthropics/skills` repo it returns a real `document-skills/pptx` SKILL.md that authors the file programmatically (python-pptx). Install it with `install_repo_skill`, then `read_skill` and follow the body. Fall back to UI only if no repo is enabled or no skill matches. UI is still the right tool for *editing* an existing deck the user opens.
- [seed · open-finished-deck] After you write the .pptx file, **open it with one PowerShell call**: `run_shell(shell="powershell", command="Start-Process powerpnt -ArgumentList '/r','<absolute_path>.pptx'")`. Then `focus_window(title_substring='<filename without extension>')` and screenshot — that's the canonical "deck is ready" check. **Do NOT use `run_shell(shell="cmd", command='start powerpnt /r "<path>"')`** — Python's subprocess escapes the inner `"` as `\"`, cmd.exe doesn't understand that escape, and PowerPoint receives the path with literal `\"...\"` wrapped around it ("无法读取 \"C:\\…\\file.pptx\""). `Start-Process -ArgumentList` passes each argv element cleanly with no quoting drama. Do NOT relaunch PowerPoint blank then File → Open → navigate; that's 6+ extra steps and triggers the start-screen pixel-click trap.
"""

LAUNCHER = {
    "name": "Microsoft PowerPoint",
    "description": "Microsoft PowerPoint desktop app. Started via the `powerpnt` App Paths alias (no recursive search).",
    "exe": "powerpnt",
    "process": "POWERPNT.EXE",
    "window_title_re": r"PowerPoint",
    # Bumped from 8.0 → 20.0: Office cold-start can take 15-20s before the
    # window is enumerable. Outer launch_app watchdog is 25s. See excel.py.
    "launch_timeout_s": 20.0,
}
