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
            "把一条值得长期保留的事实写入 memory.md（覆盖式追加，下次任务起手会注入到 system prompt）。"
            "适用：用户明确要求记住的偏好；用户的称呼 / 身份；用户的操作习惯（常用浏览器、编辑器、"
            "快捷键、保存路径、命名风格、深色模式偏好等）；以及短期内不会变的环境事实（如常用工作目录）。"
            "**不要**记单次任务的临时事实、过程中间结果、密码 token 等敏感信息。"
            "写入前先看 system prompt 末尾的『长期记忆』段，避免重复或冲突。"
            "格式要求：单行、不超过 200 字、陈述句。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要写入的一条记忆，单行陈述句。"}
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
            "把一条**操作技巧**（针对某个 App / 对话框 / 控件 怎么做更稳）追加进 tools.md，"
            "下次任务起手会注入到 system prompt。\n"
            "**何时主动调用（满足任一即可）**：\n"
            "1) 用快捷键 / 命令行 成功打开/操作了某个 App（例：Ctrl+Alt+W 开微信、"
            "Win+R→outlook 开 Outlook）——这是最高价值的技巧，**只要试过且 work，立刻登记**；\n"
            "2) 绕过了一个曾经卡住的坑（例：WeChat Enter 是发送、Shift+Enter 才换行）；\n"
            "3) 发现旧技巧错了/过时了，写一条新的覆盖性条目。\n"
            "**不要**记单次任务的临时事实（那不是技巧）、也不要记用户偏好（那是 memory.md）。"
            "写入前先看下方『操作技巧』已有条目避免重复。"
            "格式：单行陈述句、不超 200 字；带上 App / 场景标签以便检索。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要写入的技巧。例：「Outlook 用 Ctrl+R 回复比鼠标点回复按钮更稳」"},
                "kind": {"type": "string", "enum": ["success", "failure", "tip"], "description": "成功经验 / 失败教训 / 一般提示。默认 success。"},
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
            "把当前截图里的一小块（典型场景：任务栏 / 系统托盘里的小图标）裁剪下来登记到"
            "**图标记忆库**，下次任务起手会被拼成一张『图标合集』图随 system prompt 注入，"
            "帮助你（以及未来的自己）识别小尺寸图标。\n"
            "适用：用户明确教你『这个图标 = XX App』，或你通过上下文确认了某个图标的含义"
            "且该图标在多个任务里会复现（如微信 / QQ / Steam / 网易云 在系统托盘里的常驻图标）。\n"
            "**不要**登记：临时弹出的提示气泡、广告横幅、任务相关的一次性截图。\n"
            "坐标说明：x/y/w/h 是**图片像素坐标**（不是屏幕坐标）；level 指你引用的是哪一层截图："
            "L1=全屏、L2=活动窗口、L3=鼠标周边。建议直接用 L1 全屏截图框选托盘图标。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "图标的短标签（≤20 字）。例如 『微信』『QQ 音乐 托盘』。"},
                "description": {"type": "string", "description": "对该图标含义/位置的简短说明（≤200 字），便于后续检索。"},
                "x": {"type": "integer", "description": "裁剪区域左上角 x（图片像素）"},
                "y": {"type": "integer", "description": "裁剪区域左上角 y（图片像素）"},
                "w": {"type": "integer", "description": "裁剪区域宽度（图片像素）。建议 24~96。"},
                "h": {"type": "integer", "description": "裁剪区域高度（图片像素）。建议 24~96。"},
                "level": {"type": "string", "enum": ["L1", "L2", "L3"], "description": "从哪一层最近一张截图裁剪。默认 L1。"},
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
            output=f"已写入记忆：{text[:80]}" if ok else "",
            error=None if ok else "memory disabled or empty",
        )

    if fn_name == "learn_tip" and cfg.tools.enabled:
        text = (args.get("text") or "").strip()
        kind = (args.get("kind") or "success").strip().lower() or "success"
        ok = tooltips_mod.append_tip(cfg.tools, text, kind=kind, source="agent")
        return ToolResult(
            output=f"已写入技巧({kind})：{text[:80]}" if ok else "",
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
                output=(f"已登记图标 #{entry['id']} 『{entry['label']}』 "
                        f"({w}x{h} from {level} @ {x},{y})。下次任务起手会自动注入合集图。"),
            )
        return ToolResult(error="failed to crop or save icon (check image bounds)")

    return None
