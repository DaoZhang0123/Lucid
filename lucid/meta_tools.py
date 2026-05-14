"""Agent 的 "自我策展" function tools（除 `computer` 之外）。

包含 2 个常驻的其他工具：
- ``remember``       —— 把用户偏好 / 称呼 / 习惯写入 ``memory.md``
- ``learn_tip``      —— 把操作技巧（成功路径 / 失败教训）写入 ``tools.md``

每个工具暴露：
- ``XXX_SCHEMA``：OpenAI function-calling 格式的 schema dict
- 在 :func:`dispatch_meta_tool` 里的实际处理逻辑

ReAct 主循环（``loop.py``）只需调用 :func:`build_meta_tool_schemas` 拿到 schemas 列表
（按 ``cfg.{memory,tools}.enabled`` 开关过滤），并在收到 tool_call 时把
非 ``computer`` 的工具名转发到 :func:`dispatch_meta_tool`。
"""
from __future__ import annotations

import threading
from typing import Any, Callable

from .config import Config
from .tools import ToolResult
from . import memory as memory_mod
from . import tooltips as tooltips_mod
from . import launchers as launchers_mod
from . import regions as regions_mod
from . import scheduler as scheduler_mod


def _run_with_timeout(fn: Callable[[], Any], timeout_s: float) -> tuple[bool, Any]:
    """Run ``fn`` on a daemon worker thread; return ``(True, result)`` if it
    finished within ``timeout_s``, else ``(False, None)``. The worker is left
    to finish in the background so we never wedge the sidecar's main thread —
    even if the underlying Win32 call (e.g. ``AttachThreadInput`` /
    ``SetForegroundWindow``) is permanently stuck.

    See iteration-plan 20260512-144219 §"K9 wedge": a synchronous
    ``launch_app(powershell)`` hung the sidecar indefinitely and cascaded
    into 10 downstream `harness_error` rows when subsequent ``start_task``
    RPCs all timed out. This wrapper guarantees liveness by returning to
    the dispatch loop after at most ``timeout_s`` seconds.
    """
    result_box: dict[str, Any] = {}

    def _runner() -> None:
        try:
            result_box["value"] = fn()
        except Exception as exc:  # pragma: no cover — defensive
            result_box["error"] = exc

    t = threading.Thread(target=_runner, daemon=True, name="meta_tool_watchdog")
    t.start()
    t.join(timeout=max(0.5, timeout_s))
    if t.is_alive():
        return False, None
    if "error" in result_box:
        raise result_box["error"]
    return True, result_box.get("value")


REMEMBER_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "remember",
        "description": (
            "Persist a long-term fact to memory.md (append-only; injected into the system prompt at the start of every future task). "
            "Use for: preferences the user explicitly asks you to remember; how the user wants to be addressed / their identity; "
            "operating habits (preferred browser, editor, shortcuts, save paths, naming style, dark-mode preference, etc.); "
            "environment facts that won't change soon (e.g. usual working directory). "
            "**Do NOT** record one-shot facts from the current task, intermediate results, or sensitive info like passwords / tokens. "
            "Before writing, scan the 'Long-term memory' section at the end of the system prompt to avoid duplicates / conflicts. "
            "Format: single line, <=200 chars, declarative sentence."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "One memory entry, single declarative line."}
            },
            "required": ["text"],
        },
    },
}

LEARN_TIP_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "learn_tip",
        "description": (
            "Append an **operation tip** (the most reliable way to drive a specific App / dialog / control) to tools.md. "
            "It will be injected into the system prompt at the start of every future task.\n"
            "**When to call (any one of these is enough)**:\n"
            "1) You used a shortcut / command line to successfully open or operate an App "
            "(e.g. Ctrl+Alt+W opens WeChat, Win+R -> outlook opens Outlook) — these are the highest-value tips, "
            "so **as soon as it works once, log it**;\n"
            "2) You worked around a pit you previously got stuck on (e.g. WeChat: Enter sends, Shift+Enter inserts a newline);\n"
            "3) You found an old tip is wrong / outdated — write a new entry that overrides it.\n"
            "**Do NOT** record one-shot facts (those aren't tips), and don't record user preferences (those go in memory.md). "
            "Before writing, scan existing entries in the 'Operation tips' section to avoid duplicates. "
            "Format: single declarative line, <=200 chars; include the App / scenario as a tag for easier search."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The tip to record. e.g. 'Outlook: Ctrl+R to reply is more reliable than clicking the Reply button'."},
                "kind": {"type": "string", "enum": ["success", "failure", "tip"], "description": "Success story / failure lesson / general tip. Defaults to success."},
                "app": {"type": "string", "description": "Optional. App slug (e.g. 'wechat', 'vscode') — routes the tip to that app's tips/<slug>.md so it's only loaded when working with that App. Omit / empty for cross-App general tips (go to global tools.md)."},
            },
            "required": ["text"],
        },
    },
}


# ---------------------------------------------------------------------------
# Per-app tip loading
# ---------------------------------------------------------------------------

LOAD_APP_TIPS_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "load_app_tips",
        "description": (
            "Pull a specific App's tips/<app>.md into the current conversation. App-specific tips are NOT loaded by "
            "default (to save context); call this once per task when you start working with that App, then the result "
            "(the file's content) becomes part of your tool history and you can re-read it any time. "
            "Available app slugs are listed in the system prompt under 'App-specific tips'. "
            "Calling this tool is cheap (<1KB) and idempotent."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "App slug, e.g. 'wechat', 'vscode', 'browser', 'save-dialog'."},
            },
            "required": ["app"],
        },
    },
}


# ---------------------------------------------------------------------------
# Native app launching (`launch_app` meta tool)
# ---------------------------------------------------------------------------

LAUNCH_APP_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "launch_app",
        "description": (
            "Launch or focus an app via Windows native APIs (process detection + window enumeration + shortcut / URI / exe). "
            "**Always prefer this over visually clicking a desktop shortcut or taskbar icon** — it's faster, doesn't risk "
            "double-launching (it focuses the existing window if already running), and works even if the icon is off-screen. "
            "On success the App's tips file (if any) is auto-loaded so you immediately have its keyboard / quirks knowledge.\n"
            "If `launch_app` returns ok=false ('no launcher named ...'), call `list_apps()` to see what slugs exist; if your "
            "target App is genuinely missing, fall back to win+r + exe alias and consider `learn_tip(app=...)` to record what worked."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Launcher slug (e.g. 'wechat', 'vscode', 'chrome', 'explorer', 'settings')."},
                "page": {
                    "type": "string",
                    "description": (
                        "Optional URI suffix for apps that support deep-links. Appended to the app's base URI: "
                        "e.g. for `settings`, page='display' opens `ms-settings:display` directly (skipping the home page); "
                        "page='network' → `ms-settings:network`. When set, focus-existing-window is skipped so the deep-link "
                        "actually navigates. Ignored on apps that don't have a URI launcher."
                    ),
                },
            },
            "required": ["name"],
        },
    },
}

