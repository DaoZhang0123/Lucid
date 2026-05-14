"""Microsoft Photos (UWP).

The protocol handler `ms-photos:` opens the modern Photos app on Windows 10/11
without spawning a visible terminal or relying on `tasklist /V`. Both the
Chinese ("照片") and English ("Photos") window titles are recognised so
window-detection works on zh-CN installs as well.
"""

SLUG = "photos"
TITLE = "Photos"

TIPS = """\
- [seed · launch] Prefer `launch_app(name="photos")` (internally via `ms-photos:`).
- [seed · locale-title] On zh-CN systems the window title is often `照片`; on en-US it's usually `Photos`. Treat both as valid ready states and use `focus_window(title_substring="照片")` in Chinese locale when needed.
"""

LAUNCHER = {
    "name": "Photos",
    "description": "Microsoft Photos (UWP). Launched via ms-photos: protocol.",
    "uri": "ms-photos:",
    "process": "Microsoft.Photos.exe",
    "window_title_re": r"Photos|照片",
    "launch_timeout_s": 3.0,
}
