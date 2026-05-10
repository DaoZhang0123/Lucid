"""任务模板 templates.json。

存放在 ``%LOCALAPPDATA%\\dev.lucid\\templates.json``。一个模板是一段可复用
的 instruction + 默认自动度 + 默认步数。前端可以一键发送（仍然走正常的
``start_task`` RPC）。

"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

_BASENAME = "templates.json"


def _store_path() -> Path:
    if os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            return Path(local_app) / "dev.lucid" / _BASENAME
    home = os.environ.get("HOME")
    if home:
        return Path(home) / ".lucid" / _BASENAME
    return Path.cwd() / _BASENAME


def _load() -> list[dict[str, Any]]:
    p = _store_path()
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [t for t in data if isinstance(t, dict) and t.get("id")]
    except (OSError, ValueError):
        pass
    return []


def _save(items: list[dict[str, Any]]) -> None:
    p = _store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def list_templates() -> list[dict[str, Any]]:
    return _load()


def add_template(name: str, instruction: str, autonomy: str = "confirm_critical",
                 max_steps: int = 25) -> dict[str, Any]:
    name = (name or "").strip() or "未命名模板"
    instruction = (instruction or "").strip()
    if not instruction:
        raise ValueError("instruction required")
    if autonomy not in ("full", "confirm_critical", "confirm_each"):
        raise ValueError(f"invalid autonomy: {autonomy!r}")
    items = _load()
    item = {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "instruction": instruction,
        "autonomy": autonomy,
        "max_steps": int(max_steps),
        "created_ms": int(time.time() * 1000),
    }
    items.append(item)
    _save(items)
    return item


def update_template(tid: str, **fields: Any) -> dict[str, Any] | None:
    items = _load()
    for it in items:
        if it["id"] == tid:
            for k in ("name", "instruction", "autonomy", "max_steps"):
                if k in fields and fields[k] is not None:
                    it[k] = fields[k]
            it["updated_ms"] = int(time.time() * 1000)
            _save(items)
            return it
    return None


def delete_template(tid: str) -> bool:
    items = _load()
    new = [it for it in items if it["id"] != tid]
    if len(new) == len(items):
        return False
    _save(new)
    return True
