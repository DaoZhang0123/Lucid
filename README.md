# Klawbot

> **Your conversational stand-in for Windows.**
> Tell Klawbot what you want done — it watches the screen, works the mouse, and hands the desktop back the way you wanted it.
> **No MCP. No per-app APIs. No browser plugins.** Just **Claude's multimodal vision** driving your real keyboard and mouse.

**Languages:** **English** · [简体中文](README.zh-CN.md) · [Français](README.fr-FR.md)

```
You:    "Open Microsoft Teams and send 'Hello' to myself"
          ↓
Klawbot:   *takes a screenshot*
          *sees the desktop*
          → launch_app("Microsoft Teams")
          → click(chat with myself)
          → type("Hello")  → key("enter")
          → "Done."
```

Klawbot ships as a Windows desktop app (`klawbot.exe` engine + Tauri/WebView2 GUI). Below is what it can already do today and a generous helping of example prompts.

---

## Why Klawbot?

| | Traditional RPA / API-bound bots | **Klawbot** |
| --- | --- | --- |
| Per-app integration | Each app needs an SDK / plugin / MCP server | **Zero.** If a human can use it, Klawbot can use it. |
| Works with closed apps (banks, ERP, games, WeChat…) | ❌ usually not | ✅ pixels are pixels |
| Setup | Hours of glue code | Install, pick an LLM, type a sentence |
| Fails when an app updates its API | Constantly | Only if the UI changes visually |
| Cost | Vendor lock-in | Bring-your-own LLM (Anthropic / Copilot / proxy) |

---

## What Klawbot can do today (`v0.3.0`)

### Talks to you
- Conversational chat shell (Tauri 2 + SvelteKit + WebView2), system tray, global emergency hotkey (`Ctrl+Alt+Esc`).
- Trilingual UI — **English / 简体中文 / Français** (svelte-i18n), switch in Settings.
- Three LLM backends in one click: **Anthropic** direct · **GitHub Copilot** OAuth · **OpenAI-compatible proxy** (LiteLLM, OpenClaw, …).

### Watches your screen, intelligently
- **Per-Monitor V2 DPI** + multi-monitor virtual coordinates.
- **Three-level screenshot pyramid** the model picks from: L1 fullscreen, L2 active window, L3 cursor neighborhood (UIA-tightened — clamps L3 to whatever UI element is under the cursor, not a dumb 200×200 box).
- Smart context manager: pyramid retention windows + JPEG re-compression of old screenshots + auto-summarisation when the context blows past the model's budget.
- **Empty-screenshot model:** when feeding the initial L1 isn't useful, Klawbot tells the model the desktop dimensions and lets it decide whether to look.

### Drives your real desktop
- Full `computer` tool: click / drag / scroll / hotkey / Chinese-safe `type` (clipboard route — bypasses IME entirely).
- Built-in zero-GUI utilities so it doesn't have to *click* its way through trivial things: `read_file` / `write_file` / `run_shell` (output captured, console hidden, 20s timeout).
- Native app launching: `launch_app("VS Code")` uses Windows APIs (Start menu shortcuts + UWP MSIX manifest scan), pins the active window, and skips the "look for the icon" round-trip.

### Watches over your shoulder when you're away
- **Visual taskbar notify** — periodic dHash diff on the taskbar; if a candidate change appears, a cheap LLM call confirms whether it's a new message and *which* app fired it. Per-schedule **app whitelist** so it only ever touches the apps you allow.
- **Auto-reply** with a hard-coded **AUTO-REPLY SAFETY POLICY** baked into the system prompt: never leak personal info or codes, never click pay/agree/install, never accept files / friend requests / screen-share, escalate-and-stop on ambiguity.

### Schedules and templates
- **Schedules** — cron-like + one-shot + visual_notify modes. Pause / resume / "run now" buttons.
- **Templates** — save common instructions, fire with one click.
- **Per-thread context persistence** — every reply in the same thread re-uses the prior messages (after image compression).

