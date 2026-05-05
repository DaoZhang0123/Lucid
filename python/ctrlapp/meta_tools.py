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
from . import scheduler as scheduler_mod


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
# Scheduled tasks (`schedule_*`) — let the agent create / inspect / edit
# the user's scheduled instructions through conversation.
# ---------------------------------------------------------------------------

SCHEDULE_LIST_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "schedule_list",
        "description": (
            "List all scheduled tasks the user has registered. Each entry: id, name, instruction, spec "
            "(kind=hourly/daily/weekly + minute/time/weekday/tz), autonomy, max_steps, enabled, optional constraints "
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
                "autonomy":    {"type": "string", "enum": ["full", "confirm_critical", "confirm_each"], "description": "Default 'confirm_critical'."},
                "max_steps":   {"type": "integer", "description": "Step budget per fire. Default 25."},
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
                "autonomy":    {"type": "string", "enum": ["full", "confirm_critical", "confirm_each"]},
                "max_steps":   {"type": "integer"},
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
    # Scheduler tools are always on — the scheduler thread is part of the sidecar core.
    out.append(SCHEDULE_LIST_SCHEMA)
    out.append(SCHEDULE_ADD_SCHEMA)
    out.append(SCHEDULE_UPDATE_SCHEMA)
    out.append(SCHEDULE_DELETE_SCHEMA)
    return out


def _resolve_launch_region(
    sensor: Any,
    cfg: Config,
    hwnd: int,
    method: str,
    pre_cap: Any | None,
) -> tuple[int, int, int, int] | None:
    """Decide the screen rect to capture as L2 after launch_app (Docs/screenshot.md §13.3).

    Strategy:
    - If method == 'focus_existing_window': skip diff, just use window_client_rect(hwnd).
    - Otherwise (shortcut / uri / exe): wait briefly for the window to become
      visible+non-iconic, then try the diff bbox between pre_cap and a fresh L1;
      if diff misses or doesn't overlap the hwnd's client rect, fall back to
      window_client_rect.
    """
    import time as _time
    from .screen import ScreenSensor, ScreenLevel

    wait_ms = int(getattr(cfg.screenshot, "launch_wait_max_ms", 1500))
    poll_ms = int(getattr(cfg.screenshot, "launch_wait_poll_ms", 80))
    min_area = float(getattr(cfg.screenshot, "launch_diff_min_area_ratio", 0.05))
    activate_only = method == "focus_existing_window"

    # Wait for the window to materialise (best-effort; only on real launches).
    if hwnd and not activate_only:
        deadline = _time.monotonic() + wait_ms / 1000.0
        while _time.monotonic() < deadline:
            r = launchers_mod.window_client_rect(hwnd)
            if r is not None:
                break
            _time.sleep(poll_ms / 1000.0)

    client_rect = launchers_mod.window_client_rect(hwnd) if hwnd else None

    if activate_only:
        return client_rect

    # Real launch path → try diff first.
    if pre_cap is not None:
        try:
            post_cap = sensor.capture(ScreenLevel.L1)
            bbox = ScreenSensor.diff_bbox(
                pre_cap.image, post_cap.image, min_area_ratio=min_area
            )
        except Exception:
            bbox = None
        if bbox is not None:
            # bbox is in image (sent) coords; convert to screen coords.
            bx, by, bw, bh = bbox
            rw, rh = post_cap.raw_size
            sw, sh = post_cap.sent_size
            sx_scale = rw / sw if sw else 1.0
            sy_scale = rh / sh if sh else 1.0
            ox, oy = post_cap.offset
            screen_rect = (
                int(ox + bx * sx_scale),
                int(oy + by * sy_scale),
                int(bw * sx_scale),
                int(bh * sy_scale),
            )
            # Sanity-check vs client_rect IoU; otherwise fall back to client_rect.
            if client_rect is None or _iou(screen_rect, client_rect) >= 0.30:
                return screen_rect

    return client_rect


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix0 = max(ax, bx); iy0 = max(ay, by)
    ix1 = min(ax + aw, bx + bw); iy1 = min(ay + ah, by + bh)
    iw = max(0, ix1 - ix0); ih = max(0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


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

        # ---- R2: pre-launch L1 (only if diff is enabled and we have a sensor) ----
        from .screen import ScreenSensor, ScreenLevel  # local import to avoid cycles
        diff_enabled = bool(getattr(cfg.screenshot, "launch_diff_enabled", False)) and sensor is not None
        pre_cap = None
        if diff_enabled:
            try:
                # Only capture pre-L1 when we'll actually run a diff (i.e. not
                # the focus_existing_window path). We can't tell yet what method
                # launchers will pick, so capture optimistically; cheap mss grab.
                pre_cap = sensor.capture(ScreenLevel.L1)
            except Exception:
                pre_cap = None

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

        # ---- R2: visual capture (region resolution + L2 crop) ----
        image_png: bytes | None = None
        attached_l2 = None
        attached_rect: tuple[int, int, int, int] | None = None
        if result.get("ok") and sensor is not None:
            hwnd = result.get("hwnd") or 0
            method = (result.get("method") or "").lower()
            slug = result.get("slug") or name
            try:
                rect = _resolve_launch_region(
                    sensor=sensor,
                    cfg=cfg,
                    hwnd=int(hwnd) if hwnd else 0,
                    method=method,
                    pre_cap=pre_cap if diff_enabled else None,
                )
            except Exception as e:
                rect = None
                out_lines.append(f"  region resolution failed: {type(e).__name__}: {e}")
            if rect is not None:
                rx, ry, rw, rh = rect
                try:
                    l2 = sensor.capture_region(rx, ry, rw, rh)
                    image_png = l2.png_bytes()
                    attached_l2 = l2
                    attached_rect = (rx, ry, rx + rw, ry + rh)
                    out_lines.append("")
                    out_lines.append(
                        f"  region: x={rx} y={ry} w={rw} h={rh} (L2 attached as user message; this is now the active coordinate frame for the next click. Subsequent post-step screenshots will stay narrowed to this rect until you explicitly call screenshot(level='fullscreen').)"
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
                        f"  L2 capture of region ({rx},{ry},{rw}x{rh}) failed: {type(e).__name__}: {e}"
                    )
            else:
                out_lines.append(
                    f"  no region resolved (hwnd={hwnd} method={method}); skipping L2 attachment"
                )

        return ToolResult(
            output="\n".join(out_lines),
            error=None if result.get("ok") else result.get("message"),
            image_png=image_png,
            attached_capture=attached_l2,
            attached_active_rect=attached_rect,
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

    if fn_name in ("schedule_list", "schedule_add", "schedule_update", "schedule_delete"):
        return _dispatch_schedule(fn_name, args)

    return None


def _fmt_schedule(it: dict) -> str:
    spec = it.get("spec") or {}
    parts = [f"#{it.get('id')} {it.get('name')!r} ({'on' if it.get('enabled') else 'off'})",
             f"  spec: {spec}"]
    cons = it.get("constraints") or {}
    if cons:
        parts.append(f"  constraints: {cons}")
    parts.append(f"  instruction: {(it.get('instruction') or '')[:120]}")
    parts.append(f"  autonomy={it.get('autonomy')} max_steps={it.get('max_steps')}")
    nm = it.get("next_ms") or 0
    lm = it.get("last_run_ms") or 0
    if nm:
        from datetime import datetime as _dt
        parts.append(f"  next: {_dt.fromtimestamp(nm/1000).isoformat(timespec='seconds')}")
    if lm:
        from datetime import datetime as _dt
        parts.append(f"  last: {_dt.fromtimestamp(lm/1000).isoformat(timespec='seconds')}")
    return "\n".join(parts)


def _dispatch_schedule(fn_name: str, args: dict[str, Any]) -> ToolResult:
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
                autonomy=str(args.get("autonomy") or "confirm_critical"),
                max_steps=int(args.get("max_steps") or 25),
                enabled=bool(args.get("enabled", True)),
                constraints=args.get("constraints"),
            )
            return ToolResult(output="Schedule created:\n" + _fmt_schedule(it))

        if fn_name == "schedule_update":
            sid = str(args.get("id") or "").strip()
            if not sid:
                return ToolResult(error="id required")
            fields: dict[str, Any] = {}
            for k in ("name", "instruction", "spec", "autonomy", "max_steps", "enabled", "constraints"):
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
