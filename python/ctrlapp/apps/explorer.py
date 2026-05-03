"""Windows File Explorer."""

SLUG = "explorer"
TITLE = "Windows File Explorer"

TIPS = """\
- [seed · launch] Win+E opens a new File Explorer window.
- [seed · address] Click the address bar (or Ctrl+L / Alt+D) and type a full path then Enter — much faster than clicking through the sidebar tree.
- [seed · select] Ctrl+A select all, F2 rename, Delete moves to recycle bin, Shift+Delete permanent delete (avoid unless asked).
"""

LAUNCHER = {
    "name": "File Explorer",
    "description": "Windows File Explorer.",
    "shortcut": "win+e",
    "exe": "explorer",
    "process": "explorer.exe",
}