LIST_APPS_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "list_apps",
        "description": (
            "List all registered launcher slugs (with their display names and which launch methods are available: "
            "shortcut / uri / exe / window-detection). Use this when `launch_app` returns 'no launcher named ...' to find the right slug."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

CHECK_APP_RUNNING_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "check_app_running",
        "description": (
            "Inspect whether an App is currently running (process and / or visible window) without launching anything. "
            "Returns running / has_window / pid / hwnd / window_title. Useful before deciding whether to launch."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Launcher slug (same as `launch_app`)."},
            },
            "required": ["name"],
        },
    },
}

FOCUS_WINDOW_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "focus_window",
        "description": (
            "Bring the first visible top-level window whose title contains the given substring to the foreground. "
            "Use for ad-hoc switching when no launcher entry exists for the App but you know part of its window title."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title_substring": {"type": "string", "description": "Substring (case-sensitive) to match against window titles."},
            },
            "required": ["title_substring"],
        },
    },
}

GET_WINDOW_TITLE_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "get_window_title",
        "description": (
            "Return the exact title string of the **currently focused (foreground) window** — read directly via "
            "Win32 `GetWindowTextW`, no screenshot, no OCR, no shell. Use whenever the task is 'what's the title of "
            "the active window?' / 'what document is open in <app>?' / verifying which file the editor is editing — "
            "this is faster, exact, and avoids both shell-quoting bugs (PowerShell `$_.MainWindowTitle | Where ...`) "
            "and OCR truncation. Also useful right after `focus_window` / `launch_app` to confirm the right window came up."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

UPDATE_LAUNCHER_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "update_launcher",
        "description": (
            "Create OR update a launcher entry — e.g. when you discovered the global shortcut changed, the exe alias was wrong, the window title needs a different regex, or the user wants to register a brand-new app the agent has never heard of (e.g. `feishu`, `notion`, `obsidian`). "
            "The override is written to `<user data>/launchers.json` and takes precedence over the built-in defaults from this version, and survives across tasks / restarts. **Unknown slugs are auto-created**, no UI step required.\n"
            "**When to call**: \n"
            "  (a) after a `launch_app` / `check_app_running` failure that you traced to a wrong field (e.g. `shortcut` no longer triggers WeChat — try the new combo, then call `update_launcher(name='wechat', shortcut='ctrl+shift+w')`); \n"
            "  (b) when the user asks to operate an app whose slug is missing from `list_apps()` — figure out its `exe` (alias like `notion` if installed via `start notion`, otherwise the absolute `.exe` path the user can confirm) and `window_title_re`, then call `update_launcher(name='notion', exe='notion', process='Notion.exe', window_title_re='Notion')`. After creating, call `launch_app('notion')` to verify; if it works, also `learn_tip(app='notion', text='registered on YYYY-MM-DD via exe=notion')` so future sessions remember why this entry exists. \n"
            "Don't call for one-off failures (network glitch, app currently broken)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name":            {"type": "string", "description": "Launcher slug. Existing slug → patches that entry; new slug → creates a fresh user override (no built-in default required)."},
                "shortcut":        {"type": "string", "description": "Optional new global hotkey, e.g. 'ctrl+alt+w'. Pass empty string to clear."},
                "uri":             {"type": "string", "description": "Optional new shell URI, e.g. 'weixin://'. Pass empty string to clear."},
                "exe":             {"type": "string", "description": "Optional new exe alias / absolute path."},
                "process":         {"type": "string", "description": "Optional new process name (Windows tasklist style, e.g. 'WeChat.exe')."},
                "window_title":    {"type": "string", "description": "Optional new window-title substring."},
                "window_title_re": {"type": "string", "description": "Optional new window-title regex (overrides window_title)."},
            },
            "required": ["name"],
        },
    },
}


# ---------------------------------------------------------------------------
# Scheduled tasks (`schedule_*`) — let the agent create / inspect / edit
# the user's scheduled instructions through conversation.
# ---------------------------------------------------------------------------

SCHEDULE_LIST_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "schedule_list",
        "description": (
            "List all scheduled tasks the user has registered. Each entry: id, name, instruction, spec "
            "(kind=secondly/minutely/hourly/daily/weekly + every/minute/time/weekday/tz), enabled, optional constraints "
            "(hours/weekdays/date_start_ms/date_end_ms), next_ms, last_run_ms. Use this before adding a new schedule "
            "to avoid duplicates, and before update/delete to find the target id."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

# A shared shape for the spec / constraints arguments. Repeated in add + update.
_SPEC_PROP = {
    "type": "object",
    "description": (
        "Trigger spec. One of: "
        "{\"kind\":\"secondly\", \"every\":1..3600} fires every N seconds; "
        "{\"kind\":\"minutely\", \"every\":1..1440} fires every N minutes; "
        "{\"kind\":\"hourly\", \"minute\":0..59, \"tz\"?:\"IANA\"} fires every hour at that minute; "
        "{\"kind\":\"daily\", \"time\":\"HH:MM\", \"tz\"?:\"IANA\"} fires once per day at that local time; "
        "{\"kind\":\"weekly\", \"weekday\":0..6 (0=Mon), \"time\":\"HH:MM\", \"tz\"?:\"IANA\"} fires once per week. "
        "tz is optional; omit / empty for the user's local time."
    ),
}

_CONSTRAINTS_PROP = {
    "type": "object",
    "description": (
        "Optional active-window constraints (AND-combined). Omit to mean 'no limit'. Fields: "
        "hours: list[int] of 0..23 hours allowed (omit / 24 items = no limit); "
        "weekdays: list[int] of 0..6 (0=Mon) weekdays allowed (omit / 7 items = no limit); "
        "date_start_ms: epoch ms for earliest allowed fire (0 = unset); "
        "date_end_ms: epoch ms for latest allowed fire (0 = unset). "
        "Example 'weekday 9-17 only': {\"hours\":[9,10,11,12,13,14,15,16], \"weekdays\":[0,1,2,3,4]}."
    ),
}

SCHEDULE_ADD_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "schedule_add",
        "description": (
            "Create a new scheduled task that, when its trigger fires, runs `instruction` as a fresh agent task. "
            "**Use this when the user asks for anything recurring**: 每小时/每天/每周提醒 X、定时给 Y 发消息、定时检查邮件 等等. "
            "Before calling, briefly summarise to the user what you're about to register (name + spec + instruction). "
            "Tip: if the user wants 'don't repeat the same content', encode that into `instruction` itself "
            "(e.g. ask the agent to read/write a small notes file, or check chat history before sending) — "
            "the scheduler itself has no de-dup memory across runs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name":        {"type": "string", "description": "Short human-readable name (<=40 chars)."},
                "instruction": {"type": "string", "description": "The task instruction the agent will receive when this fires."},
                "spec":        _SPEC_PROP,
                "enabled":     {"type": "boolean", "description": "Default true."},
                "constraints": _CONSTRAINTS_PROP,
            },
            "required": ["name", "instruction", "spec"],
        },
    },
}

