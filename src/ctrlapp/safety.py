"""Safety Layer — 危险动作的 HITL 拦截（design.md §4.6 / §4.7）。

Phase 0 仅做关键字匹配 + 终端 y/n 二次确认。
"""
from __future__ import annotations

from rich.console import Console
from rich.prompt import Confirm

from .config import SafetyConfig

_console = Console()


class SafetyLayer:
    def __init__(self, cfg: SafetyConfig) -> None:
        self.cfg = cfg

    def should_confirm(self, action: str, params: dict) -> bool:
        if self.cfg.autonomy == "full":
            # 仅对关键字命中要求确认
            text = " ".join(str(v) for v in params.values() if v is not None).lower()
            return any(kw.lower() in text for kw in self.cfg.hitl_keywords)
        if self.cfg.autonomy == "confirm_each":
            return True
        # confirm_critical
        risky_actions = {"left_click_drag"}
        text = " ".join(str(v) for v in params.values() if v is not None).lower()
        if any(kw.lower() in text for kw in self.cfg.hitl_keywords):
            return True
        return action in risky_actions

    def confirm(self, action: str, params: dict) -> bool:
        _console.print(f"[yellow]⚠ HITL 确认[/yellow]: {action} {params}")
        try:
            return Confirm.ask("继续？", default=True)
        except (EOFError, KeyboardInterrupt):
            return False
