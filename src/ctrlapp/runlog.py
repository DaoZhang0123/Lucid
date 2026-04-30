"""每次运行的本地日志：文本 + 截图，文本与图像可独立设级。

目录结构：
    <log_dir>/
        20260425-103045-打开记事本/
            run.log           # 文本流水
            messages.jsonl    # 每步发给 LLM 的消息摘要（不含图像 base64）
            step-001-init.png
            step-001-post.png
            step-002-post.png
            ...
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import LoggingConfig


# 文本等级：数值越大越严重，OFF=最高，禁止任何文本输出
_TEXT_LEVELS: dict[str, int] = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "OFF": 100}
# 图像等级（独立于文本）：DEBUG 存全部 / INFO 存关键 / WARNING 仅错误时存 / OFF 不存
_IMAGE_LEVELS: dict[str, int] = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "OFF": 100}


def _slug(text: str, max_len: int = 32) -> str:
    text = text.strip()
    text = re.sub(r"[\s\\/:*?\"<>|]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:max_len] or "task"


class RunLogger:
    """单次任务的日志器；也接受 enabled=False 的"空操作"形态。"""

    def __init__(self, cfg: LoggingConfig, instruction: str, root: Path | None = None) -> None:
        self.cfg = cfg
        self.enabled = bool(cfg.enabled)
        self._text_threshold = _TEXT_LEVELS.get(cfg.text_level.upper(), 20)
        self._image_threshold = _IMAGE_LEVELS.get(cfg.image_level.upper(), 20)
        self._fp = None
        self._jsonl = None
        self.run_dir: Path | None = None

        if not self.enabled:
            return

        base = Path(cfg.dir)
        if not base.is_absolute():
            # 相对路径 → 优先用 %LOCALAPPDATA%\dev.ctrlapp\<dir>，避免 PyInstaller
            # onefile 模式下 __file__ 落在 %TEMP%\_MEIxxx，导致历史回放找不到目录。
            # 仅在能找到本地数据目录时使用；否则回退到包根（开发场景）。
            user_base: Path | None = None
            if os.name == "nt":
                local_app = os.environ.get("LOCALAPPDATA")
                if local_app:
                    user_base = Path(local_app) / "dev.ctrlapp"
            elif (home := os.environ.get("HOME")):
                user_base = Path(home) / ".ctrlapp"
            if user_base is not None:
                base = user_base / cfg.dir
            else:
                base = Path(__file__).resolve().parents[2] / cfg.dir
        base.mkdir(parents=True, exist_ok=True)

        self.run_dir = base / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{_slug(instruction)}"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._fp = open(self.run_dir / "run.log", "w", encoding="utf-8")
        self._jsonl = open(self.run_dir / "messages.jsonl", "w", encoding="utf-8")

        self._rotate(base, cfg.keep_runs)
        self._write_text("INFO", f"task: {instruction}")

    # ---------- 文本 ----------
    def _level_pass_text(self, level: str) -> bool:
        return _TEXT_LEVELS.get(level.upper(), 20) >= self._text_threshold

    def _write_text(self, level: str, msg: str) -> None:
        if not self.enabled or self._fp is None:
            return
        if not self._level_pass_text(level):
            return
        ts = time.strftime("%H:%M:%S")
        self._fp.write(f"[{ts}] {level:<7} {msg}\n")
        self._fp.flush()

    def debug(self, msg: str) -> None:
        self._write_text("DEBUG", msg)

    def info(self, msg: str) -> None:
        self._write_text("INFO", msg)

    def warning(self, msg: str) -> None:
        self._write_text("WARNING", msg)

    def error(self, msg: str) -> None:
        self._write_text("ERROR", msg)

    # ---------- JSONL（步骤摘要） ----------
    def step_record(self, record: dict[str, Any]) -> None:
        if not self.enabled or self._jsonl is None:
            return
        if not self._level_pass_text("INFO"):
            return
        self._jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._jsonl.flush()

    # ---------- 图像 ----------
    def _level_pass_image(self, level: str) -> bool:
        return _IMAGE_LEVELS.get(level.upper(), 20) >= self._image_threshold

    def save_image(self, png_bytes: bytes, name: str, *, level: str = "INFO") -> str | None:
        """把 PNG 字节落盘；按 image_format 决定是否转 JPG。返回相对文件名。"""
        if not self.enabled or self.run_dir is None:
            return None
        if not self._level_pass_image(level):
            return None

        fmt = self.cfg.image_format.lower()
        if fmt == "jpg":
            try:
                from PIL import Image
                im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
                fname = f"{name}.jpg"
                im.save(self.run_dir / fname, "JPEG", quality=int(self.cfg.jpg_quality))
            except Exception:  # 容错：JPG 失败回退 PNG
                fname = f"{name}.png"
                (self.run_dir / fname).write_bytes(png_bytes)
        else:
            fname = f"{name}.png"
            (self.run_dir / fname).write_bytes(png_bytes)
        return fname

    # ---------- 生命周期 ----------
    def close(self, status: str = "ok", final_text: str = "") -> None:
        if not self.enabled:
            return
        try:
            self._write_text("INFO", f"status={status}")
            if final_text:
                self._write_text("INFO", f"final: {final_text}")
        finally:
            if self._fp:
                self._fp.close()
                self._fp = None
            if self._jsonl:
                self._jsonl.close()
                self._jsonl = None

    # ---------- 轮转 ----------
    @staticmethod
    def _rotate(base: Path, keep: int) -> None:
        if keep <= 0:
            return
        runs = sorted(
            (p for p in base.iterdir() if p.is_dir()),
            key=lambda p: p.name,
        )
        excess = len(runs) - keep
        for old in runs[:excess]:
            try:
                shutil.rmtree(old, ignore_errors=True)
            except Exception:
                pass

    # ---------- context manager ----------
    def __enter__(self) -> "RunLogger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc is not None:
            self.error(f"{type(exc).__name__}: {exc}")
            self.close(status="error")
        else:
            self.close(status="ok")
