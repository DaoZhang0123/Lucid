"""Taskbar visual monitor: Step 1 diff, Step 2 optional LLM confirm.

This module is intentionally narrow:
- It continuously captures the taskbar middle strip.
- It emits a diff event when visual change exceeds threshold.
- If enabled, it asks a caller-provided confirm callback to decide whether this
  change looks like a new-message signal.

It does not perform icon-anchor matching.
"""
from __future__ import annotations

import ctypes
import io
import json
import math
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import imagehash
import mss
from PIL import Image, ImageChops

from .config import LoggingConfig, VisualNotifyConfig
from .runlog import resolve_logs_root


def get_taskbar_height() -> int:
    """获取 Windows 任务栏的实际高度（像素）。
    
    使用 Windows API 查询任务栏窗口的矩形，从中计算实际高度。
    如果失败，返回默认值 40。
    """
    try:
        # Windows API 常量和类型定义
        RECT = ctypes.c_int * 4  # x0, y0, x1, y1
        
        # 获取任务栏窗口句柄（"Shell_TrayWnd" 是任务栏窗口的类名）
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(ctypes.c_wchar_p("Shell_TrayWnd"), None)
        
        if not hwnd:
            return 40  # 默认高度
        
        # 获取窗口矩形
        rect = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return 40
        
        # rect[0]=left, rect[1]=top, rect[2]=right, rect[3]=bottom
        height = abs(rect[3] - rect[1])
        
        # 任务栏高度通常在 32-64 之间，但也可能更大（例如多行任务栏）
        # 限制在合理范围内：最小 32，最大 200
        return max(32, min(200, height))
    except Exception:
        return 40  # 默认高度


EventSink = Callable[[dict[str, Any]], None]
ConfirmCallback = Callable[[Image.Image, Image.Image | None, dict[str, Any]], dict[str, Any]]
OnConfirmedCallback = Callable[[dict[str, Any]], None]


def _fmt_log_val(v: Any, max_len: int = 5000) -> str:
    try:
        if isinstance(v, (dict, list, tuple)):
            s = json.dumps(v, ensure_ascii=False, default=str)
        else:
            s = str(v)
    except Exception:
        s = repr(v)
    s = s.replace("\n", " ").replace("\r", " ")
    if max_len > 0 and len(s) > max_len:
        s = s[:max_len] + "..."
    return s


@dataclass
class StripFrame:
    image: Image.Image
    rect: dict[str, int]
    ts_ms: int
    recent_path: str | None = None