SCHEDULE_UPDATE_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "schedule_update",
        "description": (
            "Update fields of an existing scheduled task. Pass only the fields you want to change. "
            "Use schedule_list first to find the id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id":          {"type": "string", "description": "Schedule id (from schedule_list)."},
                "name":        {"type": "string"},
                "instruction": {"type": "string"},
                "spec":        _SPEC_PROP,
                "enabled":     {"type": "boolean"},
                "constraints": _CONSTRAINTS_PROP,
            },
            "required": ["id"],
        },
    },
}

SCHEDULE_DELETE_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "schedule_delete",
        "description": (
            "Delete a scheduled task by id. **Confirm with the user before calling** (deletion is permanent). "
            "Use schedule_list first to find the id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Schedule id to delete."},
            },
            "required": ["id"],
        },
    },
}


LOAD_LOCAL_IMAGES_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "load_local_images",
        "description": (
            "Re-load a local image (PNG/JPG) from disk and re-attach it to the next request. "
            "**Two main use cases:**\n"
            "1. An old screenshot was replaced by a placeholder like "
            "`[old screenshot omitted ...; level=L2; file=step-005-post-active_window.png; "
            "path=C:\\Users\\you\\.lucid\\logs\\thread-...\\step-005-post-active_window.png]` "
            "and you still need the visual content (e.g. you saw a webpage, switched to another App, and now need to "
            "transcribe the page text). Pass the **exact `path`** from the placeholder.\n"
            "2. The user attached an image via paste / drag-drop / 📎 (listed in the initial `[Attached files]` block as a path "
            "under `~/.lucid/inbox/`). Pass that `path`.\n"
            "**Do NOT** invent paths or try to read arbitrary files — only paths emitted by the placeholder lines or the "
            "`[Attached files]` block are valid (allowlisted to `~/.lucid/logs` and `~/.lucid/inbox`).\n"
            "**Coordinate frame:** re-loaded images are for **reading content only** (text, prices, news headlines, etc.); "
            "the active App's input focus / window rect may have moved since then, so do not derive new click coordinates "
            "from a re-loaded image — take a fresh `screenshot(level='active_window')` for that."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the PNG/JPG file (must come from a placeholder's `path=...` field or the `[Attached files]` block).",
                },
                "level": {
                    "type": "string",
                    "enum": ["L1", "L2", "L3"],
                    "description": (
                        "Original level of the screenshot (copy it from the placeholder's `level=...` field). "
                        "Used so the keep-recent policy treats the re-loaded image as the same level. "
                        "For user-attached inbox images, use L2. Defaults to L2."
                    ),
                },
            },
            "required": ["path"],
        },
    },
}


READ_WEBPAGE_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "read_webpage",
        "description": (
            "Read a webpage as plaintext (markdown-flavoured) — without taking a screenshot. Two backends:\n"
            "  • `active_tab=true`  → reads the **live tab** of the user's already-open Chrome/Edge via the Chrome DevTools "
            "Protocol on `localhost:9222`. **Login state is preserved** (sees gmail / wechat-web / private dashboards). "
            "Requires the browser to have been started with `--remote-debugging-port=9222` — the default `chrome` and `edge` "
            "launchers in this app already include this flag, so a fresh `launch_app('chrome')` (after closing existing "
            "windows) enables it. Use `url_match` to pick a specific tab by url/title substring; default = first non-blank "
            "non-extension page tab.\n"
            "  • `url=\"https://...\"` (no `active_tab`) → spawns a HEADLESS browser that fetches + renders + dumps the DOM. "
            "**No login state.** Works on any URL. Slower (~1-3s).\n"
            "Returns the page title + extracted readable text (links inlined as `[text](href)`, headings as `## ...`, lists "
            "as `- ...`). The text is **vastly more accurate** than OCR-ing a browser screenshot, AND the screenshot would "
            "be downscaled later anyway. **Prefer this tool over `screenshot` whenever the goal is to READ webpage text** "
            "(news, search results, documentation, JSON in browser, etc.). Use `screenshot` only when you need to see the "
            "visual layout / images / pixel position of a button to click."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Full URL (must include scheme). Required unless `active_tab=true`.",
                },
                "active_tab": {
                    "type": "boolean",
                    "description": "If true, read the user's live tab via CDP instead of fetching a fresh URL. Default false.",
                },
                "url_match": {
                    "type": "string",
                    "description": "Optional substring filter on tab url/title (only used with `active_tab=true`).",
                },
                "browser": {
                    "type": "string",
                    "enum": ["chrome", "edge"],
                    "description": "Which browser executable to invoke for headless mode. Default 'edge' (preinstalled on Windows). Auto-falls back to the other browser if the requested one is not installed.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Truncate output text to this many characters (default 8000).",
                },
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Region calibration / lookup
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Region calibration / lookup
# ---------------------------------------------------------------------------

READ_FILE_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Read a small text file from disk and return its contents — **without** opening cmd / Notepad / "
            "any GUI app. Use this whenever the goal is just to look at file contents (config files, "
            "markdown, tip ledgers like baby_tips_sent.txt, .env, json, log tails, etc.). "
            "It is **dramatically faster and cheaper** than `launch_app('cmd') → type 'type X' → screenshot → OCR`: "
            "one tool call, no screenshot, no token-heavy console image.\n"
            "Returns plain text. If the file is missing, returns an explicit 'file not found' error (NOT an "
            "exception) — safe to use as 'create-if-missing' precheck before write_file.\n"
            "Encoding auto-detection: tries utf-8, utf-8-sig (BOM), then gbk; binary files are rejected.\n"
            "Size limits: files larger than read_max_bytes (default 64 KB) are returned head+tail with a "
            "truncation notice; files larger than read_refuse_bytes (default 5 MB) are refused outright — "
            "use a real terminal for those."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the file. Forward or back slashes both work. Env vars (%USERPROFILE%, $env:LOCALAPPDATA, ~) are expanded.",
                },
                "max_bytes": {
                    "type": "integer",
                    "description": "Override the per-call byte cap (default = read_max_bytes from config). Useful for 'just give me the first 200 bytes'.",
                },
                "encoding": {
                    "type": "string",
                    "description": "Force a specific encoding (e.g. 'utf-8', 'gbk', 'cp936', 'latin-1'). Default 'auto' tries utf-8/utf-8-sig/gbk in order.",
                },
            },
            "required": ["path"],
        },
    },
}

WRITE_FILE_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": (
            "Write text to a file on disk — **without** opening Notepad / cmd. Use for: appending a new entry "
            "to a tip-ledger / dedupe list (the canonical case: `baby_tips_sent.txt`), creating a small config "
            "file, dumping a generated message to disk before sending. Always **prefer this over** "
            "`echo X >> Y` in a shell — no quoting headaches, no console window, single tool call.\n"
            "Default mode is 'append' with a trailing newline auto-inserted (so each call adds exactly one line). "
            "Use mode='overwrite' to replace the whole file; the response then includes the previous file size as "
            "a safety hint so you can spot accidental clobbers.\n"
            "Parent directories are auto-created. The file is written as UTF-8 (no BOM). Size cap per call is "
            "write_max_bytes (default 256 KB)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path. Env vars are expanded (see read_file).",
                },
                "content": {
                    "type": "string",
                    "description": "Text to write. CJK / paths / newlines all OK; no escaping needed.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["append", "overwrite"],
                    "description": "Default 'append'. 'overwrite' replaces the whole file (response will warn with the old size).",
                },
                "ensure_trailing_newline": {
                    "type": "boolean",
                    "description": "If true (default) and content does not already end with \\n, one is added. Helpful for line-per-entry ledgers.",
                },
            },
            "required": ["path", "content"],
        },
    },
}

