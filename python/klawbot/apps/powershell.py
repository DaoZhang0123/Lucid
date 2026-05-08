"""Windows PowerShell / PowerShell 7 (pwsh)."""

SLUG = "powershell"
TITLE = "Windows PowerShell / PowerShell 7"

TIPS = """\
- [seed · launch] Prefer `launch_app(name="powershell")`. Fallbacks: Win+R → `powershell` (Windows PowerShell 5.1, ships with Windows) or `pwsh` (PowerShell 7+, only if installed). Win+X → `I` opens "Windows Terminal" with the default profile (often PowerShell). Avoid running as admin unless explicitly asked.
- [seed · always-new-window] **ALWAYS open a NEW PowerShell window for your commands — never reuse a PowerShell / Windows Terminal window the user already has open.** That window may be running the user's own long task (dev server, build watch, REPL, SSH session, script in progress) and your keystrokes would interrupt or corrupt it. Even if `find_app("powershell")` reports an existing window, call `launch_app("powershell")` to spawn a fresh one and work there. The only exception: the user explicitly tells you to use their existing terminal.
- [seed · prefer-over-cmd] For anything beyond a trivial one-liner, prefer PowerShell over cmd.exe — it has objects, pipelines, JSON support, sane error handling, and rich cmdlets.
- [seed · run-command] To run a one-shot command: `type` the command, then `key` Return. Whole line is pasted via clipboard so CJK / paths-with-spaces / quotes all survive intact.
- [seed · paths] Use forward OR back slashes; PowerShell accepts both. Wrap paths with spaces in single quotes (literal) or double quotes (interpolated): `Get-ChildItem 'C:\\Program Files'`. Use `$env:USERPROFILE` etc. for env vars (NOT `%USERPROFILE%`).
- [seed · chaining] **Use `;` to separate commands on one line, NOT `&&`** (Windows PowerShell 5.1 does NOT support `&&` / `||`; only PowerShell 7+ does). Example: `cd D:\\repo; git pull; npm install`.
- [seed · pipelines] Prefer object pipelines: `Get-ChildItem | Where-Object Length -gt 1MB | Sort-Object LastWriteTime -Descending | Select-Object -First 5 FullName,Length`. Use `Format-Table -AutoSize` (alias `ft`) only at the END of a pipeline for human-readable output.
- [seed · cmdlet-discovery] `Get-Command *keyword*` finds cmdlets by name; `Get-Help <cmdlet> -Examples` shows usage. Tab completion works for cmdlet names AND parameter names.
- [seed · output-capture] **Pick the fastest method that gives you the accuracy you need** — don't lock yourself into "screenshot the console":
  1) **Short output (one screen)**: `screenshot(level="active_window")` and transcribe. Quickest.
  2) **Long / verbatim output** (file lists, JSON, error stacks): pipe to a file then open Notepad — the rendered text is **much sharper** and survives later downscaling. Examples: `Get-ChildItem | Out-File $env:TEMP\\out.txt; notepad $env:TEMP\\out.txt`  or  `... | ConvertTo-Json -Depth 5 | Out-File ...; notepad ...`.
  3) **Output you'll feed into another App**: `Set-Clipboard` skips reading entirely — `Get-ChildItem | Out-String | Set-Clipboard`, then paste (Ctrl+V) into the destination input box.
  4) **One-shot scalar questions** ("what's the username?", "how many files match X?"): use `Out-File` + `notepad` so you read 1 line from a clean page instead of squinting at the prompt-cluttered console. Or pipe to `Set-Clipboard` and paste into the consuming App.
  5) **Interactive paging** for medium output you only need to skim: `Out-Host -Paging`.
  Whichever you pick, **transcribe the parts that matter into your assistant text this turn** — images age out, text doesn't.
- [seed · clear] `Clear-Host` (alias `cls`) clears the screen.
- [seed · history] Up/Down arrows cycle previous commands; `Get-History` lists them; `Invoke-History <id>` re-runs. F7 popup also works.
- [seed · select-copy] In modern Windows Terminal: click-drag to select, Ctrl+Shift+C to copy, Ctrl+Shift+V (or right-click) to paste. In legacy console host: QuickEdit mode is on by default — left-click-drag selects, Enter copies. Selecting **pauses** output in legacy host (press Esc to resume).
- [seed · stop] Ctrl+C interrupts the running pipeline / command. Some native exes ignore it; closing the tab/window then kills the process.
- [seed · errors] By default a non-terminating error doesn't stop the script. To force stop: `$ErrorActionPreference = 'Stop'` at the top, or per-cmdlet `-ErrorAction Stop`. Wrap risky calls in `try { ... } catch { Write-Host $_ }`.
- [seed · exec-policy] If a `.ps1` script refuses to run with "running scripts is disabled on this system", run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned` in the same session (does NOT persist beyond the window).
- [seed · exit] `exit` + Enter closes the session. Closing the window while a long-running cmdlet is in progress will kill it.
"""

LAUNCHER = {
    "name": "PowerShell",
    "description": "Windows PowerShell (5.1) or PowerShell 7 (pwsh).",
    "shortcut": "win+r",
    "exe": "powershell",
    "process": "powershell.exe",
    "window_title_re": r"PowerShell|pwsh|Windows PowerShell",
}
