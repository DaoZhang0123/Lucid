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


# ---------------------------------------------------------------------------
# Dedicated debug-log file
# ---------------------------------------------------------------------------
# The bundled exe has no root logging handler, so stdlib logger.* calls in
# this module vanish silently. We append a few key signals to a dedicated
# file under ~/.lucid/logs/cursor-debug.log so we can confirm — without
# adding any new logging machinery — whether the user-click watcher fires
# and what _user_pulse_async decides.
_DEBUG_LOG_PATH: Optional[Path] = None
try:
    _DEBUG_LOG_PATH = Path.home() / ".lucid" / "logs" / "cursor-debug.log"
    _DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
except Exception:
    _DEBUG_LOG_PATH = None


def _debug(msg: str) -> None:
    """Append a line to the cursor-debug log. Never raises."""
    if _DEBUG_LOG_PATH is None:
        return
    try:
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        with _DEBUG_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


_debug("cursor_indicator module loaded")

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
# User-click watcher — pulse the claw closed when the human (not Lucid) clicks
# ---------------------------------------------------------------------------
# Originally implemented as a WH_MOUSE_LL low-level mouse hook. That approach
# was fragile: low-level hooks can be silently dropped by Windows when the
# foreground window runs at higher integrity (UIPI), and the HOOKPROC
# callback's ctypes signature is easy to get wrong on x64 (LRESULT vs c_long).
# We don't get any visible failure mode — the claw simply never closes.
#
# Replacement: a daemon thread that polls ``GetAsyncKeyState`` every 30 ms
# for the mouse buttons. ``GetAsyncKeyState`` works regardless of UIPI /
# foreground window / message pump state, and we can detect button edges
# trivially. Lucid's own ``SendInput`` clicks ALSO show up via this API,
# so we suppress overlapping pulses by checking ``_active_kind`` — when
# Lucid is mid-click, ``pulse_click()`` has already set it to "closed", so
# we don't fire a redundant pulse.
_VK_BUTTONS = (0x01, 0x02, 0x04, 0x05, 0x06)  # L, R, M, X1, X2

_watch_thread: Optional[threading.Thread] = None
_watch_stop = threading.Event()
_pulse_active = False  # set while a user-pulse is holding the closed frame


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _force_cursor_redraw() -> None:
    """Force Windows to repaint the visible cursor.

    ``SetSystemCursor`` updates the system cursor table but the on-screen
    pointer only refreshes when something dispatches ``WM_SETCURSOR`` —
    typically a mouse-move that crosses a hit-test boundary. When the user
    clicks without moving the mouse (very common — they aim, then click),
    the visible cursor stays stale even though our state changed.

    A bare ``mouse_event(MOUSEEVENTF_MOVE, 0, 0, ...)`` does NOT trigger a
    WM_SETCURSOR re-dispatch on modern Windows — Windows short-circuits
    zero-delta moves. We need to actually shift the cursor by ≥1 pixel.

    Workaround: ``GetCursorPos`` → ``SetCursorPos(x, y)`` (same position)
    is also short-circuited. Instead, jiggle by exactly 1 px and snap back:

        SetCursorPos(x+1, y)   → forces a real move + WM_SETCURSOR
        SetCursorPos(x,   y)   → snaps back

    Both calls are synchronous and complete in <1 ms; the human eye can't
    see a 1-pixel oscillation but the cursor bitmap is now refreshed.

    Note: ``SetCursorPos`` is NOT marked LLMHF_INJECTED, but it doesn't
    generate WM_*BUTTONDOWN messages so our low-level click hook ignores it
    naturally (it only listens for button-downs, not moves).
    """
    if not _IS_WINDOWS:
        return
    try:
        user32 = ctypes.windll.user32
        # IMPORTANT: do NOT set ``user32.GetCursorPos.argtypes`` /
        # ``user32.SetCursorPos.argtypes`` here. ``ctypes.windll.user32`` is
        # a process-wide cached proxy — mutating an argtypes list rebinds
        # the signature for *every* caller in the process. Other modules
        # (input_driver.py, window.py, uia.py) pass their own POINT subclass
        # via ``byref``; if we declare argtypes=[POINTER(_POINT)] here, those
        # callers blow up with `expected LP__POINT instance instead of
        # pointer to POINT`. ctypes accepts ``byref(struct)`` for any
        # pointer parameter when argtypes is unset, and the integer overload
        # of SetCursorPos works with default int marshalling — so just call.
        pt = _POINT()
        if not user32.GetCursorPos(ctypes.byref(pt)):
            return
        user32.SetCursorPos(pt.x + 1, pt.y)
        user32.SetCursorPos(pt.x, pt.y)
    except Exception:
        pass