RUN_SHELL_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "run_shell",
        "description": (
            "Run a one-shot command in cmd.exe or PowerShell **without opening a visible terminal window**. "
            "Captures stdout + stderr + exit code and returns them as text. Use this whenever you want to run "
            "a quick command and **read** its output — `dir`, `where`, `tasklist`, `findstr`, `Get-Process`, "
            "`Get-ChildItem -Recurse | Measure-Object`, anything piping to text.\n"
            "**Strongly preferred over** `launch_app('cmd' / 'powershell') → type → screenshot → OCR` for any "
            "task whose only output is text — it skips: window opening, focus juggling, console rendering, image "
            "capture, JPEG encoding, OCR. One tool call, no screenshot, accurate verbatim output.\n"
            "Hard-capped by `[shell] timeout_s` (default 20s). For long-running stuff (servers, builds, watch "
            "loops, REPLs) DO open a real `launch_app('cmd')` window so you can leave it running.\n"
            "Output is truncated to `[shell] max_output_chars` (default 16 000) head+tail with a truncation notice."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The command line to run, as a single string. cmd: `dir /b C:\\\\Users\\\\Public`. powershell: `Get-Content 'C:\\\\Users\\\\Public\\\\baby_tips_sent.txt' -ErrorAction SilentlyContinue`. Multi-line PowerShell scripts are OK (use `\\n`).",
                },
                "shell": {
                    "type": "string",
                    "enum": ["cmd", "powershell", "pwsh"],
                    "description": "Default = `[shell] default_shell` (powershell). 'cmd' = cmd.exe /c, 'powershell' = built-in Windows PowerShell 5.1, 'pwsh' = PowerShell 7+ (must be on PATH).",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory. Default = sidecar's cwd. Env vars expanded.",
                },
                "timeout_s": {
                    "type": "number",
                    "description": "Override the default timeout (seconds). Capped at 120s no matter what — for longer tasks use a real terminal.",
                },
            },
            "required": ["command"],
        },
    },
}


REGION_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "region",
        "description": (
            "Resolve a named UI region of an App's window into **screen pixel coordinates** (returns center {x,y} + bounding "
            "rect {x,y,w,h}). Each region is a stable area like 'editor' / 'activity_bar' / 'chat_view' / 'input_box'. "
            "Use the returned center as the click target instead of guessing pixel coordinates from a screenshot. "
            "The App's window is brought to the foreground first (so coordinates reflect what's currently visible). "
            "Available (app, region) pairs are listed in the system prompt under 'Available region(...) lookups'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "App slug (same as `launch_app`)."},
                "name": {"type": "string", "description": "Region name within that App."},
            },
            "required": ["app", "name"],
        },
    },
}


def build_meta_tool_schemas(cfg: Config) -> list[dict]:
    """按 cfg 开关返回当次请求要附带的 meta tool schemas 列表。"""
    out: list[dict] = []
    if cfg.memory.enabled:
        out.append(REMEMBER_SCHEMA)
    if cfg.tools.enabled:
        out.append(LEARN_TIP_SCHEMA)
        out.append(LOAD_APP_TIPS_SCHEMA)
    if getattr(cfg, "launchers", None) and cfg.launchers.enabled:
        out.append(LAUNCH_APP_SCHEMA)
        out.append(LIST_APPS_SCHEMA)
        out.append(CHECK_APP_RUNNING_SCHEMA)
        out.append(FOCUS_WINDOW_SCHEMA)
        out.append(GET_WINDOW_TITLE_SCHEMA)
        out.append(UPDATE_LAUNCHER_SCHEMA)
    if getattr(cfg, "regions", None) and cfg.regions.enabled:
        out.append(REGION_SCHEMA)
    # Scheduler tools are always on — the scheduler thread is part of the sidecar core.
    out.append(SCHEDULE_LIST_SCHEMA)
    out.append(SCHEDULE_ADD_SCHEMA)
    out.append(SCHEDULE_UPDATE_SCHEMA)
    out.append(SCHEDULE_DELETE_SCHEMA)
    # Always-on: re-load any past screenshot or inbox attachment from disk
    # (cheaper than re-visiting the App).
    out.append(LOAD_LOCAL_IMAGES_SCHEMA)
    if getattr(cfg, "webread", None) and cfg.webread.enabled:
        out.append(READ_WEBPAGE_SCHEMA)
    if getattr(cfg, "fileio", None) and cfg.fileio.enabled:
        out.append(READ_FILE_SCHEMA)
        out.append(WRITE_FILE_SCHEMA)
    if getattr(cfg, "shell", None) and cfg.shell.enabled:
        out.append(RUN_SHELL_SCHEMA)
    return out


def _resolve_launch_region(
    cfg: Config,
    hwnd: int,
    method: str,
) -> tuple[int, int, int, int] | None:
    """Decide the screen rect to capture as L2 after launch_app.

    Wait briefly for the window to become visible+non-iconic (best-effort, only
    on real launches), then return its client rect via Win32 GetClientRect.
    """
    import time as _time

    wait_ms = int(getattr(cfg.screenshot, "launch_wait_max_ms", 1500))
    poll_ms = int(getattr(cfg.screenshot, "launch_wait_poll_ms", 80))
    activate_only = method == "focus_existing_window"

    # Wait for the window to materialise (best-effort; only on real launches).
    if hwnd and not activate_only:
        deadline = _time.monotonic() + wait_ms / 1000.0
        while _time.monotonic() < deadline:
            r = launchers_mod.window_client_rect(hwnd)
            if r is not None:
                break
            _time.sleep(poll_ms / 1000.0)

    return launchers_mod.window_client_rect(hwnd) if hwnd else None


