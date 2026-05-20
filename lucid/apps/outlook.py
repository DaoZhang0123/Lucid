"""Microsoft Outlook."""

SLUG = "outlook"
TITLE = "Microsoft Outlook"

TIPS = """\
- [seed · launch] Prefer `launch_app(name="outlook")` — it tries `start outlook` (App Paths alias).
- [seed · cold-start-budget] Office cold start can take **30–90s** on first launch after boot or with slow SSDs/many add-ins. **If `launch_app('outlook')` returns a timeout error, do NOT retry `launch_app` and do NOT immediately retry `focus_window` — Outlook is still mid-init.** Fallback: `run_shell({'command':'start outlook', 'shell':'cmd', 'timeout_s':10})` — fire-and-forget. Then take one `screenshot(level='active_window')` per step; the title-bar substring "Outlook" is the canonical "ready" signal.
- [seed · reply] Ctrl+R reply, Ctrl+Shift+R reply-all, Ctrl+N opens a new compose window, Ctrl+F forward, Ctrl+N new mail. Ctrl+Enter sends the current draft.
- [seed · navigate] Ctrl+1 inbox, Ctrl+2 calendar, Ctrl+3 contacts; Ctrl+Y picks a folder by name.
- [seed · truncated-subject] Inbox-row subjects are clipped to the column width with an ellipsis (…). To read the full subject, either (a) **hover** the row and wait ~200ms for the native tooltip then take ONE `screenshot(level="active_window")`, or (b) press `Enter` to open the message and read the title bar / header. Do NOT infer the trailing words from the visible prefix — you'll guess wrong for similar-prefix subjects (e.g. multiple "Re: Project update …" rows).
"""

LAUNCHER = {
    "name": "Outlook",
    "description": "Microsoft Outlook email client.",
    "uri": "ms-outlook://",
    "exe": "outlook",
    "process": "OUTLOOK.EXE",
    "window_title_re": r"Outlook",
    # Office cold-start can take 15-20s before the window is enumerable.
    # Outer launch_app watchdog is 25s. See excel.py.
    "launch_timeout_s": 20.0,
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