def _user_pulse_async() -> None:
    """Fire-and-forget version of ``pulse_click`` for hooked user clicks.

    Unlike the ``pulse_click`` context manager (which wraps a Lucid action),
    this just shows the closed jaw for ~``_PULSE_MIN_HOLD_SEC`` then flips
    back to open.

    **The swap to closed must happen synchronously inside the hook callback.**
    If we defer it to a worker thread, by the time the thread acquires the
    lock and calls ``SetSystemCursor``, the target window has already
    processed the click's ``WM_SETCURSOR`` against the OLD (open) cursor
    table — visible result is no perceptible change. ``SetSystemCursor``
    itself is fast (microseconds per ID, ~14 IDs total = well under 1 ms),
    far below the LowLevelHooks timeout (5 s on modern Windows).

    Only the ``sleep + restore-to-open`` half runs on the worker thread,
    because we must NOT block the input pipeline for hundreds of ms.
    """
    global _pulse_active
    with _lock:
        if _pulse_active or _active_count == 0 or _active_kind != "open":
            _debug(
                f"user-pulse skipped: pulse_active={_pulse_active} "
                f"active_count={_active_count} active_kind={_active_kind}"
            )
            return
        if not _apply_kind("closed"):
            _debug("user-pulse: _apply_kind('closed') FAILED")
            return
        _debug("user-pulse: applied closed")
        _pulse_active = True
    # Force the visible cursor to repaint right now (user clicked without
    # moving → no natural WM_SETCURSOR).
    _force_cursor_redraw()

    def _restore_open() -> None:
        global _pulse_active
        try:
            time.sleep(_PULSE_MIN_HOLD_SEC)
        finally:
            with _lock:
                if _active_count > 0 and _active_kind == "closed":
                    _apply_kind("open")
                _pulse_active = False
            _force_cursor_redraw()

    threading.Thread(target=_restore_open, name="crab-user-pulse", daemon=True).start()


def _start_user_click_hook() -> None:
    """Spin up a daemon thread that polls mouse buttons via GetAsyncKeyState.

    Replaces the previous WH_MOUSE_LL implementation. Polling at 30 ms is
    well under human reaction time and avoids all the LL-hook footguns
    (UIPI silently dropping callbacks, HOOKPROC ctypes signature on x64,
    Windows killing slow hooks). Lucid's own clicks fire here too, but
    `_user_pulse_async` short-circuits when `_active_kind != "open"` (i.e.
    when pulse_click() has already swapped to closed).
    """
    global _watch_thread
    if not _IS_WINDOWS or _watch_thread is not None:
        return

    # Use a fresh WinDLL instance (NOT ctypes.windll.user32) so any argtypes
    # we set here can't leak to other modules' GetAsyncKeyState callers via
    # the process-wide cached proxy. See the comment in _force_cursor_redraw
    # for the same footgun we hit with GetCursorPos/SetCursorPos.
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]

    _watch_stop.clear()

    def _watch() -> None:
        _debug(f"user-click poller started (interval=30ms, buttons={[hex(b) for b in _VK_BUTTONS]})")
        # Track previous "currently down" state for each button so we only
        # pulse on the down-edge, not while the button is held.
        prev_down = {vk: False for vk in _VK_BUTTONS}
        # Drain any stale "was pressed since last call" bit at startup.
        for vk in _VK_BUTTONS:
            user32.GetAsyncKeyState(vk)
        try:
            while not _watch_stop.is_set():
                for vk in _VK_BUTTONS:
                    state = user32.GetAsyncKeyState(vk)
                    is_down = (state & 0x8000) != 0
                    if is_down and not prev_down[vk]:
                        _debug(f"user-click down vk=0x{vk:02x}")
                        try:
                            _user_pulse_async()
                        except Exception as e:
                            _debug(f"user-pulse exception: {e!r}")
                    prev_down[vk] = is_down
                # 30 ms poll → user perceives the closed jaw within one frame
                # at 60 Hz. Event.wait lets us exit promptly on stop.
                if _watch_stop.wait(0.030):
                    break
        finally:
            _debug("user-click poller stopped")

    _watch_thread = threading.Thread(target=_watch, name="crab-user-watch", daemon=True)
    _watch_thread.start()


def _stop_user_click_hook() -> None:
    global _watch_thread
    if not _IS_WINDOWS or _watch_thread is None:
        return
    _watch_stop.set()
    try:
        _watch_thread.join(timeout=1.0)
    except Exception:
        pass
    _watch_thread = None


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
        # Outermost CrabCursor turns on the user-click hook so manual clicks
        # also pulse the claw closed. Best-effort; failure is silent (the
        # claw indicator still works for Lucid-driven clicks).
        if self._applied_here and _active_count == 1:
            try:
                _start_user_click_hook()
            except Exception as e:
                _debug(f"start_user_click_hook exception: {e!r}")
        _debug(f"CrabCursor entered active_count={_active_count} active_kind={_active_kind}")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self.enabled or not self._applied_here:
            return None
        global _active_count
        stopped = False
        with _lock:
            if _active_count > 0:
                _active_count -= 1
                if _active_count == 0:
                    _restore()
                    stopped = True
        if stopped:
            try:
                _stop_user_click_hook()
            except Exception as e:
                _debug(f"stop_user_click_hook exception: {e!r}")
            _debug("CrabCursor exited (last)")
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
