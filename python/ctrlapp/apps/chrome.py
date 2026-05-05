"""Google Chrome."""

SLUG = "chrome"
TITLE = "Google Chrome"

TIPS = """\
- [seed · launch] Prefer `launch_app(name="chrome")`. The cross-browser shortcuts (`browser` tips) are auto-loaded together with this file.
- [seed · new-tab] After `launch_app("chrome")` the existing window/tab is brought to the foreground — it likely has the user's current page in it. To navigate somewhere new, **always press `ctrl+t` to open a fresh tab first**, then type your URL. Do NOT click the address bar of the current tab and overwrite it; that destroys whatever the user was reading.
- [seed · read-webpage] **To READ webpage text/data — prefer `read_webpage` over `screenshot`.** It returns markdown-flavoured plaintext (titles, headings, lists, links inlined) directly from the DOM — no OCR, no downscaling. Two modes:
  - `read_webpage(active_tab=true)` reads the **user's currently open Chrome tab** with login state preserved (gmail, internal dashboards, logged-in news sites). Pass `url_match="<substring>"` to pick a specific tab. Requires Chrome to have been started with `--remote-debugging-port=9222` (this app's chrome launcher includes that flag automatically; if `read_webpage(active_tab=true)` fails with "CDP not reachable", close all Chrome windows then `launch_app("chrome")` again to enable it).
  - `read_webpage(url="https://...")` runs headless Chrome to fetch+render+dump the page. Works on any URL, no login state. Use for public pages (news, docs, search results).
  Use `screenshot` only when you need the visual layout / images / a button's pixel position to click.
- [seed · search] Quick search recipe (does NOT clobber the current tab):
  1) `launch_app(name="chrome")`
  2) `computer(action="key", text="ctrl+t")`  ← opens a new tab; focus is already in the address bar
  3) `computer(action="type", text="https://bing.com/search?q=...")`
  4) `computer(action="key", text="Return")`
  Then if you need the results as TEXT (not screenshot), follow up with `read_webpage(active_tab=true)` to extract the rendered list cleanly.
"""

INCLUDES = ("browser",)

LAUNCHER = {
    "name": "Google Chrome",
    "description": "Google Chrome browser. Started with --remote-debugging-port=9222 so read_webpage(active_tab=true) can read the live tab via CDP.",
    # Note: shell=True in launchers.py lets us pass args here. The debug port is harmless
    # when no other tool talks to it; if Chrome is already running without the flag, this
    # invocation will simply focus the existing instance and the flag won't take effect
    # until Chrome is fully closed and re-launched.
    "exe": "chrome --remote-debugging-port=9222",
    "process": "chrome.exe",
    "window_title_re": r"Google Chrome",
}