def dispatch_meta_tool(
    fn_name: str,
    args: dict[str, Any],
    cfg: Config,
    last_png_by_level: dict[str, bytes],
    sensor: Any = None,
) -> ToolResult | None:
    """处理一次 meta tool 的 tool_call。

    返回 ``None`` 表示 ``fn_name`` 不是已知 meta tool（也不在 enabled 集合里），
    调用方应当报 ``unknown tool``。

    ``sensor`` (optional) — a ``ScreenSensor``; when provided, ``launch_app``
    will perform R2 visual capture (Docs/screenshot.md §13.3): snapshot before/
    after the launch, diff to find the new window's bbox (or fall back to the
    hwnd's client rect), then attach an L2 of that region to the result.
    """
    if fn_name == "remember" and cfg.memory.enabled:
        text = (args.get("text") or "").strip()
        ok = memory_mod.append_memory(cfg.memory, text, source="agent")
        return ToolResult(
            output=f"memory saved: {text[:80]}" if ok else "",
            error=None if ok else "memory disabled or empty",
        )

    if fn_name == "learn_tip" and cfg.tools.enabled:
        text = (args.get("text") or "").strip()
        kind = (args.get("kind") or "success").strip().lower() or "success"
        app = (args.get("app") or "").strip() or None
        ok = tooltips_mod.append_tip(cfg.tools, text, kind=kind, source="agent", app=app)
        scope = f"app={app}" if app else "global"
        return ToolResult(
            output=f"tip saved ({kind}, {scope}): {text[:80]}" if ok else "",
            error=None if ok else "tools disabled or empty",
        )

    if fn_name == "load_app_tips" and cfg.tools.enabled:  
        app = (args.get("app") or "").strip()
        if not app:
            return ToolResult(error="app required")
        body = tooltips_mod.app_tips_for_prompt(cfg.tools, app)
        if not body:
            return ToolResult(error=f"no tips found for app {app!r}; check spelling against list_app_tips()")
        return ToolResult(output=body)

    if fn_name == "launch_app" and getattr(cfg, "launchers", None) and cfg.launchers.enabled:
        name = (args.get("name") or "").strip()
        if not name:
            return ToolResult(error="name required")

        page = (args.get("page") or "").strip() or None
        # Watchdog: launch_app calls into Win32 (EnumWindows, AttachThreadInput,
        # SetForegroundWindow, Popen with shell=True). Any of these can
        # synchronously block in pathological cases (foreground thread frozen,
        # console subsystem slow). Cap the whole thing at 25s so the sidecar
        # always returns to the loop.
        ok_done, result = _run_with_timeout(
            lambda: launchers_mod.launch_app(cfg.launchers, name, page=page),
            timeout_s=25.0,
        )
        if not ok_done:
            return ToolResult(error=(
                f"launch_app({name!r}) timed out after 25s — the underlying "
                "Win32 call (process spawn / window enum / foreground attach) "
                "did not return. The launch may still complete in the background; "
                "take a screenshot in your next step to verify, or try a "
                "different launch method (e.g. run_shell with `start <exe>`)."
            ))
        # On success, also append the app's tips body so the model gets it for free.
        out_lines = [f"launch_app: {result.get('message', '')}"]
        for k in ("ok", "method", "slug", "hwnd", "pid", "window_title", "foreground_title", "pending_window", "cdp_available"):
            if k in result and result[k] is not None:
                out_lines.append(f"  {k}: {result[k]}")
        if result.get("ok") and cfg.tools.enabled:
            slug = result.get("slug") or name
            # NOTE: re-attaching tips here even when the same slug was already
            # preloaded by `_plan_relevant_app_tips` at task startup is INTENTIONAL,
            # not a bug. Two reasons:
            #   1) Recency. The preloaded tips sit in the very first user message
            #      and quickly drift up the history; by the time the model has
            #      done a few screenshots + clicks they're 5-10 messages back and
            #      may have been recompressed away. Re-injecting them next to the
            #      `launch_app` result keeps the right tips ADJACENT to the model's
            #      next decision (which control to click, which shortcut to press).
            #   2) launch_app may be called WITHOUT a planning preload (e.g. the
            #      task instruction didn't mention the app by name, the planner
            #      missed it, or `tools.plan_app_tips=false`). In those cases
            #      this is the only place tips get loaded at all.
            # The duplication cost is small (tips are ~1-2 KB of text) and the
            # context-manager will dedupe / recompress old copies anyway.
            tips_body = tooltips_mod.app_tips_for_prompt(cfg.tools, slug)
            if tips_body:
                out_lines.append("")
                out_lines.append(tips_body)

        # ---- R2: visual capture (region resolution + L2 crop) ----
        # Wrap the whole capture block in a watchdog: `sensor.capture_region`
        # calls into mss / Win32 BitBlt against a window that may still be
        # mid-spawn, and we've seen it hang the entire dispatch with no
        # tool_result emitted (see E2E run 20260512-182234 K9, where the
        # 25s watchdog on `launch_app` itself returned fine but the
        # post-launch L2 capture wedged the worker for 11+ min). On timeout
        # we skip the L2 attach and return text-only.
        image_png: bytes | None = None
        attached_l2 = None
        if result.get("ok") and sensor is not None:
            hwnd = result.get("hwnd") or 0
            method = (result.get("method") or "").lower()
            slug = result.get("slug") or name

            def _capture_l2():
                rect_local = _resolve_launch_region(
                    cfg=cfg,
                    hwnd=int(hwnd) if hwnd else 0,
                    method=method,
                )
                if rect_local is None:
                    return None
                rx, ry, rw, rh = rect_local
                l2 = sensor.capture_region(rx, ry, rw, rh)
                return rect_local, l2

            cap_done, cap_value = _run_with_timeout(_capture_l2, timeout_s=10.0)
            if not cap_done:
                out_lines.append(
                    "  L2 capture timed out after 10s (window may still be "
                    "spawning); skipping L2 attachment. Take a screenshot in "
                    "your next step to verify the launch."
                )
                rect = None
            elif cap_value is None:
                rect = None
                out_lines.append(
                    f"  no region resolved (hwnd={hwnd} method={method}); skipping L2 attachment"
                )
            else:
                rect, l2 = cap_value
                rx, ry, rw, rh = rect
                try:
                    image_png = l2.png_bytes()
                    attached_l2 = l2
                    out_lines.append("")
                    out_lines.append(
                        f"  region: x={rx} y={ry} w={rw} h={rh} (L2 attached as user message; this is the active coordinate frame for your next click against this app. By default no further screenshots will be auto-attached — call action='screenshot' yourself when you need to see the screen again.)"
                    )
                    # Persist to region store as __main_window for next launch.
                    try:
                        if getattr(cfg, "regions", None):
                            data = regions_mod.load_app_regions(cfg.regions, slug) or {}
                            regions = dict(data.get("regions") or {})
                            regions["__main_window"] = {"x": rx, "y": ry, "w": rw, "h": rh}
                            data["regions"] = regions
                            data.setdefault("slug", slug)
                            regions_mod.save_app_regions(cfg.regions, slug, data)
                    except Exception:
                        pass  # cache best-effort, never break the dispatch
                except Exception as e:
                    image_png = None
                    out_lines.append(
                        f"  L2 png encode failed: {type(e).__name__}: {e}"
                    )

        return ToolResult(
            output="\n".join(out_lines),
            error=None if result.get("ok") else result.get("message"),
            image_png=image_png,
            attached_capture=attached_l2,
        )

    if fn_name == "list_apps" and getattr(cfg, "launchers", None) and cfg.launchers.enabled:
        items = launchers_mod.list_launchers(cfg.launchers)
        lines = ["Available launcher slugs:"]
        for it in items:
            methods = []
            if it.get("shortcut"):
                methods.append(f"shortcut={it['shortcut']}")
            if it.get("uri"):
                methods.append(f"uri={it['uri']}")
            if it.get("exe"):
                methods.append(f"exe={it['exe']}")
            if it.get("process"):
                methods.append(f"process={it['process']}")
            lines.append(f"- {it['slug']} ({it.get('name', it['slug'])}): {', '.join(methods) or '(no method)'}")
        return ToolResult(output="\n".join(lines))

    if fn_name == "check_app_running" and getattr(cfg, "launchers", None) and cfg.launchers.enabled:
        name = (args.get("name") or "").strip()
        if not name:
            return ToolResult(error="name required")
        info = launchers_mod.check_app_running(cfg.launchers, name)
        lines = [f"check_app_running({name!r}):"]
        for k, v in info.items():
            lines.append(f"  {k}: {v}")
        return ToolResult(output="\n".join(lines))

    if fn_name == "focus_window" and getattr(cfg, "launchers", None) and cfg.launchers.enabled:
        title = (args.get("title_substring") or "").strip()
        if not title:
            return ToolResult(error="title_substring required")
        info = launchers_mod.focus_window(title)
        return ToolResult(
            output=info.get("message", ""),
            error=None if info.get("ok") else info.get("message"),
        )

    if fn_name == "get_window_title" and getattr(cfg, "launchers", None) and cfg.launchers.enabled:
        from .window import active_window
        info = active_window()
        if info is None:
            return ToolResult(error="no foreground window (or non-Windows host)")
        return ToolResult(output=f"foreground window title: {info.title!r}")

    if fn_name == "update_launcher" and getattr(cfg, "launchers", None) and cfg.launchers.enabled:
        name = (args.get("name") or "").strip()
        if not name:
            return ToolResult(error="name required")
        # Verify the launcher exists (default or already an override).
        if launchers_mod.get_launcher(cfg.launchers, name) is None:
            return ToolResult(error=f"no launcher named {name!r}; call list_apps() to see available slugs")
        # Pull only the writable fields the schema permits.
        spec: dict[str, Any] = {}
        for key in ("shortcut", "uri", "exe", "process", "window_title", "window_title_re"):
            if key in args:
                v = args.get(key)
                if isinstance(v, str):
                    spec[key] = v.strip()
                elif v is not None:
                    spec[key] = v
        if not spec:
            return ToolResult(error="no fields to update; pass at least one of shortcut/uri/exe/process/window_title/window_title_re")
        item = launchers_mod.upsert_launcher(cfg.launchers, name, spec)
        changed = ", ".join(f"{k}={v!r}" for k, v in spec.items())
        return ToolResult(output=f"launcher {name!r} updated: {changed}\nnow active: {item}")

    if fn_name == "region" and getattr(cfg, "regions", None) and cfg.regions.enabled:
        app = (args.get("app") or "").strip()
        name = (args.get("name") or "").strip()
        if not app or not name:
            return ToolResult(error="app and name required")
        result = regions_mod.region(cfg.regions, cfg.launchers, app, name)
        if isinstance(result, dict):  # error
            return ToolResult(error=result.get("message", "region lookup failed"))
        d = result.to_dict()
        lines = [
            f"region {app}/{name}:",
            f"  description: {d['description']}",
            f"  center: ({d['center']['x']}, {d['center']['y']})",
            f"  rect: x={d['screen']['x']} y={d['screen']['y']} w={d['screen']['w']} h={d['screen']['h']}",
        ]
        return ToolResult(output="\n".join(lines))

    if fn_name in ("schedule_list", "schedule_add", "schedule_update", "schedule_delete"):
        return _dispatch_schedule(fn_name, args, cfg)

    if fn_name == "load_local_images":
        return _dispatch_load_local_images(args)

    if fn_name == "read_webpage" and getattr(cfg, "webread", None) and cfg.webread.enabled:
        return _dispatch_read_webpage(args, cfg)

    if fn_name == "read_file" and getattr(cfg, "fileio", None) and cfg.fileio.enabled:
        return _dispatch_read_file(args, cfg)

    if fn_name == "write_file" and getattr(cfg, "fileio", None) and cfg.fileio.enabled:
        return _dispatch_write_file(args, cfg)

    if fn_name == "run_shell" and getattr(cfg, "shell", None) and cfg.shell.enabled:
        return _dispatch_run_shell(args, cfg)

    return None


