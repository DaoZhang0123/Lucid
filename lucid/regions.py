"""App 区域化坐标库（initialization-time region calibration）。

每个 App 一份 `<user data>/regions/<app-slug>.json`：

```json
{
  "version": 1,
  "app": "vscode",
  "calibrated_at": "2025-..-..",
  "window_size": {"w": 1920, "h": 1080},
  "regions": {
    "activity_bar": {
      "x_pct": 0.012, "y_pct": 0.04,
      "w_pct": 0.025, "h_pct": 0.92,
      "description": "Activity bar (left edge)."
    },
    "primary_sidebar": { ... },
    ...
  }
}
```

`region(app, name)` 在调用时定位 app 的窗口，把 (x_pct, y_pct, w_pct, h_pct) 乘进当前
窗口客户区，返回**屏幕坐标**（含中心点和外接矩形），可以直接喂给 `mouse_move` / `click`。

如果 App 没有 region 文件，``calibrate(app)`` 会用窗口尺寸 + 一份针对该 App 的
启发式 layout（基于 ``DEFAULT_LAYOUTS``）写入一份初始版本；前端 `/regions` 页面
可以让用户手动微调（但本轮不做 UI）。
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import RegionsConfig
from . import launchers as launchers_mod
from .launchers import _slugify  # noqa: WPS437 — internal reuse


# ---------------------------------------------------------------------------
# Default layout heuristics per app (percentage of client area).
# ---------------------------------------------------------------------------

# `(x_pct, y_pct, w_pct, h_pct, description)`
DEFAULT_LAYOUTS: dict[str, dict[str, tuple[float, float, float, float, str]]] = {
    "vscode": {
        "activity_bar":     (0.000, 0.000, 0.030, 1.000, "Activity bar — left vertical strip with file/search/git icons."),
        "primary_sidebar":  (0.030, 0.000, 0.180, 0.940, "File explorer / search / extensions panel."),
        "editor":           (0.210, 0.030, 0.790, 0.700, "Center editor area."),
        "panel_terminal":   (0.210, 0.730, 0.790, 0.240, "Bottom panel (terminal / output / problems)."),
        "status_bar":       (0.000, 0.970, 1.000, 0.030, "Bottom status bar."),
        "tab_bar":          (0.210, 0.000, 0.790, 0.030, "Editor tab bar at top."),
    },
    "wechat": {
        "left_nav":         (0.000, 0.000, 0.060, 1.000, "Left vertical icon column (chats / contacts / favourites)."),
        "chat_list":        (0.060, 0.060, 0.220, 0.940, "Middle column with chat conversation list."),
        "chat_view":        (0.280, 0.000, 0.720, 0.620, "Right side — current chat history view."),
        "input_box":        (0.280, 0.700, 0.720, 0.270, "Bottom input box where you type messages."),
        "input_toolbar":    (0.280, 0.620, 0.720, 0.060, "Toolbar above input (emoji, file, screenshot, history)."),
        "search":           (0.060, 0.000, 0.220, 0.060, "Search box at the top of the chat list."),
    },
    "outlook": {
        "ribbon":           (0.000, 0.000, 1.000, 0.140, "Top ribbon (Home / Send-Receive / Folder ...)."),
        "folder_pane":      (0.000, 0.140, 0.180, 0.830, "Left folder list (Inbox / Sent / Drafts ...)."),
        "message_list":     (0.180, 0.140, 0.330, 0.830, "Middle message list."),
        "reading_pane":     (0.510, 0.140, 0.490, 0.830, "Right reading pane."),
        "status_bar":       (0.000, 0.970, 1.000, 0.030, "Bottom status bar."),
    },
    "explorer": {
        "address_bar":      (0.000, 0.000, 1.000, 0.060, "Top breadcrumb / address bar."),
        "navigation_pane":  (0.000, 0.060, 0.220, 0.910, "Left navigation tree."),
        "content_pane":     (0.220, 0.060, 0.780, 0.910, "Right file/folder list."),
        "status_bar":       (0.000, 0.970, 1.000, 0.030, "Bottom status bar."),
    },
    "chrome": {
        "tab_strip":        (0.000, 0.000, 1.000, 0.045, "Tab strip at very top."),
        "address_bar":      (0.000, 0.045, 1.000, 0.045, "Omnibox / address bar."),
        "bookmark_bar":     (0.000, 0.090, 1.000, 0.030, "Bookmark bar (if enabled)."),
        "viewport":         (0.000, 0.120, 1.000, 0.880, "Page viewport area."),
    },
    "edge":   None,  # alias to chrome
}


def _resolve_layout(slug: str) -> dict[str, tuple[float, float, float, float, str]] | None:
    layout = DEFAULT_LAYOUTS.get(slug)
    if layout is None:
        # alias resolution
        if slug == "edge":
            return DEFAULT_LAYOUTS.get("chrome")
        return None
    return layout


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _user_data_dir() -> Path:
    return Path.home() / ".lucid"


def regions_dir(cfg: RegionsConfig) -> Path:
    p = Path(cfg.dir)
    if p.is_absolute():
        return p
    return _user_data_dir() / cfg.dir


def regions_path(cfg: RegionsConfig, app: str) -> Path:
    return regions_dir(cfg) / f"{_slugify(app)}.json"


def load_app_regions(cfg: RegionsConfig, app: str) -> dict[str, Any] | None:
    p = regions_path(cfg, app)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_app_regions(cfg: RegionsConfig, app: str, data: dict[str, Any]) -> Path:
    p = regions_path(cfg, app)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def list_apps_with_regions(cfg: RegionsConfig) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    d = regions_dir(cfg)
    if d.is_dir():
        for f in sorted(d.glob("*.json")):
            slug = f.stem
            seen.add(slug)
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            out.append({
                "slug": slug,
                "regions": list((data.get("regions") or {}).keys()),
                "calibrated_at": data.get("calibrated_at"),
                "file": str(f),
                "source": "user",
            })
    for slug, layout in DEFAULT_LAYOUTS.items():
        if slug in seen or layout is None:
            continue
        out.append({
            "slug": slug,
            "regions": list(layout.keys()),
            "calibrated_at": None,
            "file": str(regions_path(cfg, slug)),
            "source": "default",
        })
    return out


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


def calibrate(cfg: RegionsConfig, launcher_cfg, app: str) -> dict[str, Any]:
    """Write a region file for ``app`` based on its currently visible window.

    Strategy resolution order (best-effort, falls back silently):

    1. **UIA**: if ``lucid.apps.<slug>`` exports ``REGIONS_UIA_SPEC``, ask
       Windows UI Automation for each named element's BoundingRectangle and
       convert to (x_pct, y_pct, w_pct, h_pct) of the client area. Misses
       (control not found / UIA unavailable) fall through to the next
       strategy for the same region name.
    2. **Percent heuristic**: ``DEFAULT_LAYOUTS[slug]`` provides static
       fractional rects per region — equivalent to the v0 behaviour, used
       both as standalone fallback and to cover regions the UIA spec
       didn't enumerate.

    The merged result records the resolution method per region in
    ``regions[name]["resolved_by"]`` ("uia" / "percent") and the overall
    ``calibration_strategy`` field is "uia" if **any** region was resolved by
    UIA, else "percent".
    """
    slug = _slugify(app)
    layout = _resolve_layout(slug)
    uia_spec = _load_uia_spec(slug)
    if layout is None and not uia_spec:
        return {"ok": False, "message": f"no default layout or UIA spec for {slug!r}; manual calibration required"}
    win = launchers_mod.find_app_window(launcher_cfg, slug)
    w_h = (1920, 1080)
    title = ""
    client_origin = (0, 0)
    if win is not None and sys.platform == "win32":
        try:
            cx, cy, cw, ch = _client_to_screen(win.hwnd)
            w_h = (cw, ch)
            client_origin = (cx, cy)
            title = win.title
        except Exception:
            pass

    # Try UIA first per region name.
    uia_resolved: dict[str, dict[str, Any]] = {}
    if uia_spec and win is not None and sys.platform == "win32":
        # Bring window to foreground so UIA's accessible tree is up to date
        # (some apps lazily build the tree only when focused).
        try:
            launchers_mod._force_foreground(win.hwnd)  # noqa: WPS437
            time.sleep(0.10)
        except Exception:
            pass
        try:
            from . import uia as _uia
        except Exception:
            _uia = None  # type: ignore[assignment]
        if _uia is not None:
            cw = max(1, w_h[0])
            ch = max(1, w_h[1])
            ox, oy = client_origin
            for region_name, spec in uia_spec.items():
                rect = None
                aid = spec.get("automation_id")
                names = spec.get("name_candidates") or []
                # AutomationId first when supplied — most stable across UI locales.
                if aid:
                    rect = _uia.find_first_by_automation_id(int(win.hwnd), str(aid))
                if rect is None and names:
                    rect = _uia.find_first_by_name(int(win.hwnd), list(names))
                if rect is None:
                    continue
                left, top, right, bottom = rect
                # Convert screen-space rect to client-relative percentages.
                x_pct = max(0.0, min(1.0, (left - ox) / cw))
                y_pct = max(0.0, min(1.0, (top - oy) / ch))
                w_pct = max(0.001, min(1.0, (right - left) / cw))
                h_pct = max(0.001, min(1.0, (bottom - top) / ch))
                hint: dict[str, Any] = {}
                if aid:
                    hint["automation_id"] = aid
                if names:
                    hint["name_candidates"] = list(names)
                uia_resolved[region_name] = {
                    "x_pct": round(x_pct, 4),
                    "y_pct": round(y_pct, 4),
                    "w_pct": round(w_pct, 4),
                    "h_pct": round(h_pct, 4),
                    "description": spec.get("description") or "",
                    "resolved_by": "uia",
                    "uia_hint": hint,
                }

    # Merge UIA hits with percent-heuristic layout (UIA wins).
    regions: dict[str, dict[str, Any]] = {}
    if layout is not None:
        for name, spec in layout.items():
            regions[name] = {
                "x_pct": round(spec[0], 4),
                "y_pct": round(spec[1], 4),
                "w_pct": round(spec[2], 4),
                "h_pct": round(spec[3], 4),
                "description": spec[4],
                "resolved_by": "percent",
            }
    regions.update(uia_resolved)

    strategy = "uia" if uia_resolved else "percent"
    data = {
        "version": 2,
        "app": slug,
        "calibrated_at": datetime.now().isoformat(timespec="seconds"),
        "calibration_strategy": strategy,
        "window_size": {"w": w_h[0], "h": w_h[1]},
        "window_title_sample": title,
        "regions": regions,
    }
    p = save_app_regions(cfg, slug, data)
    return {
        "ok": True,
        "slug": slug,
        "regions": list(regions.keys()),
        "uia_resolved": list(uia_resolved.keys()),
        "calibration_strategy": strategy,
        "file": str(p),
        "window_size": data["window_size"],
    }


def _load_uia_spec(slug: str) -> dict[str, dict[str, Any]] | None:
    """Return ``REGIONS_UIA_SPEC`` from ``lucid.apps.<slug>`` if defined."""
    try:
        import importlib

        mod = importlib.import_module(f"lucid.apps.{slug}")
    except Exception:
        return None
    spec = getattr(mod, "REGIONS_UIA_SPEC", None)
    if not isinstance(spec, dict) or not spec:
        return None
    return spec


# v2.1 — Signature drift detection. The cached percentages are only meaningful
# if the live client area is roughly the same shape & size as at calibration
# time. If the user dragged a sidebar way out, switched to a tablet-mode
# layout, or moved the window across DPI boundaries, the percentages no longer
# point at the right control. Rather than silently returning a wrong rect,
# refuse and tell the agent to recalibrate.
_DRIFT_RATIO_THRESHOLD = 0.25  # >25% relative size change on either edge


def _signature_drift_message(data: dict[str, Any], cw: int, ch: int) -> str | None:
    sig = data.get("window_size") or {}
    try:
        sw = int(sig.get("w") or 0)
        sh = int(sig.get("h") or 0)
    except Exception:
        sw = sh = 0
    if sw <= 0 or sh <= 0 or cw <= 0 or ch <= 0:
        return None  # missing baseline → don't block
    dw = abs(cw - sw) / sw
    dh = abs(ch - sh) / sh
    if dw <= _DRIFT_RATIO_THRESHOLD and dh <= _DRIFT_RATIO_THRESHOLD:
        return None
    slug = data.get("app") or "<app>"
    return (
        f"window for {slug!r} resized since calibration "
        f"(was {sw}x{sh}, now {cw}x{ch} — drift "
        f"{int(max(dw, dh) * 100)}%); call regions_calibrate(app={slug!r}) "
        f"to refresh the percentages, or pick coordinates from a fresh screenshot."
    )


# ---------------------------------------------------------------------------
# Lookup at runtime
# ---------------------------------------------------------------------------


@dataclass
class ResolvedRegion:
    app: str
    name: str
    description: str
    screen_x: int
    screen_y: int
    screen_w: int
    screen_h: int
    center_x: int
    center_y: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "app": self.app,
            "region": self.name,
            "description": self.description,
            "screen": {"x": self.screen_x, "y": self.screen_y, "w": self.screen_w, "h": self.screen_h},
            "center": {"x": self.center_x, "y": self.center_y},
        }


def _client_to_screen(hwnd: int) -> tuple[int, int, int, int]:
    """Return (left, top, width, height) of the client area in **screen** coords."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    rect = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    w = max(1, rect.right - rect.left)
    h = max(1, rect.bottom - rect.top)
    return int(pt.x), int(pt.y), w, h


