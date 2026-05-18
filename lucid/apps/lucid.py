"""Lucid itself (this agent's own UI).

These tips exist because Lucid users routinely ask the agent to operate on
Lucid (open Settings, switch theme, change locale, enable voice, etc.). The
agent IS Lucid — closing or relaunching the window is self-destructive.
"""

SLUG = "lucid"
TITLE = "Lucid"

TIPS = """\
- [seed · self-awareness] **You ARE Lucid.** The window titled "Lucid" is the chat UI you are talking through. Treat it like any other app for UI-verb tasks (click / type / screenshot), but obey the self-preservation rules below.
- [seed · NEVER-launch] **Do NOT `launch_app(name="lucid")`.** Lucid is already running — that IS the process driving you. Launching it again spawns a second instance and a second sidecar that fight for the same config file. If the user says "open Lucid", it is already open: just `focus_window` on title `"Lucid"`.
- [seed · NEVER-close] **Do NOT `close_window` / `kill_app` on the Lucid window or the `lucid.exe` / `app.exe` process.** Killing it kills YOU mid-task — the user sees the window vanish and your reply truncate. If the user explicitly says "exit Lucid" / "退出 Lucid", acknowledge in chat first, finish writing the reply, then call `close_window(title_re="^Lucid$")` as the FINAL tool call of the turn (no verification step after — there is no process left to verify with).
- [seed · focus] To bring Lucid to the foreground: `focus_window(title_re="^Lucid$")`. The window has `decorations: false` (no native title bar) — the draggable strip is the top ~32 px of the client area; do NOT look for a system caption when locating it on screenshots.
- [seed · settings-open] To open Settings: from the main page, click the gear icon in the top-right header (look for the cog SVG near the theme-toggle button). The settings page is a SvelteKit route at `/settings` — there is NO separate window; it replaces the main view. To return: click the back arrow in the settings header.
- [seed · settings-tabs] Settings has FOUR tabs in the left rail: **General** (theme, UI locale, hot-reload), **LLM** (provider, model, API key, base URL), **Voice** (PTT enable, engine, model size, language), **About** (version, paths, log location). Click the tab button before screenshotting — only the active tab's controls render.
- [seed · locale-change] To change Lucid's UI language: Settings → General → "Language" dropdown → pick `en` / `zh-CN` / `fr-FR` → click **Save**. Change takes effect immediately (svelte-i18n reactive store) — no reload needed. The chosen locale is ALSO what the inline-mic and PTT speech recognition send to Whisper, so changing language here fixes mis-recognition.
- [seed · theme] Theme toggle (sun/moon icon) lives in the main header next to the settings gear — one click flips light↔dark. Persisted to localStorage; no Save needed.
- [seed · voice-toggle-scope] **The "Enable voice" checkbox in Settings → Voice controls ONLY the global push-to-talk hotkey** (long-press Space). The microphone button next to the chat input on the main page is INDEPENDENT and always works. If the user complains "voice doesn't work", first ask which surface — the inline button still functions even with voice disabled in settings.
- [seed · restart-caveat] If a setting requires a sidecar restart (LLM provider switch, voice model change), tell the user in chat — DO NOT attempt to restart Lucid yourself. You cannot kill the process you are running inside and then start a new one; the new one would have no way to resume the current chat thread. Phrase it as: "Please restart Lucid for this to take effect."
- [seed · config-file] Lucid reads `config.toml` from (in order) CWD or the install root. User-level overrides live under `%APPDATA%\\Lucid\\` (apps tips, launchers.json, threads/, logs/). For "where are my logs?" questions, point to `%APPDATA%\\Lucid\\logs\\` — also surfaced in Settings → About.
- [seed · chat-input] The main-page input is a `<textarea>` — Enter sends, Shift+Enter inserts a newline. If you need to programmatically dictate text into it, use `set_dictation_sink` semantics (the inline mic already wires this); avoid raw `type` on the textarea unless you've focused it first with a click on the visible box.
- [seed · cancel-task] To cancel a running Lucid task from the UI: click the red "Cancel" button that replaces the Send button while a task is in flight. From within yourself this is moot — you wouldn't cancel your own turn; the user does it.
"""

# No LAUNCHER — Lucid is launched by the user (or autorun); the agent
# must NEVER spawn a second instance. ``launch_app`` will refuse to act on
# this slug because LAUNCHER is None.
LAUNCHER = None