def _fmt_schedule(it: dict) -> str:
    spec = it.get("spec") or {}
    parts = [f"#{it.get('id')} {it.get('name')!r} ({'on' if it.get('enabled') else 'off'})",
             f"  spec: {spec}"]
    cons = it.get("constraints") or {}
    if cons:
        parts.append(f"  constraints: {cons}")
    parts.append(f"  instruction: {(it.get('instruction') or '')[:120]}")
    nm = it.get("next_ms") or 0
    lm = it.get("last_run_ms") or 0
    if nm:
        from datetime import datetime as _dt
        parts.append(f"  next: {_dt.fromtimestamp(nm/1000).isoformat(timespec='seconds')}")
    if lm:
        from datetime import datetime as _dt
        parts.append(f"  last: {_dt.fromtimestamp(lm/1000).isoformat(timespec='seconds')}")
    return "\n".join(parts)


def _dispatch_schedule(fn_name: str, args: dict[str, Any], cfg: Config) -> ToolResult:
    try:
        if fn_name == "schedule_list":
            items = scheduler_mod.list_schedules()
            if not items:
                return ToolResult(output="No scheduled tasks.")
            return ToolResult(output=f"{len(items)} schedule(s):\n\n" + "\n\n".join(_fmt_schedule(it) for it in items))

        if fn_name == "schedule_add":
            spec = args.get("spec") or {}
            if not isinstance(spec, dict):
                return ToolResult(error="spec must be an object")
            it = scheduler_mod.add_schedule(
                name=str(args.get("name") or ""),
                instruction=str(args.get("instruction") or ""),
                spec=spec,
                enabled=bool(args.get("enabled", True)),
                constraints=args.get("constraints"),
            )
            return ToolResult(output="Schedule created:\n" + _fmt_schedule(it))

        if fn_name == "schedule_update":
            sid = str(args.get("id") or "").strip()
            if not sid:
                return ToolResult(error="id required")
            fields: dict[str, Any] = {}
            for k in ("name", "instruction", "spec", "enabled", "constraints"):
                if k in args and args[k] is not None:
                    fields[k] = args[k]
            it = scheduler_mod.update_schedule(sid, **fields)
            if it is None:
                return ToolResult(error=f"no schedule with id {sid!r}")
            return ToolResult(output="Schedule updated:\n" + _fmt_schedule(it))

        if fn_name == "schedule_delete":
            sid = str(args.get("id") or "").strip()
            if not sid:
                return ToolResult(error="id required")
            ok = scheduler_mod.delete_schedule(sid)
            return ToolResult(output=f"deleted: {ok}", error=None if ok else f"no schedule with id {sid!r}")
    except Exception as e:
        return ToolResult(error=f"{type(e).__name__}: {e}")
    return ToolResult(error=f"unknown schedule op: {fn_name}")


