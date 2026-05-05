"""ReAct loop —— 通过本地 LiteLLM 代理（OpenAI 兼容 /chat/completions）驱动 Claude。

工具：自定义的 OpenAI function tool `computer`，参数与 Anthropic computer_use_20250124
保持一致（action / coordinate / text / scroll_* / duration）。

图像通过 OpenAI 多模态 content 数组以 data URL 形式发送。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from typing import Any, Callable

from openai import APIError, APIConnectionError, APIStatusError
from rich.console import Console

from .config import Config
from .input_driver import InputDriver
from .llm_client import (
    AnthropicClient,
    CopilotClient,
    LLMClient,
    OpenAIChatClient,
)
from .runlog import ThreadLog
from .safety import SafetyLayer
from .screen import Capture, ScreenLevel, ScreenSensor
from .tools import ComputerTool, ToolResult
from . import memory as memory_mod
from . import tooltips as tooltips_mod
from . import icon_memory as icon_mod
from . import meta_tools
from . import launchers as launchers_mod
from . import regions as regions_mod
from .context_manager import ContextManager, build_summary_prompt

_console = Console()

SYSTEM_PROMPT_HEAD = """\
You are a vision-driven GUI Agent running on a Windows desktop.
You can only interact with the system through the `computer` tool (screenshot, mouse, keyboard).

Working principles:
1. **No fullscreen screenshot is sent up-front.** The first user message tells you the task — start by calling `launch_app(name="...")` (which brings the target App to the foreground AND attaches an L2 screenshot of its window) or, if you genuinely need to see the whole desktop first, call `screenshot(level="fullscreen")` explicitly.
2. **Three-tier screenshot strategy** (selected via the `level` parameter when action="screenshot"):
   - level="fullscreen": the entire virtual desktop. Use only when you need to see icons/widgets across multiple windows; this also **releases** any active-app pin (see below).
   - level="active_window": only the focused window, higher resolution, used for in-App work.
   - level="cursor_local": a small high-detail tile around the mouse, used to click small buttons, read small text, or verify selection.
   Whichever you pick, subsequent `coordinate` values must use **that screenshot's coordinate system**.
   - **Active-app pinning:** after a successful `launch_app` / `focus_window`, the system pins the App's window rect as the active coordinate frame. The very first post-step screenshot is an L2 of that whole rect (your "map"); **subsequent post-step screenshots are L3 (cursor-local) by default** — the L2 map's coordinate frame is still the one for your `coordinate` arguments. If you need a fresh L2 map, call `screenshot(level="active_window")` explicitly.
   - Click coordinates that reverse-map outside the pinned App rect are rejected. To leave the App, call `screenshot(level="fullscreen")` (which also releases the pin).
3. Keep action granularity small: do one step at a time, then screenshot to verify.
4. For text input use action="type" + text="...". The local driver pastes via the clipboard, IME-independent; CJK / English / paths can all be passed directly. **Newlines (`\n`) inside `text` are pasted as soft line breaks**, NOT as Enter — so in chat apps like WeChat / Telegram (where Enter sends), a multi-line message stays as ONE message with line breaks. To submit / send, issue a separate `key` action (e.g. `Return`).
5. **Every intermediate step MUST call the `computer` tool**; do not just emit narration like "I will now..." or "Let me...".
   The only time you may skip the tool call is when the task is confirmed complete or confirmed impossible — in that case, summarise with a message starting with "task complete:" or "task failed:".
6. Do not try to shut down / reboot the system or operate elevated/privileged windows.
7. `coordinate` must be image pixel coordinates (top-left origin); do not give percentages or relative coordinates.
8. **Screenshots age out — write down what you see.** A context manager automatically recompresses old screenshots
   (heavy JPEG downscale) and may even drop them entirely once the conversation grows. The crisp image you have
   *right now* will likely be unreadable a few steps later. So whenever a screenshot shows information you might
   need to refer back to — error dialog text, file/folder lists, form field values you just typed, target
   coordinates of a small button you spotted, OCR'd labels, the contents of a chat thread, search results,
   row/column data, etc. — **transcribe the salient parts into your assistant text in this turn**. Treat your own
   text as the durable working memory; the images are ephemeral. Be concrete: quote exact strings, list items
   verbatim, write down `(x, y)` coordinates of buttons before you click them. Do not assume "I'll look again
   later" — later you may not be able to.
"""

# Item 9 — the two-phase preview-then-confirm protocol — is ONLY appended when
# safety.verify_click_target_before is True. When the flag is False (current
# default) clicks execute immediately on first call; telling the model
# otherwise causes it to "confirm" with a duplicate click that re-hits the
# same target as a no-op (low pixel-change), which it then misreads as a miss.
TWO_PHASE_CLICK_SECTION = """\
9. **Two-phase click protocol.** Every click action with a `coordinate` (`left_click` / `right_click` /
   `middle_click` / `double_click` / `triple_click` / `left_click_drag`) goes through preview-then-confirm:
   - **First call** (no `confirmed` flag, or `confirmed=false`): the click is **NOT** executed. Instead, the
     system captures a high-detail L3 tile around the target screen coordinate and returns it to you in the
     next user message. Use this tile to **verify what is actually under the cursor at that pixel right now**
     (which button / icon / text / cell?). The screen may have shifted since your last full screenshot —
     this is your last chance to catch a wrong-target click.
   - **Second call** to actually click: re-issue the **SAME** action with the **SAME** coordinate and add
     `confirmed=true` to the args. The click then runs normally.
   - If the preview shows the wrong target, **do NOT confirm**. Pick a different coordinate, take a fresh
     screenshot, or change strategy. Always prefer keyboard shortcuts over a second click attempt when
     possible. Skipping the preview entirely (e.g. blindly retrying with `confirmed=true` after a miss) is
     forbidden.
"""

# When two-phase is OFF (default), tell the model that clicks fire on first
# call, so it doesn't waste a turn trying to "confirm". Also explain the
# `(post-click pixel-change X%)` text it will see, so it doesn't read a low
# percentage as a guaranteed miss (e.g. WeChat contact-row hover ≈ 1%).
SINGLE_PHASE_CLICK_SECTION = """\
9. **Clicks fire immediately.** Every click action (`left_click` / `right_click` / `middle_click` /
   `double_click` / `triple_click` / `left_click_drag`) is performed on the first tool call — there is
   NO preview-then-confirm step. You do **not** need to add `confirmed=true`; sending the same click
   twice will hit the target twice (and on already-selected items the second hit is usually a no-op).
   - The tool result text `(post-click pixel-change X%)` is informational, not a verdict. It is the
     fraction of pixels that changed in a small region around the cursor between just before and just
     after the click. A high % usually means the click did something visible nearby; a low % can mean
     either (a) the click missed, or (b) the click landed but the visible reaction happened **far from
     the cursor** (typical: clicking a contact in WeChat opens the chat view on the far right while
     the cursor stays on the row → only ~1% pixels changed near the cursor, but the click DID work).
     If a click result is ambiguous, take a fresh `screenshot(level="active_window")` to see the whole
     window before deciding to retry — do **not** blindly re-click the same coordinate.
   - When `(... pixel-change X%)` is below the miss threshold (default 0.5%), the system additionally
     attaches an L3 tile around the cursor with an explicit "may have missed" hint; use that tile + a
     fresh L2 if needed to decide whether to retry or change strategy.
"""

SYSTEM_PROMPT_TAIL = """\

Operation tips library (dynamically learned):
- The "## Operation tips" section below is a **dynamic tip library** injected from tools.md, containing reliable ways to drive
  various Apps / dialogs / controls. Skim it before starting a task and follow it when the situation matches; pay particular
  attention to general principles like "don't overwrite the user's in-progress work", "alt+tab first to check if it's already
  open", and "in save dialogs, type the absolute path directly".
- **When you should proactively call `learn_tip`** (any one of these is enough — don't hesitate):
  1) **You used a shortcut / command line to successfully open or operate an App** (even on the first try) — these are the
     highest-value tips, because next time you can drive the same App from the keyboard and skip several icon-recognition steps.
     e.g. `Ctrl+Alt+W` opens WeChat, `Win+R` -> `outlook` opens Outlook, `Win+E` opens File Explorer.
     **As soon as you've tried it and it works, learn_tip it.**
  2) **You worked around a pit you had previously got stuck on**: e.g. you discovered "in WeChat the Enter key sends and
     Shift+Enter inserts a newline", or "in VS Code's save dialog, pasting an absolute path is faster than clicking the sidebar".
  3) **You found an existing tip in tools.md is wrong / outdated**: write a new overriding entry, mentioning "supersedes old entry XXX" in the description.
