"""Microsoft Edge."""

SLUG = "edge"
TITLE = "Microsoft Edge"

TIPS = """\
- [seed · launch] Prefer `launch_app(name="edge")`. The cross-browser shortcuts (`browser` tips) are auto-loaded together with this file.
- [seed · new-tab] After `launch_app("edge")` the existing window/tab is brought to the foreground — it likely has the user's current page in it. To navigate somewhere new, **always press `ctrl+t` to open a fresh tab first**, then type your URL. Do NOT click the address bar of the current tab and overwrite it; that destroys whatever the user was reading.
- [seed · search] Quick search recipe (does NOT clobber the current tab):
  1) `launch_app(name="edge")`
  2) `computer(action="key", text="ctrl+t")`  ← opens a new tab; focus is already in the address bar
  3) `computer(action="type", text="https://bing.com/search?q=...")`
  4) `computer(action="key", text="Return")`
"""

INCLUDES = ("browser",)

LAUNCHER = {
    "name": "Microsoft Edge",
    "description": "Microsoft Edge browser.",
    "exe": "msedge",
    "process": "msedge.exe",
    "window_title_re": r"Microsoft.+Edge",
}