### Learns over time
- **`memory.md`** — long-term memory injected into the system prompt; Klawbot can call `remember(text)` to add to it, you edit it on the Memory page.
- **`tools.md`** — evolving "operation tips" library; Klawbot calls `learn_tip(text)` after a successful or failed run.
- **Per-app spec files** (`apps/<slug>.py`) — drop-a-file = teach Klawbot a new app, with custom launcher + tips.
- **Doze learning** — when you're idle for 5 minutes, Klawbot quietly reflects on finished threads, mining tips and *icon proposals* (small icon crops it spotted that you can accept on the Doze page so it learns icon → app mapping).
- **Self-check** for monitors / DPI / Win+R alias / click-coordinate offset.

### Honest about itself
- Per-run logs at `%LOCALAPPDATA%\dev.klawbot\logs\threads\<thread>\` — `events.jsonl`, `messages.json`, every screenshot, full LLM context dumps.
- Three autonomy levels: `full` / `confirm_critical` / `confirm_each`. HITL keyword list (`delete`, `format`, `transfer`, `confirm payment`, …) intercepts the dangerous ones even on `full`.

---

## Example prompts — what people actually use it for

These are real one-liners you can paste into the chat box. Adjust paths / names. The autonomy level is set in the footer.

### 📝 Office stuff

> *"Open Notepad, type the meeting notes I just dictated, save it as `D:\notes\2026-05-08.txt`."*

> *"Open the Excel file on my desktop called `expenses.xlsx`, scroll to the bottom of column C, and tell me the sum."*

> *"Take the PDF that's currently open in Edge, summarise the executive summary in 5 bullet points, paste them into a new Outlook draft to alice@…, subject `PDF summary`."*

### 💬 Messaging while away (with a schedule)

Set up a **schedule → action: visual_notify**, whitelist `WeChat` + `Microsoft Teams`. Default instruction:

> *"Open the matching chat client, read the latest unread, and send a short polite reply if it's safe to."*

The `AUTO-REPLY SAFETY POLICY` (in the system prompt) enforces: don't leak personal info, don't accept files / links / verification codes, don't authorise anything, escalate-and-stop if the conversation gets weird.

### ⏰ Recurring tasks (cron)

Schedules of action `task` with a daily / weekly / interval trigger:

> *"Every weekday at 9:00 — open Outlook, scan the unread inbox, and write me a 3-line summary in a popup."*

> *"Every Friday 17:00 — open `D:\Reports\template.xlsx`, fill A1 with this week's date, save as `weekly-<YYYY-MM-DD>.xlsx` in the same folder."*

> *"Every 30 minutes — check Visual Studio Code's git status bar; if the branch shows `*` (unsaved), nudge me with a toast."*

### 🌐 Browser / research

> *"Open Chrome, search for 'best ergonomic keyboards 2026', open the top 3 results in tabs, and give me a one-paragraph summary of each."*

> *"Log into the GitHub tab I have open, find issue #142 in the `acme/foo` repo, paste the comment body I'll dictate next, hit Comment."*

### 🛠️ File / system chores

> *"In `D:\Photos\unsorted`, rename every file matching `IMG_*.JPG` to `2026-05-08-<NNNN>.jpg` keeping the order."*

> *"Find the largest 5 files under `C:\Users\me\Downloads` and tell me their sizes."* (Klawbot will use `run_shell` here, not click around.)

### 🎮 Light gaming / niche apps

> *"Take a turn in Civilization VI: research Pottery, build a Worker, end turn."*

> *"In FL Studio, mute track 3, render the project to `D:\music\demo.wav`."*

(Game UIs are visually unusual — set autonomy to `confirm_each` the first time so you can step through.)

### 🧪 Sanity checks (no mouse / keyboard)

> *"Take a fullscreen screenshot and tell me how many windows are visible."*

> *"Read `C:\Users\me\AppData\Local\dev.klawbot\config.toml` and tell me which LLM provider is active."* (Uses the `read_file` meta tool, no GUI clicks.)

### 🔁 Templates worth saving

| Name | Instruction |
| --- | --- |
| **Daily standup draft** | "Open my Daily Standup OneNote page, summarise yesterday's commits and today's calendar in 3 bullets each, paste into the page." |
| **Screenshot to clipboard** | "Take a screenshot of the active window, copy it to clipboard, tell me 'done'." |
| **Quiet hours auto-reply** | (visual_notify schedule) "If WeChat or Teams pings between 19:00 and 08:00, reply 'I'm AFK, will get back tomorrow' and end the run." |

---

## Architecture, briefly

```
┌──────────── Tauri WebView (SvelteKit) ─────────────┐
│  Chat │ Schedules │ Templates │ Memory │ Doze │ ⚙  │
└──────────────────────┬─────────────────────────────┘
                       │ Tauri IPC
