"""Microsoft Outlook."""

SLUG = "outlook"
TITLE = "Microsoft Outlook"

TIPS = """\
- [seed · launch] Prefer `launch_app(name="outlook")` — it tries `start outlook` (App Paths alias).
- [seed · reply] Ctrl+R reply, Ctrl+Shift+R reply-all, Ctrl+F forward, Ctrl+N new mail. Ctrl+Enter sends the current draft.
- [seed · navigate] Ctrl+1 inbox, Ctrl+2 calendar, Ctrl+3 contacts; Ctrl+Y picks a folder by name.
"""

LAUNCHER = {
    "name": "Outlook",
    "description": "Microsoft Outlook email client.",
    "uri": "ms-outlook://",
    "exe": "outlook",
    "process": "OUTLOOK.EXE",
    "window_title_re": r"Outlook",
}
