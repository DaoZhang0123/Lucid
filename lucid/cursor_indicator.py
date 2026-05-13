"""Crab-claw cursor indicator — replaces the system cursor while Lucid is acting.

Two visual states:
    "open"   — idle while the agent loop runs (default for the whole `with` block)
    "closed" — pulsed for ~120ms around every click / drag, signalling "snap!"

Implementation notes
--------------------
* Uses Win32 ``SetSystemCursor``, which is system-wide. While the indicator is
  on, ALL apps see the crab claw — that's intentional (the user might glance
  at a different window).
* Restoration: ``SystemParametersInfoW(SPI_SETCURSORS)`` reloads the user's
  configured cursor scheme from registry. Cleaner than caching individual
  cursors and re-setting them.
* Crash safety: an ``atexit`` hook + SIGINT/SIGTERM handlers guarantee
  restoration even if the sidecar dies mid-task — otherwise the user would be
  left with a crab claw cursor until next logon.
* ``SetSystemCursor`` takes ownership of the HCURSOR you pass in, so we
  ``CopyIcon`` the loaded handle for each of the 14 system cursor IDs every
  time we apply.
* ``pulse_click()`` is a no-op outside an active ``CrabCursor`` block, so it's
  safe to call from anywhere in the dispatch path.
* No-op on non-Windows.
"""
from __future__ import annotations

import atexit
import ctypes
import logging
import signal
import sys
import threading
import time
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# OCR_* system cursor IDs (from WinUser.h). We override every visible cursor
# kind so the crab is consistent regardless of which UI element the pointer
# happens to hover over while Lucid is acting.
_SYSTEM_CURSOR_IDS = (
    32512,  # OCR_NORMAL       — arrow
    32513,  # OCR_IBEAM        — text caret
    32514,  # OCR_WAIT         — busy spinner
    32515,  # OCR_CROSS        — crosshair
    32516,  # OCR_UP           — vertical arrow
    32642,  # OCR_SIZENWSE
    32643,  # OCR_SIZENESW
    32644,  # OCR_SIZEWE
    32645,  # OCR_SIZENS
    32646,  # OCR_SIZEALL
    32648,  # OCR_NO           — slashed circle
    32649,  # OCR_HAND         — pointing hand (link)
    32650,  # OCR_APPSTARTING  — arrow + small spinner
    32651,  # OCR_HELP         — arrow + question mark
)

_SPI_SETCURSORS = 0x0057
_IS_WINDOWS = sys.platform.startswith("win")

# How long the closed-jaw "snap" frame must remain visible per click. If the
# action itself takes longer than this, we don't add extra delay.
_PULSE_MIN_HOLD_SEC = 0.5

# Process-wide guard.
_lock = threading.Lock()
_active_count = 0
_active_kind: Optional[str] = None  # "open" | "closed" | None
_loaded: dict[str, int] = {}        # kind -> HCURSOR
_atexit_registered = False
_signal_registered = False
_orig_sigint = None
_orig_sigterm = None


# ---------------------------------------------------------------------------
# Resource loading
# ---------------------------------------------------------------------------
def _resolve_cur_path(filename: str) -> Optional[Path]:
    """Locate ``filename`` in the bundled assets folder.

    Works in both source-checkout and PyInstaller (onefile / onedir) layouts.
    """
    here = Path(__file__).resolve().parent
    candidates = [here / "assets" / filename]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "lucid" / "assets" / filename)
    for p in candidates:
        if p.is_file():
            return p
    return None


def _load_cursor(kind: str) -> Optional[int]:
    """Load (and cache) an HCURSOR for the given kind. Returns None on failure."""
    if kind in _loaded:
        return _loaded[kind]
    if not _IS_WINDOWS:
        return None
    fname = "crab_claw.cur" if kind == "open" else "crab_claw_closed.cur"
    cur_path = _resolve_cur_path(fname)
    if cur_path is None:
        logger.warning(f"crab cursor: {fname} not found in lucid/assets/")
        return None
    user32 = ctypes.windll.user32
    user32.LoadCursorFromFileW.restype = ctypes.c_void_p
    user32.LoadCursorFromFileW.argtypes = [ctypes.c_wchar_p]
    h = user32.LoadCursorFromFileW(str(cur_path))
    if not h:
        err = ctypes.get_last_error()
        logger.warning(f"crab cursor: LoadCursorFromFileW({fname}) failed (GetLastError={err})")
        return None
    _loaded[kind] = int(h)
    return _loaded[kind]


