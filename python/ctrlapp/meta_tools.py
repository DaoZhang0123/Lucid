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


def build_meta_tool_schemas(cfg: Config) -> list[dict]:
    """按 cfg 开关返回当次请求要附带的 meta tool schemas 列表。"""
    out: list[dict] = []
    if cfg.memory.enabled:
        out.append(REMEMBER_SCHEMA)
    if cfg.tools.enabled:
        out.append(LEARN_TIP_SCHEMA)
    if cfg.icons.enabled:
        out.append(REMEMBER_ICON_SCHEMA)
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
        ok = tooltips_mod.append_tip(cfg.tools, text, kind=kind, source="agent")
        return ToolResult(
            output=f"tip saved ({kind}): {text[:80]}" if ok else "",
            error=None if ok else "tools disabled or empty",
        )

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
