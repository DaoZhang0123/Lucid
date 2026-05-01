"""ctrlapp · stdio JSON-RPC sidecar 模式

让 Tauri / Rust 主进程把 ctrlapp 拉起来后，通过 **行分隔 JSON（NDJSON）**
互相通信。设计要点：

- 协议帧：每行一个 JSON 对象，UTF-8。
- 通道职责：
    * **stdin**：由宿主进程发来的请求 / 通知。
    * **stdout**：本进程的 JSON-RPC 响应 / 事件（**只允许写 NDJSON**，
      绝不掺杂日志）。
    * **stderr**：rich 控制台输出和错误堆栈，宿主可选择转发。
- 请求格式：``{"id": <number>, "method": "...", "params": {...}}``
- 响应格式：``{"id": <number>, "result": ...}`` 或 ``{"id": <number>, "error": "..."}``
- 事件格式：``{"event": "...", ...payload}``，无 ``id``。
- Notification（无 ``id`` 的请求）会被静默执行，不返回响应。

支持的方法：

============== ==========================================================
方法            说明
============== ==========================================================
``ping``        连通性探活，返回 ``{"pong": True}``。
``start_task``  开始一个任务。使用当前 active thread（没有则以该 instruction
                为标题自动新建）。``params``: ``{"instruction": str,
                "autonomy"?: ..., "max_steps"?: int}``。
``cancel``      请求取消当前任务。
``get_status``  返回运行中的任务状态。
``set_autonomy`` 修改默认自动度。
``thread_new``    ``{title?}`` 创建新 thread 并设为 active。
``thread_list``   列出所有 thread（按 updated_ms 倒序）。
``thread_read``   ``{id}`` 读某 thread 的 events.jsonl + meta。
``thread_set_active`` ``{id?}`` 切换 active thread（id=null 为清空）。
``thread_delete`` ``{id}`` 删除某 thread 目录。
``shutdown``    优雅退出。
============== ==========================================================

事件类型：

- ``run_start`` ``{instruction, run_dir, model, max_steps, autonomy}``
- ``step_start`` ``{step, max_steps}``
- ``assistant_text`` ``{step, text}``
- ``tool_call`` ``{step, name, action, args}``
- ``tool_result`` ``{step, action, ok, output, error}``
- ``step_image`` ``{step, level, width, height, path, kind}``
- ``final`` ``{status, text}``  status ∈ ``ok``/``cancelled``/``max_steps``
- ``error`` ``{message}``

启动方式：``python -m ctrlapp --sidecar``。
"""
from __future__ import annotations

import json
import sys
import threading
import time
import traceback
from typing import Any

from rich.console import Console

from .config import Config, load_config
from .dpi import set_dpi_aware
from .loop import Agent
from .runlog import ThreadLog
from . import memory as memory_mod
from . import tooltips as tooltips_mod
from . import icon_memory as icon_mod
from . import templates as templates_mod
from . import scheduler as scheduler_mod

# rich 默认会写 stdout，会污染协议——sidecar 模式必须把 console 重定向到 stderr。
_err_console = Console(file=sys.stderr)


def _writeln(obj: dict[str, Any]) -> None:
    """把一行 JSON 写到 stdout 并立刻刷盘。"""
    line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


