"""WeChat for Windows (微信)."""

SLUG = "wechat"
TITLE = "WeChat for Windows (微信)"

TIPS = """\
- [seed · launch] Prefer `launch_app(name="wechat")` (it tries the global hotkey + tray + start menu) over double-clicking the desktop icon — duplicate launches are usually rejected by the running instance.
- [seed · tray] WeChat lives in the system tray (the green chat-bubble icon among the small icons on the **right of the bottom taskbar**). To open the main window: right-click the tray icon -> "Show main panel", or just left-click the tray icon.
- [seed · click-contact] After clicking a contact / group in the left chat list, focus **automatically jumps into the input box at the bottom-right** of the window — you do NOT need to click the input box separately. Just `type` your text and press Enter to send. (Useful chain: search contact → Enter → type message → Enter.)
- [seed · click-contact-no-l3] **Don't be tricked by the auto-attached L3 after a contact click.** When you click a contact in the left list, the cursor stays on the contact row but the new chat view opens far to the right. The L3 cursor-local tile (and the post-click pixel-change %) only sees the row's hover-state change — typically <1% — which looks like a "miss" but is **not**. Trust the click landed: just `type` your message next. If you really want visual confirmation, call `screenshot(level="active_window")` for a fresh L2; do **not** keep re-clicking the same contact.
- [seed · input] In the chat input box: **Enter** sends the message immediately, **Shift+Enter** inserts a newline. So if you have a multi-line message, paste the whole block first (newlines stay as soft breaks), then press Enter once to send the whole thing.
- [seed · search] Use Ctrl+F inside WeChat to open the global search box; type a contact / group name to jump straight to that chat.
"""

LAUNCHER = {
    "name": "WeChat",
    "description": "WeChat for Windows (微信). Tray-resident green chat-bubble.",
    "shortcut": "ctrl+alt+w",
    "uri": "weixin://",
    "exe": "wechat",
    "process": "WeChat.exe",
    "window_title": "微信",
}
