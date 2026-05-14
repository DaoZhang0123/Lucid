"""Windows Notepad."""

SLUG = "notepad"
TITLE = "Notepad"

TIPS = """\
- [seed · prefer-shell-for-file-output] **If the goal is purely "produce a file at path P with content C" — and the instruction does NOT explicitly require a real Notepad typing / open / close chain — the cheap correct path is `run_shell` `powershell -c "Set-Content -LiteralPath '<P>' -Value '<C>' -Encoding utf8"` (or `Out-File ... -Encoding utf8` for multi-line) followed by `Test-Path -LiteralPath '<P>'` to verify.** This is ONE tool call and ~200 ms; the GUI Save-As path can take 200–500s when the filename-box concatenates with the dialog's default suggestion (see E2E run 20260512-182234 C1 — 489s burned in a triple_click+Ctrl+A+Delete loop). **If the instruction explicitly says Open Notepad / type in Notepad / close Notepad, that is a UI-verb chain — drive the real GUI and do NOT short-circuit with Set-Content.**
- [seed · launch] `launch_app(name='notepad')` is enough. Title bar substring "Notepad" / "记事本" = ready.
- [seed · save-as] If you must use the GUI: Ctrl+S on a fresh untitled doc opens the Save dialog; `type` the absolute path directly into the filename field (the box is already selected on open — do NOT pre-`triple_click`). Press Enter. Verify with `run_shell Test-Path` immediately after, BEFORE emitting `task complete`. See the `save-dialog` tip file for filename-box pitfalls.
"""

LAUNCHER = {
    "name": "Notepad",
    "description": "Windows Notepad.",
    "exe": "notepad",
    "process": "notepad.exe",
    "window_title_re": r"Notepad|记事本",
}