def _dispatch_load_local_images(args: dict[str, Any]) -> ToolResult:
    """Read a previously-saved screenshot or user-attached inbox image from
    disk and re-attach it to the next request. Validates that the path looks
    like one of our log files (under ``~/.lucid/logs``) or an
    inbox attachment (under ``~/.lucid/inbox``) so a model
    can't use this to exfiltrate arbitrary local files.
    """
    import os
    from pathlib import Path

    def _is_under(child: Path, root: Path) -> bool:
        try:
            child.relative_to(root)
            return True
        except ValueError:
            return False

    raw_path = (args.get("path") or "").strip()
    if not raw_path:
        return ToolResult(error="path required")
    level = (args.get("level") or "L2").strip().upper()
    if level not in ("L1", "L2", "L3"):
        level = "L2"

    try:
        p = Path(os.path.expandvars(raw_path)).resolve()
    except Exception as e:
        return ToolResult(error=f"invalid path: {e}")

    if not p.exists() or not p.is_file():
        return ToolResult(error=f"file not found: {p}")
    if p.suffix.lower() not in (".png", ".jpg", ".jpeg"):
        return ToolResult(error=f"unsupported file type {p.suffix!r}; expected .png/.jpg")

    # Allowlist: must live under either
    #   - ~/.lucid/logs   (screenshots taken by the agent)
    #   - ~/.lucid/inbox  (clipboard pastes / drag-drop attachments
    #     forwarded from the desktop UI's `save_inbox_image` Tauri command)
    # so a model can't use this to exfiltrate arbitrary local files.
    allowed_roots: list[Path] = []
    base = Path.home() / ".lucid"
    for sub in ("logs", "inbox"):
        try:
            allowed_roots.append((base / sub).resolve())
        except Exception:
            pass
    # LUCID_CWD dev override (matches Tauri sidecar.rs::inbox_root)
    cwd_override = os.environ.get("LUCID_CWD")
    if cwd_override:
        try:
            allowed_roots.append((Path(cwd_override) / "inbox").resolve())
        except Exception:
            pass
    if allowed_roots:
        if not any(_is_under(p, root) for root in allowed_roots):
            roots_str = " | ".join(str(r) for r in allowed_roots)
            return ToolResult(
                error=f"path must be under one of: {roots_str} "
                "(only screenshots saved by this app or files attached via the chat input can be re-loaded)"
            )

    try:
        data = p.read_bytes()
    except Exception as e:
        return ToolResult(error=f"failed to read {p}: {e}")
    if not data:
        return ToolResult(error=f"empty file: {p}")

    # Loop.py uses tr.output as the follow-up image label for load_local_images;
    # embedding `[level=L?]` here makes the keep-recent policy treat the
    # re-loaded image as the same level as the original.
    label = f"[level={level}] re-loaded image from disk: {p.name} ({len(data)} bytes)"
    return ToolResult(output=label, image_png=data)


def _dispatch_read_webpage(args: dict[str, Any], cfg: Config) -> ToolResult:
    """Headless / CDP webpage read; returns text-only ToolResult.

    Output format begins with a single-line header so loop.py logs render the
    source/url/title legibly, followed by the extracted text body.
    """
    from . import webread as webread_mod

    url = (args.get("url") or "").strip() or None
    active_tab = bool(args.get("active_tab"))
    url_match = (args.get("url_match") or "").strip() or None
    browser = (args.get("browser") or "chrome").strip().lower()
    if browser not in ("chrome", "edge"):
        browser = "chrome"
    try:
        max_chars = int(args.get("max_chars") or cfg.webread.default_max_chars)
    except Exception:
        max_chars = cfg.webread.default_max_chars
    if max_chars <= 0:
        max_chars = cfg.webread.default_max_chars

    res = webread_mod.read_webpage(
        url=url,
        active_tab=active_tab,
        browser=browser,
        url_match=url_match,
        cdp_port=cfg.webread.cdp_port,
        max_chars=max_chars,
    )
    if not res.get("ok"):
        return ToolResult(error=res.get("error") or "read_webpage failed")
    header = (
        f"[read_webpage source={res['source']} url={res.get('url') or '?'!r} "
        f"title={res.get('title') or '?'!r} raw_html={res.get('raw_html_len', 0)} bytes]"
    )
    return ToolResult(output=f"{header}\n\n{res.get('text', '')}")


# ---------------------------------------------------------------------------
# read_file / write_file / run_shell — built-in zero-GUI utilities
# ---------------------------------------------------------------------------

def _expand_path(raw: str) -> str:
    """Expand %ENV%, $env:NAME (PowerShell-style), and ~ in a path."""
    import os
    import re
    s = raw.strip().strip('"').strip("'")
    # PowerShell-style $env:NAME → %NAME%
    s = re.sub(r"\$env:([A-Za-z_][A-Za-z0-9_]*)", lambda m: "%" + m.group(1) + "%", s)
    s = os.path.expandvars(s)
    s = os.path.expanduser(s)
    return s


