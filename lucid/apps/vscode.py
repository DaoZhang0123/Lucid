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


# Region spec for live UIA lookup (lucid.regions.region).
#
# VS Code is Electron + Chromium. By default the web-app's a11y tree is NOT
# exposed through UIA (RootWebArea sits under Chrome_WidgetWin_1 but has no
# children unless `--force-renderer-accessibility` or a screen reader is
# active). That means the workbench parts (`workbench.parts.activitybar`
# /sidebar/editor/panel) historically advertised here never actually resolved
# — they returned no match and the model wasted a click. Removed.
#
# Prefer keyboard:
#   Ctrl+Shift+P    command palette (anything reachable)
#   Ctrl+P          quick-open file
#   Ctrl+B          toggle primary sidebar
#   Ctrl+`          toggle terminal panel
#   Ctrl+Shift+E/F/G/X   focus explorer / search / git / extensions
#
# `status_bar` is kept because it does resolve (matches the Chromium widget's
# bottom strip, ~22 px high) and is occasionally useful for OCR-ing the
# branch / line-col indicator.
REGIONS_UIA_SPEC = {
    "status_bar": {
        "automation_id": "workbench.parts.statusbar",
        "description": "Bottom status bar (branch, line/col, language, problems count).",
    },
}
