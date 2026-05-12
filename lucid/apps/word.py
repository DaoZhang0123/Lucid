"""Microsoft Word (winword)."""

SLUG = "word"
TITLE = "Microsoft Word"

TIPS = """\
- [seed · launch] Use `launch_app(name='word')` — it goes through `start winword`, which resolves the App Paths alias without a recursive disk search. Do NOT `where winword` or `Get-ChildItem -Recurse` for the exe; that takes ~25s and the alias is enough.
- [seed · start-screen] On launch Word shows the start screen; press Enter (or click "Blank document") to get to an empty doc. The empty doc has the title-bar substring "Document1 - Word".
- [seed · save-as] Ctrl+S on a fresh doc opens Backstage "Save As"; press `Ctrl+S` then type the absolute path directly in the filename field of the Save dialog (works even if the sidebar shows OneDrive by default). Wait for the dialog to dismiss, then verify with `run_shell Test-Path`.
- [seed · close] Ctrl+W closes the document; if a Save prompt appears use `key text='alt+s'` for Save / `alt+n` for Don't Save.
"""

LAUNCHER = {
    "name": "Microsoft Word",
    "description": "Microsoft Word desktop app. Started via the `winword` App Paths alias (no recursive search).",
    "exe": "winword",
    "process": "WINWORD.EXE",
    "window_title_re": r"Word",
}
