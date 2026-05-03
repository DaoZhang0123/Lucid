"""Visual Studio Code."""

SLUG = "vscode"
TITLE = "Visual Studio Code"

TIPS = """\
- [seed · launch] Prefer `launch_app(name="vscode")`. To open a folder, `code "<path>"` from a terminal also works.
- [seed · cmd-palette] Ctrl+Shift+P opens the command palette — almost any action can be reached by typing a few letters there. Prefer this over hunting in menus.
- [seed · file] Ctrl+P quick-opens a file by name; Ctrl+B toggles sidebar; Ctrl+` toggles terminal panel.
- [seed · save] Ctrl+S saves; Ctrl+K S saves all; Ctrl+W closes editor tab.
"""

LAUNCHER = {
    "name": "Visual Studio Code",
    "description": "Visual Studio Code editor.",
    "uri": "vscode://",
    "exe": "code",
    "process": "Code.exe",
    "window_title_re": r"Visual Studio Code",
}