def _truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    """Return (possibly-truncated text, was_truncated)."""
    if len(text) <= max_chars:
        return text, False
    half = max(256, max_chars // 2 - 64)
    head = text[:half]
    tail = text[-half:]
    notice = f"\n\n[... truncated {len(text) - 2 * half} chars; total = {len(text)} chars ...]\n\n"
    return head + notice + tail, True


def _dispatch_read_file(args: dict[str, Any], cfg: Config) -> ToolResult:
    """Pure-Python file read; bypasses cmd+screenshot+OCR for plain-text reads."""
    import os
    raw = (args.get("path") or "").strip()
    if not raw:
        return ToolResult(error="path required")
    path = _expand_path(raw)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return ToolResult(error=f"file not found: {path}")
    except PermissionError as e:
        return ToolResult(error=f"permission denied: {path} ({e})")
    except OSError as e:
        return ToolResult(error=f"stat failed: {path} ({type(e).__name__}: {e})")
    if not (st.st_mode & 0o170000) or os.path.isdir(path):
        return ToolResult(error=f"not a regular file: {path}")

    refuse_bytes = int(getattr(cfg.fileio, "read_refuse_bytes", 5 * 1024 * 1024))
    if st.st_size > refuse_bytes:
        return ToolResult(error=(
            f"file too large ({st.st_size} bytes > read_refuse_bytes={refuse_bytes}); "
            f"use run_shell with `Get-Content -TotalCount N` or open it in a real editor"
        ))

    max_bytes = args.get("max_bytes")
    try:
        max_bytes = int(max_bytes) if max_bytes is not None else int(cfg.fileio.read_max_bytes)
    except (TypeError, ValueError):
        max_bytes = int(cfg.fileio.read_max_bytes)
    max_bytes = max(256, min(max_bytes, refuse_bytes))

    # Read raw bytes (capped) then decode.
    try:
        with open(path, "rb") as f:
            data = f.read(max_bytes + 1)
    except Exception as e:
        return ToolResult(error=f"read failed: {type(e).__name__}: {e}")
    truncated_bytes = len(data) > max_bytes
    if truncated_bytes:
        data = data[:max_bytes]

    enc_arg = (args.get("encoding") or "auto").strip().lower() or "auto"
    encodings = [enc_arg] if enc_arg != "auto" else ["utf-8", "utf-8-sig", "gbk"]
    text: str | None = None
    used_enc: str = ""
    last_err: str = ""
    for enc in encodings:
        try:
            text = data.decode(enc)
            used_enc = enc
            break
        except UnicodeDecodeError as e:
            last_err = f"{enc}: {e}"
            continue
        except LookupError as e:
            last_err = f"{enc}: unknown encoding ({e})"
            continue
    if text is None:
        return ToolResult(error=(
            f"could not decode {path} as text ({last_err}); "
            f"file is likely binary. Use run_shell to inspect it."
        ))

    notice_parts = [f"path={path}", f"size={st.st_size}", f"encoding={used_enc}"]
    if truncated_bytes:
        notice_parts.append(f"truncated_to_first={max_bytes}_bytes")
    header = "[read_file " + " ".join(notice_parts) + "]"
    body = text
    if truncated_bytes:
        body += f"\n\n[... {st.st_size - max_bytes} more bytes not shown; raise max_bytes to see them]"
    return ToolResult(output=f"{header}\n{body}")


def _dispatch_write_file(args: dict[str, Any], cfg: Config) -> ToolResult:
    """Pure-Python file write; default mode = append + ensure trailing newline."""
    import os
    raw = (args.get("path") or "").strip()
    if not raw:
        return ToolResult(error="path required")
    if "content" not in args:
        return ToolResult(error="content required")
    content = args.get("content")
    if not isinstance(content, str):
        return ToolResult(error="content must be a string")
    mode = (args.get("mode") or "append").strip().lower()
    if mode not in ("append", "overwrite"):
        return ToolResult(error="mode must be 'append' or 'overwrite'")
    ensure_nl = bool(args.get("ensure_trailing_newline", True))

    payload = content
    if ensure_nl and not payload.endswith("\n"):
        payload += "\n"
    encoded = payload.encode("utf-8")
    cap = int(getattr(cfg.fileio, "write_max_bytes", 256 * 1024))
    if len(encoded) > cap:
        return ToolResult(error=(
            f"content too large ({len(encoded)} bytes > write_max_bytes={cap}); "
            f"split it across multiple write_file calls or use run_shell with redirection"
        ))

    path = _expand_path(raw)
    parent = os.path.dirname(path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception as e:
            return ToolResult(error=f"failed to create parent dir {parent}: {type(e).__name__}: {e}")

    prev_size: int | None = None
    if mode == "overwrite":
        try:
            prev_size = os.path.getsize(path)
        except OSError:
            prev_size = None

    open_mode = "ab" if mode == "append" else "wb"
    try:
        with open(path, open_mode) as f:
            f.write(encoded)
    except Exception as e:
        return ToolResult(error=f"write failed: {type(e).__name__}: {e}")

    msg_parts = [f"wrote {len(encoded)} bytes to {path} (mode={mode})"]
    if mode == "overwrite" and prev_size is not None:
        msg_parts.append(f"previous file was {prev_size} bytes (now replaced)")
    return ToolResult(output="; ".join(msg_parts))


def _dispatch_run_shell(args: dict[str, Any], cfg: Config) -> ToolResult:
    """Run cmd / powershell command via subprocess, no console window, capture text."""
    import os
    import subprocess
    cmd = args.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return ToolResult(error="command required")
    shell = (args.get("shell") or cfg.shell.default_shell or "powershell").strip().lower()
    if shell not in ("cmd", "powershell", "pwsh"):
        return ToolResult(error="shell must be 'cmd', 'powershell', or 'pwsh'")

    if shell == "cmd":
        argv = ["cmd.exe", "/d", "/c", cmd]
    elif shell == "powershell":
        # Windows PowerShell 5.1 defaults to SSL3/TLS1.0 for ServicePointManager,
        # which makes Invoke-WebRequest / Invoke-RestMethod against modern HTTPS
        # endpoints (bing.com, github.com, …) fail with "基础连接已经关闭 / 发送时
        # 发生错误". Auto-enable TLS 1.2+1.3 so HTTP one-liners just work. Cheap,
        # idempotent, no observable side effects on offline commands.
        tls_prelude = (
            "try { [Net.ServicePointManager]::SecurityProtocol = "
            "[Net.ServicePointManager]::SecurityProtocol -bor "
            "[Net.SecurityProtocolType]::Tls12 } catch {}; "
        )
        argv = ["powershell.exe", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", tls_prelude + cmd]
    else:  # pwsh
        argv = ["pwsh.exe", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", cmd]

    cwd_arg = args.get("cwd")
    cwd: str | None = None
    if cwd_arg:
        cwd = _expand_path(str(cwd_arg))
        if not os.path.isdir(cwd):
            return ToolResult(error=f"cwd does not exist or is not a dir: {cwd}")

    try:
        timeout = float(args.get("timeout_s") or cfg.shell.timeout_s)
    except (TypeError, ValueError):
        timeout = float(cfg.shell.timeout_s)
    timeout = max(0.5, min(timeout, 120.0))  # hard cap

    # CREATE_NO_WINDOW = 0x08000000 — hides any console window the child would
    # otherwise spawn. On Windows only; harmless flag elsewhere.
    creationflags = 0x08000000 if os.name == "nt" else 0

    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            timeout=timeout,
            capture_output=True,
            creationflags=creationflags,
            shell=False,  # argv is already split; do NOT let cmd re-parse
        )
    except FileNotFoundError as e:
        return ToolResult(error=f"shell executable not found: {argv[0]} ({e})")
    except subprocess.TimeoutExpired as e:
        # Best-effort: include any partial output captured before the timeout.
        partial_out = (e.stdout or b"").decode("utf-8", "replace")[-2000:]
        partial_err = (e.stderr or b"").decode("utf-8", "replace")[-2000:]
        return ToolResult(error=(
            f"command timed out after {timeout}s. "
            f"For long-running tasks open a real terminal via launch_app('{shell}'). "
            f"\n--- partial stdout (last 2000 chars) ---\n{partial_out}"
            f"\n--- partial stderr (last 2000 chars) ---\n{partial_err}"
        ))
    except Exception as e:
        return ToolResult(error=f"subprocess failed: {type(e).__name__}: {e}")

    # Decode (Windows console is often gbk/cp936 for cmd, utf-8 for PowerShell 7+).
    def _dec(b: bytes) -> str:
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                return b.decode(enc)
            except UnicodeDecodeError:
                continue
        return b.decode("utf-8", "replace")

    out = _dec(proc.stdout or b"")
    err = _dec(proc.stderr or b"")

    max_chars = int(getattr(cfg.shell, "max_output_chars", 16_000))
    combined = ""
    if out:
        combined += f"--- stdout ({len(out)} chars) ---\n{out}"
    if err:
        if combined:
            combined += "\n"
        combined += f"--- stderr ({len(err)} chars) ---\n{err}"
    if not combined:
        combined = "(no output)"
    combined, _ = _truncate_text(combined, max_chars)

    header = f"[run_shell shell={shell} exit_code={proc.returncode} timeout_s={timeout}]"
    text = f"{header}\n{combined}"
    # exit code != 0 is informational, not necessarily an error worth signalling
    # via tr.error (the model needs to see stdout/stderr to decide). We surface
    # it via the header line.
    return ToolResult(output=text)

