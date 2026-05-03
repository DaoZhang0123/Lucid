"""Microsoft Edge."""

SLUG = "edge"
TITLE = "Microsoft Edge"

TIPS = """\
- [seed · launch] Prefer `launch_app(name="edge")`. See the `browser` tips file for cross-browser shortcuts.
"""

LAUNCHER = {
    "name": "Microsoft Edge",
    "description": "Microsoft Edge browser.",
    "exe": "msedge",
    "process": "msedge.exe",
    "window_title_re": r"Microsoft.+Edge",
}
