"""Google Chrome."""

SLUG = "chrome"
TITLE = "Google Chrome"

TIPS = """\
- [seed · launch] Prefer `launch_app(name="chrome")`. See the `browser` tips file for cross-browser shortcuts (ctrl+t, ctrl+l, ctrl+shift+t ...).
"""

LAUNCHER = {
    "name": "Google Chrome",
    "description": "Google Chrome browser.",
    "exe": "chrome",
    "process": "chrome.exe",
    "window_title_re": r"Google Chrome",
}
