"""Microsoft Teams desktop app."""

SLUG = "teams"
TITLE = "Microsoft Teams"

TIPS = """\
- [seed · launch] Prefer `launch_app(name='teams')`. If it opens a recent chat / activity view, that already counts as the main window being ready — do NOT restart it just because the left rail isn't on the tab you want yet.
- [seed · focus] If Teams opens behind other windows or keystrokes miss, call `focus_window(title_substring="Microsoft Teams")` before typing.
- [seed · tab] The left rail usually contains Activity / Chat / Teams / Calendar / Calls / Files. For read-only tasks, one L2 of the active window is enough; do NOT click into the message list unless the task explicitly asks to open a chat.
- [seed · self-chat] For self-chat / first-chat tasks, after selecting a chat the message box at the bottom usually has focus already. Prefer `type` directly; don't add an extra click unless the caret is clearly missing.
- [seed · compose-region-canonical] **The ONLY reliable way to focus the Teams compose box is `region(app="teams", name="input_box")` followed by `left_click` at its returned `center`, then `type` + `Return`.** Do NOT eyeball y=1080 / y=965 / y=937 from an L2 screenshot — every banner ("Dao Zhang 是外部组织的一部分..."), every sidebar pop-out, every DPI tweak shifts the bottom anchor by 30-100px and a single-pixel miss puts you on a chat bubble (which then opens the emoji-reaction popup). Three or more pixel-guess clicks at the bottom = protocol violation; bail with `task failed:` instead. (Note: Teams' advertised `Ctrl+Shift+X` compose-focus shortcut does NOT work on current Windows builds — don't try it.)
- [seed · reaction-popup-trap] If after a click + `type` you see a small floating row of emoji (👍 ❤️ 😂 😯 😢 ✔️) instead of your text in the composer, the click landed on a chat bubble and opened its reaction picker, NOT the composer. Press `Escape` once to dismiss, then go straight to `region(app="teams", name="input_box")` — do NOT re-click the same area.
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


# Region calibration spec consumed by lucid/regions.py::_calibrate_via_uia.
# Each entry says "to find the screen rect of <region>, ask UIA for the first
# element under the Teams main window whose Name matches one of these
# candidates" (multiple to cover localised builds). See Docs/regions.md §3.1.
REGIONS_UIA_SPEC = {
    "input_box": {
        "name_candidates": [
            "\u952e\u5165\u6d88\u606f",         # 键入消息
            "Type a message",
            "Type a new message",
            "Tapez un message",                  # fr-FR
            "\u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435",  # ru-RU
        ],
        "description": "Compose box at the bottom of the active chat pane.",
    },
    "send_button": {
        "name_candidates": [
            "\u53d1\u9001",                      # 发送
            "Send",
            "Envoyer",
            "\u041e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c",  # Отправить
        ],
        "description": "Send button next to the compose box.",
    },
}
