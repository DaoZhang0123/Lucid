"""Microsoft Paint (mspaint)."""

SLUG = "paint"
TITLE = "Paint"

TIPS = """\
- [seed · launch] `launch_app(name='paint')` resolves the `mspaint` App Paths alias. Title bar substring "Paint" / "画图" = ready.
- [seed · cold-start] First launch may take 1–3s for the canvas to render; the system already attaches one L2 after launch_app.
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