┌──────────────────────┴─────────────────────────────┐
│   Rust shell — sidecar lifecycle, settings, tray   │
└──────────────────────┬─────────────────────────────┘
                       │ JSON-RPC over stdio
┌──────────────────────┴─────────────────────────────┐
│  Python sidecar (klawbot.exe)                       │
│  ReAct · scheduler · taskbar monitor · doze · mem.  │
│        ↓ mss screenshots          ↓ pyautogui       │
│        ↓ HTTP                                       │
│   Anthropic API   ·   GitHub Copilot   ·   proxy    │
└─────────────────────────────────────────────────────┘
```

User data: `%LOCALAPPDATA%\dev.klawbot\` (config, logs, schedules, memory, icons cache, Copilot token).

---

## Install (end users)

Download `klawbot_<version>_x64-setup.exe` from a release, run it, launch **Klawbot** from the Start menu.

On first run, open **Settings** and pick an LLM provider:

- **GitHub Copilot** — click *Sign in to GitHub Copilot*, do the device-code flow. Free as long as you have a Copilot subscription.
- **Anthropic** — paste an `sk-ant-…` key.
- **Proxy** — point it at any OpenAI-compatible endpoint (e.g. [litellm-ghc-proxy-lite](https://github.com/codetrek/litellm-ghc-proxy)).

---

## Build from source

### Prerequisites
- Windows 10 / 11
- Python 3.11+ (verified on 3.14)
- Node.js 20+ and npm
- Rust toolchain (stable) + the **WebView2 Runtime** (preinstalled on Win11)

### 1) Python sidecar

```powershell
cd D:\Project\Klawbot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .

pip install pyinstaller
pyinstaller packaging\klawbot.spec
# → dist\klawbot.exe
```

### 2) Tauri app

```powershell
cd app
npm install
npm run tauri build
# → app\src-tauri\target\release\bundle\nsis\klawbot_<ver>_x64-setup.exe
```

The Rust shell expects `klawbot.exe` next to it (or installed under `%LOCALAPPDATA%\klawbot\`); copy the PyInstaller output into place before launching the dev build.

---

## CLI usage (no GUI)

The original CLI still works and is the fastest way to smoke-test a setup:

```powershell
# Connectivity smoke test (single round, no mouse/keyboard)
python -m klawbot --smoke-test "Who are you? One sentence."

# Cautious mode: ask y/n on each step
python -m klawbot --max-steps 4 --autonomy confirm_each `
    "Take a fullscreen screenshot and tell me how many windows are visible."

# Switch model
python -m klawbot --model claude-sonnet-4.5 "Open Notepad and type hello"

# Full autonomy (only on a sandbox / VM)
python -m klawbot --autonomy full "Open Notepad, type hello world, save to Desktop"
```

`Ctrl+C` to abort. Slamming the mouse to the **top-left corner** triggers PyAutoGUI's fail-safe.

---

## Configuration

