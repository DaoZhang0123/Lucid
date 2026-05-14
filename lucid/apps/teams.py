"""Microsoft Teams desktop app."""

SLUG = "teams"
TITLE = "Microsoft Teams"

TIPS = """\
- [seed · launch] Prefer `launch_app(name='teams')`. If it opens a recent chat / activity view, that already counts as the main window being ready — do NOT restart it just because the left rail isn't on the tab you want yet.
- [seed · focus] If Teams opens behind other windows or keystrokes miss, call `focus_window(title_substring="Microsoft Teams")` before typing.
- [seed · tab] The left rail usually contains Activity / Chat / Teams / Calendar / Calls / Files. For read-only tasks, one L2 of the active window is enough; do NOT click into the message list unless the task explicitly asks to open a chat.
- [seed · self-chat] For self-chat / first-chat tasks, after selecting a chat the message box at the bottom usually has focus already. Prefer `type` directly; don't add an extra click unless the caret is clearly missing.
- [seed · input-bottom] The message composer (`键入消息`) is anchored at the very bottom of the chat pane. If focus is uncertain, first refresh with `screenshot(level="active_window")`, then click once near the bottom composer area as a fallback, and continue with keyboard input.
"""

LAUNCHER = {
    "name": "Microsoft Teams",
    "description": "Microsoft Teams desktop app.",
    "uri": "ms-teams:",
    "exe": "ms-teams",
    "process": "ms-teams.exe|teams.exe",
    "window_title_re": r"Teams|Microsoft Teams|聊天|Chat",
    "launch_timeout_s": 4.0,
}