class Sidecar:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._cancel = threading.Event()
        self._current_instruction: str | None = None
        self._current_thread_id: str | None = None
        self._shutdown = threading.Event()
        self._active_thread: ThreadLog | None = None
        # 任务队列：当 worker 在跑时，后续 start_task 会被排在这里，上一个任务结束
        # 后自动堆出跳起下一个。每项包含独立的 ThreadLog，让侧边栏能看到“排队中”。
        self._queue: list[dict[str, Any]] = []
        self._scheduler = scheduler_mod.Scheduler(self._on_schedule_fire)

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def serve(self) -> int:
        _writeln({"event": "ready", "model": self._active_model(),
                  "provider": (self.cfg.llm.provider or "proxy").lower(),
                  "autonomy": self.cfg.safety.autonomy,
                  "max_steps": self.cfg.llm.max_steps})
        self._scheduler.start()
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            if self._shutdown.is_set():
                break
            try:
                req = json.loads(raw)
            except json.JSONDecodeError as e:
                _writeln({"id": None, "error": f"invalid json: {e}"})
                continue
            self._handle(req)
        # 退出前等任务收尾
        if self._worker and self._worker.is_alive():
            self._cancel.set()
            self._worker.join(timeout=10)
        self._scheduler.stop()
        return 0

    # ------------------------------------------------------------------
    # 单条请求派发
    # ------------------------------------------------------------------
    def _handle(self, req: dict[str, Any]) -> None:
        rid = req.get("id")
        method = req.get("method", "")
        params = req.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        try:
            handler = getattr(self, f"_rpc_{method}", None)
            if handler is None:
                if rid is not None:
                    _writeln({"id": rid, "error": f"unknown method: {method}"})
                return
            result = handler(params)
            if rid is not None:
                _writeln({"id": rid, "result": result})
        except Exception as e:
            _err_console.print_exception()
            if rid is not None:
                _writeln({"id": rid, "error": f"{type(e).__name__}: {e}"})

    # ------------------------------------------------------------------
    # JSON-RPC 方法实现
    # ------------------------------------------------------------------
    def _rpc_ping(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {"pong": True}

    def _rpc_get_status(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {
            "running": bool(self._worker and self._worker.is_alive()),
            "instruction": self._current_instruction,
            "current_thread_id": self._current_thread_id,
            "queue": self._queue_snapshot(),
            "autonomy": self.cfg.safety.autonomy,
            "max_steps": self.cfg.llm.max_steps,
            "model": self._active_model(),
            "provider": (self.cfg.llm.provider or "proxy").lower(),
        }

    def _queue_snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "thread_id": it["thread"].id,
                "title": it["thread"].title,
                "instruction": it["instruction"],
                "queued_ms": it["queued_ms"],
            }
            for it in self._queue
        ]

    def _active_model(self) -> str:
        provider = (self.cfg.llm.provider or "proxy").lower()
        if provider == "anthropic":
            return self.cfg.llm.anthropic.model
        if provider == "copilot":
            return self.cfg.llm.copilot.model
        return self.cfg.llm.proxy.model

    # ---- Copilot OAuth (device-code) ----

    def _copilot_manager(self):  # type: ignore[no-untyped-def]
        from .auth.copilot import CopilotTokenManager
        if not getattr(self, "_cop_tm", None):
            self._cop_tm = CopilotTokenManager(self.cfg.llm.copilot.state_file or None)
        return self._cop_tm

    def _rpc_copilot_status(self, _params: dict[str, Any]) -> dict[str, Any]:
        return self._copilot_manager().status()

    def _rpc_copilot_login_begin(self, _params: dict[str, Any]) -> dict[str, Any]:
        return self._copilot_manager().begin_login()

    def _rpc_copilot_login_poll(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._copilot_manager().poll_login(params.get("device_code"))

    def _rpc_copilot_logout(self, _params: dict[str, Any]) -> dict[str, Any]:
        self._copilot_manager().logout()
        return {"ok": True}

    # ---- runtime config reload (settings page 保存后可热重载) ----

    def _rpc_reload_config(self, _params: dict[str, Any]) -> dict[str, Any]:
        from .config import load_config
        try:
            new_cfg = load_config(None)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        # 不在任务运行中重载，避免中途换后端
        if self._worker and self._worker.is_alive():
            return {"ok": False, "error": "task running; cancel first"}
        self.cfg = new_cfg
        return {
            "ok": True,
            "provider": (new_cfg.llm.provider or "proxy").lower(),
            "model": self._active_model(),
        }

    def _rpc_set_autonomy(self, params: dict[str, Any]) -> dict[str, Any]:
        autonomy = params.get("autonomy", "")
        if autonomy not in ("full", "confirm_critical", "confirm_each"):
            raise ValueError(f"invalid autonomy: {autonomy!r}")
        self.cfg.safety.autonomy = autonomy
        return {"autonomy": autonomy}

    # ---- thread management ----

    def _ensure_active_thread(self, fallback_title: str) -> ThreadLog:
        if self._active_thread is None:
            self._active_thread = ThreadLog.create(self.cfg.logging, fallback_title)
            _writeln({"event": "thread_changed", "id": self._active_thread.id,
                      "title": self._active_thread.title})
        return self._active_thread

    def _rpc_thread_new(self, params: dict[str, Any]) -> dict[str, Any]:
        title = (params.get("title") or "").strip() or "新对话"
        # 不再取消当前任务——后续 start_task 会自动排队。
        self._active_thread = ThreadLog.create(self.cfg.logging, title)
        _writeln({"event": "thread_changed", "id": self._active_thread.id,
                  "title": self._active_thread.title})
        return {"id": self._active_thread.id, "title": self._active_thread.title}

    def _rpc_thread_list(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {"threads": ThreadLog.list_threads(self.cfg.logging),
                "active": self._active_thread.id if self._active_thread else None}

    def _rpc_thread_read(self, params: dict[str, Any]) -> dict[str, Any]:
        tid = params.get("id")
        if not tid:
            raise ValueError("id required")
        return ThreadLog.read_thread(self.cfg.logging, tid)

    def _rpc_thread_set_active(self, params: dict[str, Any]) -> dict[str, Any]:
        tid = params.get("id")
        # 允许在任务跑时切换 active thread（UI 看哪个）——运行中的 Agent 自带 thread_log 引用，
        # 不受 _active_thread 影响。
        if not tid:
            self._active_thread = None
            return {"active": None}
        self._active_thread = ThreadLog.open(self.cfg.logging, tid)
        _writeln({"event": "thread_changed", "id": self._active_thread.id,
                  "title": self._active_thread.title})
        return {"active": self._active_thread.id}

    def _rpc_thread_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        tid = params.get("id")
        if not tid:
            raise ValueError("id required")
        # 如果在队列里，先从队列移除。
        with self._lock:
            self._queue = [it for it in self._queue if it["thread"].id != tid]
        if self._active_thread and self._active_thread.id == tid:
            if self._worker and self._worker.is_alive() and self._current_thread_id == tid:
                raise RuntimeError("该对话正在运行，先急停再删除")
            self._active_thread = None
        ok = ThreadLog.delete_thread(self.cfg.logging, tid)
        _writeln({"event": "queue_changed", "queue": self._queue_snapshot()})
        return {"deleted": ok}

    # ---- memory.md ----

    def _rpc_memory_read(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {
            "enabled": self.cfg.memory.enabled,
            "path": str(memory_mod.memory_path(self.cfg.memory)),
            "text": memory_mod.read_memory(self.cfg.memory),
        }

    def _rpc_memory_write(self, params: dict[str, Any]) -> dict[str, Any]:
        text = params.get("text")
        if text is None:
            raise ValueError("text required")
        memory_mod.write_memory_raw(self.cfg.memory, text)
        return {"ok": True}

    def _rpc_memory_append(self, params: dict[str, Any]) -> dict[str, Any]:
        text = (params.get("text") or "").strip()
        source = params.get("source") or "user"
        ok = memory_mod.append_memory(self.cfg.memory, text, source=source)
        return {"ok": ok}

    def _rpc_memory_clear(self, _params: dict[str, Any]) -> dict[str, Any]:
        memory_mod.clear_memory(self.cfg.memory)
        return {"ok": True}

    # ---- tools.md （操作技巧）----

    def _rpc_tools_read(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {
            "enabled": self.cfg.tools.enabled,
            "path": str(tooltips_mod.tools_path(self.cfg.tools)),
            "text": tooltips_mod.read_tools(self.cfg.tools),
        }

    def _rpc_tools_write(self, params: dict[str, Any]) -> dict[str, Any]:
        text = params.get("text")
        if text is None:
            raise ValueError("text required")
        tooltips_mod.write_tools_raw(self.cfg.tools, text)
        return {"ok": True}

    def _rpc_tools_append(self, params: dict[str, Any]) -> dict[str, Any]:
        text = (params.get("text") or "").strip()
        source = params.get("source") or "user"
        kind = params.get("kind") or "tip"
        ok = tooltips_mod.append_tip(self.cfg.tools, text, kind=kind, source=source)
        return {"ok": ok}

    def _rpc_tools_reset(self, _params: dict[str, Any]) -> dict[str, Any]:
        tooltips_mod.reset_to_seed(self.cfg.tools)
        return {"ok": True}

    # ---- icons/ （图标记忆库）----

    def _rpc_icons_list(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {
            "enabled": self.cfg.icons.enabled,
            "dir": str(icon_mod.icons_dir(self.cfg.icons)),
            "items": icon_mod.list_icons(self.cfg.icons),
        }

    def _rpc_icons_add(self, params: dict[str, Any]) -> dict[str, Any]:
        """从 base64 PNG 直接登记一张图标。"""
        import base64
        b64 = params.get("png_b64") or ""
        label = params.get("label") or ""
        desc = params.get("description") or ""
        try:
            png = base64.b64decode(b64)
        except Exception as exc:
            raise ValueError(f"invalid png_b64: {exc}") from exc
        entry = icon_mod.add_icon(self.cfg.icons, png, label, desc)
        return {"ok": entry is not None, "entry": entry}

    def _rpc_icons_remove(self, params: dict[str, Any]) -> dict[str, Any]:
        iid = params.get("id")
        if not iid:
            raise ValueError("id required")
        return {"ok": icon_mod.remove_icon(self.cfg.icons, iid)}

    def _rpc_icons_clear(self, _params: dict[str, Any]) -> dict[str, Any]:
        icon_mod.clear_icons(self.cfg.icons)
        return {"ok": True}

    def _rpc_icons_get_png(self, params: dict[str, Any]) -> dict[str, Any]:
        """返回某图标的 base64 PNG（供 UI 预览）。"""
        import base64
        iid = params.get("id")
        if not iid:
            raise ValueError("id required")
        png = icon_mod.read_icon_png(self.cfg.icons, iid)
        if png is None:
            return {"ok": False}
        return {"ok": True, "png_b64": base64.b64encode(png).decode("ascii")}

    def _rpc_icons_atlas(self, _params: dict[str, Any]) -> dict[str, Any]:
        """返回当前合集图（base64 PNG + 文字索引），用于 UI 预览。"""
        import base64
        atlas = icon_mod.build_atlas(self.cfg.icons)
        if atlas is None:
            return {"ok": False}
        return {
            "ok": True,
            "png_b64": base64.b64encode(atlas.png_bytes).decode("ascii"),
            "width": atlas.width,
            "height": atlas.height,
            "captions": atlas.captions,
        }

    # ---- 任务模板 ----

    def _rpc_template_list(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {"templates": templates_mod.list_templates()}

    def _rpc_template_add(self, params: dict[str, Any]) -> dict[str, Any]:
        return templates_mod.add_template(
            name=params.get("name", ""),
            instruction=params.get("instruction", ""),
            autonomy=params.get("autonomy") or "confirm_critical",
            max_steps=int(params.get("max_steps") or 25),
        )

    def _rpc_template_update(self, params: dict[str, Any]) -> dict[str, Any]:
        tid = params.get("id")
        if not tid:
            raise ValueError("id required")
        item = templates_mod.update_template(
            tid,
            name=params.get("name"),
            instruction=params.get("instruction"),
            autonomy=params.get("autonomy"),
            max_steps=params.get("max_steps"),
        )
        if item is None:
            raise ValueError(f"template {tid!r} not found")
        return item

    def _rpc_template_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        tid = params.get("id")
        if not tid:
            raise ValueError("id required")
        return {"deleted": templates_mod.delete_template(tid)}

    # ---- 定时任务 ----

    def _rpc_schedule_list(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {"schedules": scheduler_mod.list_schedules()}

    def _rpc_schedule_add(self, params: dict[str, Any]) -> dict[str, Any]:
        spec = params.get("spec") or {}
        return scheduler_mod.add_schedule(
            name=params.get("name", ""),
            instruction=params.get("instruction", ""),
            spec=spec,
            autonomy=params.get("autonomy") or "confirm_critical",
            max_steps=int(params.get("max_steps") or 25),
            enabled=bool(params.get("enabled", True)),
        )

    def _rpc_schedule_update(self, params: dict[str, Any]) -> dict[str, Any]:
        sid = params.get("id")
        if not sid:
            raise ValueError("id required")
        item = scheduler_mod.update_schedule(
            sid,
            name=params.get("name"),
            instruction=params.get("instruction"),
            spec=params.get("spec"),
            autonomy=params.get("autonomy"),
            max_steps=params.get("max_steps"),
            enabled=params.get("enabled"),
        )
        if item is None:
            raise ValueError(f"schedule {sid!r} not found")
        return item

    def _rpc_schedule_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        sid = params.get("id")
        if not sid:
            raise ValueError("id required")
        return {"deleted": scheduler_mod.delete_schedule(sid)}

    def _on_schedule_fire(self, item: dict[str, Any]) -> None:
        """调度器在子线程里调用。忙碌时排队，不抢占人工任务。"""
        try:
            instruction = item.get("instruction", "")
            sched_thread = ThreadLog.create(
                self.cfg.logging, f"⏰ {item.get('name', '定时任务')}"
            )
            res = self._rpc_start_task({
                "instruction": instruction,
                "autonomy": item.get("autonomy"),
                "max_steps": item.get("max_steps"),
                "_thread": sched_thread,
            })
            if res.get("queued"):
                _writeln({"event": "schedule_queued", "id": item.get("id"),
                          "name": item.get("name"),
                          "thread_id": res.get("thread_id"),
                          "position": res.get("position")})
            else:
                _writeln({"event": "schedule_fired", "id": item.get("id"),
                          "name": item.get("name"),
                          "thread_id": res.get("thread_id")})
        except Exception as e:
            _writeln({"event": "schedule_error", "id": item.get("id"),
                      "message": f"{type(e).__name__}: {e}"})
            if res.get("queued"):
                _writeln({"event": "schedule_queued", "id": item.get("id"),
                          "name": item.get("name"),
                          "thread_id": res.get("thread_id"),
                          "position": res.get("position")})
            else:
                _writeln({"event": "schedule_fired", "id": item.get("id"),
                          "name": item.get("name"),
                          "thread_id": res.get("thread_id")})
        except Exception as e:
            _writeln({"event": "schedule_error", "id": item.get("id"),
                      "message": f"{type(e).__name__}: {e}"})

    def _rpc_start_task(self, params: dict[str, Any]) -> dict[str, Any]:
        instruction = (params.get("instruction") or "").strip()
        if not instruction:
            raise ValueError("instruction is required")
        autonomy = params.get("autonomy")
        max_steps = params.get("max_steps")
        if autonomy and autonomy not in ("full", "confirm_critical", "confirm_each"):
            raise ValueError(f"invalid autonomy: {autonomy!r}")
        # 可选：调用方（如 scheduler）已经为本任务建好了独立 thread。
        preset_thread = params.get("_thread")
        if not isinstance(preset_thread, ThreadLog):
            preset_thread = None
        with self._lock:
            running = bool(self._worker and self._worker.is_alive())
            if running:
                # 入队：为这个排队任务创建独立 thread，在侧边栏马上可见。
                qthread = preset_thread or ThreadLog.create(self.cfg.logging, instruction)
                qthread.append_user_input(instruction)
                self._queue.append({
                    "instruction": instruction,
                    "thread": qthread,
                    "autonomy": autonomy,
                    "max_steps": max_steps,
                    "queued_ms": int(time.time() * 1000),
                })
                _writeln({"event": "task_queued",
                          "thread_id": qthread.id,
                          "title": qthread.title,
                          "instruction": instruction,
                          "position": len(self._queue),
                          "queue": self._queue_snapshot()})
                return {"queued": True, "position": len(self._queue),
                        "thread_id": qthread.id}

            # 立刻跑：覆盖静态配置，使用 preset_thread / active thread / 新建 thread。
            if autonomy:
                self.cfg.safety.autonomy = autonomy
            if max_steps:
                self.cfg.llm.max_steps = int(max_steps)
            if preset_thread is not None:
                thread = preset_thread
                self._active_thread = preset_thread
                _writeln({"event": "thread_changed", "id": thread.id,
                          "title": thread.title})
            else:
                thread = self._ensure_active_thread(instruction)
            thread.append_user_input(instruction)
            self._cancel.clear()
            self._current_instruction = instruction
            self._current_thread_id = thread.id
            t = threading.Thread(
                target=self._run_task, args=(instruction, thread), daemon=True
            )
            self._worker = t
            t.start()
        return {"started": True, "instruction": instruction, "thread_id": thread.id}

    def _rpc_queue_list(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {"queue": self._queue_snapshot(),
                "current_thread_id": self._current_thread_id}

    def _rpc_queue_remove(self, params: dict[str, Any]) -> dict[str, Any]:
        tid = params.get("thread_id") or params.get("id")
        if not tid:
            raise ValueError("thread_id required")
        removed = False
        with self._lock:
            new_q: list[dict[str, Any]] = []
            for it in self._queue:
                if not removed and it["thread"].id == tid:
                    removed = True
                    continue
                new_q.append(it)
            self._queue = new_q
        if removed:
            _writeln({"event": "queue_changed", "queue": self._queue_snapshot()})
        return {"removed": removed}

    def _rpc_queue_clear(self, _params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            n = len(self._queue)
            self._queue = []
        if n:
            _writeln({"event": "queue_changed", "queue": []})
        return {"cleared": n}

    def _rpc_cancel(self, _params: dict[str, Any]) -> dict[str, Any]:
        if self._worker and self._worker.is_alive():
            self._cancel.set()
            return {"cancelling": True}
        return {"cancelling": False}

    def _rpc_shutdown(self, _params: dict[str, Any]) -> dict[str, Any]:
        if self._worker and self._worker.is_alive():
            self._cancel.set()
        self._shutdown.set()
        return {"bye": True}

    # ------------------------------------------------------------------
    # 任务工作线程
    # ------------------------------------------------------------------
    def _run_task(self, instruction: str, thread: ThreadLog) -> None:
        def sink(evt: dict[str, Any]) -> None:
            # 1) 走 stdout 让前端实时看到
            _writeln(evt)
            # 2) 同步追加到 thread events.jsonl持久化
            try:
                thread.append_event(evt)
            except Exception:
                pass
        try:
            agent = Agent(self.cfg, event_sink=sink, cancel_event=self._cancel,
                          thread_log=thread)
            agent.run(instruction)
        except Exception as e:
            _err_console.print_exception()
            sink({"event": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            self._current_instruction = None
            self._current_thread_id = None
            # 堆出下一个排队任务（如有）。
            self._drain_queue()

    def _drain_queue(self) -> None:
        if self._shutdown.is_set():
            return
        with self._lock:
            if not self._queue:
                _writeln({"event": "queue_changed", "queue": []})
                return
            nxt = self._queue.pop(0)
            instruction = nxt["instruction"]
            thread = nxt["thread"]
            autonomy = nxt.get("autonomy")
            max_steps = nxt.get("max_steps")
            if autonomy:
                self.cfg.safety.autonomy = autonomy
            if max_steps:
                self.cfg.llm.max_steps = int(max_steps)
            self._cancel.clear()
            self._current_instruction = instruction
            self._current_thread_id = thread.id
            t = threading.Thread(
                target=self._run_task, args=(instruction, thread), daemon=True
            )
            self._worker = t
            t.start()
        _writeln({"event": "task_dequeued",
                  "thread_id": thread.id,
                  "title": thread.title,
                  "instruction": instruction,
                  "queue": self._queue_snapshot()})


def run_sidecar(cfg: Config) -> int:
    """供 __main__ 调用的入口。"""
    # Tauri 子进程通过管道传 UTF-8 字节，但 Windows 上 Python 默认 stdin/stdout
    # 用 cp936/GBK 解码，会把中文/emoji 切成代理对（\udcXX）然后在 json.dumps
    # 时报 "surrogates not allowed"。强制 UTF-8。
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", newline="\n")
        except (AttributeError, OSError):
            pass
    set_dpi_aware()
    try:
        return Sidecar(cfg).serve()
    except KeyboardInterrupt:
        return 130
    except Exception:
        _err_console.print_exception()
        return 1


__all__ = ["Sidecar", "run_sidecar"]