- **Do NOT** record one-shot facts ("this task I saved a file to D:\\tmp" is not a tip); also don't record user preferences
  (those go in memory.md).
- Calling convention: `learn_tip(text="<App / scenario / approach>", kind="success" | "failure" | "tip")`.
  Before writing, scan the existing "## Operation tips" section to avoid duplicates (don't log the same shortcut twice).
- e.g. `learn_tip(text="WeChat for PC: Ctrl+Alt+W brings up the main window directly (no need to click the tray icon)", kind="success")`
- e.g. `learn_tip(text="Outlook: Ctrl+R to reply is more reliable than clicking the Reply button", kind="success")`
- e.g. `learn_tip(text="WeChat input box: Enter sends immediately; use Shift+Enter for newline", kind="failure")`

Long-term memory:
- Use the `remember(text)` tool to persist information worth keeping long-term to memory.md. **It is NOT** an action of the
  `computer` tool; it is a separate function whose only argument is a single string `text`.
- What you SHOULD record (any one of these):
  1) The user **explicitly** asks: "remember...", "from now on always...", "I prefer...", "my X is Y".
  2) The user's **identity / how they want to be addressed**: self-introduced name, nickname, preferred form of address
     ("call me Lao Zhang", "I'm Zhang"), profession / role.
  3) The user's **operating habits / preferences**: preferred browser, editor, common shortcuts, preferred language
     (Chinese / English), default save paths, typical filename style, preferred search engine, dark-mode preference, etc.
  4) The user's **environment facts** that won't change soon: usual working directory, persistently running Apps,
     dual-monitor / primary-monitor specifics, desktop shortcut layout conventions, and so on.
  Before writing, **first scan the current memory.md content** (already injected at the end of the system prompt) to avoid
  duplicates / conflicts; if you find a conflict (the user changed preference), write a new overriding entry rather than keep the old.
- What you should NOT record: one-shot facts from the current task ("I just saved a file to D:\\tmp"), procedural intermediate
  results, one-time observations, passwords / tokens / bank account numbers / verification codes or other private/sensitive info.
- Format: each memory entry is **a single line, <=200 chars**, declarative rather than imperative; you may write 0 to several
  entries per task, but don't split the same idea into multiple entries.

Icon memory (visual knowledge base):
- You're inherently weak at recognising 16-32 px icons (taskbar / system tray / favourites / tab favicons).
  For this we provide `remember_icon(label, description, x, y, w, h, level)`: it crops a small region (typically 24-96 px)
  from the most recent screenshot at the given level (default L1) using **image pixel coordinates** and registers it.
  At the start of every future task all registered icons are composed into one 'icon atlas' image (tagged [level=L0],
  never pruned) and injected with the prompt, letting you 'identify icons by their atlas number'.
- **When you should proactively register** (any one of these):
  1) The user explicitly tells you "this icon = App X";
  2) You **clicked a tray / taskbar icon and confirmed it succeeded** (the corresponding App's main window appeared), and that
     icon does **not** yet have an entry in the atlas — register it now so next time you can just look it up instead of probing.
  3) You have **confirmed** a resident icon's meaning via context (surrounding text / hover tooltip / Task Manager / etc.).
  4) **You just opened / interacted with an App by ANY means (shortcut, Win+R, Start menu, alt-tab) and you can now spot its
     resident tray / taskbar icon on screen.** This is the most common opportunity and you should not skip it just because you
     didn't click the icon. Workflow: take an L3 (`cursor_local`) or L2 (`active_window` of the taskbar area) screenshot to
     locate the icon precisely, read off its `(x, y, w, h)` in **image pixel coordinates of that screenshot**, then call
     `remember_icon(label, description, x, y, w, h, level)`. Examples: opened WeChat with Ctrl+Alt+W → take L3 over the system
     tray, find the green chat-bubble, register it. Opened VS Code via Win+R → look at the taskbar, find the blue ribbon icon,
     register it. Doing this once per App pays for itself many times over.
- **Do NOT** register: transient popups, one-shot task-related screenshots, ad banners, purely decorative non-App images.
- **Avoid duplicates**: before registering, check the text index of the 'icon atlas' injected after the system prompt; if the
  same App already has a number, **do not** register it again (even at slightly different resolutions). If you find an existing
  entry's description is wrong, you may register a new one and note "replaces #N" in the description; the user can decide whether to delete the old.