def region(
    cfg: RegionsConfig,
    launcher_cfg,
    app: str,
    name: str,
    *,
    window_index: int = 0,
) -> ResolvedRegion | dict[str, Any]:
    """Resolve a named region to **screen** coordinates.

    Returns a ``ResolvedRegion`` on success, or an ``{ok: False, message}`` dict on failure.

    ``window_index`` (v3.4) selects which matching window to resolve against when
    an app has multiple visible windows (e.g. Teams chat pop-outs). Default 0
    (main window). If the index is out of range an error dict is returned.
    """
    slug = _slugify(app)
    data = load_app_regions(cfg, slug)
    if data is None:
        # try to calibrate from defaults on the fly
        result = calibrate(cfg, launcher_cfg, slug)
        if not result.get("ok"):
            return {"ok": False, "message": result.get("message", f"no region file for {slug!r}")}
        data = load_app_regions(cfg, slug) or {}
    regions = data.get("regions") or {}
    spec = regions.get(name)
    if spec is None:
        return {
            "ok": False,
            "message": f"region {name!r} not found in {slug!r}; available: {sorted(regions.keys())}",
        }
    if sys.platform != "win32":
        return {"ok": False, "message": f"window for {slug!r} not currently visible; launch_app first"}
    wins = launchers_mod.find_app_windows(launcher_cfg, slug)
    if not wins:
        return {"ok": False, "message": f"window for {slug!r} not currently visible; launch_app first"}
    if window_index < 0 or window_index >= len(wins):
        titles = [w.title for w in wins]
        return {
            "ok": False,
            "message": (
                f"window_index={window_index} out of range for {slug!r} "
                f"(found {len(wins)} window(s): {titles}); pass 0..{len(wins) - 1}"
            ),
        }
    win = wins[window_index]
    # Make sure foreground (some apps use overlays that change layout when bg)
    launchers_mod._force_foreground(win.hwnd)  # noqa: WPS437
    time.sleep(0.05)
    left, top, cw, ch = _client_to_screen(win.hwnd)
    # Signature drift: if the live client area is dramatically different from
    # the size at calibration time, the cached percentages are likely wrong.
    drift = _signature_drift_message(data, cw, ch)
    if drift:
        return {"ok": False, "message": drift, "slug": slug, "name": name}
    x = int(round(left + cw * float(spec.get("x_pct", 0))))
    y = int(round(top + ch * float(spec.get("y_pct", 0))))
    w = max(1, int(round(cw * float(spec.get("w_pct", 0)))))
    h = max(1, int(round(ch * float(spec.get("h_pct", 0)))))
    return ResolvedRegion(
        app=slug,
        name=name,
        description=str(spec.get("description") or ""),
        screen_x=x,
        screen_y=y,
        screen_w=w,
        screen_h=h,
        center_x=x + w // 2,
        center_y=y + h // 2,
    )


