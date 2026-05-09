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
from .input_driver import InputDriver
from .llm_client import build_llm_client as _build_llm_client
from .runlog import ThreadLog
from .safety import SafetyLayer
from .screen import Capture, ScreenLevel, ScreenSensor
from .tools import ComputerTool, ToolResult
from . import memory as memory_mod
from . import tooltips as tooltips_mod
from . import launcher_icons as launcher_icons_mod
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
        self.driver = InputDriver(cfg.input)
        self.tool = ComputerTool(self.sensor, self.driver, cfg.screenshot)
        self.safety = SafetyLayer(cfg.safety)
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
        # `load_screenshot` / `read_file` / `run_shell` 按需读取。
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
        # meta tool 的 schema / 派发逻辑都在 otterscope.meta_tools 模块里。
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
                + ("\n\n" + self.extra_system if self.extra_system else "")
            )},
        ]
        # 已安装应用图标合集图（每天定时全量扫描得到）：拼成一张大图注入到 system 之
        # 后、真正的 instruction 之前的一对 user/assistant 伪对话里。这样跨各家模型
        # 都能稳定接住图。和 taskbar 通知共用同一张大图。
        try:
            atlas = launcher_icons_mod.build_atlas(self.cfg)
        except Exception as exc:
            log.warning(f"launcher atlas build failed: {type(exc).__name__}: {exc}")
            atlas = None
        if atlas is not None:
            atlas_name = log.save_image(atlas.png_bytes, "launcher-icons-atlas", level="INFO")
            self._record_img(atlas.png_bytes, atlas_name or "launcher-icons-atlas")
            messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "[level=L0] [Launcher icons] Below is a collage of icons for apps installed on this Windows machine "
                            "(auto-scanned daily). When you see a small icon on screen — especially in the taskbar / system tray / "
                            "Start menu — **cross-reference this collage** to identify which App it is, and use the exact "
                            "app name (the text after `[N]`) when you reason about it.\n"
                            f"Text index:\n{atlas.captions}"
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": _data_url(atlas.png_bytes)}},
                ],
            })
            messages.append({"role": "assistant", "content": "Got it, I'll cross-reference this launcher-icons collage when I encounter small icons on screen."})
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
        instruction_text = (
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
                "图片 → `load_screenshot(path=…, level=\"L2\")`，文本 → `read_file(path=…)`，"
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
                if looks_done:
                    final = stripped or "(no text output)"
                    log.step_record({"step": step + 1, "final_text": final})
                    log.close(status="ok", final_text=final)
                    self._emit("final", status="ok", text=final)
                    return final
                # 不调工具又没说完成：温和提醒一下，继续走下一步。最终兜底是 max_steps。
                log.warning("assistant returned text without tool_call; reminding to continue")
                messages.append({
                    "role": "user",
                    "content": (
                        "Please don't just narrate. Either call the `computer` tool for the next step, "
                        "or summarise and finish with a message starting with \"task complete:\" or \"task failed:\"."
                    ),
                })
                continue

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
                        # If the cursor has wandered outside the active app's
                        # rect (focus stolen by another app, modal popup, click
                        # missed, …) the L3 cursor-local tile would just show
                        # unrelated pixels — useless for the model. Detect that
                        # and fall back to a fresh L2 of the active app rect.
                        try:
                            from .window import cursor_pos as _cursor_pos
                            cx, cy = _cursor_pos()
                        except Exception:
                            cx, cy = (-1, -1)
                        left, top, right, bottom = self.tool.active_app_rect
                        cursor_in_app = (left <= cx < right) and (top <= cy < bottom)
                        if not cursor_in_app:
                            w, h = max(1, right - left), max(1, bottom - top)
                            try:
                                post = self.sensor.capture_region(left, top, w, h)
                                self.tool.last_capture = post
                                tag = ("动作后截图 (L2 active app, cursor outside app rect "
                                       f"→ L3 skipped; cursor=({cx},{cy}) rect=({left},{top})-({right},{bottom}))")
                            except Exception as e:
                                log.warning(f"active_app_rect L2 capture (cursor-outside) failed, falling back to L1: {e}")
                                post = self.sensor.capture(ScreenLevel.L1)
                                self.tool.last_capture = post
                                self.tool.set_active_app_rect(None)
                                tag = "动作后截图 (L1)"
                        else:
                            try:
                                # L3 around the click target (if this step contained a click
                                # whose `coordinate` we can read), else around the current
                                # cursor. The cursor-based fallback is only reliable when
                                # the action actually moved the cursor; for clicks we already
                                # know the intended target, so prefer it — that way, if focus
                                # got stolen mid-step or the cursor drifted, the L3 still
                                # frames the spot the model wanted to see.
                                click_target_screen: tuple[int, int] | None = None
                                for rec in reversed(step_tool_records):
                                    if rec.get("action") not in click_actions:
                                        continue
                                    coord = (rec.get("args") or {}).get("coordinate")
                                    if not (isinstance(coord, (list, tuple)) and len(coord) == 2):
                                        continue
                                    try:
                                        ix, iy = int(coord[0]), int(coord[1])
                                    except Exception:
                                        continue
                                    # Reverse-map image-pixel coordinate to screen coordinate
                                    # using the active coord-frame capture (set when the
                                    # active app rect was pinned).
                                    base = self.tool.last_capture
                                    if base is None:
                                        click_target_screen = (ix, iy)
                                    else:
                                        sx_r = base.raw_size[0] / max(1, base.sent_size[0])
                                        sy_r = base.raw_size[1] / max(1, base.sent_size[1])
                                        click_target_screen = (
                                            int(base.offset[0] + ix * sx_r),
                                            int(base.offset[1] + iy * sy_r),
                                        )
                                    break

                                if click_target_screen is not None:
                                    # Fixed radius works better than UIA-smart for click verify:
                                    # we want a predictable frame around the requested pixel,
                                    # not whatever UI element happens to span it.
                                    radius = int(getattr(self.sensor.cfg, "l3_radius_px", 200))
                                    post = self.sensor.capture_around(
                                        click_target_screen[0], click_target_screen[1], radius
                                    )
                                    tag = ("动作后截图 (L3 around click target "
                                           f"{click_target_screen}; coordinate frame still = L2 active app)")
                                else:
                                    # Non-click step (or coord missing): fall back to
                                    # cursor-local. Don't overwrite last_capture — the
                                    # L2-of-rect remains the active coordinate frame
                                    # for the next click.
                                    post = self.sensor.capture(ScreenLevel.L3)
                                    tag = "动作后截图 (L3 cursor-local; coordinate frame still = L2 active app)"
                            except Exception as e:
                                log.warning(f"L3 post capture failed, falling back to active-app L2: {e}")
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
