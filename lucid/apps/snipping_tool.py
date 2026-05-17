"""Windows Snipping Tool (modern Snip & Sketch / Screen Sketch)."""

SLUG = "snipping-tool"
TITLE = "Snipping Tool"

TIPS = """\
- [seed · launch] Prefer `launch_app(name="snipping-tool")`. Fallback: Win+R → `ms-screenclip:` (URI scheme — opens the modern overlay reliably on Win10/11). Plain `snippingtool.exe` is the legacy host on Win10 and may be missing on Win11.
- [seed · shortcut] `Win+Shift+S` is the OS-wide hotkey that puts the screen into snip-overlay mode without opening the main Snipping Tool window at all — fastest path when the user just wants ONE clip. Result lands on the clipboard AND in the Snipping Tool notification toast.
- [seed · overlay-modes] Top-center of the overlay: rectangular (default), freeform, window, fullscreen. Pick with the keyboard: `Tab` cycles, `Enter` selects. Press `Esc` to cancel the overlay without capturing.
- [seed · capture-to-clipboard] By default every snip is copied to the clipboard. To paste it: focus the destination app, `key Ctrl+V`. Don't open the Snipping Tool editor unless you need to annotate / save — clipboard is enough for "paste into chat" tasks.
- [seed · save] In the post-snip editor: `Ctrl+S` opens Save As; type an explicit absolute path with extension (`.png` / `.jpg`); press Enter; verify with `run_shell Test-Path` BEFORE `task complete`. See save-dialog tips for filename-box pitfalls.
- [seed · close] `Alt+F4` or `Esc` closes the overlay / editor.
- [seed · agent-alternative] **If the goal is "give Lucid a screenshot to look at", do NOT use Snipping Tool — call the `screenshot` tool directly** (`level="active_window"` or `level="full"`). Snipping Tool only makes sense when the user explicitly wants a clip saved to disk / clipboard / shared into another app.
"""

LAUNCHER = {
    "name": "Snipping Tool",
    "description": "Windows modern Snipping Tool / Snip & Sketch.",
    "uri": "ms-screenclip:",
    "exe": "snippingtool",
    "process": "SnippingTool.exe|ScreenClippingHost.exe|ScreenSketch.exe",
    "window_title_re": r"Snipping Tool|截图工具|Snip & Sketch",
    "launch_timeout_s": 3.0,
}
