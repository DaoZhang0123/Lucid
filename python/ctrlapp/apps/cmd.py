"""Windows Command Prompt (cmd.exe)."""

SLUG = "cmd"
TITLE = "Windows Command Prompt (cmd.exe)"

TIPS = """\
- [seed · launch] Prefer `launch_app(name="cmd")`. Fallbacks: Win+R → `cmd` → Enter, or Win+X then press `C` (only on systems where the legacy menu still shows Command Prompt). To open AS ADMIN, Win+X → `A` (admin terminal) — note we generally avoid elevated windows; only use if explicitly asked.
- [seed · always-new-window] **ALWAYS open a NEW cmd window for your commands — never reuse a cmd window the user already has open.** That window may be running the user's own long task (build, server, REPL, watch loop) and typing into it would interrupt or corrupt it. Even if `find_app("cmd")` reports an existing window, call `launch_app("cmd")` to spawn a fresh one and operate there. The only exception: the user explicitly tells you to use their existing terminal.
- [seed · run-command] To run a one-shot command: `type` the command, then `key` Return. The whole line is pasted via clipboard so paths with spaces / CJK work without quoting tricks; you still need to quote yourself if cmd.exe needs the quotes (e.g. `dir "C:\\Program Files"`).
- [seed · cd-quoting] cmd.exe is picky: `cd /d "D:\\some path\\foo"` (note `/d` to also switch drive). On `&` / `^` / `%` in arguments, escape with `^` (e.g. `echo a ^& b`).
- [seed · output-capture] **Pick the fastest method that gives you the accuracy you need** — don't assume "screenshot the console" is the only option:
  1) **Short output (a few lines, fits in one console page)**: just `screenshot(level="active_window")` of the cmd window and transcribe the salient lines into your assistant text (the screenshot will be downscaled later). Easiest path; no extra commands.
  2) **Long / multi-line / precise output you must read verbatim** (file lists, error stacks, paths with funny chars): redirect to a file then open it in Notepad — e.g. `dir /b > %TEMP%\\out.txt && notepad %TEMP%\\out.txt`. The Notepad render is **much sharper** than the console bitmap and survives later JPEG compression. Screenshot Notepad and transcribe.
  3) **Output you only need to feed back into another command**: `clip` puts it on the clipboard — `dir /b | clip` — then paste it (Ctrl+V) into the target App's input box. Skips the read-then-retype round-trip entirely.
  4) **One-shot questions you only need the answer to** (e.g. "what's the username?", "is process X running?"): pipe to `> %TEMP%\\out.txt && notepad %TEMP%\\out.txt` so you read 1 word from a clean Notepad page instead of squinting at the prompt-cluttered console.
  Whichever you pick, **transcribe the parts that matter into your assistant text this turn** — images age out, text doesn't.
- [seed · clear] Type `cls` + Enter to clear the screen before a fresh command if previous output is cluttering the view.
- [seed · history] Up/Down arrow keys cycle previous commands; F7 opens a small history popup (visual list, arrow + Enter to pick).
- [seed · select-copy] cmd.exe has "QuickEdit" mode by default in Windows 10/11 — left-click-drag selects a rectangular block of text; press Enter (or right-click) to copy to clipboard. Right-click again pastes. Avoid clicking inside the window when a command is running unless you intend to select — selecting **pauses** command output until you press Esc.
- [seed · stop] Ctrl+C interrupts the current command. Ctrl+Break is the harder kill (some long-running tools ignore Ctrl+C).
- [seed · exit] `exit` + Enter closes the window. Closing the window with the `X` while a command is running will kill the command.
- [seed · prefer-powershell] For anything beyond trivial one-liners (loops, JSON, file globs, env vars, error handling), prefer launching PowerShell instead — it's far more capable and the syntax is more predictable.
"""

LAUNCHER = {
    "name": "Command Prompt",
    "description": "Windows Command Prompt (cmd.exe). Legacy DOS-style shell.",
    "shortcut": "win+r",  # Fallback path: Win+R → type 'cmd' → Enter (handled below by exe).
    "exe": "cmd",
    "process": "cmd.exe",
    "window_title_re": r"Command Prompt|命令提示符|cmd\.exe",
}
