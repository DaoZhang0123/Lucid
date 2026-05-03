"""Agent 的 "自我策展" function tools（除 `computer` 之外）。

包含 3 个工具：
- ``remember``       —— 把用户偏好 / 称呼 / 习惯写入 ``memory.md``
- ``learn_tip``      —— 把操作技巧（成功路径 / 失败教训）写入 ``tools.md``
- ``remember_icon``  —— 从最近一张截图裁一小块登记到 ``icons/`` 图标记忆库

每个工具暴露：
- ``XXX_SCHEMA``：OpenAI function-calling 格式的 schema dict
- 在 :func:`dispatch_meta_tool` 里的实际处理逻辑

ReAct 主循环（``loop.py``）只需调用 :func:`build_meta_tool_schemas` 拿到 schemas 列表
（按 ``cfg.{memory,tools,icons}.enabled`` 开关过滤），并在收到 tool_call 时把
非 ``computer`` 的工具名转发到 :func:`dispatch_meta_tool`。
"""
from __future__ import annotations

from typing import Any

from .config import Config
from .tools import ToolResult
from . import memory as memory_mod
from . import tooltips as tooltips_mod
from . import icon_memory as icon_mod
from . import launchers as launchers_mod
from . import regions as regions_mod


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

REMEMBER_ICON_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "remember_icon",
        "description": (
            "Crop a small region of the most recent screenshot (typical use: a tiny icon in the taskbar or system tray) "
            "and register it into the **icon memory library**. At the start of every future task all registered icons are "
            "composed into one 'icon atlas' image and injected with the system prompt, helping you (and your future self) "
            "recognise small icons.\n"
            "Use when: the user explicitly tells you 'this icon = App X', or you have confirmed an icon's meaning from context "
            "AND the icon will recur across tasks (e.g. WeChat / QQ / Steam / NetEase Music persistent tray icons).\n"
            "**Do NOT** register: transient popup bubbles, ad banners, one-shot task screenshots.\n"
            "Coordinates: x/y/w/h are **image pixel coordinates** (NOT screen coordinates); `level` selects which screenshot "
            "layer you reference: L1=fullscreen, L2=active window, L3=cursor-local. Recommend cropping tray icons directly from L1."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "Short label for the icon (<=20 chars). e.g. 'WeChat', 'QQ Music tray'."},
                "description": {"type": "string", "description": "Brief note about meaning / location (<=200 chars) for later search."},
                "x": {"type": "integer", "description": "Crop top-left x (image pixels)."},
                "y": {"type": "integer", "description": "Crop top-left y (image pixels)."},
                "w": {"type": "integer", "description": "Crop width (image pixels). Recommended 24-96."},
                "h": {"type": "integer", "description": "Crop height (image pixels). Recommended 24-96."},
                "level": {"type": "string", "enum": ["L1", "L2", "L3"], "description": "Which most-recent screenshot layer to crop from. Default L1."},
            },
            "required": ["label", "x", "y", "w", "h"],
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

UPDATE_LAUNCHER_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "update_launcher",
        "description": (
            "Persistently update fields of a launcher entry — e.g. when you discovered the global shortcut changed, the exe alias was wrong, or the window title needs a different regex. "
            "The override is written to `<user data>/launchers.json` and takes precedence over the built-in defaults from this version, and survives across tasks / restarts.\n"
            "**When to call**: after a `launch_app` / `check_app_running` failure that you traced to a wrong field (e.g. `shortcut` no longer triggers WeChat — try the new combo, then call `update_launcher(name='wechat', shortcut='ctrl+shift+w')`). Don't call for one-off failures (network glitch, app currently broken). After updating, call `launch_app` again to verify, and consider also `learn_tip(app=..., text='shortcut changed from X to Y on YYYY-MM')`."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name":            {"type": "string", "description": "Launcher slug to update (must already exist; call `list_apps()` to see slugs). To create a brand-new launcher use the UI."},
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
# Region calibration / lookup
# ---------------------------------------------------------------------------

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
    if cfg.icons.enabled:
        out.append(REMEMBER_ICON_SCHEMA)
    if getattr(cfg, "launchers", None) and cfg.launchers.enabled:
        out.append(LAUNCH_APP_SCHEMA)
        out.append(LIST_APPS_SCHEMA)
        out.append(CHECK_APP_RUNNING_SCHEMA)
        out.append(FOCUS_WINDOW_SCHEMA)
        out.append(UPDATE_LAUNCHER_SCHEMA)
    if getattr(cfg, "regions", None) and cfg.regions.enabled:
        out.append(REGION_SCHEMA)
    return out


def dispatch_meta_tool(
    fn_name: str,
    args: dict[str, Any],
    cfg: Config,
    last_png_by_level: dict[str, bytes],
) -> ToolResult | None:
    """处理一次 meta tool 的 tool_call。

    返回 ``None`` 表示 ``fn_name`` 不是已知 meta tool（也不在 enabled 集合里），
    调用方应当报 ``unknown tool``。
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
        result = launchers_mod.launch_app(cfg.launchers, name)
        # On success, also append the app's tips body so the model gets it for free.
        out_lines = [f"launch_app: {result.get('message', '')}"]
        for k in ("ok", "method", "slug", "hwnd", "pid", "window_title"):
            if k in result and result[k] is not None:
                out_lines.append(f"  {k}: {result[k]}")
        if result.get("ok") and cfg.tools.enabled:
            slug = result.get("slug") or name
            tips_body = tooltips_mod.app_tips_for_prompt(cfg.tools, slug)
            if tips_body:
                out_lines.append("")
                out_lines.append(tips_body)
        return ToolResult(output="\n".join(out_lines), error=None if result.get("ok") else result.get("message"))

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

    if fn_name == "remember_icon" and cfg.icons.enabled:
        label = (args.get("label") or "").strip()
        desc = (args.get("description") or "").strip()
        level = (args.get("level") or "L1").strip().upper() or "L1"
        try:
            x = int(args.get("x", 0))
            y = int(args.get("y", 0))
            w = int(args.get("w", 0))
            h = int(args.get("h", 0))
        except (TypeError, ValueError):
            x = y = w = h = 0
        src_png = last_png_by_level.get(level)
        if not src_png:
            return ToolResult(error=f"no recent {level} screenshot to crop from")
        if not label:
            return ToolResult(error="label required")
        if w <= 0 or h <= 0:
            return ToolResult(error="w/h must be > 0 (image pixels)")
        entry = icon_mod.crop_and_add(cfg.icons, src_png, x, y, w, h, label, desc)
        if entry:
            return ToolResult(
                output=(f"icon registered #{entry['id']} '{entry['label']}' "
                        f"({w}x{h} from {level} @ {x},{y}). It will be auto-injected as part of the icon atlas at the start of every future task."),
            )
        return ToolResult(error="failed to crop or save icon (check image bounds)")

    return None
