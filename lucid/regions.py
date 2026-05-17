"""Per-app UI region resolver — live UI Automation query on every call.

This module exposes one runtime entry point:

    region(cfg, launcher_cfg, app, name) -> ResolvedRegion | {ok=False, ...}

For each call we:
  1. Find the (currently-visible) main window of ``app`` via the launcher
     registry's window-enumeration.
  2. If ``name == "__main_window"``: return the live client rect of the
     window (no UIA needed — ``GetClientRect`` + ``ClientToScreen``).
  3. Otherwise: look up ``REGIONS_UIA_SPEC[name]`` defined inside
     ``lucid.apps.<slug>`` and ask Windows UI Automation for that element's
     ``BoundingRectangle`` **right now**. No cache, no percentages, no
     calibration step.

Rationale (2026-05-17 rewrite). The previous version cached a percent
``(x_pct, y_pct, w_pct, h_pct)`` per region in
``~/.lucid/regions/<slug>.json`` after a single UIA snapshot. That:

  * was wrong on any user whose layout differed from the calibration host
    (sidebar widths, ribbon collapse state, call-bar overlays, DPI scaling
    boundaries) — the cached numbers looked authoritative but pointed at
    empty space or the wrong control;
  * silently rotted across app updates that moved controls (the WeChat 4.x
    update moved the input toolbar from y≈0.62 to y≈0.91 — every
    ``region(wechat, input_toolbar)`` returned a coord ~200 px above the
    real icons until someone noticed);
  * still cost ~50–100 ms per call (``_force_foreground`` + signature-drift
    check + JSON read) — barely faster than just doing UIA live.

A live UIA query costs ~50–200 ms per call (one tree walk under the app's
HWND). The model invokes ``region(...)`` roughly once or twice per task, so
the absolute time cost is negligible compared to the cost of clicking the
wrong place and looping. Apps that **don't** expose a useful UIA tree
(Qt-native: WeChat; Chromium-without-AutomationId: most of Chrome / Edge
chrome) simply have **no** entry in ``REGIONS_UIA_SPEC`` — those apps fall
back to the vision path (L2 + colour-grid + two-phase click preview), which
is more honest than a fake-precise percent table.
"""
from __future__ import annotations

import importlib
import pkgutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import RegionsConfig
from . import launchers as launchers_mod
from .launchers import _slugify  # noqa: WPS437 — internal reuse


# ---------------------------------------------------------------------------
# Catalog discovery — enumerate ``lucid.apps.<slug>.REGIONS_UIA_SPEC``.
# ---------------------------------------------------------------------------


def _load_uia_spec(slug: str) -> dict[str, dict[str, Any]] | None:
    """Return ``REGIONS_UIA_SPEC`` from ``lucid.apps.<slug>`` if defined."""
    try:
        mod = importlib.import_module(f"lucid.apps.{slug}")
    except Exception:
        return None
    spec = getattr(mod, "REGIONS_UIA_SPEC", None)
    if not isinstance(spec, dict) or not spec:
        return None
    return spec


def _all_app_slugs_with_specs() -> list[tuple[str, dict[str, dict[str, Any]]]]:
    """Walk ``lucid.apps`` and yield ``(slug, REGIONS_UIA_SPEC)`` for every
    app module that defines a non-empty spec.
    """
    out: list[tuple[str, dict[str, dict[str, Any]]]] = []
    try:
        import lucid.apps as _apps_pkg  # noqa: WPS433 — runtime import OK
        for mod_info in pkgutil.iter_modules(_apps_pkg.__path__):
            slug = mod_info.name
            spec = _load_uia_spec(slug)
            if spec:
                out.append((slug, spec))
    except Exception:
        pass
    out.sort(key=lambda it: it[0])
    return out


# ---------------------------------------------------------------------------
# Live runtime lookup.
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
            "screen": {
                "x": self.screen_x,
                "y": self.screen_y,
                "w": self.screen_w,
                "h": self.screen_h,
            },
            "center": {"x": self.center_x, "y": self.center_y},
        }