- Example call: `remember_icon(label="WeChat", description="Resident green chat-bubble icon in the Windows system tray", x=1620, y=1410, w=28, h=28, level="L1")`
"""


def _build_system_prompt(cfg: Any) -> str:
    """Assemble the system prompt, picking the click-protocol section that
    matches the current ``safety.verify_click_target_before`` setting so we
    don't lie to the model about whether clicks are previewed first.
    """
    two_phase = bool(getattr(getattr(cfg, "safety", None), "verify_click_target_before", False))
    click_section = TWO_PHASE_CLICK_SECTION if two_phase else SINGLE_PHASE_CLICK_SECTION
    return SYSTEM_PROMPT_HEAD + click_section + SYSTEM_PROMPT_TAIL


def _data_url(png: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(png).decode("ascii")


def _prune_old_images_dispatch(messages: list[dict[str, Any]], keep_n: int,
                               name_lookup: Callable[[bytes], str | None] | None = None,
                               run_dir: Any = None,
                               keep_per_level: dict[str, int] | None = None) -> int:
    """按级别 + 全局最近 N 张做滑窗裁剪。

    规则（保留集 = 下面两者的并集）：
      A) **每级最近 K 张**：L1/L2/L3 各自按 keep_per_level 给的 K 值保留最近 K 张
         （未指定时默认每级 1 张）；
      B) **全局最近 keep_n 张**：再额外保留出现顺序最末的 keep_n 张（keep_n<=0 时跳过 B）。

    其余图片在 content list 里被原地替换成占位文字，文字里携带本地文件名/路径，
    模型/调试者按需引用。返回被裁掉的图片数量。
    """
    if keep_per_level is None:
        keep_per_level = {"L1": 1, "L2": 1, "L3": 1}
    # 收集所有 image_url 块及其级别标签 + base64 字节，按消息出现顺序。
    entries: list[tuple[int, int, str, bytes | None]] = []
    for mi, m in enumerate(messages):
        c = m.get("content")
        if not isinstance(c, list):
            continue
        last_text = ""
        for ci, part in enumerate(c):
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                last_text = part.get("text", "") or ""
            elif ptype == "image_url":
                tag = "L1"
                if "[level=L0]" in last_text:
                    tag = "L0"
                elif "[level=L2]" in last_text:
                    tag = "L2"
                elif "[level=L3]" in last_text:
                    tag = "L3"
                png: bytes | None = None
                url = (part.get("image_url") or {}).get("url", "")
                if isinstance(url, str) and url.startswith("data:image"):
                    try:
                        png = base64.b64decode(url.split(",", 1)[1])
                    except Exception:
                        png = None
                entries.append((mi, ci, tag, png))

    if not entries:
        return 0

    keep_idx: set[int] = set()
    # L0 = 永不丢弃（图标合集等"教学"图）
    for i, e in enumerate(entries):
        if e[2] == "L0":
            keep_idx.add(i)
    # 规则 A：每级最新 K 张
    for level in ("L1", "L2", "L3"):
        k = max(0, int(keep_per_level.get(level, 1)))
        if k <= 0:
            continue
        seen = 0
        for i in range(len(entries) - 1, -1, -1):
            if entries[i][2] == level:
                keep_idx.add(i)
                seen += 1
                if seen >= k:
                    break
    # 规则 B：全局最近 keep_n 张（keep_n<=0 跳过）
    if keep_n > 0:
        for i in range(max(0, len(entries) - keep_n), len(entries)):
            keep_idx.add(i)

    dropped = 0
    for i, (mi, ci, tag, png) in enumerate(entries):
        if i in keep_idx:
            continue
        name = name_lookup(png) if (name_lookup and png is not None) else None
        if name and run_dir is not None:
            placeholder = (
                f"[旧截图已省略以控制请求大小; level={tag}; file={name}; "
                f"path={run_dir}\\{name}]"
            )
        elif name:
            placeholder = f"[旧截图已省略; level={tag}; file={name}]"
        else:
            placeholder = f"[旧截图已省略; level={tag}]"
        messages[mi]["content"][ci] = {"type": "text", "text": placeholder}
        dropped += 1
    return dropped


def _prune_old_images(messages: list[dict[str, Any]], keep_n: int) -> int:
    """向后兼容旧签名（无文件名查询）。"""
    return _prune_old_images_dispatch(messages, keep_n)


def _resolve_proxy_key(api_key_in_cfg: str) -> str:
    return (
        api_key_in_cfg
        or os.environ.get("LITELLM_MASTER_KEY", "")
        or os.environ.get("OPENAI_API_KEY", "")
    )


def _build_llm_client(cfg: Config, api_key_override: str | None) -> LLMClient:
    """按 cfg.llm.provider 选三种后端之一。

    "proxy"     -> OpenAI 兼容代理 (LiteLLM / OpenClaw / 任意 OpenAI 兼容端点)
    "anthropic" -> Anthropic 原生 Messages API
    "copilot"   -> GitHub Copilot OAuth

    显式设置但凭据缺失时，会按 anthropic > copilot > proxy 的优先级回退
    （避免“点了 copilot 却悄悄走 proxy”这种隐形错配）。
    """
    raw_provider = (cfg.llm.provider or "anthropic").strip().lower()

    # 候选探测（按用户指定的优先级）
    a = cfg.llm.anthropic
    anthropic_key = api_key_override or a.api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    has_anthropic = bool(anthropic_key)

    has_copilot = False
    copilot_tm = None
    try:
        from .auth.copilot import CopilotTokenManager  # 延迟导入避免循环
        copilot_tm = CopilotTokenManager(cfg.llm.copilot.state_file or None)
        has_copilot = bool(copilot_tm.status().get("logged_in"))
    except Exception:
        copilot_tm = None
        has_copilot = False

    proxy = cfg.llm.proxy
    proxy_key = api_key_override or _resolve_proxy_key(proxy.api_key)
    has_proxy = bool(proxy_key)

    # 把"auto" / 未知值，或显式 provider 但凭据缺失的情况，按优先级回退
    def _auto_pick() -> str:
        if has_anthropic:
            return "anthropic"
        if has_copilot:
            return "copilot"
        if has_proxy:
            return "proxy"
        return "anthropic"  # 没有任何凭据时，让下面 anthropic 分支报详细错

    if raw_provider not in ("anthropic", "copilot", "proxy"):
        provider = _auto_pick()
    elif raw_provider == "anthropic" and not has_anthropic and (has_copilot or has_proxy):
        provider = _auto_pick()
    elif raw_provider == "copilot" and not has_copilot and (has_anthropic or has_proxy):
        provider = _auto_pick()
    elif raw_provider == "proxy" and not has_proxy and (has_anthropic or has_copilot):
        provider = _auto_pick()
    else:
        provider = raw_provider

    if provider == "anthropic":
        if not anthropic_key:
            raise RuntimeError(
                "缺少 Anthropic API Key：请设置 ANTHROPIC_API_KEY 环境变量，"
                "或在设置页 Anthropic.api_key 填入。"
            )
        return AnthropicClient(
            api_key=anthropic_key,
            model=a.model,
            base_url=a.base_url,
            anthropic_version=a.anthropic_version,
        )
    if provider == "copilot":
        from .auth.copilot import CopilotAuthError, CopilotTokenManager
        c = cfg.llm.copilot
        tm = copilot_tm or CopilotTokenManager(c.state_file or None)
        if not tm.status().get("logged_in"):
            raise RuntimeError(
                "未登录 GitHub Copilot。请到设置页点 “登录 GitHub Copilot” 完成设备码授权。"
            )
        # 预热一次：提前发现 token 到期 / 账号被吃
        try:
            tm.get_active()
        except CopilotAuthError as e:
            raise RuntimeError(str(e))
        return CopilotClient(token_manager=tm, model=c.model)
    # proxy
    if not proxy_key:
        raise RuntimeError(
            "缺少代理 API Key：请设置 LITELLM_MASTER_KEY 环境变量，"
            "或在 config.toml 的 [llm.proxy].api_key 填入。"
        )
    return OpenAIChatClient(
        base_url=proxy.base_url,
        api_key=proxy_key,
        model=proxy.model,
        extra_headers=dict(proxy.extra_headers) if proxy.extra_headers else None,
    )


class CancelledError(RuntimeError):
    """外部取消驱动时抛出（例如 sidecar.cancel）。"""


EventSink = Callable[[dict[str, Any]], None]


class Agent:
    def __init__(
        self,
        cfg: Config,
        api_key: str | None = None,
        *,
        event_sink: EventSink | None = None,
        cancel_event: threading.Event | None = None,
        thread_log: ThreadLog | None = None,
    ) -> None:
        self.cfg = cfg
        self.client = _build_llm_client(cfg, api_key)
        self.model = self.client.model
        self.sensor = ScreenSensor(cfg.screenshot)
        self.driver = InputDriver(cfg.input)
        self.tool = ComputerTool(self.sensor, self.driver, cfg.screenshot)
        self.safety = SafetyLayer(cfg.safety)
        self.event_sink = event_sink
        self.cancel_event = cancel_event
        self.thread_log = thread_log
        self.context_mgr = ContextManager(cfg.context)
        # md5(png) -> 代表该截图的文件名，用于 context.log 里用文件名替换 base64 图。
        self._image_names: dict[str, str] = {}
        self._current_step: int = 0
        # 最近一次发给模型的 L1 / L2 / L3 截图原始 PNG 字节，
        # 供 remember_icon 工具按图片像素坐标裁剪图标用。
        self._last_png_by_level: dict[str, bytes] = {}

    # 事件上报：sidecar 模式下避免使用 rich 控制台（会污染 stdout JSONRPC 通道）
    def _emit(self, kind: str, **payload: Any) -> None:
        if self.event_sink is None:
            return
        try:
            evt = {"event": kind, **payload}
            self.event_sink(evt)
        except Exception:
            # event sink 出错不应该干扰主流程
            pass

    def _print(self, *args: Any, **kw: Any) -> None:
        """sidecar 模式下静默；CLI 模式下走 rich console。"""
        if self.event_sink is None:
            _console.print(*args, **kw)

    # ---- 图片名记账 + 调试用 context.log ----

    def _record_img(self, png: bytes, name: str | None) -> None:
        if name:
            self._image_names[hashlib.md5(png).hexdigest()] = name

    def _img_block(self, png: bytes, name: str | None = None) -> dict[str, Any]:
        """返回发送给 LLM 的 image_url 块，并顺带把 png→文件名 记入 mapping。"""
        self._record_img(png, name)
        return {"type": "image_url", "image_url": {"url": _data_url(png)}}

    def _capture_image_part(self, cap: Capture) -> tuple[dict[str, Any], bytes]:
        """Build an image_url block from a Capture, picking JPEG for L1/L2 and PNG for L3.
        Returns (block, sent_bytes) so the caller can record the md5 against the
        actual bytes that go on the wire (compress_old_images decodes them later)."""
        sc = self.cfg.screenshot
        prefer_jpeg = bool(sc.send_jpeg_for_l1_l2) and cap.level in (ScreenLevel.L1, ScreenLevel.L2)
        sent_bytes, mime = cap.encoded_for_send(prefer_jpeg, sc.send_jpeg_quality)
        return {"type": "image_url", "image_url": {"url": _data_url(sent_bytes, mime)}}, sent_bytes

    def _sanitize_for_log(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """复制一份 messages，把所有 image_url 的 base64 数据替换成 [image: <文件名>]。"""
        out: list[dict[str, Any]] = []
        for m in messages:
            c = m.get("content")
            if isinstance(c, list):
                new_c: list[Any] = []
                for part in c:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        url = (part.get("image_url") or {}).get("url", "")
                        name = "(unknown)"
                        if isinstance(url, str) and url.startswith("data:image"):
                            try:
                                b64 = url.split(",", 1)[1]
                                png = base64.b64decode(b64)
                                name = self._image_names.get(
                                    hashlib.md5(png).hexdigest(),
                                    f"sha1:{hashlib.sha1(png).hexdigest()[:10]}.png",
                                )
                            except Exception:
                                name = "(unparsable data url)"
                        new_c.append({"type": "image_ref", "file": name})
                    else:
                        new_c.append(part)
                out.append({**m, "content": new_c})
            else:
                out.append(m)
        return out

    def _dump_context(self, messages: list[dict[str, Any]], tools: Any, step: int) -> None:
        """把每次发给 LLM 的完整 context（图片用文件名替换）追加到 thread 目录下的 context.log。"""
        if self.thread_log is None or self.thread_log.run_dir is None:
            return
        try:
            path = self.thread_log.run_dir / "context.log"
            sanitized = self._sanitize_for_log(messages)
            tool_names = []
            try:
                for t in (tools or []):
                    if isinstance(t, dict):
                        fn = t.get("function") or {}
                        tool_names.append(fn.get("name") or t.get("name") or "?")
            except Exception:
                tool_names = ["<unavailable>"]
            header = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "step": step,
                "model": self.model,
                "provider": (self.cfg.llm.provider or "proxy").lower(),
                "tools": tool_names,
                "messages_count": len(sanitized),
            }
            with path.open("a", encoding="utf-8") as f:
                f.write(f"\n===== step {step} @ {header['ts']} =====\n")
                f.write(json.dumps(header, ensure_ascii=False) + "\n")
                f.write(json.dumps(sanitized, ensure_ascii=False, indent=2) + "\n")
        except Exception:
            pass

    def _rule(self, *args: Any, **kw: Any) -> None:
        if self.event_sink is None:
            _console.rule(*args, **kw)

    # ---- F3: 同 thread 多轮 run 的 messages 持久化 ----
    def _messages_path(self, log: ThreadLog) -> Any:
        if log is None or log.run_dir is None:
            return None
        return log.run_dir / "messages.json"

    def _load_messages_tail(self, log: ThreadLog) -> list[dict[str, Any]]:
        """读取上次保存的 tail（不含 prelude；新 instruction 会追加在它之后）。"""
        p = self._messages_path(log)
        if p is None or not p.is_file():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return []
        if not isinstance(data, dict):
            return []
        tail = data.get("tail")
        if not isinstance(tail, list):
            return []
        # 简单合法性检查：每个元素必须是 dict 且含 role
        return [m for m in tail if isinstance(m, dict) and "role" in m]

    def _save_messages_tail(self, log: ThreadLog) -> None:
        """把当前 messages 去掉 prelude 后落盘。"""
        if not getattr(self, "_current_messages", None):
            return
        p = self._messages_path(log)
        if p is None:
            return
        try:
            tail = list(self._current_messages[self._current_prelude_len:])
            payload = {
                "saved_ms": int(time.time() * 1000),
                "model": self.model,
                "prelude_len_when_saved": self._current_prelude_len,
                "tail": tail,
            }
            p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _check_cancel(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise CancelledError("cancelled by user")

    def _summarize_segment(self, text_segment: list[dict[str, Any]], max_tokens: int) -> str:
        """Callback used by ContextManager. Sends a cheap text-only chat to the
        active LLM client to condense the given message segment into a recap.
        Returns the summary text (empty string on failure)."""
        prompt_msgs = build_summary_prompt(text_segment)
        try:
            resp = self.client.chat(
                messages=prompt_msgs,
                tools=[],
                max_tokens=int(max_tokens) if max_tokens else 1500,
            )
            return (resp.text or "").strip()
        except Exception:
            # Caller (ContextManager) logs and skips on failure.
            raise

    def _plan_relevant_app_tips(self, instruction: str) -> str:
        """One-shot LLM call: pick app slugs whose tips are likely useful for
        this task, then inline their tips bodies. Returns the rendered block
        (empty string when feature disabled / no tips / planning failed).

        Cheaper than auto-loading on launch_app because the model sees the
        right tips BEFORE deciding which app to open — a task like "把下载
        文件夹里最新一张图通过微信发给爸爸" will preload BOTH explorer and
        wechat tips, so the model knows it can paste images straight into
        WeChat (no need to click the file icon → dialog → navigate).
        """
        cfg_tools = self.cfg.tools
        if not getattr(cfg_tools, "plan_app_tips", False) or not cfg_tools.enabled:
            return ""
        instruction = (instruction or "").strip()
        if not instruction:
            return ""
        apps = [it for it in tooltips_mod.list_app_tips(cfg_tools) if it.get("lines", 0) > 0]
        if not apps:
            return ""
        catalog = "\n".join(f"- {it['slug']}: {it['title']}" for it in apps)
        valid_slugs = {it["slug"] for it in apps}

        # Extra-fast planner: text-only, tiny output, no tools. Even Opus is
        # quick at this since we cap output at ~80 tokens.
        sys_msg = (
            "You are a planner. Given a user task and a catalog of apps with available tip-files, "
            "pick the slugs whose tips are likely useful for accomplishing the task. "
            "Output ONLY a JSON array of slug strings (no prose, no fences). "
            "Pick at most "
            f"{int(getattr(cfg_tools, 'plan_app_tips_max', 5) or 5)} slugs. "
            "If none are obviously relevant, output []."
        )
        user_msg = f"Task: {instruction}\n\nCatalog:\n{catalog}\n\nRelevant slugs (JSON array):"
        try:
            resp = self.client.chat(
                messages=[
                    {"role": "system", "content": sys_msg},
                    {"role": "user", "content": user_msg},
                ],
                tools=[],
                max_tokens=120,
                temperature=0.0,
            )
        except Exception:
            return ""
        raw = (resp.text or "").strip()
        # Tolerate fenced output anyway.
        if raw.startswith("```"):
            raw = raw.strip("`")
            nl = raw.find("\n")
            if nl >= 0:
                raw = raw[nl + 1 :]
            raw = raw.strip()
        try:
            picked = json.loads(raw)
        except Exception:
            # Fallback: scan catalog slugs as substrings.
            picked = [s for s in valid_slugs if s in raw]
        if not isinstance(picked, list):
            return ""
        # Preserve LLM ordering, dedupe, drop unknown.
        seen: set[str] = set()
        chosen: list[str] = []
        for s in picked:
            if not isinstance(s, str):
                continue
            slug = s.strip().lower()
            if slug in valid_slugs and slug not in seen:
                seen.add(slug)
                chosen.append(slug)
        cap = int(getattr(cfg_tools, "plan_app_tips_max", 5) or 5)
        chosen = chosen[:cap]
        if not chosen:
            return ""
        sections: list[str] = []
        for slug in chosen:
            body = tooltips_mod.app_tips_for_prompt(cfg_tools, slug)
            if body:
                sections.append(body.strip())
        if not sections:
            return ""
        return (
            "## Pre-loaded app tips (auto-selected for this task)\n"
            f"Selected based on the task instruction: {', '.join(chosen)}.\n"
            "Skim these BEFORE you start — they often contain a one-shot shortcut "
            "(keyboard / dialog trick / paste-to-input) that beats clicking around.\n\n"
            + "\n\n".join(sections)
            + "\n"
        )

    # ---- 点击前预检（pre-click target verification） ----

    _CLICK_ACTIONS_FOR_VERIFY = {
        "left_click", "right_click", "middle_click",
        "double_click", "triple_click", "left_click_drag",
    }

    def _maybe_pre_click_verify(self, action: str, args: dict[str, Any], log: ThreadLog) -> "ToolResult | None":
        """Two-phase click protocol.

        On the FIRST call (no ``confirmed=true`` in args), do NOT click; instead
        capture a high-detail L3 tile around the target screen coordinate and
        return it to the model. The model then inspects the tile and either
        (a) re-issues the same click with ``confirmed=true`` to actually
        perform it, or (b) chooses a different action. This makes the model
        the final arbiter on every click — no automatic similarity heuristic.

        Returns:
          - None  → not a click / no coordinate / verification disabled / model
                    explicitly passed ``confirmed=true`` → caller proceeds with
                    the normal click dispatch.
          - ToolResult(image_png=..., output=...) → preview tile; caller uses
                    this as the click's tool result. The click is NOT executed.
        """
        if not self.cfg.safety.verify_click_target_before:
            return None
        if action not in self._CLICK_ACTIONS_FOR_VERIFY:
            return None
        coord = args.get("coordinate")
        if not (isinstance(coord, (list, tuple)) and len(coord) == 2):
            return None
        # Model has explicitly confirmed after seeing the preview → let it click.
        if bool(args.get("confirmed", False)):
            # Strip the flag so it's not forwarded to the actual driver call.
            args.pop("confirmed", None)
            return None
        ref_cap = self.tool.last_capture
        if ref_cap is None:
            return None
        try:
            cx_img, cy_img = int(coord[0]), int(coord[1])
        except (TypeError, ValueError):
            return None

        sx, sy = ref_cap.model_to_screen(cx_img, cy_img)
        radius_screen = max(8, int(self.cfg.safety.verify_click_target_radius_px))

        try:
            live = self.sensor.capture_around(sx, sy, radius_screen)
        except Exception as e:
            log.warning(f"pre-click preview: live capture failed: {e}; allowing click through")
            return None

        live_png = live.png_bytes()
        live_name = log.save_image(
            live_png, f"step-{self._current_step:03d}-preclick-preview", level="INFO"
        )
        if live_name:
            self._record_img(live_png, live_name)
        # Cache as the most-recent L3 so remember_icon can reuse it.
        self._last_png_by_level["L3"] = live_png

        log.info(
            f"pre-click preview: {action} @ img({cx_img},{cy_img})→screen({sx},{sy}) "
            f"radius={radius_screen}px; awaiting model confirmation"
        )
        self._emit(
            "preclick_preview",
            step=self._current_step,
            action=action,
            coord=[cx_img, cy_img],
            screen=[sx, sy],
            radius=radius_screen,
        )
        v_ox, v_oy = live.offset
        v_rw, v_rh = live.raw_size
        v_sw, v_sh = live.sent_size
        return ToolResult(
            image_png=live_png,
            output=(
                f"[pre-click preview, NOT clicked yet] {action} would land at "
                f"image-coord ({cx_img},{cy_img}) → screen ({sx},{sy}). "
                f"Attached: a {v_sw}x{v_sh} L3 tile (raw {v_rw}x{v_rh}) centred on the target, "
                f"screen-rect (left={v_ox}, top={v_oy}, right={v_ox + v_rw}, bottom={v_oy + v_rh}). "
                f"Inspect this tile carefully. If the target really is what you intended to click, "
                f"re-issue the SAME action with the SAME coordinate AND add `confirmed=true` to actually click. "
                f"If the target is wrong, choose a different action instead — do NOT confirm."
            ),
        )

    def run(self, instruction: str) -> str:
        log = self.thread_log or ThreadLog.create(self.cfg.logging, instruction)
        log.info(
            f"proxy={self.cfg.llm.proxy.base_url} model={self.model} "
            f"max_steps={self.cfg.llm.max_steps} autonomy={self.cfg.safety.autonomy}"
        )
        run_dir = str(log.run_dir) if log.run_dir is not None else None
        thread_id = self.thread_log.id if self.thread_log is not None else None
        self._emit("run_start", instruction=instruction, run_dir=run_dir,
                   model=self.model, max_steps=self.cfg.llm.max_steps,
                   autonomy=self.cfg.safety.autonomy, thread_id=thread_id)
        # 用于 F3 续接：_run 里会在每次 prune / 步末更新 self._current_messages，
        # 这里在 finally 落盘到 thread 目录的 messages.json。
        self._current_messages = []
        self._current_prelude_len = 0
        try:
            return self._run(instruction, log)
        except CancelledError:
            log.warning("cancelled")
            log.close(status="cancelled")
            self._emit("final", status="cancelled", text="")
            return "(已取消)"
        except Exception as e:
            log.error(f"{type(e).__name__}: {e}")
            log.close(status="error")
            self._emit("error", message=f"{type(e).__name__}: {e}")
            raise
        finally:
            self._save_messages_tail(log)

    def _chat_with_retry(self, messages, tools, log) -> Any:
        """在 SDK 重试之外再加一层应用层重试，专门处理 connection/timeout/5xx。

        SDK 默认 max_retries=2，但 multimodal 请求走代理 / Copilot 上游抽风时仍然容易报
        ``APIConnectionError``。上层看到的体验就是“任务到一半突然 connection error”。这里再重
        试 2 轮，退避 1.5s/3s；401/403/4xx 不重试。任一轮成功即返回。
        """
        # 在发送前记一笔完整 context（图片用文件名代替）便于 debug
        self._dump_context(messages, tools, self._current_step)
        backoff = [1.5, 3.0]
        last_exc: Exception | None = None
        for attempt in range(len(backoff) + 1):
            try:
                return self.client.chat(
                    messages=messages,
                    tools=tools,
                    max_tokens=self.cfg.llm.max_tokens,
                    temperature=self.cfg.llm.temperature,
                    top_p=self.cfg.llm.top_p,
                )
            except APIConnectionError as e:
                last_exc = e
                kind = "connection"
            except APIStatusError as e:
                # 仅重试 5xx（8xx 不存在但以防万一）
                status = getattr(e, "status_code", 0) or 0
                if status < 500:
                    raise
                last_exc = e
                kind = f"http{status}"
            if attempt >= len(backoff):
                break
            wait = backoff[attempt]
            log.warning(f"chat retry due to {kind}: {last_exc!r}; sleeping {wait}s")
            self._emit("warning", message=f"连接抖动，{wait}s 后第 {attempt + 2} 次重试…")
            time.sleep(wait)
        assert last_exc is not None
        raise last_exc

    def _run(self, instruction: str, log: ThreadLog) -> str:
        # 起手 L1 截图：仅当配置要喂给 LLM 时才真正抓图 + 落盘 + 设为 last_capture。
        # 否则只问一下虚拟桌面尺寸用于 tool schema；step-000-init.png 不存在。
        feed_initial = bool(getattr(self.cfg.screenshot, "feed_initial_l1_to_llm", False))
        first: Capture | None = None
        init_name: str | None = None
        if feed_initial:
            first = self.sensor.capture(ScreenLevel.L1)
            self.tool.last_capture = first
            screen_w, screen_h = first.sent_size
        else:
            # 只查虚拟桌面尺寸，不抓像素，也不写文件。模型若需要看桌面会自己
            # 调 screenshot；坐标的反算交给后续的真实截图（active_app L2 等）。
            screen_w, screen_h = self.sensor.virtual_size()
        tool_schema = ComputerTool.openai_tool_schema(screen_w, screen_h)
        # 本次 run 要发给 LLM 的 tools 列表（computer + 按 cfg 启用的 meta tools）。
        # meta tool 的 schema / 派发逻辑都在 ctrlapp.meta_tools 模块里。
        tools_for_llm = [tool_schema] + meta_tools.build_meta_tool_schemas(self.cfg)
        log.info(f"initial screen {screen_w}x{screen_h} (feed_initial_l1={feed_initial})")
        if feed_initial and first is not None:
            init_name = log.save_image(first.png_bytes(), "step-000-init", level="INFO")
            self._record_img(first.png_bytes(), init_name)
            if init_name:
                self._emit("step_image", step=0, level=first.level.value,
                           width=screen_w, height=screen_h,
                           path=str(log.run_dir / init_name) if log.run_dir else None,
                           file=init_name,
                           thread_id=log.id if log else None,
                           phase="init")

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": (
                _build_system_prompt(self.cfg)
                + memory_mod.memory_for_prompt(self.cfg.memory)
                + tooltips_mod.tools_for_prompt(self.cfg.tools)
                + (launchers_mod.catalog_for_prompt(self.cfg.launchers)
                   if getattr(self.cfg, "launchers", None) and self.cfg.launchers.enabled
                      and self.cfg.launchers.inject_catalog_in_system_prompt else "")
                + (regions_mod.regions_for_prompt(self.cfg.regions)
                   if getattr(self.cfg, "regions", None) and self.cfg.regions.enabled else "")
                + (
                    "\n\n## Icon memory library status\nCurrently empty. If you click a tray/taskbar icon and confirm "
                    "success (the corresponding App opened), use `remember_icon` to register it, so future tasks can "
                    "identify it by reference instead of probing.\n"
                    if self.cfg.icons.enabled and not icon_mod.list_icons(self.cfg.icons)
                    else ""
                )
            )},
        ]
        # 图标合集图（如果有的话）以一对 user/assistant 伪对话的形式注入到 system 之后、
        # 真正的 instruction 之前。这样跨各家模型都能稳定接住图。
        atlas = icon_mod.build_atlas(self.cfg.icons) if self.cfg.icons.enabled else None
        if atlas is not None:
            atlas_name = log.save_image(atlas.png_bytes, "icon-atlas", level="INFO")
            self._record_img(atlas.png_bytes, atlas_name or "icon-atlas")
            existing_labels = ", ".join(
                f"#{i + 1} {it.get('label', '?')}"
                for i, it in enumerate(icon_mod.list_icons(self.cfg.icons))
            )
            messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "[level=L0] [Icon memory library] Below is the atlas of icons the user has taught me (laid out by number). "
                            "From now on, when you see a small icon on screen, **cross-reference this atlas** to identify which App it is.\n"
                            f"Already registered (**do NOT register again**): {existing_labels}\n"
                            f"Text index:\n{atlas.captions}"
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": _data_url(atlas.png_bytes)}},
                ],
            })
            messages.append({"role": "assistant", "content": "Got it, I've memorised these icons and will identify by atlas number when I see them; for any new icon not in the atlas, I'll register it via remember_icon."})
        # NOTE: the "atlas is empty" notice used to be injected here as a synthetic
        # user/assistant turn, but that bloated the visible message log AND broke the
        # "messages should be real conversation" invariant. It's now appended to the
        # system prompt directly (see _build_system_prompt → empty-atlas hint above).
        # Planner: pre-load tips for apps the LLM judges relevant to this
        # specific instruction. Cheap one-shot text-only call. Result is
        # injected as one extra user message right BEFORE the actual task.
        try:
            planned_tips = self._plan_relevant_app_tips(instruction)
        except Exception as e:
            log.warning(f"plan_app_tips failed: {type(e).__name__}: {e}")
            planned_tips = ""
        if planned_tips:
            messages.append({"role": "user", "content": planned_tips})
            messages.append({"role": "assistant", "content": "Got it, I'll consult these app tips before I start clicking."})
            log.info(f"plan_app_tips: injected {planned_tips.count('## App tips for')} app tip section(s)")
        # 真正的 instruction + (可选)起手 L1 截图（feed_initial 已在 _run 顶部解析）
        instruction_content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    f"任务：{instruction}\n\n"
                    + (
                        f"[level=L1] 以下是当前桌面截图：发送尺寸 {screen_w}x{screen_h}，"
                        f"原始 {first.raw_size[0]}x{first.raw_size[1]}；"
                        f"在屏幕坐标系中位于 (left={first.offset[0]}, top={first.offset[1]}, "
                        f"right={first.offset[0] + first.raw_size[0]}, bottom={first.offset[1] + first.raw_size[1]})。"
                        f" `computer` 工具的 coordinate 用 {screen_w}x{screen_h} 这个发送坐标系。"
                        if feed_initial else
                        f"（未附起手截图。系统已就位，coordinate 默认坐标系为 {screen_w}x{screen_h}；"
                        "若需要看桌面再决策，请显式调用 `screenshot` 或先 `launch_app` 让目标 App 置前。）"
                    )
                ),
            },
        ]
        if feed_initial:
            first_block, first_sent = self._capture_image_part(first)
            self._record_img(first_sent, init_name)
            instruction_content.append(first_block)
        messages.append({
            "role": "user",
            "content": instruction_content,
        })
        # 缓存"最近一张 L1"，供 remember_icon 工具裁剪用（仅当我们真的抓了 L1）
        if feed_initial and first is not None:
            self._last_png_by_level["L1"] = first.png_bytes()

        # ---- F3 持久化续接 ----
        # 如果 thread 目录里有 messages.json（同 thread 上一次任务结尾保存的快照），
        # 就把它当成上文：保留 prelude（system + atlas）不变，把上次的"任务历史段"
        # （即上次起手到结尾）插到当前 instruction **之前**，让模型看到完整对话。
        # 注意：上次保存的 messages 已经在保存前过了 prune（旧 image 都变成占位文字了），
        # 所以体积可控。
        prelude_len = len(messages) - 1  # 不含本次刚 append 的 instruction
        history_tail = self._load_messages_tail(log)
        if history_tail:
            # messages = [prelude..., <history_tail...>, <new instruction>]
            new_instruction = messages[-1]
            messages = messages[:prelude_len] + history_tail + [new_instruction]
            log.info(f"loaded {len(history_tail)} message(s) from previous run(s)")
        # 暴露给 run() finally 用于落盘
        self._current_messages = messages
        self._current_prelude_len = prelude_len

        nudges_left = 1  # 模型只输文本不调工具时的最多推一把次数
        # 行为兜底：保存/打开对话框激活态。看到 ctrl+s / ctrl+shift+s / F12 / ctrl+o 触发；
        # 命中后给若干步预算，超出或检测到 Enter/Escape 视为对话框已结束。
        save_dialog_armed_steps = 0
        # 哪些点击类动作需要架构层 L3 兜底（落点高清取证）
        click_actions = {
            "left_click", "right_click", "middle_click",
            "double_click", "triple_click", "left_click_drag",
        }
        save_dialog_triggers = {"ctrl+s", "ctrl+shift+s", "f12", "ctrl+o"}
        save_dialog_close_keys = {"return", "enter", "esc", "escape"}

        for step in range(self.cfg.llm.max_steps):
            self._check_cancel()
            self._current_step = step + 1
            self._rule(f"[cyan]Step {step + 1}/{self.cfg.llm.max_steps}")
            log.info(f"--- step {step + 1}/{self.cfg.llm.max_steps} ---")
            self._emit("step_start", step=step + 1, max_steps=self.cfg.llm.max_steps)
            recompressed, dropped = self.context_mgr.compress_old_images(
                messages,
                keep_per_level={
                    "L1": self.cfg.screenshot.keep_recent_l1,
                    "L2": self.cfg.screenshot.keep_recent_l2,
                    "L3": self.cfg.screenshot.keep_recent_l3,
                },
                keep_recent_global=self.cfg.llm.keep_recent_screenshots,
                min_per_l2_app=self.cfg.screenshot.min_per_l2_app,
                image_names=self._image_names,
                run_dir=(log.run_dir if log else None),
            )
            if recompressed or dropped:
                log.debug(
                    f"context manager: recompressed {recompressed} image(s), "
                    f"dropped {dropped} to placeholder"
                )
            # Adaptive history summarisation: if we're heading over the model's
            # context window, condense the oldest non-prelude messages with a
            # cheap LLM call.
            try:
                summarised = self.context_mgr.maybe_summarize(
                    messages,
                    prelude_len=self._current_prelude_len,
                    summarizer=self._summarize_segment,
                    log_fn=log.info,
                )
                if summarised:
                    self._current_messages = messages
                    self._emit(
                        "context_summarised",
                        step=step + 1,
                        message_count=len(messages),
                    )
            except Exception as e:
                log.warning(f"context summariser skipped: {type(e).__name__}: {e}")
            try:
                resp = self._chat_with_retry(messages, tools_for_llm, log)
            except APIError as e:
                self._print(f"[red]API 错误：{e}[/red]")
                log.error(f"API error: {e}")
                log.close(status="api_error")
                self._emit("error", message=f"API error: {e}")
                return f"(API error) {e}"
            except Exception as e:
                # Anthropic / Copilot SDK 有自己的错误类型，用 base Exception 兜住
                self._print(f"[red]LLM 调用错误：{type(e).__name__}: {e}[/red]")
                log.error(f"LLM error: {type(e).__name__}: {e}")
                log.close(status="api_error")
                self._emit("error", message=f"LLM error: {e}")
                return f"(LLM error) {e}"

            tool_calls = resp.tool_calls
            text_content = resp.text or ""

            if text_content.strip():
                self._print(f"[white]{text_content}[/white]")
                log.info(f"assistant text: {text_content.strip()}")
                self._emit("assistant_text", step=step + 1, text=text_content)

            # 把 assistant 消息原样回塞
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": text_content}
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": tc.arguments_json or "{}",
                        },
                    }
                    for tc in tool_calls
                ]
            messages.append(assistant_msg)

            if not tool_calls:
                stripped = text_content.strip()
                completion_markers = ("任务完成", "任务失败", "无法完成", "task complete", "task failed", "cannot complete")
                looks_done = any(m in stripped.lower() if m.isascii() else m in stripped for m in completion_markers)
                if looks_done or nudges_left <= 0:
                    final = stripped or "(no text output)"
                    log.step_record({"step": step + 1, "final_text": final})
                    log.close(status="ok", final_text=final)
                    self._emit("final", status="ok", text=final)
                    return final
                # Nudge once: append a user message asking the model to either call a tool or summarise.
                nudges_left -= 1
                log.warning("assistant returned text without tool_call; nudging to continue")
                messages.append({
                    "role": "user",
                    "content": (
                        "Please don't just narrate. Either call the `computer` tool for the next step, "
                        "or summarise and finish with a message starting with \"task complete:\" or \"task failed:\"."
                    ),
                })
                continue
            nudges_left = 1  # any action resets the nudge budget

            # 派发每个 tool_call
            had_screenshot = False
            step_tool_records: list[dict[str, Any]] = []
            step_preview_pngs: list[bytes] = []
            # R2/R3 follow-up images: tool image_png from non-screenshot actions.
            # Format: (label, png_bytes, attached_capture | None) — appended to
            # the post-step user message; if a Capture is attached, its coord
            # metadata is rendered alongside so the model uses the right frame.
            step_meta_pngs: list[tuple[str, bytes, Any]] = []
            for tc in tool_calls:
                fn_name = tc.name
                try:
                    args = json.loads(tc.arguments_json or "{}")
                except json.JSONDecodeError:
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                action = args.pop("action", "")
                self._print(f"[magenta]→ {fn_name}.{action}[/magenta] {args}")
                log.info(f"tool_call {fn_name}.{action} {args}")
                self._emit("tool_call", step=step + 1, name=fn_name, action=action, args=args)

                if fn_name != "computer":
                    tr = meta_tools.dispatch_meta_tool(
                        fn_name, args, self.cfg, self._last_png_by_level,
                        sensor=self.sensor,
                    )
                    if tr is None:
                        tr = ToolResult(error=f"unknown tool: {fn_name}")
                elif self.safety.should_confirm(action, args) and not self.safety.confirm(action, args):
                    tr = ToolResult(error="user declined this action")
                    log.warning(f"user declined {action} {args}")
                else:
                    tr = self._maybe_pre_click_verify(action, args, log)
                    if tr is not None and tr.image_png:
                        # Two-phase click: this iteration is a preview, not an
                        # actual click. Stash the tile so the post-step user
                        # message includes it for the model to inspect.
                        step_preview_pngs.append(tr.image_png)
                    if tr is None:
                        tr = self.tool.dispatch(action, args)

                if action == "screenshot":
                    had_screenshot = True

                parts: list[str] = []
                if tr.output:
                    parts.append(tr.output)
                if tr.error:
                    parts.append(f"ERROR: {tr.error}")
                    log.error(f"tool error: {tr.error}")
                content_str = " | ".join(parts) if parts else "(ok)"

                # WARNING 级图像：供 image_level=WARNING 时仅错误时落盘使用
                if tr.error and tr.image_png:
                    log.save_image(tr.image_png, f"step-{step + 1:03d}-error-{action}", level="WARNING")
                elif action == "screenshot" and tr.image_png:
                    log.save_image(tr.image_png, f"step-{step + 1:03d}-shot", level="DEBUG")

                step_tool_records.append({"action": action, "args": args, "result": content_str})
                self._emit("tool_result", step=step + 1, action=action,
                           ok=tr.error is None, output=tr.output or "", error=tr.error or "")

                # R2 / R3 follow-up image (e.g. launch_app L2, click-verify L2 on
                # suspected miss). For action='screenshot' the image already
                # rides via the post-step capture path below; skip duplicates.
                if tr.image_png and action != "screenshot" and not tr.error:
                    if fn_name == "launch_app":
                        label = f"launch_app({args.get('name','?')}) L2"
                        save_tag = f"step-{step + 1:03d}-launch_app-l2"
                    elif fn_name == "load_screenshot":
                        # The tool itself stamps `[level=L?]` into tr.output so
                        # the keep-recent policy classifies the re-loaded image
                        # at its original level. Just forward that as the label.
                        label = tr.output or f"load_screenshot({args.get('path','?')})"
                        save_tag = f"step-{step + 1:03d}-load_screenshot"
                    elif fn_name == "computer":
                        label = f"{action} post-click L3 around cursor (low pixel-change → likely miss; coordinate frame unchanged)"
                        save_tag = f"step-{step + 1:03d}-{action}-postverify-l3"
                    else:
                        label = f"{fn_name} follow-up image"
                        save_tag = f"step-{step + 1:03d}-{fn_name}-l2"
                    # If the tool also attached a Capture, pass it along so the
                    # coordinate-frame metadata text and self.tool.last_capture
                    # can be updated together. Exception: if the tool ALSO
                    # attached an active-app rect, the post-step path below
                    # will re-capture an L2 of that exact rect and show it as
                    # the main image — so skip the follow-up to avoid sending
                    # a near-duplicate image. Only the rect/capture state is
                    # installed.
                    if tr.attached_active_rect is not None:
                        if tr.attached_capture is not None:
                            self.tool.last_capture = tr.attached_capture
                        self.tool.set_active_app_rect(tr.attached_active_rect)
                        try:
                            saved_meta_name = log.save_image(tr.image_png, save_tag, level="INFO")
                            if saved_meta_name:
                                self._record_img(tr.image_png, saved_meta_name)
                        except Exception as e:
                            log.warning(f"failed to persist follow-up image {save_tag}: {e}")
                    else:
                        step_meta_pngs.append((label, tr.image_png, tr.attached_capture))
                        if tr.attached_capture is not None:
                            self.tool.last_capture = tr.attached_capture
                        try:
                            saved_meta_name = log.save_image(tr.image_png, save_tag, level="INFO")
                            if saved_meta_name:
                                self._record_img(tr.image_png, saved_meta_name)
                        except Exception as e:
                            log.warning(f"failed to persist follow-up image {save_tag}: {e}")

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": content_str,
                    }
                )

            # ---- 行为兜底（保存对话框 sidebar guard）状态机 ----
            #  1) 本步出现保存触发键 → armed
            #  2) armed 状态下出现 Return/Esc → 视为已离开对话框，清状态
            #  3) armed 状态下若有点击且坐标位于截图左侧 ~25% → 触发纠正提示
            saved_dialog_hint: str | None = None
            if self.cfg.safety.save_dialog_sidebar_guard:
                sw = self.tool.last_capture.sent_size[0] if self.tool.last_capture else 1
                click_threshold_x = int(sw * 0.25)
                for rec in step_tool_records:
                    act = rec["action"]
                    args = rec["args"]
                    if act == "key":
                        keytxt = (args.get("text") or "").lower().replace(" ", "")
                        if keytxt in save_dialog_triggers:
                            save_dialog_armed_steps = 6
                        elif keytxt in save_dialog_close_keys and save_dialog_armed_steps > 0:
                            save_dialog_armed_steps = 0
                    if save_dialog_armed_steps > 0 and act in click_actions:
                        coord = args.get("coordinate")
                        if isinstance(coord, (list, tuple)) and len(coord) == 2:
                            cx = int(coord[0])
                            if cx < click_threshold_x:
                                saved_dialog_hint = (
                                    "提示：你刚刚在保存/打开对话框的左侧导航区点击了。"
                                    "下次请优先在文件名输入框 type 完整绝对路径并回车，"
                                    "或先点击对话框顶部的地址栏（路径面包屑）再 type 路径，"
                                    "**不要**在左侧『快速访问/此电脑』树里逐层点击。"
                                )
                if save_dialog_armed_steps > 0:
                    save_dialog_armed_steps -= 1

            # 架构层兜底：若本步执行了点击动作，额外抓一张 L3 落点高清图，
            # 让模型在下一轮能看清"我刚刚到底点中了什么"。可由 [safety].verify_click_with_l3 关闭。
            click_happened = (
                self.cfg.safety.verify_click_with_l3
                and any(rec["action"] in click_actions for rec in step_tool_records)
            )

            # 每步动作后给模型一张视觉输入：
            #   - 如果模型本步调用了 screenshot（可能是 L2/L3）：把那张原样回送，
            #     这样三级金字塔才真正起作用。
            #   - 否则补一张 L1 全屏，作为粗粒度的"动作后变化"验证图。
            if had_screenshot and self.tool.last_capture is not None:
                post = self.tool.last_capture
                tag = f"模型请求的截图 ({post.level.value})"
            elif self.tool.active_app_rect is not None:
                # Frame-stickiness: while an active-app rect is pinned (set by
                # launch_app / focus_window), keep the coordinate frame stable.
                # First time after the rect was (re)installed we show an L2 of
                # the whole rect so the model has a complete "map". After that
                # we downgrade to L3 (cursor_local) per design — the model has
                # already memorised the rect's structure, so subsequent posts
                # only need a small high-detail tile around the cursor for
                # change verification. Coordinate frame stays L2 (the rect),
                # because last_capture isn't overwritten by the L3.
                if not self.tool.active_app_l2_shown:
                    left, top, right, bottom = self.tool.active_app_rect
                    w, h = max(1, right - left), max(1, bottom - top)
                    try:
                        post = self.sensor.capture_region(left, top, w, h)
                        self.tool.last_capture = post
                        self.tool.active_app_l2_shown = True
                        tag = "动作后截图 (L2 active app, first map)"
                    except Exception as e:
                        log.warning(f"active_app_rect L2 capture failed, falling back to L1: {e}")
                        post = self.sensor.capture(ScreenLevel.L1)
                        self.tool.last_capture = post
                        self.tool.set_active_app_rect(None)
                        tag = "动作后截图 (L1)"
                else:
                    # 默认 L3 cursor-local；但如果 [screenshot] post_step_use_l3 = false
                    # 则继续每步重拍 active_app_rect 的 L2（适用于点击常导致焦点远离鼠标的 App）。
                    use_l3 = bool(getattr(self.cfg.screenshot, "post_step_use_l3", True))
                    if not use_l3:
                        left, top, right, bottom = self.tool.active_app_rect
                        w, h = max(1, right - left), max(1, bottom - top)
                        try:
                            post = self.sensor.capture_region(left, top, w, h)
                            self.tool.last_capture = post
                            tag = "动作后截图 (L2 active app, post_step_use_l3=false)"
                        except Exception as e:
                            log.warning(f"active_app_rect L2 re-capture failed, falling back to L1: {e}")
                            post = self.sensor.capture(ScreenLevel.L1)
                            self.tool.last_capture = post
                            self.tool.set_active_app_rect(None)
                            tag = "动作后截图 (L1)"
                    else:
                        try:
                            # L3 is around the current cursor. Don't overwrite
                            # last_capture — the L2-of-rect remains the active
                            # coordinate frame for the next click.
                            post = self.sensor.capture(ScreenLevel.L3)
                            tag = "动作后截图 (L3 cursor-local; coordinate frame still = L2 active app)"
                        except Exception as e:
                            log.warning(f"L3 post capture failed, falling back to active-app L2: {e}")
                            left, top, right, bottom = self.tool.active_app_rect
                            w, h = max(1, right - left), max(1, bottom - top)
                            try:
                                post = self.sensor.capture_region(left, top, w, h)
                                self.tool.last_capture = post
                                tag = "动作后截图 (L2 active app, L3 fallback)"
                            except Exception as e2:
                                log.warning(f"active_app_rect L2 capture also failed: {e2}")
                                post = self.sensor.capture(ScreenLevel.L1)
                                self.tool.last_capture = post
                                self.tool.set_active_app_rect(None)
                                tag = "动作后截图 (L1)"
            else:
                post = self.sensor.capture(ScreenLevel.L1)
                self.tool.last_capture = post
                tag = "动作后截图 (L1)"
            level_tag = {
                ScreenLevel.L1: "L1",
                ScreenLevel.L2: "L2",
                ScreenLevel.L3: "L3",
            }[post.level]
            post_png = post.png_bytes()
            saved_name = log.save_image(post_png, f"step-{step + 1:03d}-post-{post.level.value}", level="INFO")
            self._record_img(post_png, saved_name)
            # 缓存最近一张该 level 的截图，供 remember_icon 工具按图片像素裁剪用
            self._last_png_by_level[level_tag] = post_png
            self._emit("step_image", step=step + 1, level=level_tag,
                       width=post.sent_size[0], height=post.sent_size[1],
                       path=str(log.run_dir / saved_name) if (log.run_dir and saved_name) else None,
                       file=saved_name,
                       thread_id=log.id if log else None,
                       phase="post")

            # 抓 L3 落点取证图（如果本步有点击且当前 post 不是 L3 本身）
            verify_capture = None
            verify_png: bytes | None = None
            if click_happened and post.level is not ScreenLevel.L3:
                try:
                    verify_capture = self.sensor.capture(ScreenLevel.L3)
                    verify_png = verify_capture.png_bytes()
                    verify_name = log.save_image(verify_png, f"step-{step + 1:03d}-verify-cursor_local", level="INFO")
                    self._record_img(verify_png, verify_name)
                    self._last_png_by_level["L3"] = verify_png
                except Exception as e:  # 截边缘可能 mss 越界
                    log.warning(f"L3 verify capture failed: {e}")
                    verify_capture = None
                    verify_png = None

            log.step_record(
                {
                    "step": step + 1,
                    "assistant_text": text_content,
                    "tools": step_tool_records,
                    "post_image": saved_name,
                    "verify_image": "step-{:03d}-verify-cursor_local".format(step + 1) if verify_png else None,
                    "save_dialog_hint": bool(saved_dialog_hint),
                }
            )

            # 组装回送消息：post 主图（L1/L2/L3）+ 可选 L3 取证图 + 可选纠正提示
            p_ox, p_oy = post.offset
            p_rw, p_rh = post.raw_size
            p_sw, p_sh = post.sent_size
            content: list[dict[str, Any]] = [
                {"type": "text", "text": (
                    f"[level={level_tag}] {tag}：发送尺寸 {p_sw}x{p_sh}（原始 {p_rw}x{p_rh}）；"
                    f"在屏幕坐标系中位于 (left={p_ox}, top={p_oy}, right={p_ox + p_rw}, bottom={p_oy + p_rh})；"
                    f"图片内 (px,py) → 屏幕坐标 ({p_ox}+px*{p_rw / p_sw:.3f}, {p_oy}+py*{p_rh / p_sh:.3f})。"
                    + (
                        "（活动 App 已锁定，后续 post 截图都会以此 App 的窗口区域为准；"
                        "若需要重新看整桌面，请显式调用 screenshot(level='fullscreen')，这会同时解除锁定。）"
                        if (level_tag == "L2" and self.tool.active_app_rect is not None)
                        else ""
                    )
                )},
            ]
            post_block, post_sent = self._capture_image_part(post)
            self._record_img(post_sent, saved_name)
            content.append(post_block)
            if verify_png and verify_capture is not None:
                v_ox, v_oy = verify_capture.offset
                v_rw, v_rh = verify_capture.raw_size
                v_sw, v_sh = verify_capture.sent_size
                content.append({
                    "type": "text",
                    "text": (
                        f"[level=L3] 落点取证（鼠标周边 {v_sw}x{v_sh}，原始 {v_rw}x{v_rh}）；"
                        f"在屏幕坐标系中位于 (left={v_ox}, top={v_oy}, right={v_ox + v_rw}, bottom={v_oy + v_rh})。"
                        "用于核对你刚才点击/拖拽到底命中了什么。"
                        "**下一步 coordinate 仍以上方主图为准**（这张 L3 仅供视觉验证）。"
                    ),
                })
                v_block, _v_sent = self._capture_image_part(verify_capture)
                content.append(v_block)
            if saved_dialog_hint:
                content.append({"type": "text", "text": saved_dialog_hint})
                log.warning("save_dialog sidebar guard triggered; hint injected")
            for label, meta_png, meta_cap in step_meta_pngs:
                if meta_cap is not None:
                    m_ox, m_oy = meta_cap.offset
                    m_rw, m_rh = meta_cap.raw_size
                    m_sw, m_sh = meta_cap.sent_size
                    coord_text = (
                        f"[follow-up image] {label}: 发送尺寸 {m_sw}x{m_sh}（原始 {m_rw}x{m_rh}）；"
                        f"在屏幕坐标系中位于 (left={m_ox}, top={m_oy}, right={m_ox + m_rw}, bottom={m_oy + m_rh})；"
                        f"图片内 (px,py) → 屏幕坐标 ({m_ox}+px*{m_rw / m_sw:.3f}, {m_oy}+py*{m_rh / m_sh:.3f})。"
                        f"**这张图是当前活动的坐标参考系**——下一步 `coordinate` 必须按这张图的像素来给（不是上方的 L1）。"
                    )
                else:
                    coord_text = f"[follow-up image] {label}"
                content.append({"type": "text", "text": coord_text})
                content.append({"type": "image_url", "image_url": {"url": _data_url(meta_png)}})
            for idx, prev_png in enumerate(step_preview_pngs, start=1):
                content.append({
                    "type": "text",
                    "text": (
                        f"[level=L3] Pre-click preview #{idx}: tile around the click target you just proposed. "
                        "The click was NOT executed. Inspect this image carefully — if it really shows what you intended to click, "
                        "re-issue the SAME click action with the SAME coordinate AND `confirmed=true`. Otherwise, change your plan."
                    ),
                })
                content.append({"type": "image_url", "image_url": {"url": _data_url(prev_png)}})

            messages.append({"role": "user", "content": content})

        log.warning("max_steps reached")
        log.close(status="max_steps")
        self._emit("final", status="max_steps", text="")
        return "(达到最大步数预算，任务可能未完成)"
