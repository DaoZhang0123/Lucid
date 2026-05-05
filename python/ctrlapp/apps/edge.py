"""Microsoft Edge."""

SLUG = "edge"
TITLE = "Microsoft Edge"

TIPS = """\
- [seed · launch] Prefer `launch_app(name="edge")`. The cross-browser shortcuts (`browser` tips) are auto-loaded together with this file.
- [seed · new-tab] After `launch_app("edge")` the existing window/tab is brought to the foreground — it likely has the user's current page in it. To navigate somewhere new, **always press `ctrl+t` to open a fresh tab first**, then type your URL. Do NOT click the address bar of the current tab and overwrite it; that destroys whatever the user was reading.
- [seed · read-webpage] **To READ webpage text/data — prefer `read_webpage` over `screenshot`.** Two modes:
  - `read_webpage(active_tab=true, browser="edge")` reads the user's currently open Edge tab with login state preserved (work tenant, M365, internal portals). Pass `url_match="<substring>"` to pick a specific tab. Requires Edge to have been started with `--remote-debugging-port=9222` (this app's edge launcher includes that flag automatically; if it fails with "CDP not reachable", close all Edge windows then `launch_app("edge")` again to enable it).
  - `read_webpage(url="https://...", browser="edge")` runs headless Edge to fetch+render+dump. Any URL, no login state.
  Use `screenshot` only when you need the visual layout or a button's pixel position to click.
- [seed · search] Quick search recipe (does NOT clobber the current tab):
  1) `launch_app(name="edge")`
  2) `computer(action="key", text="ctrl+t")`  ← opens a new tab; focus is already in the address bar
  3) `computer(action="type", text="https://bing.com/search?q=...")`
  4) `computer(action="key", text="Return")`
  For the result text, follow up with `read_webpage(active_tab=true, browser="edge")`.
"""

INCLUDES = ("browser",)

LAUNCHER = {
    "name": "Microsoft Edge",
    "description": "Microsoft Edge browser. Started with --remote-debugging-port=9222 so read_webpage(active_tab=true) can read the live tab via CDP.",
    "exe": "msedge --remote-debugging-port=9222",
    "process": "msedge.exe",
    "window_title_re": r"Microsoft.+Edge",
}
