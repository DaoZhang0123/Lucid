"""Thread-scoped 日志：一个 thread 是一段对话上下文，可包含多次 start_task。

目录结构（默认在 ~/.lucid/logs/ 下）::

    thread-20260430-235012-苏州天气/
        meta.json         # {id, title, created_ms, updated_ms, task_count}
        events.jsonl      # 按时间追加的所有事件（user_input / run_start /
                          # assistant_text / tool_call / tool_result /
                          # step_image / final / error / task_close ...）
        run.log           # 人类可读的文本摘要（DEBUG/INFO/WARNING/ERROR）
        step-NNN-*.png    # 每步动作截图，NNN 是 thread 内全局递增

向后兼容旧 RunLogger：保留 info/warning/error/debug/step_record/save_image/close/run_dir
等接口，loop.py 几乎不用改；新增 append_event()，sidecar 的 event_sink 会调用它把
所有事件落盘，从而支持"返回到旧 thread 继续对话"的回放。
"""
from __future__ import annotations

import io
import json
import os
import re
import secrets
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import LoggingConfig

_TEXT_LEVELS: dict[str, int] = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "OFF": 100}
_IMAGE_LEVELS: dict[str, int] = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "OFF": 100}

_THREAD_PREFIX = "thread-"
_THREADS_SUBDIR = "threads"


