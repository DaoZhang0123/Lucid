"""Microsoft Photos (UWP).

The protocol handler `ms-photos:` opens the modern Photos app on Windows 10/11
without spawning a visible terminal or relying on `tasklist /V`. Both the
Chinese ("照片") and English ("Photos") window titles are recognised so
window-detection works on zh-CN installs as well.
"""

SLUG = "photos"
TITLE = "Photos"

TIPS = ""

LAUNCHER = {
    "name": "Photos",
    "description": "Microsoft Photos (UWP). Launched via ms-photos: protocol.",
    "uri": "ms-photos:",
    "process": "Microsoft.Photos.exe",
    "window_title_re": r"Photos|照片",
}
