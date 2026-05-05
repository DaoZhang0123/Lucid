"""Calculator."""

SLUG = "calculator"
TITLE = "Calculator"

TIPS = """\
- [seed · prefer-shell] **For pure arithmetic, prefer PowerShell / Python over this GUI.** A one-liner like `launch_app("powershell")` then `type "[math]::Sqrt(2) * 1024"` (or `python -c "print(2**32-1)"`) returns text you can read directly — no screenshot/OCR round-trip, no click errors. Only open Calculator when the user explicitly asks for it, or when you genuinely need its specialized modes (Programmer bit-toggle visualization, Date-difference, Currency/Unit converter with offline rates).
- [seed · keyboard-input] **Type with the keyboard — do NOT click number/operator buttons.** All digits, `+ - * /`, `.`, `Enter` (=), `Backspace`, `Esc` (Clear), `Delete` (CE) work directly. So a full computation is just `type "1234*5678"` then `key Return` — one tool call, no per-button screenshot. The display will show the answer; one screenshot at the end is enough.
- [seed · modes] Calculator has multiple modes (hamburger menu top-left, or shortcuts): **Standard** `Alt+1`, **Scientific** `Alt+2` (adds sin/cos/log/^/π/!), **Graphing** `Alt+3`, **Programmer** `Alt+4` (HEX/DEC/OCT/BIN, bitwise AND/OR/XOR/NOT/Lsh/Rsh — useful for showing the user a base conversion visually), **Date Calculation** `Alt+5`, plus Converter sub-modes (Currency, Volume, Length, Weight, Temperature, Energy, Area, Speed, Time, Power, Data, Pressure, Angle). Switch mode FIRST, then type.
- [seed · scientific-keys] In Scientific mode useful keystrokes: `s`/`o`/`t` = sin/cos/tan, `q` = x², `y` = xʸ (power), `l` = log10, `n` = ln, `!` = factorial, `r` = 1/x. Pressing `F9` toggles sign (±). `M+` `M-` `MR` `MC` for memory.
- [seed · programmer-bases] In Programmer mode press `F5` HEX, `F6` DEC, `F7` OCT, `F8` BIN to switch input/display base. Bitwise: `&` AND, `|` OR, `^` XOR, `~` NOT, `<` Lsh, `>` Rsh. The four base lines are all shown simultaneously — handy when the user asks "what is 0xDEADBEEF in decimal/binary".
- [seed · history-memory] In Standard/Scientific, click the clock icon (top-right) or press `Ctrl+H` to toggle history pane. `Ctrl+Shift+D` clears history. Past results can be clicked to recall.
- [seed · copy-result] To get the answer back as text instead of OCR-from-screenshot: `Ctrl+C` copies the current display value to clipboard. Then read it via `paste`-into-an-input-box, or just use a shell instead from the start.
- [seed · launch-fallbacks] Fallbacks if `launch_app` misses: Win+R → `calc` → Enter. Window title is "Calculator" (English) or "计算器" (Chinese Windows).
"""

LAUNCHER = {
    "name": "Calculator",
    "description": "Windows Calculator.",
    "exe": "calc",
    "process": "Calculator.exe",
    "window_title_re": r"Calculator|计算器",
}
