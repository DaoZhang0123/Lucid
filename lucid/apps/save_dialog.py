"""Windows save / open file dialogs (no launcher)."""

SLUG = "save-dialog"
TITLE = "Windows save / open file dialogs"

TIPS = """\
- [seed · path] Prefer typing a full absolute path into the filename field and pressing Enter; or click the address bar (path breadcrumb) at the top of the dialog and type a path then Enter. **Do NOT** navigate by clicking through the 'Quick access / This PC' tree on the left — it is slow and error-prone.
- [seed · filename-box-preselected] On Win10/11 the filename ComboBox is **already fully selected** when the dialog opens. Do NOT pre-`triple_click` it — that often shifts focus to a path-segment / suggestion list and the next `type` lands in the wrong control, producing a silently-empty save. Just `type` the absolute path directly; the existing selection is overwritten.
- [seed · default-name] When a filename field already has a default value (e.g. *.txt) that you can't tell whether it's selected, ctrl+a select-all first then type the new path, to avoid concatenation errors.
- [seed · filename-corrupted-bail-out] **If the first save attempt produced a corrupted filename** (concatenated with the dialog's default suggestion, or `\\` rendered as `、` / full-width chars from an active CJK IME), **do NOT iterate `triple_click` + `Ctrl+A` + `Delete` cycles to recover** — that just digs the hole deeper (E2E run 20260512-182234 C1 burned 250s in this loop). Bail with `task failed: save dialog filename corrupted` and stop. Reissuing keystrokes against an already-confused dialog will not converge.
- [seed · ime-paths] **Path / backslash IME guard**: when typing a filesystem path (`%USERPROFILE%\\...`, `C:\\...`) in any Save dialog, the input driver routes 100% of `type` through clipboard paste (WM_PASTE), which IMEs do not see. Just call `type` with the literal path. If the resulting screenshot ever shows `、` instead of `\\`, that's an input-driver bug, not something to retry.
- [seed · paths] **Use env vars instead of guessing the username.** The filename box and address bar both expand them: `%USERPROFILE%\\Desktop`, `%USERPROFILE%\\Downloads`, `%USERPROFILE%\\Pictures`, `%LOCALAPPDATA%\\...`, `%APPDATA%\\...`. Also accepts `shell:Downloads`, `shell:Desktop`, etc. Only fall back to `C:\\Users\\<name>\\...` if the dialog refuses to expand env vars (rare — standard Windows dialogs always do).
"""

LAUNCHER = None
