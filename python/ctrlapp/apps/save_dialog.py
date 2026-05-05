"""Windows save / open file dialogs (no launcher)."""

SLUG = "save-dialog"
TITLE = "Windows save / open file dialogs"

TIPS = """\
- [seed · path] Prefer typing a full absolute path into the filename field and pressing Enter; or click the address bar (path breadcrumb) at the top of the dialog and type a path then Enter. **Do NOT** navigate by clicking through the 'Quick access / This PC' tree on the left — it is slow and error-prone.
- [seed · default-name] When a filename field already has a default value (e.g. *.txt), ctrl+a select-all first then type the new path, to avoid concatenation errors.
- [seed · paths] **Use env vars instead of guessing the username.** The filename box and address bar both expand them: `%USERPROFILE%\\Desktop`, `%USERPROFILE%\\Downloads`, `%USERPROFILE%\\Pictures`, `%LOCALAPPDATA%\\...`, `%APPDATA%\\...`. Also accepts `shell:Downloads`, `shell:Desktop`, etc. Only fall back to `C:\\Users\\<name>\\...` if the dialog refuses to expand env vars (rare — standard Windows dialogs always do).
"""

LAUNCHER = None