def _client_to_screen(hwnd: int) -> tuple[int, int, int, int]:
    """Return ``(left, top, width, height)`` of the client area in screen coords."""
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


def _resolve_main_window(hwnd: int, slug: str, name: str) -> ResolvedRegion:
    left, top, w, h = _client_to_screen(hwnd)
    return ResolvedRegion(
        app=slug,
        name=name,
        description="The full client area of the app's main window (live GetClientRect).",
        screen_x=left,
        screen_y=top,
        screen_w=w,
        screen_h=h,
        center_x=left + w // 2,
        center_y=top + h // 2,
    )


def _query_uia(hwnd: int, spec_entry: dict[str, Any]) -> tuple[int, int, int, int] | None:
    """Run one UIA query (AutomationId first, then Name candidates).

    Returns the element's ``(left, top, right, bottom)`` in screen coords, or
    ``None`` if no element matched (or UIA unavailable on this platform).
    """
    try:
        from . import uia as _uia
    except Exception:
        return None
    aid = spec_entry.get("automation_id")
    if aid:
        rect = _uia.find_first_by_automation_id(int(hwnd), str(aid))
        if rect is not None:
            return rect
    names = spec_entry.get("name_candidates") or []
    if names:
        rect = _uia.find_first_by_name(int(hwnd), list(names))
        if rect is not None:
            return rect
    return None


