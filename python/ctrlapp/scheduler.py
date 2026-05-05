"""定时任务调度器 schedules.json + 后台 minute tick。

设计取舍：避免引入 cron / apscheduler 依赖，自己写 minute 级 tick。支持三类触发：

* ``hourly``    : 每小时的第 ``minute`` 分钟跑一次
* ``daily``     : 每天 ``time`` (HH:MM) 跑一次
* ``weekly``    : 每周 ``weekday`` (0=周一..6=周日) 的 ``time`` 跑一次

另外每个调度项可携带一个 ``constraints`` 字段（限制项可叠加，取交集）：

* ``hours``        : list[int]，0..23 中允许起跳的小时（缺省等同未启用）
* ``weekdays``     : list[int]，0=周一..6=周日 中允许的星期几
* ``date_start_ms``: 起始日期 UTC 毫秒时间戳（0 = 不限）
* ``date_end_ms``  : 截止日期 UTC 毫秒时间戳（0 = 不限）

到点时若任一限制项不满足，本次则跳过不触发。hours / weekdays 按 ``spec.tz`` （未设则本机）计算。

调度器线程 60 秒醒一次（启动时立刻 tick 一次，可命中错过窗口）。任务到点时，
通过传入的 ``trigger`` 回调启动一次新任务（``trigger`` 由 sidecar 注入，内部
会 ``thread_new + start_task``）。

下次执行时刻 (``next_ms``) 持久化到 schedules.json，重启后能续上。"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Python <3.9
    ZoneInfo = None  # type: ignore
    ZoneInfoNotFoundError = Exception  # type: ignore

_BASENAME = "schedules.json"
_TICK_SEC = 60


def _resolve_tz(name: str | None):
    """返回 tzinfo 或 None（None 代表本机时间）。``name`` 为空 / 'local' / 'system' 也返回 None。"""
    if not name:
        return None
    n = name.strip().lower()
    if n in ("", "local", "system", "native"):
        return None
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as e:
        raise ValueError(f"unknown timezone: {name!r}") from e


def _now_in(tz) -> datetime:
    """读当前时间。tz=None 返回 naive 本机时间；tz 非空返回该时区的 aware datetime。"""
    if tz is None:
        return datetime.now()
    return datetime.now(tz=tz)


def _store_path() -> Path:
    if os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            return Path(local_app) / "dev.ctrlapp" / _BASENAME
    home = os.environ.get("HOME")
    if home:
        return Path(home) / ".ctrlapp" / _BASENAME
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


def _validate(spec: dict[str, Any]) -> None:
    kind = spec.get("kind")
    if kind == "hourly":
        m = int(spec.get("minute", 0))
        if not (0 <= m < 60):
            raise ValueError("minute must be 0..59")
    elif kind == "daily":
        _ = _parse_hhmm(spec.get("time", ""))
    elif kind == "weekly":
        wd = int(spec.get("weekday", -1))
        if wd < 0 or wd > 6:
            raise ValueError("weekday must be 0..6 (0=Mon)")
        _ = _parse_hhmm(spec.get("time", ""))
    else:
        raise ValueError(f"unknown kind: {kind!r}")
    _ = _resolve_tz(spec.get("tz"))


def _parse_hhmm(s: str) -> tuple[int, int]:
    parts = (s or "").split(":")
    if len(parts) != 2:
        raise ValueError(f"time must be HH:MM, got {s!r}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"invalid time: {s!r}")
    return h, m


def _next_run(spec: dict[str, Any], now: datetime) -> datetime:
    """根据 spec 算出 ``now`` 之后的下一次执行时刻。

    返回值总是 **与 ``now`` 同类型**（均 naive 或均 aware）以便上层调用者统一处理。内部处理如下：
      * spec.tz 空 / 'local' → 用本机时间 (naive)
      * spec.tz=IANA 名 → 在该时区里算 HH:MM 的下一次发生，转成本地 naive 返回。
    """
    kind = spec["kind"]
    tz = _resolve_tz(spec.get("tz"))
    if tz is None:
        base = now
    else:
        # 把 now 转成目标时区的 aware
        if now.tzinfo is None:
            base = now.astimezone().astimezone(tz)
        else:
            base = now.astimezone(tz)
    if kind == "hourly":
        m = int(spec["minute"])
        cand = base.replace(minute=m, second=0, microsecond=0)
        if cand <= base:
            cand += timedelta(hours=1)
    elif kind == "daily":
        h, m = _parse_hhmm(spec["time"])
        cand = base.replace(hour=h, minute=m, second=0, microsecond=0)
        if cand <= base:
            cand += timedelta(days=1)
    elif kind == "weekly":
        h, m = _parse_hhmm(spec["time"])
        target_wd = int(spec["weekday"])  # 0=Mon
        cand = base.replace(hour=h, minute=m, second=0, microsecond=0)
        delta_days = (target_wd - cand.weekday()) % 7
        cand += timedelta(days=delta_days)
        if cand <= base:
            cand += timedelta(days=7)
    else:
        raise ValueError(f"unknown kind: {kind!r}")
    # 转回与 now 同类型。
    if now.tzinfo is None and cand.tzinfo is not None:
        # 转成本地 naive
        return cand.astimezone().replace(tzinfo=None)
    return cand


def list_schedules() -> list[dict[str, Any]]:
    return _load()


def _validate_constraints(c: dict[str, Any] | None) -> dict[str, Any]:
    if not c:
        return {}
    out: dict[str, Any] = {}
    if "hours" in c and c["hours"] is not None:
        hs = sorted({int(x) for x in c["hours"]})
        for h in hs:
            if not (0 <= h < 24):
                raise ValueError(f"hours item out of range: {h}")
        # 空列表 = 永远不足，不允许。
        if not hs:
            raise ValueError("hours constraint must allow at least one hour")
        # 24 个全选 等同未启用，不存。
        if len(hs) < 24:
            out["hours"] = hs
    if "weekdays" in c and c["weekdays"] is not None:
        ws = sorted({int(x) for x in c["weekdays"]})
        for w in ws:
            if not (0 <= w < 7):
                raise ValueError(f"weekdays item out of range: {w}")
        if not ws:
            raise ValueError("weekdays constraint must allow at least one weekday")
        if len(ws) < 7:
            out["weekdays"] = ws
    ds = int(c.get("date_start_ms") or 0)
    de = int(c.get("date_end_ms") or 0)
    if ds and de and de < ds:
        raise ValueError("date_end_ms must be >= date_start_ms")
    if ds:
        out["date_start_ms"] = ds
    if de:
        out["date_end_ms"] = de
    return out


def _allowed(constraints: dict[str, Any] | None, now: datetime) -> bool:
    """检查调度项在当前时刻是否满足所有限制项。存不在的限制项一律视为允许。"""
    if not constraints:
        return True
    now_ms = int(now.timestamp() * 1000)
    ds = int(constraints.get("date_start_ms") or 0)
    de = int(constraints.get("date_end_ms") or 0)
    if ds and now_ms < ds:
        return False
    if de and now_ms > de:
        return False
    hours = constraints.get("hours")
    weekdays = constraints.get("weekdays")
    if hours or weekdays:
        # 使用本地时间读小时 / 星期（hourly/daily/weekly 的 spec.tz 已作用于 next_ms。这里是门控判断、
        # 用本机时区读足够与用户期望一致）。
        local = now if now.tzinfo is None else now.astimezone()
        if hours and int(local.hour) not in set(hours):
            return False
        if weekdays and int(local.weekday()) not in set(weekdays):
            return False
    return True


def add_schedule(name: str, instruction: str, spec: dict[str, Any],
                 autonomy: str = "confirm_critical", max_steps: int = 25,
                 enabled: bool = True,
                 constraints: dict[str, Any] | None = None) -> dict[str, Any]:
    name = (name or "").strip() or "未命名计划"
    instruction = (instruction or "").strip()
    if not instruction:
        raise ValueError("instruction required")
    if autonomy not in ("full", "confirm_critical", "confirm_each"):
        raise ValueError(f"invalid autonomy: {autonomy!r}")
    _validate(spec)
    cons = _validate_constraints(constraints)
    items = _load()
    item = {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "instruction": instruction,
        "spec": spec,
        "autonomy": autonomy,
        "max_steps": int(max_steps),
        "enabled": bool(enabled),
        "created_ms": int(time.time() * 1000),
        "next_ms": int(_next_run(spec, datetime.now()).timestamp() * 1000),
        "last_run_ms": 0,
        "constraints": cons,
    }
    items.append(item)
    _save(items)
    return item


def update_schedule(sid: str, **fields: Any) -> dict[str, Any] | None:
    items = _load()
    for it in items:
        if it["id"] == sid:
            if "spec" in fields and fields["spec"]:
                _validate(fields["spec"])
                it["spec"] = fields["spec"]
                it["next_ms"] = int(_next_run(it["spec"], datetime.now()).timestamp() * 1000)
            for k in ("name", "instruction", "autonomy", "max_steps", "enabled"):
                if k in fields and fields[k] is not None:
                    it[k] = fields[k]
            # constraints 传 None 表示不变，传 {} / dict 表示覆盖。
            if "constraints" in fields and fields["constraints"] is not None:
                it["constraints"] = _validate_constraints(fields["constraints"])
            it["updated_ms"] = int(time.time() * 1000)
            _save(items)
            return it
    return None


def delete_schedule(sid: str) -> bool:
    items = _load()
    new = [it for it in items if it["id"] != sid]
    if len(new) == len(items):
        return False
    _save(new)
    return True


# ---------------- runtime ----------------

class Scheduler:
    """常驻后台线程，60s tick 一次。`trigger(item)` 由 sidecar 注入。"""

    def __init__(self, trigger: Callable[[dict[str, Any]], None]) -> None:
        self._trigger = trigger
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ctrlapp-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        # 启动时立刻 tick 一次（处理停机错过的任务）。
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                pass
            self._stop.wait(_TICK_SEC)

    def _tick(self) -> None:
        items = _load()
        now = datetime.now()
        now_ms = int(now.timestamp() * 1000)
        dirty = False
        for it in items:
            if not it.get("enabled"):
                continue
            next_ms = int(it.get("next_ms", 0))
            if next_ms == 0:
                # 老条目兜底
                it["next_ms"] = int(_next_run(it["spec"], now).timestamp() * 1000)
                dirty = True
                continue
            if now_ms < next_ms:
                continue
            # 限制项门控：任一不满足 → 本次不触发，但 next_ms 仍推进到下一调度点。
            constraints = it.get("constraints") or {}
            if not _allowed(constraints, now):
                it["next_ms"] = int(_next_run(it["spec"], now).timestamp() * 1000)
                dirty = True
                continue
            # 触发
            try:
                self._trigger(it)
            except Exception:
                pass
            it["last_run_ms"] = now_ms
            it["next_ms"] = int(_next_run(it["spec"], now).timestamp() * 1000)
            dirty = True
        if dirty:
            _save(items)
