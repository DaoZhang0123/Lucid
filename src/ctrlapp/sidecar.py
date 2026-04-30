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
``start_task``  开始一个任务。``params``: ``{"instruction": str,
                "autonomy"?: "full"|"confirm_critical"|"confirm_each",
                "max_steps"?: int}``。
                同一时刻只能有一个任务在跑。
``cancel``      请求取消当前任务（在两步之间生效）。
``get_status``  返回 ``{"running": bool, "instruction"?: str,
                "autonomy": str, "max_steps": int}``。
``set_autonomy`` 修改默认自动度（不影响正在跑的任务）。
``shutdown``    优雅退出。先 cancel，再返回响应，然后进程退出。
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
import traceback
from typing import Any

from rich.console import Console

from .config import Config, load_config
from .dpi import set_dpi_aware
from .loop import Agent

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
        self._shutdown = threading.Event()

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def serve(self) -> int:
        _writeln({"event": "ready", "model": self._active_model(),
                  "provider": (self.cfg.llm.provider or "proxy").lower(),
                  "autonomy": self.cfg.safety.autonomy,
                  "max_steps": self.cfg.llm.max_steps})
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
            "autonomy": self.cfg.safety.autonomy,
            "max_steps": self.cfg.llm.max_steps,
            "model": self._active_model(),
            "provider": (self.cfg.llm.provider or "proxy").lower(),
        }

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

    def _rpc_start_task(self, params: dict[str, Any]) -> dict[str, Any]:
        instruction = (params.get("instruction") or "").strip()
        if not instruction:
            raise ValueError("instruction is required")
        with self._lock:
            if self._worker and self._worker.is_alive():
                raise RuntimeError("a task is already running; cancel it first")

            # 临时覆盖（不写回 cfg 全局，下一次任务恢复默认）
            autonomy = params.get("autonomy")
            max_steps = params.get("max_steps")
            if autonomy:
                if autonomy not in ("full", "confirm_critical", "confirm_each"):
                    raise ValueError(f"invalid autonomy: {autonomy!r}")
                self.cfg.safety.autonomy = autonomy
            if max_steps:
                self.cfg.llm.max_steps = int(max_steps)

            self._cancel.clear()
            self._current_instruction = instruction
            t = threading.Thread(
                target=self._run_task, args=(instruction,), daemon=True
            )
            self._worker = t
            t.start()
        return {"started": True, "instruction": instruction}

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
    def _run_task(self, instruction: str) -> None:
        try:
            agent = Agent(self.cfg, event_sink=_writeln, cancel_event=self._cancel)
            agent.run(instruction)
        except Exception as e:
            _err_console.print_exception()
            _writeln({"event": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            self._current_instruction = None


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