def regions_for_prompt(cfg: RegionsConfig) -> str:
    """One-line catalog of (app -> region names) for the system prompt."""
    items = list_apps_with_regions(cfg)
    if not items:
        return ""
    lines = []
    for it in items:
        names = ", ".join(it.get("regions") or [])
        if names:
            lines.append(f"  - `{it['slug']}`: {names}")
    if not lines:
        return ""
    return (
        "\n## Available `region(app, name)` lookups\n"
        "Use `region(app=\"<slug>\", name=\"<region>\")` to convert a known UI region of an app's window into "
        "**screen coordinates** (returns center + bounding rect). The app's window must be focusable. Use this "
        "instead of guessing pixel coordinates from a screenshot. "
        "Pass `window_index=N` (default 0) to target a specific window when an app has multiple visible "
        "windows (e.g. Teams chat pop-outs).\n"
        + "\n".join(lines)
        + "\n"
    )


# ---------------------------------------------------------------------------
# v3.3 — Background auto-recalibration (called from DozeWorker idle pass).
# ---------------------------------------------------------------------------


def auto_recalibrate_pass(cfg: RegionsConfig, launcher_cfg) -> dict[str, Any]:
    """Scan all calibrated apps; for each one whose window is currently visible
    AND whose live client area drifted past ``_DRIFT_RATIO_THRESHOLD``, run a
    fresh ``calibrate(...)`` so the cached percentages stay accurate.

    Returns a summary dict ``{ok, scanned, recalibrated, skipped, errors}``.
    Caller (DozeWorker) is responsible for the OS-level "user idle" gating —
    this function does NOT check input idleness; it just does the work.
    """
    if sys.platform != "win32":
        return {"ok": False, "scanned": 0, "recalibrated": [], "skipped": [], "errors": ["non-win32"]}
    items = list_apps_with_regions(cfg)
    recalibrated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[str] = []
    for it in items:
        slug = it.get("slug") or ""
        if not slug:
            continue
        try:
            data = load_app_regions(cfg, slug)
            if not data:
                continue
            win = launchers_mod.find_app_window(launcher_cfg, slug)
            if win is None:
                skipped.append({"slug": slug, "reason": "window not visible"})
                continue
            try:
                _left, _top, cw, ch = _client_to_screen(win.hwnd)
            except Exception as exc:
                errors.append(f"{slug}: client-rect: {type(exc).__name__}: {exc}")
                continue
            if _signature_drift_message(data, cw, ch) is None:
                skipped.append({"slug": slug, "reason": "no drift"})
                continue
            # Drifted → recalibrate. Note: ``calibrate`` calls _force_foreground
            # which briefly raises the window. The DozeWorker only invokes us
            # when the OS reports the user has been idle for a while, so this
            # is acceptable.
            result = calibrate(cfg, launcher_cfg, slug)
            if result.get("ok"):
                recalibrated.append({
                    "slug": slug,
                    "old_size": data.get("window_size"),
                    "new_size": result.get("window_size"),
                })
            else:
                errors.append(f"{slug}: calibrate: {result.get('message', '?')}")
        except Exception as exc:  # never crash the doze loop
            errors.append(f"{slug}: {type(exc).__name__}: {exc}")
    return {
        "ok": True,
        "scanned": len(items),
        "recalibrated": recalibrated,
        "skipped": skipped,
        "errors": errors,
    }
