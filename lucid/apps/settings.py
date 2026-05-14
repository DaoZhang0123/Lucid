"""Windows Settings (URI-only)."""

SLUG = "settings"
TITLE = "Windows Settings"

TIPS = """\
- [seed · launch] Prefer `launch_app(name="settings")` for generic Settings tasks.
- [seed · about-page] For OS/version queries, use `launch_app(name="settings", page="about")` to deep-link directly to About (`ms-settings:about`) instead of navigating through the left rail.
- [seed · verification] If the task asks for edition/version/build text, take one `screenshot(level="active_window")` on the About page and transcribe from that L2; avoid registry fallback for UI-verb tasks.
"""

LAUNCHER = {
    "name": "Windows Settings",
    "description": "Windows 11 Settings app.",
    "uri": "ms-settings:",
    "process": "SystemSettings.exe|ApplicationFrameHost.exe",
    "window_title_re": r"Settings|设置",
    "launch_timeout_s": 3.0,
}