class TaskbarCaptureStore:
    def __init__(self, visual_cfg: VisualNotifyConfig, logging_cfg: LoggingConfig) -> None:
        self._enabled = bool(visual_cfg.save_screenshots)
        self._recent_keep = max(0, int(visual_cfg.recent_screenshot_keep))
        self._key_keep = max(0, int(visual_cfg.key_screenshot_keep))
        self._fmt = (logging_cfg.image_format or "png").strip().lower()
        if self._fmt not in ("png", "jpg"):
            self._fmt = "png"
        self._jpg_quality = int(logging_cfg.jpg_quality)
        self._seq = 0
        self._lock = threading.Lock()
        self._root = resolve_logs_root(logging_cfg) / "taskbar-monitor"
        self._recent_dir = self._root / "recent"
        self._key_dir = self._root / "key"
        # Learning queue: per-event JSONL of confirmed/rejected results, with
        # focus_crop paths under key/, consumed by the doze worker so the
        # reflector can learn each app's true-vs-false notification signature.
        self._learn_queue_path = self._root / "learn-queue.jsonl"
        self._learn_queue_max = 500
        if self._enabled:
            self._recent_dir.mkdir(parents=True, exist_ok=True)
            self._key_dir.mkdir(parents=True, exist_ok=True)

    def append_learn_record(self, record: dict) -> None:
        """Append one taskbar_notify_{confirmed,rejected} event to the learn
        queue. Keeps at most ``_learn_queue_max`` records (rolling)."""
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, ensure_ascii=False)
            with self._lock:
                # Cheap tail-cap: read existing line count occasionally.
                if self._learn_queue_path.exists():
                    try:
                        with open(self._learn_queue_path, "r", encoding="utf-8") as f:
                            existing = f.readlines()
                    except OSError:
                        existing = []
                    if len(existing) >= self._learn_queue_max:
                        existing = existing[-(self._learn_queue_max - 1):]
                        with open(self._learn_queue_path, "w", encoding="utf-8") as f:
                            f.writelines(existing)
                with open(self._learn_queue_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except OSError:
            pass

    def save_recent(self, frame: StripFrame) -> str | None:
        if not self._enabled or self._recent_keep <= 0:
            return None
        with self._lock:
            path = self._save_image(self._recent_dir, frame.image, f"recent-{frame.ts_ms}")
            self._prune(self._recent_dir, self._recent_keep)
        return str(path) if path is not None else None

    def save_key_event(
        self,
        event_name: str,
        *,
        current: StripFrame,
        previous: StripFrame | None = None,
        meta: dict[str, Any] | None = None,
        focus_crops: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not self._enabled or self._key_keep <= 0:
            return {}
        slug = self._slug(event_name)
        out: dict[str, Any] = {}
        with self._lock:
            current_path = self._save_image(self._key_dir, current.image, f"{slug}-{current.ts_ms}-current")
            if current_path is not None:
                out["current"] = str(current_path)
            if previous is not None:
                prev_path = self._save_image(self._key_dir, previous.image, f"{slug}-{current.ts_ms}-previous")
                if prev_path is not None:
                    out["previous"] = str(prev_path)
            saved_focus: list[dict[str, Any]] = []
            if focus_crops:
                for idx, fc in enumerate(focus_crops, start=1):
                    cur_img = fc.get("current_image")
                    prv_img = fc.get("previous_image")
                    x0 = int(fc.get("x0", 0))
                    x1 = int(fc.get("x1", 0))
                    entry: dict[str, Any] = {"x0": x0, "x1": x1, "index": idx}
                    if isinstance(cur_img, Image.Image):
                        cp = self._save_image(
                            self._key_dir, cur_img,
                            f"{slug}-{current.ts_ms}-focus{idx:02d}-x{x0}-{x1}-current",
                        )
                        if cp is not None:
                            entry["current"] = str(cp)
                    if isinstance(prv_img, Image.Image):
                        pp = self._save_image(
                            self._key_dir, prv_img,
                            f"{slug}-{current.ts_ms}-focus{idx:02d}-x{x0}-{x1}-previous",
                        )
                        if pp is not None:
                            entry["previous"] = str(pp)
                    saved_focus.append(entry)
            if saved_focus:
                out["focus_crops"] = saved_focus
            if meta:
                meta_name = self._next_name(f"{slug}-{current.ts_ms}-meta", "json")
                meta_path = self._key_dir / meta_name
                # meta may include PIL images (in focus_crops); strip them for json
                meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
                out["meta"] = str(meta_path)
            self._prune(self._key_dir, self._key_keep, include_json=True)
        return out

    def _save_image(self, base_dir: Path, image: Image.Image, stem: str) -> Path | None:
        try:
            ext = "jpg" if self._fmt == "jpg" else "png"
            name = self._next_name(stem, ext)
            path = base_dir / name
            if self._fmt == "jpg":
                image.convert("RGB").save(path, "JPEG", quality=self._jpg_quality)
            else:
                image.save(path, "PNG")
            return path
        except Exception:
            return None

    def _next_name(self, stem: str, ext: str) -> str:
        self._seq += 1
        return f"{stem}-{self._seq:06d}.{ext}"

    @staticmethod
    def _slug(text: str) -> str:
        cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in (text or "event"))
        cleaned = "-".join(part for part in cleaned.split("-") if part)
        return cleaned or "event"

    @staticmethod
    def _sort_key(path: Path) -> tuple[float, str]:
        try:
            return (path.stat().st_mtime, path.name)
        except OSError:
            return (0.0, path.name)

    def _prune(self, base_dir: Path, keep: int, *, include_json: bool = False) -> None:
        suffixes = {".png", ".jpg", ".jpeg"}
        if include_json:
            suffixes.add(".json")
        files = [p for p in base_dir.iterdir() if p.is_file() and p.suffix.lower() in suffixes]
        overflow = len(files) - keep
        if overflow <= 0:
            return
        for old in sorted(files, key=self._sort_key)[:overflow]:
            try:
                old.unlink()
            except OSError:
                pass


class TaskbarMonitor:
    def __init__(
        self,
        cfg: VisualNotifyConfig,
        *,
        logging_cfg: LoggingConfig,
        event_sink: EventSink,
        confirm_callback: ConfirmCallback | None = None,
        on_confirmed: OnConfirmedCallback | None = None,
    ) -> None:
        self.cfg = cfg
        self._event_sink = event_sink
        self._confirm_callback = confirm_callback
        self._on_confirmed = on_confirmed
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._confirm_thread: threading.Thread | None = None
        self._last_frame: StripFrame | None = None
        self._last_confirm_ms: int = 0
        # 最近被 LLM 拒绝的 x_projection 段：list of (x0, x1, ts_ms)。
        # 用于在拒绝冷却窗口内跳过覆盖相同区域的 diff，
        # 避免 hover / 聚焦下划线 / 运行状态变化反复触发昂贵的 LLM 调用。
        self._rejected_segments: list[tuple[int, int, int]] = []
        self._rejected_segments_lock = threading.Lock()
        self._capture_store = TaskbarCaptureStore(cfg, logging_cfg)
        # 异步确认队列：(current_image, prev_image, payload_dict)
        self._confirm_queue: queue.Queue[tuple[Image.Image, Image.Image | None, dict[str, Any]]] = queue.Queue(maxsize=50)

        self._configured_strip_height_px = max(40, int(cfg.strip_height_px))
        self._auto_detect_taskbar_height = bool(getattr(cfg, "auto_detect_taskbar_height", True))
        self._detected_taskbar_height_px: int | None = None
        # 自动检测任务栏高度
        if self._auto_detect_taskbar_height:
            detected_h = get_taskbar_height()
            self._detected_taskbar_height_px = detected_h
            self._strip_height_px = detected_h
            self._strip_height_source = "auto_detect"
        else:
            self._strip_height_px = self._configured_strip_height_px
            self._strip_height_source = "config"

        # detector 步骤日志（按天滚动）
        self._detector_log_dir = resolve_logs_root(logging_cfg) / "taskbar-monitor"
        try:
            self._detector_log_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self._detector_log_lock = threading.Lock()
        self._log_step(
            "monitor_init",
            strip_height_px=self._strip_height_px,
            strip_height_source=self._strip_height_source,
            auto_detect=self._auto_detect_taskbar_height,
            detected_px=self._detected_taskbar_height_px,
            configured_px=self._configured_strip_height_px,
            llm_confirm_enabled=bool(self.cfg.llm_confirm_enabled),
            diff_method=self.cfg.diff_method,
            diff_threshold=float(self.cfg.diff_threshold),
        )

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="lucid-taskbar-monitor", daemon=True)
        self._thread.start()
        # 如果启用 LLM 确认，启动确认处理线程
        self._ensure_confirm_worker()
        self._emit({
            "event": "taskbar_monitor_started",
            "strip_height_px": self._strip_height_px,
            "strip_height_source": self._strip_height_source,
            "configured_strip_height_px": self._configured_strip_height_px,
            "auto_detect_taskbar_height": self._auto_detect_taskbar_height,
            "detected_taskbar_height_px": self._detected_taskbar_height_px,
        })

    def _ensure_confirm_worker(self) -> None:
        if not (self.cfg.llm_confirm_enabled and self._confirm_callback):
            return
        if self._confirm_thread and self._confirm_thread.is_alive():
            return
        self._stop.clear()
        self._confirm_thread = threading.Thread(
            target=self._confirm_worker, name="lucid-taskbar-confirm", daemon=True
        )
        self._confirm_thread.start()
        self._log_step("confirm_worker_started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._confirm_thread:
            self._confirm_thread.join(timeout=2)
        self._emit({"event": "taskbar_monitor_stopped"})

    def bump_external_cooldown(
        self,
        reason: str,
        app_candidates: list[str],
        suppress_sec: float,
    ) -> None:
        """Called by an external channel (e.g. the UIA monitor) to suppress
        the next ``suppress_sec`` worth of step-2 LLM calls on the visual
        channel — we already know there's a real notification, no need to pay
        for visual confirmation again."""
        try:
            now_ms = int(time.time() * 1000)
            extra_ms = max(0, int(float(suppress_sec) * 1000))
            current_cooldown_ms = int(
                max(0.0, float(self.cfg.llm_confirm_cooldown_sec)) * 1000
            )
            # Push `_last_confirm_ms` forward enough that
            # `now - _last_confirm_ms < cooldown_ms` stays true for suppress_sec.
            self._last_confirm_ms = now_ms + extra_ms - current_cooldown_ms
            self._emit({
                "event": "taskbar_visual_suppressed_by_external",
                "reason": reason,
                "app_candidates": list(app_candidates or []),
                "suppress_sec": float(suppress_sec),
            })
        except Exception:
            pass

    def _run(self) -> None:
        interval = max(0.5, float(self.cfg.poll_interval_sec))
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:
                self._emit({"event": "taskbar_monitor_error", "message": f"{type(exc).__name__}: {exc}"})
            self._stop.wait(interval)

    def tick_once(self) -> None:
        """Single detection tick, intended for scheduler-driven mode."""
        self._tick()

    def _tick(self) -> None:
        current = self._capture_strip()
        current.recent_path = self._capture_store.save_recent(current)
        prev = self._last_frame
        self._last_frame = current
        if prev is None:
            self._log_step("tick_first_frame", strip_rect=current.rect, recent=current.recent_path)
            return

        diff_score, diff_detail = self._diff_score(prev.image, current.image)
        threshold = float(self.cfg.diff_threshold)
        changed = diff_score >= threshold
        # Only log ticks that actually crossed the threshold — the no-op
        # tick_diff lines (changed=False) ran every ~1s and bloated
        # detector-YYYYMMDD.log past 100 MB/day with no debugging value.
        # When something genuinely changes we still log it (and everything
        # downstream — `taskbar_diff_detected`, LLM confirm, etc. — keeps
        # logging unconditionally via `_emit`).
        if changed:
            self._log_step(
                "tick_diff",
                diff_method=self.cfg.diff_method,
                diff_score=round(float(diff_score), 4),
                diff_threshold=threshold,
                changed=changed,
            )
        if not changed:
            return

        payload = {
            "event": "taskbar_diff_detected",
            "diff_method": self.cfg.diff_method,
            "diff_score": diff_score,
            "diff_threshold": threshold,
            "strip_rect": current.rect,
        }
        if diff_detail:
            payload["diff_detail"] = diff_detail
        if current.recent_path:
            payload["current_capture"] = current.recent_path
        if prev.recent_path:
            payload["previous_capture"] = prev.recent_path
        # 仅在未启用 LLM 确认时，把 diff 阶段当作终态保存 key 事件，
        # 否则交给确认阶段保存最终结果，避免同一次检测产生重复的 key 文件。
        if not (self.cfg.llm_confirm_enabled and self._confirm_callback is not None):
            key_paths = self._capture_store.save_key_event(
                "diff_detected",
                current=current,
                previous=prev,
                meta=payload,
            )
            if key_paths:
                payload["key_captures"] = key_paths
        self._emit(payload)

        if not self.cfg.llm_confirm_enabled or self._confirm_callback is None:
            self._log_step(
                "llm_confirm_skipped_disabled",
                llm_confirm_enabled=bool(self.cfg.llm_confirm_enabled),
                has_callback=self._confirm_callback is not None,
            )
            return

        # 检查冷却时间
        now_ms = int(time.time() * 1000)
        cooldown_ms = int(max(0.0, float(self.cfg.llm_confirm_cooldown_sec)) * 1000)
        if now_ms - self._last_confirm_ms < cooldown_ms:
            self._emit({
                "event": "taskbar_llm_confirm_skipped_cooldown",
                "strip_rect": current.rect,
                "diff_score": diff_score,
            })
            return

        # 段落级拒绝冷却：若本次所有 x_projection 段都落在最近被 LLM 拒绝的
        # 区域里（±pad px，且在 rejected_segment_cooldown_sec 秒内），跳过。
        cur_segments = self._extract_segments(diff_detail)
        if cur_segments and self._all_segments_recently_rejected(cur_segments, now_ms):
            self._emit({
                "event": "taskbar_llm_confirm_skipped_recent_rejection",
                "strip_rect": current.rect,
                "diff_score": diff_score,
                "segments": [{"x0": x0, "x1": x1} for x0, x1 in cur_segments],
            })
            return

        self._last_confirm_ms = now_ms
        # 第一阶段就地裁剪聚焦区域（按 x_projection 的 segments），并落盘到 key/，
        # 让第二阶段（LLM 确认）只需消费已经准备好的图片 + 路径，避免把整幅细长 strip
        # 直接喂给 LLM 导致缩放后细节丢失。
        focus_crops = self._build_focus_crops(current.image, prev.image, diff_detail)
        # 提前把 current / previous / focus_crops 落盘到 key/，让 sidecar 在记录
        # taskbar_llm_confirm_request 时可以直接引用永久路径（recent/ 会被
        # 轮转裁剪，追查时容易丢失）；同时 confirm worker 拿到同一份
        # paths 后不再重复保存。
        pre_key_paths = self._capture_store.save_key_event(
            "llm-confirm-request",
            current=current,
            previous=prev,
            focus_crops=focus_crops,
        )
        # 确保 confirm worker 已启动（调度驱动模式下 start() 不会被调）
        self._ensure_confirm_worker()
        # 将确认请求放入异步队列，不阻塞监控线程
        try:
            self._confirm_queue.put_nowait((
                current.image,
                prev.image,
                {
                    "diff_score": diff_score,
                    "strip_rect": current.rect,
                    "ts_ms": now_ms,
                    "diff_detail": diff_detail,
                    "current": current,
                    "prev": prev,
                    "current_capture": current.recent_path,
                    "previous_capture": prev.recent_path,
                    "focus_crops": focus_crops,
                    "key_captures": pre_key_paths or {},
                },
            ))
            self._emit({
                "event": "taskbar_llm_confirm_queued",
                "strip_rect": current.rect,
                "diff_score": diff_score,
            })
        except queue.Full:
            self._emit({
                "event": "taskbar_llm_confirm_queue_full",
                "strip_rect": current.rect,
                "diff_score": diff_score,
            })

    def _confirm_worker(self) -> None:
        """后台线程：处理异步 LLM 确认请求队列。"""
        while not self._stop.is_set():
            try:
                # 设置超时以便定期检查 _stop 标志
                try:
                    current_img, prev_img, meta = self._confirm_queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                # 调用确认回调
                if self._confirm_callback is None:
                    continue

                try:
                    result = self._confirm_callback(current_img, prev_img, meta)
                except Exception as exc:
                    self._emit({
                        "event": "taskbar_llm_confirm_error",
                        "message": f"{type(exc).__name__}: {exc}",
                    })
                    continue

                # 构建结果事件
                has_new = bool(result.get("has_new_message", False))
                ev = "taskbar_notify_confirmed" if has_new else "taskbar_notify_rejected"

                # 拒绝时把本次 diff 涉及的 x_projection 段记入"最近被拒"列表，
                # 让 _tick 在冷却窗口内跳过覆盖相同区域的新 diff。
                if not has_new:
                    self._record_rejected_segments(meta.get("diff_detail") or {})

                emit_payload = {
                    "event": ev,
                    "source": "visual",
                    "strip_rect": meta.get("strip_rect"),
                    "diff_score": meta.get("diff_score"),
                    "has_new_message": has_new,
                    "app_candidates": result.get("app_candidates") or [],
                    "confidence": float(result.get("confidence", 0.0) or 0.0),
                    "reason": str(result.get("reason", "") or "").strip(),
                    "raw": str(result.get("raw", "") or "")[:1000],
                }
                if meta.get("diff_detail"):
                    emit_payload["diff_detail"] = meta["diff_detail"]

                # 保存关键事件（复用 _tick 阶段已经预先落盘的 key 路径，
                # 不再重复写盘）。没有预存路径时（旧路径/异常路径）才 fallback
                # 到以前的 事后保存逻辑。
                current_frame = meta.get("current")
                prev_frame = meta.get("prev")
                pre_key = meta.get("key_captures") or {}
                if current_frame and isinstance(current_frame, StripFrame):
                    if current_frame.recent_path:
                        emit_payload["current_capture"] = current_frame.recent_path
                    if prev_frame and isinstance(prev_frame, StripFrame) and prev_frame.recent_path:
                        emit_payload["previous_capture"] = prev_frame.recent_path

                    if pre_key:
                        emit_payload["key_captures"] = pre_key
                    else:
                        key_paths = self._capture_store.save_key_event(
                            ev,
                            current=current_frame,
                            previous=prev_frame,
                            meta=emit_payload,
                            focus_crops=meta.get("focus_crops") or [],
                        )
                        if key_paths:
                            emit_payload["key_captures"] = key_paths

                self._emit(emit_payload)
                # Persist a learning record (current+previous focus crops, the
                # LLM step-2 reason, and the ground truth label) so the doze
                # worker can later use it to teach itself each app's true vs
                # false notification visual signature.
                try:
                    learn_key = emit_payload.get("key_captures") or {}
                    learn_focus = []
                    for fc in (learn_key.get("focus_crops") or []):
                        learn_focus.append({
                            "index": int(fc.get("index", 0) or 0),
                            "x0": int(fc.get("x0", 0) or 0),
                            "x1": int(fc.get("x1", 0) or 0),
                            "current": fc.get("current"),
                            "previous": fc.get("previous"),
                        })
                    self._capture_store.append_learn_record({
                        "ts_ms": int(time.time() * 1000),
                        "kind": "confirmed" if has_new else "rejected",
                        "diff_score": meta.get("diff_score"),
                        "app_candidates": list(emit_payload.get("app_candidates") or []),
                        "reason": str(emit_payload.get("reason") or ""),
                        "current_capture": learn_key.get("current") or emit_payload.get("current_capture"),
                        "previous_capture": learn_key.get("previous") or emit_payload.get("previous_capture"),
                        "focus_crops": learn_focus,
                        "processed": False,
                    })
                except Exception:
                    pass
                if has_new and self._on_confirmed is not None:
                    self._on_confirmed(emit_payload)

            except Exception as exc:
                self._emit({
                    "event": "taskbar_confirm_worker_error",
                    "message": f"{type(exc).__name__}: {exc}",
                })
                time.sleep(1)  # 避免快速重复错误

    @staticmethod
    def _extract_segments(diff_detail: dict[str, Any] | None) -> list[tuple[int, int]]:
        """从 diff_detail 中拿到 x_projection segments，归一化成 (x0, x1) 列表。"""
        if not diff_detail:
            return []
        proj = diff_detail.get("projection") or {}
        segs = proj.get("segments") or []
        out: list[tuple[int, int]] = []
        for s in segs:
            try:
                x0 = int(s.get("x0", 0) or 0)
                x1 = int(s.get("x1", 0) or 0)
                if x1 > x0:
                    out.append((x0, x1))
            except Exception:
                continue
        return out

    def _record_rejected_segments(self, diff_detail: dict[str, Any] | None) -> None:
        segs = self._extract_segments(diff_detail)
        if not segs:
            return
        now_ms = int(time.time() * 1000)
        ttl_ms = int(max(0.0, float(getattr(self.cfg, "rejected_segment_cooldown_sec", 30.0))) * 1000)
        with self._rejected_segments_lock:
            for x0, x1 in segs:
                self._rejected_segments.append((x0, x1, now_ms))
            cutoff = now_ms - ttl_ms
            self._rejected_segments = [s for s in self._rejected_segments if s[2] >= cutoff][-64:]

    def _all_segments_recently_rejected(
        self, segments: list[tuple[int, int]], now_ms: int
    ) -> bool:
        if not segments:
            return False
        ttl_ms = int(max(0.0, float(getattr(self.cfg, "rejected_segment_cooldown_sec", 30.0))) * 1000)
        if ttl_ms <= 0:
            return False
        pad = int(max(0, getattr(self.cfg, "rejected_segment_overlap_pad_px", 20) or 0))
        with self._rejected_segments_lock:
            rejected = [s for s in self._rejected_segments if now_ms - s[2] <= ttl_ms]
            self._rejected_segments = rejected[-64:]
            if not rejected:
                return False
            for x0, x1 in segments:
                if not any((x0 >= rx0 - pad and x1 <= rx1 + pad) for rx0, rx1, _ in rejected):
                    return False
            return True

    def _capture_strip(self) -> StripFrame:
        with mss.mss() as sct:
            mon = sct.monitors[0]
            sw = int(mon["width"])
            sh = int(mon["height"])
            left = int(mon["left"])
            top = int(mon["top"])
            strip_h = self._strip_height_px  # 使用自动检测或配置的高度
            left_skip = int(max(0, getattr(self.cfg, "strip_left_skip_px", 0) or 0))
            right_skip = int(max(0, getattr(self.cfg, "strip_right_skip_px", 0) or 0))
            if left_skip > 0 or right_skip > 0:
                # 跳过像素模式：覆盖到屏幕左/右边缘并裁掉两端的跳过区。
                strip_x = left + left_skip
                strip_w = max(80, sw - left_skip - right_skip)
            else:
                center_ratio = min(1.0, max(0.1, float(self.cfg.strip_center_width_ratio)))
                strip_w = int(sw * center_ratio)
                strip_x = left + (sw - strip_w) // 2
            strip_y = top + max(0, sh - strip_h)
            region = {
                "left": strip_x,
                "top": strip_y,
                "width": strip_w,
                "height": strip_h,
            }
            shot = sct.grab(region)
            image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

        return StripFrame(
            image=image,
            rect={"x": region["left"], "y": region["top"], "w": region["width"], "h": region["height"]},
            ts_ms=int(time.time() * 1000),
        )

    def _diff_score(self, prev: Image.Image, curr: Image.Image) -> tuple[float, dict[str, Any]]:
        method = (self.cfg.diff_method or "dhash").strip().lower()
        if prev.size != curr.size:
            curr = curr.resize(prev.size, Image.BILINEAR)

        detail: dict[str, Any] = {"mode": "full_strip"}
        if bool(getattr(self.cfg, "x_projection_enabled", True)):
            seg_detail = self._segment_by_x_projection(prev, curr)
            segments: list[tuple[int, int]] = seg_detail.get("segments") or []
            if segments:
                scores: list[float] = []
                segment_detail: list[dict[str, Any]] = []
                for x0, x1 in segments:
                    p = prev.crop((x0, 0, x1, prev.height))
                    c = curr.crop((x0, 0, x1, curr.height))
                    s = self._diff_score_pair(p, c, method)
                    scores.append(s)
                    segment_detail.append({"x0": int(x0), "x1": int(x1), "score": float(s)})
                agg = str(getattr(self.cfg, "x_projection_score_agg", "max") or "max").strip().lower()
                if agg == "sum_top2":
                    score = float(sum(sorted(scores, reverse=True)[:2])) if scores else 0.0
                else:
                    score = float(max(scores)) if scores else 0.0
                    agg = "max"
                detail = {
                    "mode": "x_projection",
                    "agg": agg,
                    "projection": {
                        "active_col_threshold": seg_detail.get("active_col_threshold", 0),
                        "pixel_threshold": seg_detail.get("pixel_threshold", 0),
                        "segments": segment_detail,
                    },
                }
                return score, detail

        score = self._diff_score_pair(prev, curr, method)
        return score, detail

    def _build_focus_crops(
        self,
        current: Image.Image,
        previous: Image.Image,
        diff_detail: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """根据 x_projection segments 把变化区域裁出来并放大，返回 PIL 图列表。

        每项: {"x0", "x1", "current_image": PIL, "previous_image": PIL}
        """
        out: list[dict[str, Any]] = []
        try:
            projection = (diff_detail or {}).get("projection") or {}
            segments = projection.get("segments") or []
            if not segments or current is None or previous is None:
                return out
            strip_w, strip_h = current.size
            pad_px = 32     # 左右各扩 32px，给模型一些上下文
            zoom = 6        # 放大倍数：48px -> 288px 高，红边框可清晰看见
            max_segments = 4
            for idx, seg in enumerate(segments[:max_segments], start=1):
                try:
                    x0 = int(seg.get("x0", 0)) if isinstance(seg, dict) else int(seg[0])
                    x1 = int(seg.get("x1", 0)) if isinstance(seg, dict) else int(seg[1])
                except Exception:
                    continue
                x0p = max(0, x0 - pad_px)
                x1p = min(strip_w, x1 + pad_px)
                if x1p - x0p < 8:
                    continue
                cur_crop = current.crop((x0p, 0, x1p, strip_h))
                prv_crop = previous.crop((x0p, 0, x1p, strip_h))
                new_size = (max(1, (x1p - x0p) * zoom), max(1, strip_h * zoom))
                cur_zoom = cur_crop.resize(new_size, Image.NEAREST)
                prv_zoom = prv_crop.resize(new_size, Image.NEAREST)
                out.append({
                    "index": idx,
                    "x0": x0p,
                    "x1": x1p,
                    "current_image": cur_zoom,
                    "previous_image": prv_zoom,
                })
        except Exception:
            return out
        return out

    def _diff_score_pair(self, prev: Image.Image, curr: Image.Image, method: str) -> float:
        if method == "pixel":
            diff = ImageChops.difference(prev, curr)
            hist = diff.convert("L").histogram()
            total = float(sum(hist))
            if total <= 0:
                return 0.0
            strong = float(sum(hist[20:]))
            return strong / total

        h1 = imagehash.dhash(prev, hash_size=16)
        h2 = imagehash.dhash(curr, hash_size=16)
        return float(h1 - h2)

    def _segment_by_x_projection(self, prev: Image.Image, curr: Image.Image) -> dict[str, Any]:
        diff_gray = ImageChops.difference(prev, curr).convert("L")
        w, h = diff_gray.size
        if w <= 0 or h <= 0:
            return {"segments": []}

        px_thresh = int(max(1, getattr(self.cfg, "x_projection_pixel_threshold", 20)))
        active_ratio = float(getattr(self.cfg, "x_projection_active_ratio", 0.08) or 0.08)
        active_ratio = min(1.0, max(0.01, active_ratio))
        active_col_threshold = max(1, int(math.ceil(h * active_ratio)))
        min_w = int(max(4, getattr(self.cfg, "x_projection_min_segment_width_px", 20)))
        pad = int(max(0, getattr(self.cfg, "x_projection_pad_px", 4)))
        merge_gap = int(max(0, getattr(self.cfg, "x_projection_merge_gap_px", 8)))
        max_segments = int(max(1, getattr(self.cfg, "x_projection_max_segments", 10)))

        pix = diff_gray.load()
        projection: list[int] = [0] * w
        for x in range(w):
            cnt = 0
            for y in range(h):
                if pix[x, y] >= px_thresh:
                    cnt += 1
            projection[x] = cnt

        runs: list[tuple[int, int]] = []
        start: int | None = None
        for x, cnt in enumerate(projection):
            if cnt >= active_col_threshold:
                if start is None:
                    start = x
            else:
                if start is not None:
                    runs.append((start, x))
                    start = None
        if start is not None:
            runs.append((start, w))

        if not runs:
            return {
                "segments": [],
                "active_col_threshold": active_col_threshold,
                "pixel_threshold": px_thresh,
            }

        merged: list[tuple[int, int]] = []
        cur_l, cur_r = runs[0]
        for l, r in runs[1:]:
            if l - cur_r <= merge_gap:
                cur_r = r
            else:
                merged.append((cur_l, cur_r))
                cur_l, cur_r = l, r
        merged.append((cur_l, cur_r))

        padded: list[tuple[int, int]] = []
        for l, r in merged:
            l2 = max(0, l - pad)
            r2 = min(w, r + pad)
            if r2 - l2 >= min_w:
                padded.append((l2, r2))

        if not padded:
            return {
                "segments": [],
                "active_col_threshold": active_col_threshold,
                "pixel_threshold": px_thresh,
            }

        if len(padded) > max_segments:
            weighted = []
            for seg in padded:
                l, r = seg
                score = sum(projection[l:r])
                weighted.append((score, seg))
            selected = [seg for _score, seg in sorted(weighted, key=lambda item: item[0], reverse=True)[:max_segments]]
            padded = sorted(selected, key=lambda seg: seg[0])

        return {
            "segments": padded,
            "active_col_threshold": active_col_threshold,
            "pixel_threshold": px_thresh,
        }

    def _emit(self, event: dict[str, Any]) -> None:
        payload = {"ts_ms": int(time.time() * 1000), **event}
        try:
            self._event_sink(payload)
        except Exception:
            pass
        # 同步写入 detector 文件日志，便于离线 debug
        ev_name = str(event.get("event") or "event")
        rest = {k: v for k, v in event.items() if k != "event"}
        self._log_step(ev_name, **rest)

    def _log_step(self, step: str, **fields: Any) -> None:
        try:
            day = time.strftime("%Y%m%d")
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            path = self._detector_log_dir / f"detector-{day}.log"
            # 某些字段（如 content_parts/text/raw 等可能很长但调试需要全文）不做截断。
            full_keys = {"content_parts", "text", "raw", "messages"}
            parts = [
                f"{k}={_fmt_log_val(v, max_len=0 if k in full_keys else 500)}"
                for k, v in fields.items()
            ]
            line = f"[{ts}] {step}" + (" " + " ".join(parts) if parts else "") + "\n"
            with self._detector_log_lock:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception:
            pass
