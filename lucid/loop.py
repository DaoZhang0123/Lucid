"""ReAct loop —— 通过本地 LiteLLM 代理（OpenAI 兼容 /chat/completions）驱动 Claude。

工具：自定义的 OpenAI function tool `computer`，参数与 Anthropic computer_use_20250124
保持一致（action / coordinate / text / scroll_* / duration）。

图像通过 OpenAI 多模态 content 数组以 data URL 形式发送。
"""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from typing import Any, Callable

from openai import APIError, APIConnectionError, APIStatusError
from rich.console import Console

from .config import Config
from .cursor_indicator import CrabCursor
from .input_driver import InputDriver
from .llm_client import build_llm_client as _build_llm_client
from .runlog import ThreadLog
from .screen import Capture, ScreenLevel, ScreenSensor
from .tools import ComputerTool, ToolResult
from . import memory as memory_mod
from . import tooltips as tooltips_mod
from . import meta_tools
from . import launchers as launchers_mod
from . import regions as regions_mod
from .context_manager import ContextManager, build_summary_prompt
from .system_prompt import build_system_prompt as _build_system_prompt

_console = Console()


def _data_url(png: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(png).decode("ascii")


class CancelledError(RuntimeError):
    """外部取消驱动时抛出（例如 sidecar.cancel）。"""


EventSink = Callable[[dict[str, Any]], None]


# Hard ceiling on agent loop iterations. Not user-configurable; the model is
# expected to bail out itself with `task failed: ...` once it can't make
# progress. This is a final kill-switch for runaway loops.
HARD_STEP_CAP = 200


class Agent:
    def __init__(
        self,
        cfg: Config,
        api_key: str | None = None,
        *,
        event_sink: EventSink | None = None,
        cancel_event: threading.Event | None = None,
        thread_log: ThreadLog | None = None,
        extra_system: str = "",
        file_refs: list[dict[str, str]] | None = None,
    ) -> None:
        self.cfg = cfg
        self.client = _build_llm_client(cfg, api_key)
        self.model = self.client.model
        self.sensor = ScreenSensor(cfg.screenshot)
        # icon_atlas (L0) needs the full Config to call launcher_icons.build_atlas.
        self.sensor.set_root_config(cfg)
        self.driver = InputDriver(cfg.input)
        self.tool = ComputerTool(self.sensor, self.driver, cfg.screenshot)
        self.event_sink = event_sink
        self.cancel_event = cancel_event
        self.thread_log = thread_log
        # 调用方追加在 system prompt 末尾的额外约束，例如 visual_notify 自动
        # 回复任务的 AUTO-REPLY SAFETY POLICY。这样长策略不会污染 user 侧的
        # instruction、不会进 thread 标题，也不会被 prune 掉。
        self.extra_system = (extra_system or "").strip()
        # 多模态附件：前端随 start_task 传过来的本地文件 / 图片路径。
        # 仅在本轮起手 user message 里以 [Attached files] 文本块出现；
        # 不被 inline 为 image_url，避免 多图 拥塞上下文，交由模型调用
        # `load_local_images` / `read_file` / `run_shell` 按需读取。
        self.file_refs: list[dict[str, str]] = []
        if file_refs:
            for ref in file_refs:
                if not isinstance(ref, dict):
                    continue
                pt = (ref.get("path") or "").strip()
                if not pt:
                    continue
                self.file_refs.append({
                    "name": (ref.get("name") or "").strip() or pt,
                    "path": pt,
                    "kind": (ref.get("kind") or "").strip() or "file",
                })
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
            f"proxy={self.cfg.llm.proxy.base_url} model={self.model}"
        )
        run_dir = str(log.run_dir) if log.run_dir is not None else None
        thread_id = self.thread_log.id if self.thread_log is not None else None
        self._emit("run_start", instruction=instruction, run_dir=run_dir,
                   model=self.model, thread_id=thread_id)
        # 用于 F3 续接：_run 里会在每次 prune / 步末更新 self._current_messages，
        # 这里在 finally 落盘到 thread 目录的 messages.json。
        self._current_messages = []
        self._current_prelude_len = 0
        # 蟹钳鼠标：整个 run 期间把系统光标换成蟹钳，结束（含取消/异常）自动还原。
        # 受 [input].crab_cursor 控制，默认开启。
        crab_enabled = bool(getattr(self.cfg.input, "crab_cursor", True))
        try:
            with CrabCursor(enabled=crab_enabled):
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

        额外的 wall-clock watchdog：SDK 自己的 ``timeout=`` 在 httpx 连接挂住 /
        chunked 上游不写时不一定真触发（见 run 20260512-182234 O2 wedge：worker
        卡在 step 13 之后 11 分钟无任何事件）。这里用 daemon 线程包一层硬超时
        ``_CHAT_WALL_TIMEOUT_S``（默认 ``_CHAT_TIMEOUT_SEC * 2``），到点视为
        connection-style 错误进入下一轮 backoff，永远不让 chat 调用挂死主 worker。
        """
        # 在发送前记一笔完整 context（图片用文件名代替）便于 debug
        self._dump_context(messages, tools, self._current_step)
        backoff = [1.5, 3.0]
        last_exc: Exception | None = None
        wall_timeout = float(getattr(self.cfg.llm, "chat_wall_timeout_sec", 180.0))
        for attempt in range(len(backoff) + 1):
            result_box: dict[str, Any] = {}

            def _runner() -> None:
                try:
                    result_box["value"] = self.client.chat(
                        messages=messages,
                        tools=tools,
                        max_tokens=self.cfg.llm.max_tokens,
                        temperature=self.cfg.llm.temperature,
                        top_p=self.cfg.llm.top_p,
                    )
                except BaseException as exc:  # noqa: BLE001 — re-raised on main
                    result_box["error"] = exc

            t = threading.Thread(
                target=_runner, daemon=True, name="llm_chat_watchdog"
            )
            t.start()
            t.join(timeout=wall_timeout)
            if t.is_alive():
                # 线程留给 GC，不能 join 死等。视为连接超时进入退避重试。
                last_exc = TimeoutError(
                    f"llm chat wall-clock timeout after {wall_timeout:.0f}s"
                )
                kind = "wall-timeout"
            elif "error" in result_box:
                exc = result_box["error"]
                if isinstance(exc, APIConnectionError):
                    last_exc = exc
                    kind = "connection"
                elif isinstance(exc, APIStatusError):
                    status = getattr(exc, "status_code", 0) or 0
                    if status < 500 and status != 499:
                        raise exc
                    last_exc = exc
                    kind = f"http{status}"
                else:
                    raise exc
            else:
                return result_box["value"]
            if attempt >= len(backoff):
                break
            wait = backoff[attempt]
            log.warning(f"chat retry due to {kind}: {last_exc!r}; sleeping {wait}s")
            self._emit("warning", message=f"连接抖动，{wait}s 后第 {attempt + 2} 次重试…")
            time.sleep(wait)
        assert last_exc is not None
        raise last_exc

    def _run(self, instruction: str, log: ThreadLog) -> str:
        # Per Docs/screenshot.md v2 §3.1, we do NOT take a startup screenshot.
        # The model gets system prompt + user task only; if it needs to see
        # the desktop it must call action='screenshot' explicitly. We still
        # need the virtual-screen size to seed the `computer` tool schema's
        # default coordinate space (cheap; no pixel grab, no file written).
        screen_w, screen_h = self.sensor.virtual_size()
        tool_schema = ComputerTool.openai_tool_schema(screen_w, screen_h)
        # 本次 run 要发给 LLM 的 tools 列表（computer + 按 cfg 启用的 meta tools）。
        # meta tool 的 schema / 派发逻辑都在 lucid.meta_tools 模块里。
        tools_for_llm = [tool_schema] + meta_tools.build_meta_tool_schemas(self.cfg)
        log.info(f"initial screen {screen_w}x{screen_h} (no startup screenshot)")

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
                + ("\n\n" + self.extra_system if self.extra_system else "")
            )},
        ]
        # Per Docs/screenshot.md v2 §3.1 we no longer auto-inject the launcher
        # icons atlas as a synthetic user/assistant turn here. The model can
        # request it on demand via `screenshot(level="icon_atlas")`; the
        # system prompt teaches it when to do so.
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
        # 真正的 instruction（v2：起手不附截图，模型按需主动 screenshot）
        instruction_text = (
            f"任务：{instruction}\n\n"
            f"（未附起手截图。系统已就位，coordinate 默认坐标系为 {screen_w}x{screen_h}；"
            "若需要看桌面再决策，请显式调用 `screenshot` 或先 `launch_app` 让目标 App 置前。）"
        )
        # 挨在"任务…"之后、L1 说明之前，插入 [Attached files] 块
        if self.file_refs:
            attached_lines = [
                "",
                "[Attached files] 用户随本次任务附带了以下本地文件/图片（绝对路径，已落盘）。",
                "**默认情况下不要打开它们看内容**——绝大多数随附文件的角色只是\"载荷\"："
                "要发给某人、转发到某群、作为附件上传、打印、复制到某处、重命名、移动…等。"
                "这种情况下你只需要把路径**作为参数**传给目标 App / 工具即可，例如：",
                "  - 微信发送：复制路径到剪贴板（`run_shell` `Set-Clipboard -Path '<path>'`）→ 切到对应聊天 → Ctrl+V → Enter；"
                "或用聊天框上方的回形针按钮在文件对话框里直接 `type` 路径回车。",
                "  - 邮件附件：在 Outlook 写邮件时点\"附加文件\"→ 在对话框 `type` 路径回车。",
                "  - 上传到网页：网页里点上传按钮唤起系统对话框 → `type` 路径回车。",
                "  - 仅当任务**明确**需要 \"看 / 总结 / 提取内容 / OCR / 改写\" 时，才打开它："
                "图片 → `load_local_images(path=…, level=\"L2\")`，文本 → `read_file(path=…)`，"
                "PDF/Office 等二进制 → `run_shell` 提取或 `launch_app` 打开对应软件。",
                "  - 路径是**目录** → `run_shell` 调 `dir` / `Get-ChildItem` 列出后再决定怎么处理。",
                "判断方法：从用户原话里找动词。\"发/转/上传/附加/打印/重命名/移动…\" → 当载荷，**别读**；"
                "\"看看/总结/提取/读出/识别/翻译/改…里面的…\" → 当内容源，可以读。",
                "",
            ]
            for ref in self.file_refs:
                nm = (ref.get("name") or "").replace("\n", " ").replace("\r", " ").replace("`", "'")[:200]
                pt = (ref.get("path") or "").replace("\n", " ").replace("\r", " ").replace("`", "'")
                kind = ref.get("kind") or "file"
                tag = "image" if kind == "image" else ("folder" if kind == "folder" else "file")
                attached_lines.append(f"  - [{tag}] {nm}  →  {pt}")
            attached_lines.append("")
            # 插在 "\n\n" 分隔后、L1 说明之前：拆两段拼接。
            head, sep, tail = instruction_text.partition("\n\n")
            instruction_text = head + sep + "\n".join(attached_lines) + "\n" + tail
        instruction_content: list[dict[str, Any]] = [
            {"type": "text", "text": instruction_text},
        ]
        messages.append({
            "role": "user",
            "content": instruction_content,
        })

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

        # Hard safety cap: 200 steps. The system prompt tells the model to
        # bail out itself when it can't make progress (`task failed: ...`).
        hard_cap = HARD_STEP_CAP
        consecutive_narration = 0
        NARRATION_LIMIT = 3
        for step in range(hard_cap):
            self._check_cancel()
            self._current_step = step + 1
            self._rule(f"[cyan]Step {step + 1}")
            log.info(f"--- step {step + 1} ---")
            self._emit("step_start", step=step + 1, total_steps=hard_cap)
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
                if looks_done:
                    final = stripped or "(no text output)"
                    log.step_record({"step": step + 1, "final_text": final})
                    log.close(status="ok", final_text=final)
                    self._emit("final", status="ok", text=final)
                    return final
                # 不调工具又没说完成：温和提醒一下，继续走下一步。
                log.warning("assistant returned text without tool_call; reminding to continue")
                consecutive_narration += 1
                if consecutive_narration >= NARRATION_LIMIT:
                    final = (f"task failed: narration_loop ({consecutive_narration} consecutive assistant-only turns)")
                    log.error(final)
                    log.close(status="narration_loop", final_text=final)
                    self._emit("final", status="error", text=final)
                    return final
                messages.append({
                    "role": "user",
                    "content": (
                        "Please don't just narrate. Either call the `computer` tool for the next step, "
                        "or summarise and finish with a message starting with \"task complete:\" or \"task failed:\"."
                    ),
                })
                continue

            # 派发每个 tool_call
            consecutive_narration = 0
            had_screenshot = False
            # The Capture returned by this step's most recent screenshot tool
            # call (None if the step contained no screenshot). Used by the
            # post-step block to decide whether to render an L0 atlas message
            # or a normal coord-frame message.
            last_step_screenshot_cap: Capture | None = None
            step_tool_records: list[dict[str, Any]] = []
            step_preview_pngs: list[bytes] = []
            # R2/R3 follow-up images: tool image_png from non-screenshot actions.
            # Format: (label, png_bytes, attached_capture | None) — appended to
            # the post-step user message; if a Capture is attached, its coord
            # metadata is rendered alongside so the model uses the right frame.
            step_meta_pngs: list[tuple[str, bytes, Any]] = []
            # Anti-snowball guard for read_webpage. The model occasionally
            # fans out 5–8 read_webpage calls in a single assistant turn
            # (one URL per backend / news source) and burns the step waiting
            # on every one of them — most of which fail identically. Cap at
            # 2 per step; any extras short-circuit with an explanatory error
            # that mirrors system_prompt Rule 19.
            _READ_WEBPAGE_PER_STEP_CAP = 2
            read_webpage_count = 0
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

                # E (anti-snowball): cap read_webpage to 2 per step. The 3rd+
                # call is rejected without doing any network / browser work so
                # the model is forced to read the first two results before
                # fanning out further. See system_prompt Rule 19.
                if fn_name == "read_webpage":
                    read_webpage_count += 1
                    if read_webpage_count > _READ_WEBPAGE_PER_STEP_CAP:
                        tr = ToolResult(error=(
                            f"read_webpage rate-limited: this step already "
                            f"issued {_READ_WEBPAGE_PER_STEP_CAP} read_webpage "
                            f"calls (the per-step cap). Wait for those results, "
                            f"then decide whether to fetch more. Do NOT fan out "
                            f"5–8 URLs in one turn — see system_prompt rule 19."
                        ))
                        content_str = f"ERROR: {tr.error}"
                        log.error(f"tool error: {tr.error}")
                        step_tool_records.append({"action": action, "args": args, "result": content_str})
                        self._emit("tool_result", step=step + 1, action=action,
                                   ok=False, output="", error=tr.error or "")
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": content_str,
                        })
                        continue

                if fn_name != "computer":
                    tr = meta_tools.dispatch_meta_tool(
                        fn_name, args, self.cfg, self._last_png_by_level,
                        sensor=self.sensor,
                    )
                    if tr is None:
                        tr = ToolResult(error=f"unknown tool: {fn_name}")
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
                    if not tr.error and tr.attached_capture is not None:
                        last_step_screenshot_cap = tr.attached_capture

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
                    elif fn_name == "load_local_images":
                        # The tool itself stamps `[level=L?]` into tr.output so
                        # the keep-recent policy classifies the re-loaded image
                        # at its original level. Just forward that as the label.
                        label = tr.output or f"load_local_images({args.get('path','?')})"
                        save_tag = f"step-{step + 1:03d}-load_local_images"
                    elif fn_name == "computer":
                        label = f"{action} post-click L3 around cursor (low pixel-change → likely miss; coordinate frame unchanged)"
                        save_tag = f"step-{step + 1:03d}-{action}-postverify-l3"
                    else:
                        label = f"{fn_name} follow-up image"
                        save_tag = f"step-{step + 1:03d}-{fn_name}-l2"
                    # Forward the follow-up image as a post-step block. If the
                    # tool also attached a Capture (e.g. launch_app's L2), it
                    # becomes the new active coordinate frame for the next click.
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

            # ---- post-step image policy (Docs/screenshot.md v2 §3.3) ----
            # Three branches, no auto-L1/L2/L3 fallback:
            #   1) Model called screenshot(level="icon_atlas") this step → render
            #      L0 message (synthetic image, NOT a coord frame).
            #   2) Model called screenshot(level=fullscreen|active_window|
            #      cursor_local) this step → render with coord-frame text.
            #   3) Otherwise → no main post-image. Tool-result text already
            #      went through as a tool message; any launch_app L2 / click-
            #      verify miss tile is appended below via step_meta_pngs.
            content: list[dict[str, Any]] = []
            saved_name: str | None = None
            post_for_record: Capture | None = None

            if last_step_screenshot_cap is not None and last_step_screenshot_cap.level is ScreenLevel.L0:
                atlas_cap = last_step_screenshot_cap
                atlas_png = atlas_cap.png_bytes()
                saved_name = log.save_image(
                    atlas_png, f"step-{step + 1:03d}-icon_atlas", level="INFO"
                )
                self._record_img(atlas_png, saved_name)
                self._last_png_by_level["L0"] = atlas_png
                self._emit("step_image", step=step + 1, level="L0",
                           width=atlas_cap.sent_size[0], height=atlas_cap.sent_size[1],
                           path=str(log.run_dir / saved_name) if (log.run_dir and saved_name) else None,
                           file=saved_name,
                           thread_id=log.id if log else None,
                           phase="post")
                post_for_record = atlas_cap
                content.append({"type": "text", "text": (
                    f"[level=L0] icon_atlas (launcher icons collage): "
                    f"{atlas_cap.sent_size[0]}x{atlas_cap.sent_size[1]}. "
                    "**THIS IS NOT THE SCREEN** — coordinates in this image are meaningless for clicks. "
                    "Use it only to identify which app a small icon belongs to. "
                    "The next click coordinate must still refer to the most recent real screenshot."
                )})
                content.append({"type": "image_url", "image_url": {"url": _data_url(atlas_png)}})
            elif last_step_screenshot_cap is not None:
                post = last_step_screenshot_cap
                level_tag = post.level.name  # "L1" / "L2" / "L3"
                post_png = post.png_bytes()
                saved_name = log.save_image(
                    post_png, f"step-{step + 1:03d}-post-{post.level.value}", level="INFO"
                )
                self._record_img(post_png, saved_name)
                self._last_png_by_level[level_tag] = post_png
                self._emit("step_image", step=step + 1, level=level_tag,
                           width=post.sent_size[0], height=post.sent_size[1],
                           path=str(log.run_dir / saved_name) if (log.run_dir and saved_name) else None,
                           file=saved_name,
                           thread_id=log.id if log else None,
                           phase="post")
                post_for_record = post
                p_ox, p_oy = post.offset  # type: ignore[misc]
                p_rw, p_rh = post.raw_size
                p_sw, p_sh = post.sent_size
                content.append({"type": "text", "text": (
                    f"[level={level_tag}] 模型请求的截图 ({post.level.value})："
                    f"发送尺寸 {p_sw}x{p_sh}（原始 {p_rw}x{p_rh}）；"
                    f"在屏幕坐标系中位于 (left={p_ox}, top={p_oy}, right={p_ox + p_rw}, bottom={p_oy + p_rh})；"
                    f"图片内 (px,py) → 屏幕坐标 ({p_ox}+px*{p_rw / p_sw:.3f}, {p_oy}+py*{p_rh / p_sh:.3f})。"
                )})
                post_block, post_sent = self._capture_image_part(post)
                self._record_img(post_sent, saved_name)
                content.append(post_block)

            log.step_record(
                {
                    "step": step + 1,
                    "assistant_text": text_content,
                    "tools": step_tool_records,
                    "post_image": saved_name,
                    "save_dialog_hint": bool(saved_dialog_hint),
                }
            )

            if saved_dialog_hint:
                content.append({"type": "text", "text": saved_dialog_hint})
                log.warning("save_dialog sidebar guard triggered; hint injected")

            # Follow-up images from non-screenshot tools (launch_app L2,
            # click-verify miss tile, load_local_images, …). Each carries its
            # own coordinate frame via attached_capture (already installed onto
            # self.tool.last_capture during dispatch).
            for label, meta_png, meta_cap in step_meta_pngs:
                if meta_cap is not None and meta_cap.offset is not None:
                    m_ox, m_oy = meta_cap.offset
                    m_rw, m_rh = meta_cap.raw_size
                    m_sw, m_sh = meta_cap.sent_size
                    coord_text = (
                        f"[follow-up image] {label}: 发送尺寸 {m_sw}x{m_sh}（原始 {m_rw}x{m_rh}）；"
                        f"在屏幕坐标系中位于 (left={m_ox}, top={m_oy}, right={m_ox + m_rw}, bottom={m_oy + m_rh})；"
                        f"图片内 (px,py) → 屏幕坐标 ({m_ox}+px*{m_rw / m_sw:.3f}, {m_oy}+py*{m_rh / m_sh:.3f})。"
                        f"**这张图是当前活动的坐标参考系**——下一步 `coordinate` 必须按这张图的像素来给。"
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

            # Per Docs/screenshot.md v2 §3.3: only emit a user message when we
            # actually have something visual / corrective to add. The tool-
            # result text already went out via individual tool messages, so an
            # empty user turn would just waste tokens.
            if content:
                messages.append({"role": "user", "content": content})

        log.warning("step_cap reached")
        log.close(status="step_cap")
        self._emit("final", status="step_cap", text="")
        return "(达到最大步数预算，任务可能未完成)"