Default template: [config.toml](config.toml). The **real** user config is at `%LOCALAPPDATA%\dev.klawbot\config.toml` — edit that one (the bundled file is overwritten on upgrade).

Key sections:

| Section | What it controls |
| --- | --- |
| `[llm]` | provider, max steps, max tokens, prompt-cache, temperature/top-p, screenshot retention |
| `[llm.anthropic]` / `[llm.copilot]` / `[llm.proxy]` | per-provider model + endpoint + key |
| `[logging]` | per-run log dir, text/image levels (`DEBUG/INFO/WARNING/ERROR/OFF`), `png`/`jpg`, retention |
| `[screenshot]` | three-pyramid intervals, downscale long edges, per-level retention, change-detection threshold |
| `[safety]` | HITL keywords, emergency hotkey (`ctrl+alt+esc`), default autonomy, click verification, save-dialog guard |
| `[input]` | `chinese_input = "clipboard"` (recommended) or `unicode_sendinput`, action delay |
| `[visual_notify]` | taskbar polling, dHash threshold, LLM confirmation cadence, auto-chat instruction |
| `[doze]` | idle-time reflection limits |
| `[memory]` / `[tools]` | long-term memory + operation tips on/off and limits |
| `[fileio]` / `[shell]` | enable / sandbox `read_file` / `write_file` / `run_shell` |

GUI Settings hot-reloads the sidecar after saving.

---

## Common issues

- **`HTTP 500 … Connection error`** — Copilot upstream hiccup; the client already retries 5xx. Re-run.
- **`HTTP 413 Request Entity Too Large`** — too many screenshots accumulated. Lower `[llm].keep_recent_screenshots`, `[screenshot].l1_max_long_edge`, `--max-steps`, or set `[logging].image_format = "jpg"`.
- **`AuthenticationError: Failed to refresh API key`** — Copilot device-login token expired. Sign in again from Settings.
- **`No such model …`** — model not enabled on your proxy / wrong model id. Switch in Settings.
- **`BitBlt: Access Denied`** — Windows is on the lock / Winlogon secure desktop. Unlock; or use *display off* (`nircmd monitor off`) instead of *lock* so screenshots keep working.
- **Garbled Chinese typing** — make sure `[input].chinese_input = "clipboard"` (default). It bypasses the IME entirely.
- **Click off-target on multi-monitor** — keep all displays at the same scaling, or tune `[screenshot].l1_max_long_edge` so UI isn't shrunk too aggressively.

---

## Risk reminder

- The model **takes over your real mouse and keyboard**. Run it on a desktop you can afford to interrupt, or in a VM.
- Screenshots are uploaded to whichever LLM backend you choose (Anthropic / GitHub Copilot upstream / your proxy).
  **Close or minimise sensitive windows (password fields, banking, private chats) before running tasks.**
- Visual taskbar auto-reply has a hard-coded safety policy at the system-prompt layer (no leaking codes / addresses, no clicking pay / agree, escalate-and-stop on ambiguity), but you should still review which apps you whitelist.

---

## Stargazers · benchmark vs OpenAdapt

[![GitHub stars](https://img.shields.io/github/stars/codetrek/Klawbot?style=social)](https://github.com/codetrek/Klawbot/stargazers)

We track our reach against the spiritual neighbour [OpenAdaptAI/OpenAdapt](https://github.com/OpenAdaptAI/OpenAdapt) — same lane (generative RPA / computer-use agent), longer-running project. Updated monthly:

| Date | Klawbot ★ | OpenAdapt ★ | Note |
| --- | ---: | ---: | --- |
| 2026-05-01 | _tbd_ | ~1566 | OpenAdapt baseline at 233 forks |
| 2026-06-01 |  |  |  |

Refresh script:

```powershell
gh api repos/OpenAdaptAI/OpenAdapt --jq '.stargazers_count'
gh api repos/codetrek/Klawbot --jq '.stargazers_count'
```
