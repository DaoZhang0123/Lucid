"""Microsoft Outlook."""

SLUG = "outlook"
TITLE = "Microsoft Outlook"

TIPS = """\
- [seed · launch] Prefer `launch_app(name="outlook")` — it tries `start outlook` (App Paths alias).
- [seed · reply] Ctrl+R reply, Ctrl+Shift+R reply-all, Ctrl+N opens a new compose window, Ctrl+F forward, Ctrl+N new mail. Ctrl+Enter sends the current draft.
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


# Region calibration spec — see Docs/regions.md §10 (v2.3). Outlook (Win32)
# exposes localised Names; we list candidate strings for zh-CN / en / fr.
# ``REGIONS_UIA_SPEC`` is consumed by ``lucid.regions._calibrate_via_uia``.
REGIONS_UIA_SPEC = {
    "folder_pane": {
        "name_candidates": [
            "\u90ae\u4ef6",       # 邮件
            "Mail",
            "Courrier",
            "\u6536\u4ef6\u7bb1",  # 收件箱
            "Inbox",
            "Bo\u00eete de r\u00e9ception",
        ],
        "description": "Left folder list (Inbox / Sent / Drafts ...).",
    },
    "reading_pane": {
        "name_candidates": [
            "\u9605\u8bfb\u7a97\u683c",  # 阅读窗格
            "Reading pane",
            "Volet de lecture",
        ],
        "description": "Right reading pane showing the selected message body.",
    },
}
