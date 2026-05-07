"""Doze (idle-time) reflection learning.

See ``Docs/doze.md`` for the design. v0 scope:

- Background tick thread; wakes every ``tick_interval_sec``.
- When sidecar idle > ``idle_threshold_sec`` AND no worker / queue / running
  doze pass, it scans the threads root for the most-recent thread not yet
  in ``doze_processed.json`` and asks the LLM (text-only) to extract reusable
  ``learn_tip`` / ``remember`` calls from its ``events.jsonl``.
- Tools available to the reflector are restricted to ``learn_tip`` / ``remember``
  / ``load_app_tips`` (no GUI). v0 deliberately skips the icon channel.
- Cooperative cancel: the worker checks ``cancel_event`` before each LLM call
  and after each tool dispatch; user activity (``bump_activity()``) sets the
  flag.
"""
from __future__ import annotations

import io
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import Config
from . import memory as memory_mod
from . import tooltips as tooltips_mod
from . import meta_tools as meta_tools_mod
from . import icon_proposals as icon_proposals_mod
from .runlog import ThreadLog, resolve_threads_root
from .llm_client import LLMClient


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _user_data_dir() -> Path:
    if os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            return Path(local_app) / "dev.ctrlapp"
    home = os.environ.get("HOME")
    if home:
        return Path(home) / ".ctrlapp"
    return Path.cwd()


def _processed_path(cfg: Config) -> Path:
    p = Path(cfg.doze.processed_path)
    return p if p.is_absolute() else _user_data_dir() / p


def _log_path(cfg: Config) -> Path:
    p = Path(cfg.doze.log_path)
    return p if p.is_absolute() else _user_data_dir() / p


def _load_processed(cfg: Config) -> dict[str, Any]:
    p = _processed_path(cfg)
    if not p.is_file():
        return {"items": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"items": []}


