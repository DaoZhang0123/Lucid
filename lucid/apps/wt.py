"""Windows Terminal (`wt.exe`)."""

SLUG = "wt"
TITLE = "Windows Terminal"

TIPS = """\
- [seed · launch] Prefer `launch_app(name='wt')`. If it isn't installed, fall back to `launch_app(name='powershell')` or `launch_app(name='cmd')` rather than guessing taskbar icons.
- [seed · title] Fresh WT windows often title themselves by the active profile (`Windows PowerShell`, `PowerShell`, `Command Prompt`) rather than the literal words `Windows Terminal`; treat those as valid ready states.
- [seed · keyboard] Once the prompt is visible, command entry is a pure keyboard chain: `type` the command, then `key text='Return'`. If you only need the printed scalar output, the first screenshot / title read containing it should be the same turn that emits `task complete:`.
"""

LAUNCHER = {
    "name": "Windows Terminal",
    "description": "Windows Terminal (`wt.exe`). Falls back to PowerShell semantics via profile titles.",
    "exe": "wt.exe",
    "process": "WindowsTerminal.exe|wt.exe",
    "window_title_re": r"Windows Terminal|Windows PowerShell|PowerShell|Command Prompt|终端|命令提示符",
    "launch_timeout_s": 4.0,
}