def _slug(text: str, max_len: int = 32) -> str:
    text = (text or "").strip()
    text = re.sub(r"[\s\\/:*?\"<>|]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:max_len] or "thread"


def resolve_logs_root(cfg: LoggingConfig) -> Path:
    """把 cfg.dir 解析成绝对路径（相对路径落到 ~/.lucid/）。"""
    base = Path(cfg.dir)
    if base.is_absolute():
        return base
    return Path.home() / ".lucid" / cfg.dir


def resolve_threads_root(cfg: LoggingConfig) -> Path:
    """Thread 子目录：所有 thread-* 都放在 logs/threads/ 下，避免顶层过乱。"""
    root = resolve_logs_root(cfg) / _THREADS_SUBDIR
    return root


def _migrate_legacy_threads(cfg: LoggingConfig) -> None:
    """把历史遗留在 logs/ 顶层的 thread-* 目录迁移到 logs/threads/ 下。
    幂等：已迁移过的不会重复处理；目标已存在则跳过。
    """
    try:
        top = resolve_logs_root(cfg)
        if not top.exists():
            return
        new_root = resolve_threads_root(cfg)
        moved = 0
        for entry in list(top.iterdir()):
            if not entry.is_dir():
                continue
            if not entry.name.startswith(_THREAD_PREFIX):
                continue
            new_root.mkdir(parents=True, exist_ok=True)
            target = new_root / entry.name
            if target.exists():
                continue
            try:
                entry.rename(target)
                moved += 1
            except Exception:
                # 跨卷或权限失败时忽略，让旧目录留在原处
                pass
    except Exception:
        pass


class ThreadLog:
    """一段对话上下文的持久化句柄。安全地多次打开同一个目录（追加模式）。"""

    def __init__(self, cfg: LoggingConfig, run_dir: Path) -> None:
        self.cfg = cfg
        self.run_dir: Path | None = run_dir
        self.enabled = bool(cfg.enabled)
        self._text_threshold = _TEXT_LEVELS.get(cfg.text_level.upper(), 20)
        self._image_threshold = _IMAGE_LEVELS.get(cfg.image_level.upper(), 20)
        self._image_seq = 0
        self._meta = self._load_meta()
        # scan existing pngs to resume image counter
        if self.run_dir and self.run_dir.exists():
            for p in self.run_dir.iterdir():
                m = re.match(r"step-(\d+)-", p.name)
                if m:
                    self._image_seq = max(self._image_seq, int(m.group(1)))

    # ---------- factory ----------

    @classmethod
    def create(cls, cfg: LoggingConfig, title: str) -> "ThreadLog":
        if not cfg.enabled:
            # disabled: still construct a no-op handle with run_dir=None
            obj = cls.__new__(cls)
            obj.cfg = cfg
            obj.run_dir = None
            obj.enabled = False
            obj._text_threshold = 100
            obj._image_threshold = 100
            obj._image_seq = 0
            obj._meta = {}
            return obj
        _migrate_legacy_threads(cfg)
        root = resolve_threads_root(cfg)
        root.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        slug = _slug(title)
        # Always append a short random suffix so two threads created in the
        # same second with the same title (or any title at all) get distinct
        # directories. Using exist_ok=False + retry guarantees uniqueness even
        # under improbable collision.
        for _ in range(8):
            suffix = secrets.token_hex(3)  # 6 hex chars
            thread_id = f"{_THREAD_PREFIX}{ts}-{suffix}-{slug}"
            run_dir = root / thread_id
            try:
                run_dir.mkdir(parents=True, exist_ok=False)
                break
            except FileExistsError:
                continue
        else:
            raise RuntimeError("could not allocate unique thread directory")
        obj = cls(cfg, run_dir)
        now_ms = int(time.time() * 1000)
        obj._meta = {
            "id": thread_id,
            "title": title.strip() or thread_id,
            "created_ms": now_ms,
            "updated_ms": now_ms,
            "task_count": 0,
        }
        obj._save_meta()
        obj._rotate(root, cfg.keep_runs)
        return obj

    @classmethod
    def open(cls, cfg: LoggingConfig, thread_id: str) -> "ThreadLog":
        _migrate_legacy_threads(cfg)
        root = resolve_threads_root(cfg)
        run_dir = root / thread_id
        if not run_dir.exists():
            raise FileNotFoundError(f"thread not found: {thread_id}")
        return cls(cfg, run_dir)

    # ---------- meta ----------

    def _meta_path(self) -> Path | None:
        return self.run_dir / "meta.json" if self.run_dir else None

    def _load_meta(self) -> dict[str, Any]:
        p = self._meta_path()
        if p and p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_meta(self) -> None:
        p = self._meta_path()
        if p is None:
            return
        try:
            p.write_text(json.dumps(self._meta, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    @property
    def id(self) -> str:
        return str(self._meta.get("id") or (self.run_dir.name if self.run_dir else ""))

    @property
    def title(self) -> str:
        return str(self._meta.get("title") or self.id)

    def set_title(self, title: str) -> None:
        title = (title or "").strip()
        if title and title != self._meta.get("title"):
            self._meta["title"] = title
            self._touch_meta()

    def _touch_meta(self, *, bump_task: bool = False) -> None:
        if not self.run_dir:
            return
        self._meta["updated_ms"] = int(time.time() * 1000)
        if bump_task:
            self._meta["task_count"] = int(self._meta.get("task_count", 0)) + 1
        self._save_meta()

    # ---------- events.jsonl ----------

    def append_event(self, evt: dict[str, Any]) -> None:
        """所有 sidecar 发出的事件经此持久化。"""
        if not self.enabled or self.run_dir is None:
            return
        try:
            payload = dict(evt)
            payload.setdefault("ts_ms", int(time.time() * 1000))
            with open(self.run_dir / "events.jsonl", "a", encoding="utf-8") as fp:
                fp.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
            kind = payload.get("event")
            self._touch_meta(bump_task=(kind == "run_start"))
        except Exception:
            pass

    def append_user_input(self, text: str) -> None:
        self.append_event({"event": "user_input", "text": text})
        # 第一条用户输入 → 用作 thread 标题（如果还是默认 id）
        if self._meta.get("title") in (None, "", self.id):
            self.set_title(text)

    # ---------- text log ----------

    def _level_pass_text(self, level: str) -> bool:
        return _TEXT_LEVELS.get(level.upper(), 20) >= self._text_threshold

    def _write_text(self, level: str, msg: str) -> None:
        if not self.enabled or self.run_dir is None:
            return
        if not self._level_pass_text(level):
            return
        try:
            ts = time.strftime("%H:%M:%S")
            with open(self.run_dir / "run.log", "a", encoding="utf-8") as fp:
                fp.write(f"[{ts}] {level:<7} {msg}\n")
        except Exception:
            pass

    def debug(self, msg: str) -> None:   self._write_text("DEBUG", msg)
    def info(self, msg: str) -> None:    self._write_text("INFO", msg)
    def warning(self, msg: str) -> None: self._write_text("WARNING", msg)
    def error(self, msg: str) -> None:   self._write_text("ERROR", msg)

    # ---------- step records (legacy) ----------

    def step_record(self, record: dict[str, Any]) -> None:
        self.append_event({"event": "step_summary", **record})

    # ---------- images ----------

    def _level_pass_image(self, level: str) -> bool:
        return _IMAGE_LEVELS.get(level.upper(), 20) >= self._image_threshold

    def save_image(self, png_bytes: bytes, name: str, *, level: str = "INFO") -> str | None:
        if not self.enabled or self.run_dir is None:
            return None
        if not self._level_pass_image(level):
            return None
        # name 可能形如 "step-001-post-fullscreen"，但 thread 视角下我们要全局序号
        self._image_seq += 1
        # 把 step-NNN- 前缀替换为新的全局序号；其余部分保留
        m = re.match(r"step-(\d+)-(.+)", name)
        suffix = m.group(2) if m else name
        fname_base = f"step-{self._image_seq:03d}-{suffix}"
        fmt = self.cfg.image_format.lower()
        try:
            if fmt == "jpg":
                from PIL import Image
                im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
                fname = f"{fname_base}.jpg"
                im.save(self.run_dir / fname, "JPEG", quality=int(self.cfg.jpg_quality))
            else:
                fname = f"{fname_base}.png"
                (self.run_dir / fname).write_bytes(png_bytes)
        except Exception:
            fname = f"{fname_base}.png"
            try:
                (self.run_dir / fname).write_bytes(png_bytes)
            except Exception:
                return None
        return fname

    # ---------- task lifecycle ----------

    def close(self, status: str = "ok", final_text: str = "") -> None:
        """单次 task 结束时 sidecar 调用；thread 文件保持打开（追加更多 task）。"""
        if not self.enabled:
            return
        self._write_text("INFO", f"task close status={status}")
        if final_text:
            self._write_text("INFO", f"final: {final_text}")
        self.append_event({"event": "task_close", "status": status, "final_text": final_text})

    # ---------- listing / deletion ----------

    @classmethod
    def list_threads(cls, cfg: LoggingConfig) -> list[dict[str, Any]]:
        _migrate_legacy_threads(cfg)
        root = resolve_threads_root(cfg)
        if not root.exists():
            return []
        out: list[dict[str, Any]] = []
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            if not entry.name.startswith(_THREAD_PREFIX):
                continue
            meta_path = entry / "meta.json"
            meta: dict[str, Any] = {}
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except Exception:
                    meta = {}
            mtime_ms = int(entry.stat().st_mtime * 1000)
            out.append({
                "id": meta.get("id") or entry.name,
                "title": meta.get("title") or entry.name[len(_THREAD_PREFIX):],
                "created_ms": int(meta.get("created_ms") or mtime_ms),
                "updated_ms": int(meta.get("updated_ms") or mtime_ms),
                "task_count": int(meta.get("task_count") or 0),
            })
        out.sort(key=lambda x: x["updated_ms"], reverse=True)
        return out

    @classmethod
    def delete_thread(cls, cfg: LoggingConfig, thread_id: str) -> bool:
        if not thread_id.startswith(_THREAD_PREFIX):
            return False
        _migrate_legacy_threads(cfg)
        root = resolve_threads_root(cfg)
        target = root / thread_id
        if not target.exists() or not target.is_dir():
            return False
        try:
            shutil.rmtree(target, ignore_errors=True)
            return True
        except Exception:
            return False

    @classmethod
    def read_thread(cls, cfg: LoggingConfig, thread_id: str) -> dict[str, Any]:
        _migrate_legacy_threads(cfg)
        root = resolve_threads_root(cfg)
        run_dir = root / thread_id
        if not run_dir.exists():
            raise FileNotFoundError(thread_id)
        meta: dict[str, Any] = {}
        mp = run_dir / "meta.json"
        if mp.exists():
            try:
                meta = json.loads(mp.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        events: list[dict[str, Any]] = []
        ep = run_dir / "events.jsonl"
        if ep.exists():
            for line in ep.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue
        return {
            "id": thread_id,
            "title": meta.get("title") or thread_id,
            "created_ms": meta.get("created_ms"),
            "updated_ms": meta.get("updated_ms"),
            "dir": str(run_dir),
            "events": events,
        }

    # ---------- rotation ----------

    @staticmethod
    def _rotate(base: Path, keep: int) -> None:
        if keep <= 0:
            return
        runs = sorted(
            (p for p in base.iterdir() if p.is_dir() and p.name.startswith(_THREAD_PREFIX)),
            key=lambda p: p.name,
        )
        excess = len(runs) - keep
        if excess <= 0:
            # Important: bail out before slicing. ``runs[:excess]`` with a
            # negative excess is interpreted as ``runs[:-N]`` (all but the
            # last N), which would silently delete the oldest threads on
            # every new-thread creation — capping the on-disk count at
            # roughly ``keep`` even when the user has way fewer than that.
            return
        for old in runs[:excess]:
            shutil.rmtree(old, ignore_errors=True)


# 兼容别名：老代码写 `from .runlog import RunLogger` 直接报 ImportError 太刺眼，
# 仍提供一个 alias；不推荐新代码使用。
RunLogger = ThreadLog