# ---------------------------------------------------------------------------
# System cursor swap / restore
# ---------------------------------------------------------------------------
def _apply_kind(kind: str) -> bool:
    """Replace every system cursor with a copy of ``kind``. True on success."""
    global _active_kind
    if not _IS_WINDOWS:
        return False
    h = _load_cursor(kind)
    if not h:
        return False
    user32 = ctypes.windll.user32
    user32.CopyIcon.restype = ctypes.c_void_p
    user32.CopyIcon.argtypes = [ctypes.c_void_p]
    user32.SetSystemCursor.restype = ctypes.c_int
    user32.SetSystemCursor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    ok_any = False
    for ocr in _SYSTEM_CURSOR_IDS:
        copy = user32.CopyIcon(h)
        if not copy:
            continue
        # SetSystemCursor takes ownership of `copy` on success.
        if user32.SetSystemCursor(copy, ocr):
            ok_any = True
        else:
            user32.DestroyCursor(copy)
    if ok_any:
        _active_kind = kind
    return ok_any


def _restore() -> None:
    """Reload the user's default cursor scheme from registry."""
    global _active_kind
    if not _IS_WINDOWS:
        return
    user32 = ctypes.windll.user32
    user32.SystemParametersInfoW.restype = ctypes.c_int
    user32.SystemParametersInfoW.argtypes = [
        ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint,
    ]
    user32.SystemParametersInfoW(_SPI_SETCURSORS, 0, None, 0)
    _active_kind = None


def _emergency_restore() -> None:
    """Called from atexit / signal handlers — best-effort, never raises."""
    try:
        global _active_count
        if _active_count > 0 or _active_kind is not None:
            _restore()
            _active_count = 0
    except Exception:
        pass


def _ensure_safety_hooks() -> None:
    global _atexit_registered, _signal_registered, _orig_sigint, _orig_sigterm
    if not _atexit_registered:
        atexit.register(_emergency_restore)
        _atexit_registered = True
    if not _signal_registered:
        def _make_handler(prev):
            def _handler(signum, frame):
                _emergency_restore()
                if callable(prev):
                    try:
                        prev(signum, frame)
                    except Exception:
                        pass
                elif prev == signal.SIG_DFL:
                    signal.signal(signum, signal.SIG_DFL)
                    try:
                        signal.raise_signal(signum)  # type: ignore[attr-defined]
                    except Exception:
                        pass
            return _handler
        try:
            _orig_sigint = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, _make_handler(_orig_sigint))
        except (ValueError, OSError):
            # signal can only be set from the main thread; sidecar runs the
            # loop on a worker thread — that's OK, atexit still covers us.
            pass
        try:
            _orig_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, _make_handler(_orig_sigterm))
        except (ValueError, OSError, AttributeError):
            pass
        _signal_registered = True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def is_active() -> bool:
    """True if a CrabCursor block is currently in effect."""
    return _active_count > 0


class CrabCursor(AbstractContextManager):
    """Context manager that swaps system cursors to the crab "open" frame.

    Reentrant: nested ``with`` blocks share a single application; only the
    outermost exit restores. If ``enabled=False`` it's a no-op (test hook).
    """

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = bool(enabled) and _IS_WINDOWS
        self._applied_here = False

    def __enter__(self) -> "CrabCursor":
        if not self.enabled:
            return self
        global _active_count
        with _lock:
            _ensure_safety_hooks()
            if _active_count == 0:
                if _apply_kind("open"):
                    _active_count = 1
                    self._applied_here = True
                # If apply failed (no .cur, etc.) we just no-op — the with
                # block still runs the user's code.
            else:
                _active_count += 1
                self._applied_here = True
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self.enabled or not self._applied_here:
            return None
        global _active_count
        with _lock:
            if _active_count > 0:
                _active_count -= 1
                if _active_count == 0:
                    _restore()
        return None


@contextmanager
def pulse_click():
    """Briefly swap to the closed-jaw frame around a click action.

    No-op when no ``CrabCursor`` block is active or when running outside
    Windows. Guarantees the closed frame is visible for at least
    ``_PULSE_MIN_HOLD_SEC`` so quick clicks still register visually.

    Usage::

        with pulse_click():
            d.left_click(x, y)
    """
    if not _IS_WINDOWS or _active_count == 0:
        yield
        return
    swapped = False
    t0 = time.monotonic()
    with _lock:
        # Only swap if we're currently showing "open"; if another pulse is
        # already in flight (e.g. nested triple_click), don't re-apply.
        if _active_kind == "open":
            if _apply_kind("closed"):
                swapped = True
    try:
        yield
    finally:
        if swapped:
            elapsed = time.monotonic() - t0
            if elapsed < _PULSE_MIN_HOLD_SEC:
                time.sleep(_PULSE_MIN_HOLD_SEC - elapsed)
            with _lock:
                # Only restore to "open" if we're still active (CrabCursor
                # might have exited in the meantime — _restore() already ran).
                if _active_count > 0 and _active_kind == "closed":
                    _apply_kind("open")
