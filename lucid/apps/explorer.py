"""Windows File Explorer."""

SLUG = "explorer"
TITLE = "Windows File Explorer"

TIPS = """\
- [seed · launch] Win+E opens a new File Explorer window.
- [seed · address] Click the address bar (or Ctrl+L / Alt+D) and type a full path then Enter — much faster than clicking through the sidebar tree.
- [seed · user-folders-no-username-needed] **You almost never need to know the user's Windows account name.** Windows expands env vars in any path field (address bar, filename box, Win+R, Run dialog, save/open dialogs, even most app dialogs):
  - Downloads: `%USERPROFILE%\\Downloads`  (NOT `C:\\Users\\<name>\\Downloads`)
  - Desktop:   `%USERPROFILE%\\Desktop`
  - Documents: `%USERPROFILE%\\Documents`  (or the shell shortcut `shell:Documents`)
  - Pictures:  `%USERPROFILE%\\Pictures`   (or `shell:My Pictures`)
  - AppData:   `%LOCALAPPDATA%\\...` and `%APPDATA%\\...`
  Other handy `shell:` shortcuts that work in the address bar AND Win+R: `shell:Downloads`, `shell:Desktop`, `shell:Startup`, `shell:SendTo`, `shell:RecycleBinFolder`. Use these by default; resolve the literal `C:\\Users\\<name>\\...` form ONLY when an App stubbornly refuses to expand env vars.
- [seed · get-username-when-needed] If you genuinely need the literal name (e.g. the App rejects `%USERPROFILE%`), the cheapest options:
  1) Look at the address-bar breadcrumb of any open Explorer window pointing into your home folder — it shows `> <username> >`.
  2) Win+R → `cmd /k echo %USERNAME%` → read off the line. Or in PowerShell: `$env:USERNAME` / `whoami`.
  3) The path of any file under `C:\\Users\\` already in a screenshot.
  Don't burn a step on this if a `%USERPROFILE%`-based path will do.
- [seed · sort-by-date] To find the **latest** file in a folder: View tab → Sort by → Date modified → Descending (or click the "Date modified" column header twice). Or just use `Sort-Object LastWriteTime -Descending | Select -First 1` from PowerShell on the same folder.
- [seed · select] Ctrl+A select all, F2 rename, Delete moves to recycle bin, Shift+Delete permanent delete (avoid unless asked).
- [seed · count-via-shell] **"How many files/subfolders in <path>" → ground-truth via PowerShell, do NOT eyeball the icon grid.** Visual counting at small icons is unreliable (workspace fold / hidden-files toggles / virtualised scrolling all skew the visible count). When the task is UI-verbed as "open File Explorer, navigate to <path>, tell me how many …", the navigation is the UI part; the count itself is data: `run_shell powershell -c "(Get-ChildItem -LiteralPath '<P>' -Directory | Measure-Object).Count"` for subfolders, `-File` for files, drop both flags for everything. Override the shell number only if the user explicitly says "as the UI displays".
"""

LAUNCHER = {
    "name": "File Explorer",
    "description": "Windows File Explorer.",
    "shortcut": "win+e",
    "exe": "explorer",
    "process": "explorer.exe",
}
