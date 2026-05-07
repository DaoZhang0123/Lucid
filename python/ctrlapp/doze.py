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
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .config import Config
from . import memory as memory_mod
from . import tooltips as tooltips_mod
from . import meta_tools as meta_tools_mod
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
# Per-output ledger: every time the reflector successfully calls learn_tip /
# remember we append one record to ``<user data>/logs/doze_outputs.jsonl``.
# This powers the "cumulative outputs" list on the doze page (with delete).
# ---------------------------------------------------------------------------

def _outputs_path(cfg: Config) -> Path:
    return _user_data_dir() / "logs" / "doze_outputs.jsonl"


def _resolve_target_file(cfg: Config, name: str, args: dict[str, Any]) -> Path | None:
    if name == "remember":
        return memory_mod.memory_path(cfg.memory)
    if name == "learn_tip":
        app = (args.get("app") or "").strip()
        if app:
            return tooltips_mod.app_tips_path(cfg.tools, app)
        return tooltips_mod.tools_path(cfg.tools)
    return None


def _read_last_entry_line(p: Path) -> str:
    """Return the last bullet line (``- [...] ...``) of ``p``, or '' on miss."""
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in reversed(text.splitlines()):
        if line.startswith("- ["):
            return line
    return ""


def _record_output(
    cfg: Config,
    *,
    name: str,
    args: dict[str, Any],
    thread_id: str,
) -> None:
    target = _resolve_target_file(cfg, name, args)
    if target is None:
        return
    entry_line = _read_last_entry_line(target)
    if not entry_line:
        return
    record = {
        "id": uuid.uuid4().hex,
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ts_ms": int(time.time() * 1000),
        "name": name,
        "kind": "memory" if name == "remember" else "tip",
        "app": (args.get("app") or "") if name == "learn_tip" else "",
        "tip_kind": (args.get("kind") or "") if name == "learn_tip" else "",
        "text": (args.get("text") or "").strip(),
        "file": str(target),
        "entry": entry_line,
        "thread_id": thread_id or "",
    }
    p = _outputs_path(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _load_outputs(cfg: Config) -> list[dict[str, Any]]:
    p = _outputs_path(cfg)
    if not p.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("id"):
                    out.append(rec)
    except OSError:
        return []
    return out


def _rewrite_outputs(cfg: Config, items: list[dict[str, Any]]) -> None:
    p = _outputs_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for rec in items:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tmp.replace(p)


def _delete_entry_from_file(target: Path, entry_line: str) -> bool:
    """Remove the first line in ``target`` that exactly equals ``entry_line``.
    Returns True if a line was removed and the file rewritten."""
    if not entry_line or not target.is_file():
        return False
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return False
    lines = text.splitlines(keepends=True)
    target_stripped = entry_line.rstrip("\r\n")
    found = False
    new_lines: list[str] = []
    for ln in lines:
        if not found and ln.rstrip("\r\n") == target_stripped:
            found = True
            continue
        new_lines.append(ln)
    if not found:
        return False
    try:
        target.write_text("".join(new_lines), encoding="utf-8")
    except OSError:
        return False
    return True



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

Strict rules:
  1. Be conservative. If unsure, write nothing. Bad tips are worse than missing tips.
  2. Deduplicate against the "Existing tips" / "Existing memory" digests below.
     If something close already exists, skip it.
  3. Tips must be concrete and actionable: "<App>: <action> via <method>" or
     "<App>: avoid <pitfall> because <reason>". No vague advice.
  4. Memory entries must be user-stable facts (preferences, naming, paths). Do NOT
     record one-shot task facts (timestamps, search queries, recipient names).
  5. At most 3 learn_tip calls and 1 remember call per pass.
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

    return (
        f"## Thread\n"
        f"id: {thread.get('id')}\n"
        f"title: {thread.get('title')}\n"
        f"\n## Existing global tips digest (do not duplicate)\n"
        f"{_digest(tips_text, cfg.doze.max_tips_digest_lines)}\n"
        f"\n## Existing memory digest (do not duplicate)\n"
        f"{_digest(mem_text, cfg.doze.max_memory_digest_lines)}\n"
        f"\n## Event timeline\n"
        f"{timeline}\n"
        f"\n## Your turn\n"
        f"Decide: zero or more learn_tip / remember calls (within"
        f" the limits in the system prompt). End with one short summary sentence.\n"
    )


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


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

    _MAX_TOOL_NAMES = ("learn_tip", "remember", "load_app_tips")

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
        items = list(proc.get("items") or [])
        totals: dict[str, int] = {}
        for it in items:
            for k, v in (it.get("outcomes") or {}).items():
                if not isinstance(v, (int, float)):
                    continue
                # Skip the meta "rounds" counter — totals should reflect the
                # number of saves, not the number of LLM round-trips.
                if k == "rounds":
                    continue
                totals[k] = totals.get(k, 0) + int(v)
        return {
            "enabled": bool(self.cfg.doze.enabled),
            "running": self._running,
            "last_pass_ms": self._last_pass_ms,
            "last_thread_id": self._last_thread_id,
            "last_outcome": dict(self._last_outcome),
            "processed_count": len(items),
            "totals": totals,
            "log_path": str(_log_path(self.cfg)),
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

    def list_outputs(self, limit: int = 200) -> dict[str, Any]:
        items = _load_outputs(self.cfg)
        items.sort(key=lambda r: int(r.get("ts_ms") or 0), reverse=True)
        if limit > 0:
            items = items[:limit]
        return {"items": items, "path": str(_outputs_path(self.cfg))}

    def delete_output(self, output_id: str) -> dict[str, Any]:
        if not output_id:
            return {"ok": False, "error": "id required"}
        items = _load_outputs(self.cfg)
        target = next((r for r in items if r.get("id") == output_id), None)
        if target is None:
            return {"ok": False, "error": "not found"}
        file_path = Path(target.get("file") or "")
        entry = target.get("entry") or ""
        removed = _delete_entry_from_file(file_path, entry)
        # Always drop the ledger record so the UI doesn't keep showing a ghost.
        new_items = [r for r in items if r.get("id") != output_id]
        _rewrite_outputs(self.cfg, new_items)
        _append_log(
            self.cfg,
            f"[{_now_iso()}]   DELETED {target.get('kind')} id={output_id} file={file_path} "
            f"removed_from_file={removed}",
        )
        return {"ok": True, "removed_from_file": removed}

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
            # 不要把“一轮都没跑成”的 thread 记为已处理——那种通常是
            # 连接错误 / cancel，thread 本身还没被 reflector 看过，
            # 应该下轮重试。只要有任何一轮 LLM 返回了结果（哪怕是
            # “nothing worth saving”）才算走过。
            if not self._cancel.is_set() and outcome.get("rounds", 0) > 0:
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
                   "rounds": 0}
        try:
            client = self._llm_factory()
        except Exception as exc:
            self._last_error = f"llm_factory: {type(exc).__name__}: {exc}"
            return outcome

        all_schemas = meta_tools_mod.build_meta_tool_schemas(self.cfg)
        tools = [s for s in all_schemas
                 if (s.get("function") or {}).get("name") in self._MAX_TOOL_NAMES]

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
                    if c.name == "load_app_tips":
                        # load_app_tips falls through to dispatch_meta_tool below.
                        pass
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
                # 通用一行（截断到 200 char），便于扫一眼。成功保存的 tip / memory
                # 单独再写一条完整记录，方便日后直接拿来人工核对甚至 grep。
                _append_log(self.cfg, f"[{_now_iso()}]   call {c.name}({c.arguments_json[:200]}) -> {(error_text or out_text)[:200]}")
                if not error_text and c.name in ("learn_tip", "remember"):
                    try:
                        full_args = json.loads(c.arguments_json or "{}")
                    except json.JSONDecodeError:
                        full_args = {}
                    if c.name == "learn_tip":
                        app = full_args.get("app") or "*global*"
                        kind = full_args.get("kind") or "tip"
                        tip_text = (full_args.get("text") or "").replace("\n", " ").strip()
                        _append_log(
                            self.cfg,
                            f"[{_now_iso()}]   SAVED tip thread={thread_id} app={app} kind={kind}: {tip_text}",
                        )
                    else:  # remember
                        mem_text = (full_args.get("text") or "").replace("\n", " ").strip()
                        _append_log(
                            self.cfg,
                            f"[{_now_iso()}]   SAVED memory thread={thread_id}: {mem_text}",
                        )
                    _record_output(self.cfg, name=c.name, args=full_args, thread_id=thread_id)
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