def _save_processed(cfg: Config, data: dict[str, Any]) -> None:
    p = _processed_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_log(cfg: Config, line: str) -> None:
    p = _log_path(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Prompt construction (text-only, no images)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are ctrlapp's "doze reflector": a low-priority background reviewer that runs while
the user is idle. Your job is to read ONE past task transcript and decide what
reusable knowledge (if any) should be promoted to long-term storage.

You CANNOT control the GUI. The only tools available are:
  - learn_tip(text, kind, app?)        — append to tools.md / apps/<slug>/tips.md
  - remember(text)                     — append to memory.md
  - load_app_tips(app)                 — read existing per-app tips before writing
  - propose_icon(image_filename, x, y, w, h, label, description?)
        — propose a small icon crop from one of the step images listed below.
          The proposal is QUEUED for the user to review (not auto-committed).
          Use ONLY when the assistant text in the timeline already states the
          icon's meaning AND gives concrete pixel coordinates inside that step
          image. Never guess.

Strict rules:
  1. Be conservative. If unsure, write nothing. Bad tips are worse than missing tips.
  2. Deduplicate against the "Existing tips" / "Existing memory" digests below.
     If something close already exists, skip it.
  3. Tips must be concrete and actionable: "<App>: <action> via <method>" or
     "<App>: avoid <pitfall> because <reason>". No vague advice.
  4. Memory entries must be user-stable facts (preferences, naming, paths). Do NOT
     record one-shot task facts (timestamps, search queries, recipient names).
  5. At most 3 learn_tip calls, 1 remember call, and 2 propose_icon calls per pass.
  6. After your tool_calls, reply ONE short sentence summarising what you wrote
     (or "nothing worth saving" if you skipped everything).
"""


def _summarise_event(evt: dict[str, Any], max_chars: int) -> str | None:
    """Compact one events.jsonl row into a single line. Returns None to skip."""
    et = evt.get("event") or evt.get("role") or ""
    if et in ("step_image", "step_start", "thread_changed", "queue_changed",
              "ready", "schedule_queued", "schedule_fired", "visual_notify_tick"):
        return None
    if et == "user_input":
        text = (evt.get("text") or evt.get("instruction") or "").strip()
        if not text:
            return None
        return f"USER: {text[:max_chars]}"
    if et == "assistant_text":
        text = (evt.get("text") or "").strip()
        if not text:
            return None
        return f"ASSISTANT: {text[:max_chars]}"
    if et == "tool_call":
        name = evt.get("name") or evt.get("tool") or "?"
        args = evt.get("arguments") or evt.get("args") or {}
        if isinstance(args, dict):
            arg_s = json.dumps({k: v for k, v in args.items() if k != "image_png"},
                               ensure_ascii=False)[:max_chars]
        else:
            arg_s = str(args)[:max_chars]
        return f"TOOL_CALL {name}({arg_s})"
    if et == "tool_result":
        out = (evt.get("output") or evt.get("text") or "").strip()
        err = (evt.get("error") or "").strip()
        body = err and f"ERR: {err}" or out
        if not body:
            return None
        if len(body) > max_chars:
            body = body[: max_chars // 2] + " … " + body[-max_chars // 2 :]
        return f"TOOL_RESULT: {body}"
    if et == "task_close":
        return f"TASK_CLOSE status={evt.get('status')} final={(evt.get('final_text') or '')[:200]}"
    if et == "final":
        return f"FINAL: {(evt.get('text') or '')[:max_chars]}"
    if et == "error":
        return f"ERROR: {(evt.get('message') or '')[:max_chars]}"
    return None


def _digest(text: str, max_lines: int) -> str:
    if not text:
        return "(empty)"
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} more lines elided)"


def build_user_prompt(cfg: Config, thread: dict[str, Any]) -> str:
    events = thread.get("events") or []
    summarised: list[str] = []
    for evt in events:
        line = _summarise_event(evt, cfg.doze.max_event_text_chars)
        if line:
            summarised.append(line)
    timeline = "\n".join(summarised) if summarised else "(no actionable events)"

    tips_text = tooltips_mod.read_tools(cfg.tools) if cfg.tools.enabled else ""
    mem_text = memory_mod.read_memory(cfg.memory) if cfg.memory.enabled else ""

    # Step images available for propose_icon. Pull from step_image events
    # (authoritative file names + dimensions); fall back to scanning run_dir.
    image_lines: list[str] = []
    seen_files: set[str] = set()
    for evt in events:
        if (evt.get("event") or "") != "step_image":
            continue
        f = (evt.get("file") or "").strip()
        if not f or f in seen_files:
            continue
        seen_files.add(f)
        w = evt.get("width") or "?"
        h = evt.get("height") or "?"
        lvl = evt.get("level") or "?"
        image_lines.append(f"  - {f} ({w}x{h}, level={lvl}, step={evt.get('step', '?')})")
    images_block = "\n".join(image_lines) if image_lines else "  (no step images)"

    return (
        f"## Thread\n"
        f"id: {thread.get('id')}\n"
        f"title: {thread.get('title')}\n"
        f"\n## Existing global tips digest (do not duplicate)\n"
        f"{_digest(tips_text, cfg.doze.max_tips_digest_lines)}\n"
        f"\n## Existing memory digest (do not duplicate)\n"
        f"{_digest(mem_text, cfg.doze.max_memory_digest_lines)}\n"
        f"\n## Step images available to propose_icon (use exact filename)\n"
        f"{images_block}\n"
        f"\n## Event timeline\n"
        f"{timeline}\n"
        f"\n## Your turn\n"
        f"Decide: zero or more learn_tip / remember / propose_icon calls (within"
        f" the limits in the system prompt). End with one short summary sentence.\n"
    )


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

_PROPOSE_ICON_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "propose_icon",
        "description": (
            "Propose a small icon crop from one of the step images of the thread "
            "you are reflecting on. The crop is QUEUED for the user to review in "
            "the /doze page (NOT auto-committed to the icon atlas). "
            "Use ONLY when the assistant text in the timeline already states what "
            "the icon means AND gives concrete pixel coordinates inside that image. "
            "Never guess coordinates."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "image_filename": {"type": "string", "description": "Exact filename from the 'Step images available' list (e.g. 'step-003-post-fullscreen.png')."},
                "x": {"type": "integer", "description": "Crop top-left x in image pixels."},
                "y": {"type": "integer", "description": "Crop top-left y in image pixels."},
                "w": {"type": "integer", "description": "Crop width (recommended 24-96)."},
                "h": {"type": "integer", "description": "Crop height (recommended 24-96)."},
                "label": {"type": "string", "description": "Short label, <=40 chars (e.g. 'WeChat')."},
                "description": {"type": "string", "description": "Optional brief note (<=200 chars)."},
            },
            "required": ["image_filename", "x", "y", "w", "h", "label"],
        },
    },
}


def _dispatch_propose_icon(
    cfg: Config,
    args: dict[str, Any],
    *,
    thread_id: str,
    thread_dir: str,
) -> tuple[str, str]:
    """Returns (output_text, error_text). error_text non-empty means failure."""
    if not cfg.icons.enabled:
        return ("", "icons disabled")
    fname = (args.get("image_filename") or "").strip()
    label = (args.get("label") or "").strip()
    desc = (args.get("description") or "").strip()
    if not fname or not label:
        return ("", "image_filename and label required")
    try:
        x = int(args.get("x", 0)); y = int(args.get("y", 0))
        w = int(args.get("w", 0)); h = int(args.get("h", 0))
    except (TypeError, ValueError):
        return ("", "x/y/w/h must be integers")
    if w <= 0 or h <= 0:
        return ("", "w/h must be > 0")
    if not thread_dir:
        return ("", "thread_dir missing on this pass")
    # Path traversal guard.
    if "/" in fname or "\\" in fname or fname.startswith(".."):
        return ("", f"invalid image_filename {fname!r}")
    p = Path(thread_dir) / fname
    if not p.is_file():
        return ("", f"image not found: {fname}")
    try:
        png_bytes = p.read_bytes()
    except OSError as e:
        return ("", f"read failed: {e}")
    # Crop using icon_memory's helper (handles bound clamping + PNG re-encode).
    from . import icon_memory as icon_mod
    try:
        from PIL import Image
        with Image.open(io.BytesIO(png_bytes)) as src:
            src = src.convert("RGBA")
            sw, sh = src.size
            x0 = max(0, min(x, sw - 1)); y0 = max(0, min(y, sh - 1))
            x1 = max(x0 + 1, min(x + w, sw)); y1 = max(y0 + 1, min(y + h, sh))
            crop = src.crop((x0, y0, x1, y1))
            buf = io.BytesIO()
            crop.save(buf, format="PNG", optimize=True)
            crop_png = buf.getvalue()
    except Exception as e:
        return ("", f"crop failed: {type(e).__name__}: {e}")
    entry = icon_proposals_mod.add_proposal(
        cfg,
        png_bytes=crop_png,
        label=label,
        description=desc,
        source_thread=thread_id,
        source_file=fname,
        x=x0, y=y0, w=x1 - x0, h=y1 - y0,
    )
    if entry is None:
        return ("", "failed to persist proposal")
    return (
        f"icon proposal #{entry['id']} '{entry['label']}' queued "
        f"({entry['w']}x{entry['h']} from {fname} @ {entry['x']},{entry['y']}); "
        f"awaiting user review in /doze.",
        "",
    )


@dataclass
class DozeStatus:
    enabled: bool = False
    running: bool = False
    last_pass_ms: int = 0
    last_thread_id: str = ""
    last_outcome: dict[str, int] = field(default_factory=dict)
    processed_count: int = 0
    last_activity_ms: int = 0
    last_error: str = ""


class DozeWorker:
    """Background idle-detection + reflection loop. Owned by the sidecar.

    Wiring (from sidecar):
        worker = DozeWorker(cfg, llm_factory=lambda: build_llm_client(cfg),
                            is_busy=lambda: self._worker_alive() or bool(self._queue),
                            event_sink=_writeln)
        worker.start()
        # on every user-driven RPC:
        worker.bump_activity()
        # on shutdown:
        worker.stop()
    """

    _MAX_TOOL_NAMES = ("learn_tip", "remember", "load_app_tips", "propose_icon")

    def __init__(
        self,
        cfg: Config,
        *,
        llm_factory: Callable[[], LLMClient],
        is_busy: Callable[[], bool],
        event_sink: Callable[[dict[str, Any]], None],
    ) -> None:
        self.cfg = cfg
        self._llm_factory = llm_factory
        self._is_busy = is_busy
        self._event_sink = event_sink
        self._tick = threading.Event()
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._running = False
        self._last_activity_ms = int(time.time() * 1000)
        self._last_pass_ms = 0
        self._last_thread_id = ""
        self._last_outcome: dict[str, int] = {}
        self._last_error = ""

    # ---- lifecycle ----

    def start(self) -> None:
        if self._thread is not None:
            return
        if not self.cfg.doze.enabled:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="doze-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._cancel.set()
        self._tick.set()
        t = self._thread
        if t is not None:
            t.join(timeout=5.0)
        self._thread = None

    # ---- public hooks ----

    def bump_activity(self) -> None:
        """Call from sidecar whenever a user-driven RPC arrives or a task closes."""
        self._last_activity_ms = int(time.time() * 1000)
        if self._running:
            self._cancel.set()

    def status(self) -> dict[str, Any]:
        proc = _load_processed(self.cfg)
        return {
            "enabled": bool(self.cfg.doze.enabled),
            "running": self._running,
            "last_pass_ms": self._last_pass_ms,
            "last_thread_id": self._last_thread_id,
            "last_outcome": dict(self._last_outcome),
            "processed_count": len(proc.get("items") or []),
            "last_activity_ms": self._last_activity_ms,
            "last_error": self._last_error,
        }

    def run_now(self) -> dict[str, Any]:
        """Force one pass on the next tick (ignores idle threshold but still
        respects cancel and busy-checks)."""
        self._last_activity_ms = 0  # so idle check trivially passes
        self._tick.set()
        return {"queued": True}

    def clear_processed(self) -> dict[str, Any]:
        _save_processed(self.cfg, {"items": []})
        return {"ok": True}

    # ---- main loop ----

    def _loop(self) -> None:
        # initial small delay so sidecar finishes booting first
        self._tick.wait(timeout=5.0)
        while not self._stop.is_set():
            try:
                self._maybe_run_pass()
            except Exception as exc:  # never crash the daemon
                self._last_error = f"{type(exc).__name__}: {exc}"
                _append_log(self.cfg, f"[{_now_iso()}] loop error: {self._last_error}")
            interval = max(5, int(self.cfg.doze.tick_interval_sec))
            self._tick.wait(timeout=interval)
            self._tick.clear()

    def _maybe_run_pass(self) -> None:
        if not self.cfg.doze.enabled:
            return
        if self._is_busy():
            return
        idle_ms = int(time.time() * 1000) - self._last_activity_ms
        if idle_ms < self.cfg.doze.idle_threshold_sec * 1000:
            return
        thread = self._pick_thread()
        if thread is None:
            return
        self._cancel.clear()
        self._running = True
        self._event_sink({
            "event": "doze_idle_start",
            "idle_sec": idle_ms // 1000,
            "thread_id": thread.get("id"),
        })
        try:
            outcome = self._reflect(thread)
            self._last_pass_ms = int(time.time() * 1000)
            self._last_thread_id = thread.get("id") or ""
            self._last_outcome = outcome
            self._last_error = ""
            if not self._cancel.is_set():
                self._mark_processed(thread.get("id") or "", outcome)
            self._event_sink({
                "event": "doze_pass_done",
                "thread_id": thread.get("id"),
                "outcomes": outcome,
                "interrupted": self._cancel.is_set(),
            })
            _append_log(self.cfg, f"[{_now_iso()}] pass thread={thread.get('id')} outcomes={outcome} interrupted={self._cancel.is_set()}")
        finally:
            self._running = False

    # ---- thread selection ----

    def _pick_thread(self) -> dict[str, Any] | None:
        try:
            all_threads = ThreadLog.list_threads(self.cfg.logging)
        except Exception:
            return None
        processed_ids = {it.get("thread_id") for it in _load_processed(self.cfg).get("items", [])
                         if it.get("version") == self.cfg.doze.prompt_version}
        for t in all_threads:
            tid = t.get("id")
            if not tid or tid in processed_ids:
                continue
            if int(t.get("task_count") or 0) <= 0:
                continue
            try:
                full = ThreadLog.read_thread(self.cfg.logging, tid)
            except Exception:
                continue
            if not full.get("events"):
                continue
            return full
        return None

    # ---- LLM round ----

    def _reflect(self, thread: dict[str, Any]) -> dict[str, int]:
        outcome = {"learn_tip": 0, "remember": 0, "load_app_tips": 0,
                   "propose_icon": 0, "rounds": 0}
        try:
            client = self._llm_factory()
        except Exception as exc:
            self._last_error = f"llm_factory: {type(exc).__name__}: {exc}"
            return outcome

        all_schemas = meta_tools_mod.build_meta_tool_schemas(self.cfg)
        tools = [s for s in all_schemas
                 if (s.get("function") or {}).get("name") in self._MAX_TOOL_NAMES]
        if self.cfg.icons.enabled:
            tools.append(_PROPOSE_ICON_SCHEMA)

        thread_id = thread.get("id") or ""
        thread_dir = thread.get("dir") or ""

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(self.cfg, thread)},
        ]
        total_calls = 0
        for round_idx in range(max(1, int(self.cfg.doze.max_rounds_per_thread))):
            if self._cancel.is_set():
                break
            try:
                resp = client.chat(
                    messages=messages,
                    tools=tools,
                    max_tokens=int(self.cfg.doze.max_tokens),
                    temperature=0.2,
                )
            except Exception as exc:
                self._last_error = f"chat: {type(exc).__name__}: {exc}"
                _append_log(self.cfg, f"[{_now_iso()}] chat error: {self._last_error}")
                break
            outcome["rounds"] += 1
            calls = list(resp.tool_calls or [])
            text = resp.text or ""
            _append_log(self.cfg, f"[{_now_iso()}] round={round_idx} text={text[:200]!r} calls={len(calls)}")
            if not calls:
                break

            # Append assistant message with tool_calls (OpenAI format).
            asst_calls = [{
                "id": c.id,
                "type": "function",
                "function": {"name": c.name, "arguments": c.arguments_json},
            } for c in calls]
            messages.append({"role": "assistant", "content": text, "tool_calls": asst_calls})

            for c in calls:
                if self._cancel.is_set():
                    break
                if total_calls >= self.cfg.doze.max_tool_calls_per_pass:
                    break
                total_calls += 1
                if c.name not in self._MAX_TOOL_NAMES:
                    out_text = f"[doze] tool {c.name!r} not allowed in reflection mode"
                    error_text = out_text
                else:
                    try:
                        args = json.loads(c.arguments_json or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    if c.name == "propose_icon":
                        out_text, error_text = _dispatch_propose_icon(
                            self.cfg, args, thread_id=thread_id, thread_dir=thread_dir,
                        )
                        if not error_text:
                            outcome["propose_icon"] += 1
                    else:
                        res = meta_tools_mod.dispatch_meta_tool(
                            c.name, args, self.cfg, last_png_by_level={}, sensor=None,
                        )
                        if res is None:
                            out_text = error_text = f"[doze] dispatch returned None for {c.name}"
                        else:
                            out_text = res.output or ""
                            error_text = res.error or ""
                            if c.name in outcome and not error_text:
                                outcome[c.name] += 1
                _append_log(self.cfg, f"[{_now_iso()}]   call {c.name}({c.arguments_json[:200]}) -> {(error_text or out_text)[:200]}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": c.id,
                    "content": error_text or out_text or "(no output)",
                })

            if total_calls >= self.cfg.doze.max_tool_calls_per_pass:
                break
        return outcome

    def _mark_processed(self, thread_id: str, outcome: dict[str, int]) -> None:
        if not thread_id:
            return
        data = _load_processed(self.cfg)
        items = list(data.get("items") or [])
        items = [it for it in items if it.get("thread_id") != thread_id]
        items.append({
            "thread_id": thread_id,
            "processed_ms": int(time.time() * 1000),
            "version": int(self.cfg.doze.prompt_version),
            "outcomes": outcome,
        })
        # Soft cap: keep last 1000 entries.
        if len(items) > 1000:
            items = items[-1000:]
        data["items"] = items
        _save_processed(self.cfg, data)


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
