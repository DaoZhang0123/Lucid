# <img src="app/src-tauri/icons/128x128.png" width="32" alt="Lucid icon" /> Lucid

A true "human-like computer-use" AI assistant: no MCP, direct control of your Windows apps, and continuous auto-reply while you are away.

> **A clear-eyed assistant for your Windows desktop — Vision Agent for Windows.**
> Tell Lucid what you want done. It scopes out the screen, works the mouse, reads incoming messages while you're away, and quietly replies on your behalf.
> **No MCP. No per-app APIs. No browser plugins.** Just **Claude's multimodal vision** driving your real keyboard and mouse.
> **Unlike official bots (WeChat, etc.), Lucid controls your actual client** — so it can read any message, see any context, and reply as you, with full state persistence and no registration overhead.

> **Why "Lucid"?** *Lucid* — clear, perceptive, transparent of mind. The reticle at the centre of our logo is the eye; what the eye sees, the agent does. A three-level screenshot pyramid plus a taskbar monitor that doesn't blink keep the perception sharp; a single ReAct loop keeps the action honest. **Lucid = the eye that sees + the hands that act.**
> *Easter egg: watch the launch splash — a tiny crab scuttles into the reticle's centre.*

**Languages:** **English** · [简体中文](README.zh-CN.md) · [Français](README.fr-FR.md)

<video src="Docs/Teams.mp4" controls width="720">Your browser does not support embedded video. <a href="Docs/Teams.mp4">Download the demo</a>.</video>

```
Teams (incoming):  "Tell me a joke about dog and cat"
          ↓
Lucid:   *taskbar UIA listener sees a new Teams message (no LLM confirm needed)*
          → launch_app("Microsoft Teams")  → open the chat, read the request
          → think up a joke about a dog and a cat
          → click(chat input) → type("…joke text…") → key("enter")
          → "Done. Replied in Teams with the joke."
```

Lucid ships as a Windows desktop app (`lucid.exe` engine + Tauri/WebView2 GUI). Below is what it can already do today and a generous helping of example prompts.

---

## Why Lucid?

| | Traditional RPA / API-bound bots | **Lucid** |
| --- | --- | --- |
| Per-app integration | Each app needs an SDK / plugin / MCP server | **Zero.** If a human can use it, Lucid can use it. |
| Works with closed apps (banks, ERP, games, WeChat…) | ❌ usually not | ✅ pixels are pixels |
| Auto-reply to messages | Official bots only; registration required; can't persist state; can't see full context | ✅ **Controls your real client.** Reads any message, sees full history, replies as you, stateful. |
| Setup | Hours of glue code | Install, pick an LLM, type a sentence |
| Fails when an app updates its API | Constantly | Only if the UI changes visually |
| Cost | Vendor lock-in | Bring-your-own LLM (Anthropic / Copilot / proxy) |

---

## Architecture, briefly

![Lucid architecture](Docs/arch.png)

User data: `~/.lucid/` (config, logs, schedules, memory, icons cache, Copilot token).

---

## Install (end users)

Download `lucid_<version>_x64-setup.exe` from a release, run it, launch **Lucid** from the Start menu.

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
cd D:\Project\Lucid
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .

pip install pyinstaller
pyinstaller packaging\lucid.spec
# → dist\lucid.exe
```

### 2) Tauri app

```powershell
cd app
npm install
npm run tauri build
# → app\src-tauri\target\release\bundle\nsis\lucid_<ver>_x64-setup.exe
```

The Rust shell expects `lucid.exe` next to it (or installed under `%LOCALAPPDATA%\lucid\`); copy the PyInstaller output into place before launching the dev build.

---

## CLI usage (no GUI)

Run from the repo root (`D:\Project\Lucid`).

If your provider needs a key, set it first:

```powershell
# proxy provider
$env:LITELLM_MASTER_KEY = "your_proxy_key"

# anthropic provider
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

Then run:

```powershell
cd D:\Project\Lucid

# Connectivity smoke test (single round, no mouse/keyboard)
.venv\Scripts\python.exe -m lucid --smoke-test "Who are you? One sentence."

# Run a task
.venv\Scripts\python.exe -m lucid `
    "Take a fullscreen screenshot and tell me how many windows are visible."

# Switch model
.venv\Scripts\python.exe -m lucid --model claude-sonnet-4.5 "Open Notepad and type hello"

# Run on a sandbox / VM only when you trust the instruction
.venv\Scripts\python.exe -m lucid "Open Notepad, type hello world, save to Desktop"
```

If you see `missing api_key (config .api_key or LITELLM_MASTER_KEY environment variable)`, set `[llm.proxy].api_key` in `~/.lucid/config.toml` or export `LITELLM_MASTER_KEY`.

`Ctrl+C` to abort. Slamming the mouse to the **top-left corner** triggers PyAutoGUI's fail-safe.

---

## Configuration

Default template: [config.toml](config.toml). The **real** user config is at `~/.lucid/config.toml` — edit that one (the bundled file is overwritten on upgrade).

Key sections:

| Section | What it controls |
| --- | --- |
| `[llm]` | provider, max tokens, prompt-cache, temperature/top-p, screenshot retention |
| `[llm.anthropic]` / `[llm.copilot]` / `[llm.proxy]` | per-provider model + endpoint + key |
| `[logging]` | per-run log dir, text/image levels (`DEBUG/INFO/WARNING/ERROR/OFF`), `png`/`jpg`, retention |
| `[screenshot]` | three-pyramid intervals, downscale long edges, per-level retention, change-detection threshold |
| `[safety]` | emergency hotkey (`ctrl+alt+esc`), click verification, save-dialog guard |
| `[input]` | `chinese_input = "clipboard"` (recommended) or `unicode_sendinput`, action delay |
| `[visual_notify]` | taskbar polling, dHash threshold, LLM confirmation cadence, auto-chat instruction |
| `[doze]` | idle-time reflection limits |
| `[memory]` / `[tools]` | long-term memory + operation tips on/off and limits |
| `[fileio]` / `[shell]` | enable / sandbox `read_file` / `write_file` / `run_shell` |

GUI Settings hot-reloads the sidecar after saving.

---

## Risk reminder

- The model **takes over your real mouse and keyboard**. Run it on a desktop you can afford to interrupt, or in a VM.
- Screenshots are uploaded to whichever LLM backend you choose (Anthropic / GitHub Copilot upstream / your proxy).
  **Close or minimise sensitive windows (password fields, banking, private chats) before running tasks.**
- Visual taskbar auto-reply has a hard-coded safety policy at the system-prompt layer (no leaking codes / addresses, no clicking pay / agree, escalate-and-stop on ambiguity), but you should still review which apps you whitelist.

---

## Stargazers

[![GitHub stars](https://img.shields.io/github/stars/codetrek/Lucid?style=social)](https://github.com/codetrek/Lucid/stargazers)