def region(
    cfg: RegionsConfig,  # noqa: ARG001 — kept for API stability; nothing to read
    launcher_cfg,
    app: str,
    name: str,
    *,
    window_index: int = 0,
) -> ResolvedRegion | dict[str, Any]:
    """Resolve a named region to live screen coordinates.

    Returns a :class:`ResolvedRegion` on success, or
    ``{"ok": False, "message": ...}`` on failure.

    No caching, no percentage maths. Each call walks the live UIA tree under
    the target window. Cost: ~50–200 ms per call on typical hardware (Teams
    / Outlook on the slower end, Explorer / VS Code on the faster end).
    """
    slug = _slugify(app)

    if sys.platform != "win32":
        return {"ok": False, "message": "region() requires Windows"}

    wins = launchers_mod.find_app_windows(launcher_cfg, slug)
    if not wins:
        return {
            "ok": False,
            "message": f"window for {slug!r} not currently visible; call launch_app first",
        }
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
    hwnd = int(win.hwnd)

    # Special-case the synthetic "__main_window" region — every app has it,
    # and it doesn't need UIA (just GetClientRect on the live HWND).
    if name == "__main_window":
        return _resolve_main_window(hwnd, slug, name)

    uia_spec = _load_uia_spec(slug)
    if not uia_spec:
        return {
            "ok": False,
            "message": (
                f"no UIA region spec for {slug!r}; only `__main_window` is available. "
                f"Take a screenshot and read coordinates from the colour grid instead."
            ),
        }
    spec_entry = uia_spec.get(name)
    if spec_entry is None:
        return {
            "ok": False,
            "message": (
                f"region {name!r} not defined for {slug!r}; "
                f"available: {sorted(['__main_window', *uia_spec.keys()])}"
            ),
        }

    # Try the live query without forcing foreground first (most apps respond
    # fine to UIA queries on background windows; saves a window-flash).
    rect = _query_uia(hwnd, spec_entry)
    if rect is None:
        # Some apps (Teams, modern Outlook) lazily build their UIA tree only
        # when the window is foregrounded. Try once more with focus.
        try:
            launchers_mod._force_foreground(hwnd)  # noqa: WPS437
        except Exception:
            pass
        rect = _query_uia(hwnd, spec_entry)
    if rect is None:
        return {
            "ok": False,
            "message": (
                f"UIA query for {slug}/{name!r} returned no match; the control "
                f"may be hidden, the window may not be foreground, or the app "
                f"version may have renamed it. Take a screenshot and target "
                f"the control visually."
            ),
        }
    left, top, right, bottom = rect
    w = max(1, right - left)
    h = max(1, bottom - top)
    return ResolvedRegion(
        app=slug,
        name=name,
        description=str(spec_entry.get("description") or ""),
        screen_x=int(left),
        screen_y=int(top),
        screen_w=int(w),
        screen_h=int(h),
        center_x=int(left + w // 2),
        center_y=int(top + h // 2),
    )


# ---------------------------------------------------------------------------
# Prompt-side catalog (system prompt + /regions sidecar page).
# ---------------------------------------------------------------------------


def list_apps_with_regions(_cfg: RegionsConfig) -> list[dict[str, Any]]:
    """Enumerate every ``lucid.apps.<slug>`` that defines ``REGIONS_UIA_SPEC``.

    The synthetic ``__main_window`` is always present (resolved without UIA),
    so it is appended to every app's region list.
    """
    out: list[dict[str, Any]] = []
    for slug, spec in _all_app_slugs_with_specs():
        names = ["__main_window", *spec.keys()]
        out.append({
            "slug": slug,
            "regions": names,
            "source": "uia_spec",
        })
    return out


def regions_for_prompt(cfg: RegionsConfig) -> str:
    """One-line catalog of (app -> region names) for the system prompt."""
    items = list_apps_with_regions(cfg)
    if not items:
        return ""
    lines: list[str] = []
    for it in items:
        names = ", ".join(it.get("regions") or [])
        if names:
            lines.append(f"  - `{it['slug']}`: {names}")
    if not lines:
        return ""
    return (
        "\n## Available `region(app, name)` lookups\n"
        "Use `region(app=\"<slug>\", name=\"<region>\")` to convert a known UI element of an app's "
        "window into **live screen coordinates** (returns center + bounding rect). The app's window "
        "must be currently visible. Each call queries Windows UI Automation in real time — no caching, "
        "always reflects the current layout. ``__main_window`` returns the whole window client rect. "
        "Pass `window_index=N` (default 0) to target a specific window when an app has multiple "
        "visible windows (e.g. Teams chat pop-outs).\n"
        + "\n".join(lines)
        + "\n"
    )


# ---------------------------------------------------------------------------
# Back-compat shims for callers that haven't been updated to the live-query
# world yet. These intentionally no-op or return harmless defaults so the
# sidecar /regions page and the DozeWorker auto-recalibrate pass don't crash.
# ---------------------------------------------------------------------------


def regions_dir(cfg: RegionsConfig) -> Path:
    """Legacy cache directory. Live-query mode writes nothing here, but the
    sidecar /regions page still asks for the path string."""
    p = Path(cfg.dir)
    if p.is_absolute():
        return p
    return Path.home() / ".lucid" / cfg.dir


def regions_path(cfg: RegionsConfig, app: str) -> Path:
    return regions_dir(cfg) / f"{_slugify(app)}.json"


def load_app_regions(_cfg: RegionsConfig, _app: str) -> dict[str, Any] | None:
    """Live-query mode keeps no per-app cache; nothing to load."""
    return None


def save_app_regions(_cfg: RegionsConfig, _app: str, _data: dict[str, Any]) -> Path | None:
    """Live-query mode keeps no per-app cache; nothing to save."""
    return None


def calibrate(_cfg: RegionsConfig, _launcher_cfg, app: str) -> dict[str, Any]:
    """No-op in live-query mode — every ``region()`` call already queries UIA."""
    slug = _slugify(app)
    spec = _load_uia_spec(slug)
    if not spec:
        return {
            "ok": False,
            "slug": slug,
            "message": (
                f"{slug!r} has no UIA region spec; nothing to calibrate. "
                f"Only `__main_window` is available for this app."
            ),
        }
    return {
        "ok": True,
        "slug": slug,
        "message": "live-query mode: no calibration needed, each region() call hits UIA fresh",
        "regions": ["__main_window", *spec.keys()],
        "calibration_strategy": "live_uia",
    }


def auto_recalibrate_pass(_cfg: RegionsConfig, _launcher_cfg) -> dict[str, Any]:
    """No-op in live-query mode. The DozeWorker keeps calling this on idle;
    we just report 'nothing to do' so the worker's logs stay clean."""
    return {
        "ok": True,
        "scanned": 0,
        "recalibrated": [],
        "skipped": [],
        "errors": [],
        "mode": "live_uia",
    }
