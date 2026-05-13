"""Safety Layer — 危险动作的 HITL 拦截（design.md §4.6 / §4.7）。

Phase 0 仅做关键字匹配 + 终端 y/n 二次确认。

**Sidecar wedge guard (2026-05-13)**: when running as a Tauri sidecar there is
no TTY, so `Confirm.ask` would block forever on `sys.stdin` — the loop wedges
*before* `ComputerTool.dispatch`, so the dispatch watchdog can't help. Worse,
on Windows `input()` in a worker thread is not interruptible by signal, so
急停 (Ctrl+Alt+Esc) can't unwedge it either. We now detect non-TTY stdin and
auto-decline with a warning event; the model gets a normal "user declined"
tool result and the loop continues.
"""
from __future__ import annotations

import sys
from typing import Any, Callable

from rich.console import Console
from rich.prompt import Confirm

from .config import SafetyConfig

_console = Console()


# Actions whose `params` actually carry destructive payload (run_shell text,
# typed text). For `key`/`mouse_*`/`screenshot`/etc. the payload is just a
# control name like "Delete" / "Backspace" — substring-matching that against
# `hitl_keywords` (which contains "delete") produces false positives that
# wedge the entire sidecar at the HITL prompt. Limit keyword scanning to
# these payload-bearing actions.
_PAYLOAD_ACTIONS = {"type"}


class SafetyLayer:
    def __init__(
        self,
        cfg: SafetyConfig,
        event_sink: Callable[..., None] | None = None,
    ) -> None:
        self.cfg = cfg
        self._emit = event_sink

    def should_confirm(self, action: str, params: dict) -> bool:
        if self.cfg.autonomy == "full":
            if action not in _PAYLOAD_ACTIONS:
                return False
            text = " ".join(str(v) for v in params.values() if v is not None).lower()
            return any(kw.lower() in text for kw in self.cfg.hitl_keywords)
        if self.cfg.autonomy == "confirm_each":
            return True
        # confirm_critical
        risky_actions = {"left_click_drag"}
        if action in _PAYLOAD_ACTIONS:
            text = " ".join(str(v) for v in params.values() if v is not None).lower()
            if any(kw.lower() in text for kw in self.cfg.hitl_keywords):
                return True
        return action in risky_actions

    def confirm(self, action: str, params: dict) -> bool:
        _console.print(f"[yellow]⚠ HITL 确认[/yellow]: {action} {params}")
        # Sidecar / no-TTY guard: blocking on stdin here wedges the worker
        # forever (see module docstring). Auto-decline so the loop continues
        # with a normal "user declined" tool result; the model can adjust.
        if not _stdin_is_interactive():
            msg = (
                f"HITL prompt for {action!r} auto-declined (no TTY in sidecar). "
                f"Loosen safety.hitl_keywords or set safety.autonomy='full' if "
                f"you want such actions to run unattended."
            )
            _console.print(f"[yellow]⚠ {msg}[/yellow]")
            if self._emit is not None:
                try:
                    self._emit("warning", message=msg)
                except Exception:
                    pass
            return False
        try:
            return Confirm.ask("继续？", default=True)
        except (EOFError, KeyboardInterrupt):
            return False


def _stdin_is_interactive() -> bool:
    """True only when ``sys.stdin`` is a real TTY we can read y/n from.

    Tauri sidecars get a piped stdin (not a TTY); reading from it blocks
    indefinitely. ``isatty`` is the cheap, reliable signal.
    """
    stream: Any = getattr(sys, "stdin", None)
    if stream is None:
        return False
    try:
        return bool(stream.isatty())
    except Exception:
        return False
