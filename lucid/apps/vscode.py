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


# Region calibration spec — see Docs/regions.md §10 (v2.2). VS Code is
# Electron + Chromium, so the most stable selector is AutomationId, which
# doesn't change with UI locale. ``REGIONS_UIA_SPEC`` is consumed by
# ``lucid.regions._calibrate_via_uia`` (AutomationId first, then name fallback).
REGIONS_UIA_SPEC = {
    "activity_bar": {
        "automation_id": "workbench.parts.activitybar",
        "description": "Left-edge vertical strip with explorer / search / git / extensions icons.",
    },
    "primary_sidebar": {
        "automation_id": "workbench.parts.sidebar",
        "description": "Primary sidebar (file explorer / search / etc.).",
    },
    "editor": {
        "automation_id": "workbench.parts.editor",
        "description": "Center editor area where files are open in tabs.",
    },
    "panel": {
        "automation_id": "workbench.parts.panel",
        "description": "Bottom panel (terminal / output / problems).",
    },
    "status_bar": {
        "automation_id": "workbench.parts.statusbar",
        "description": "Bottom status bar.",
    },
}
