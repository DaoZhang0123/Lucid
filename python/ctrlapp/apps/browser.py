"""Generic web-browser tips (no launcher — use chrome/edge specifically)."""

SLUG = "browser"
TITLE = "Web browsers (Chrome / Edge / Firefox)"

TIPS = """\
- [seed · tabs] Open new window ctrl+n, new tab ctrl+t, close tab ctrl+w, address bar ctrl+l or alt+d, back/forward alt+left/right, refresh f5.
- [seed · tabs] Don't click the '+' in the top-right corner to open a new tab — use ctrl+t, it's more reliable.
- [seed · don't-overwrite] Open a new page with ctrl+n or ctrl+t; do NOT navigate the current tab via the address bar, it loses the user's current page.
- [seed · navigate] Within a page, ctrl+f to find, ctrl+= / ctrl+- to zoom, ctrl+0 to reset zoom; ctrl+shift+t reopens the most recently closed tab.
- [seed · read-not-screenshot] **When the goal is to READ web content (text, search results, articles, JSON, tables), prefer the `read_webpage` meta-tool over `screenshot`** — it pulls the actual DOM as plaintext (much more accurate than OCR + survives later context compression). Use `read_webpage(active_tab=true)` for the user's open tab (login state preserved), or `read_webpage(url="https://...")` for a fresh fetch (headless, no login). Reserve `screenshot` for visual tasks (find a button to click, see an image).
"""

LAUNCHER = None
