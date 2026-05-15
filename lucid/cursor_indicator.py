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
# User-click hook — pulse the claw closed when the human (not Lucid) clicks
# ---------------------------------------------------------------------------
# Low-level mouse hook detects every system-wide mouse event. We filter
# ``LLMHF_INJECTED`` so Lucid's own SendInput/mouse_event clicks (which
# already get the explicit ``pulse_click()`` from ``tools.dispatch``) don't
# double-trigger. Hook callback runs on the dedicated pump thread; the
# pulse itself is dispatched to a tiny worker thread so we don't block
# Windows' input pipeline.
_WH_MOUSE_LL = 14
_WM_QUIT = 0x0012
_WM_LBUTTONDOWN = 0x0201
_WM_RBUTTONDOWN = 0x0204
_WM_MBUTTONDOWN = 0x0207
_WM_XBUTTONDOWN = 0x020B
_LLMHF_INJECTED = 0x00000001
_LLMHF_LOWER_IL_INJECTED = 0x00000002
_USER_CLICK_DOWNS = {
    _WM_LBUTTONDOWN, _WM_RBUTTONDOWN, _WM_MBUTTONDOWN, _WM_XBUTTONDOWN,
}

_hook_thread: Optional[threading.Thread] = None
_hook_thread_id: int = 0
_hook_handle: int = 0
# Keep a strong reference to the ctypes callback so it isn't GC'd.
_hook_proc_ref = None
_pulse_active = False  # debounce: skip overlapping user pulses


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt_x", ctypes.c_long),
        ("pt_y", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("flags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _force_cursor_redraw() -> None:
    """Force Windows to repaint the visible cursor.

    ``SetSystemCursor`` updates the system cursor table but the on-screen
    pointer only refreshes when something dispatches ``WM_SETCURSOR`` —
    typically a mouse-move. When the user clicks without moving (very
    common — they aim, then click) the visible cursor stays stale even
    though our state changed.

    Trick: post a 0-pixel injected ``MOUSEEVENTF_MOVE`` via ``mouse_event``.
    Windows treats this as a no-op for position but still walks the cursor
    refresh path, so the new system cursor becomes visible immediately.
    The injected event carries ``LLMHF_INJECTED`` so our own hook ignores it.
    """
    if not _IS_WINDOWS:
        return
    try:
        # MOUSEEVENTF_MOVE = 0x0001
        ctypes.windll.user32.mouse_event(0x0001, 0, 0, 0, 0)
    except Exception:
        pass


def _user_pulse_async() -> None:
    """Fire-and-forget version of ``pulse_click`` for hooked user clicks.

    Unlike the ``pulse_click`` context manager (which wraps a Lucid action),
    this just shows the closed jaw for ~``_PULSE_MIN_HOLD_SEC`` then flips
    back to open, all on a worker thread so the LL hook callback returns
    immediately (Windows kills hooks that block the input pipeline).
    """
    global _pulse_active
    with _lock:
        if _pulse_active or _active_count == 0 or _active_kind != "open":
            return
        if not _apply_kind("closed"):
            return
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
    """Spin up a daemon thread that owns a low-level mouse hook + msg pump."""
    global _hook_thread, _hook_thread_id, _hook_handle, _hook_proc_ref
    if not _IS_WINDOWS or _hook_thread is not None:
        return

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    HOOKPROC = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
    )

    def _proc(nCode, wParam, lParam):
        try:
            if nCode == 0 and int(wParam) in _USER_CLICK_DOWNS:
                info = ctypes.cast(lParam, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
                injected = bool(info.flags & (_LLMHF_INJECTED | _LLMHF_LOWER_IL_INJECTED))
                if not injected:
                    _user_pulse_async()
        except Exception:
            pass
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    cb = HOOKPROC(_proc)

    ready = threading.Event()

    def _pump() -> None:
        global _hook_thread_id, _hook_handle
        _hook_thread_id = kernel32.GetCurrentThreadId()
        try:
            user32.SetWindowsHookExW.restype = ctypes.c_void_p
            user32.SetWindowsHookExW.argtypes = [
                ctypes.c_int, HOOKPROC, ctypes.c_void_p, ctypes.c_uint,
            ]
            hmod = kernel32.GetModuleHandleW(None)
            handle = user32.SetWindowsHookExW(_WH_MOUSE_LL, cb, hmod, 0)
            if not handle:
                return
            _hook_handle = handle
        finally:
            ready.set()
        # Standard PeekMessage/GetMessage pump. PostThreadMessage(WM_QUIT)
        # from _stop_user_click_hook will break us out.
        msg = ctypes.create_string_buffer(64)  # MSG fits in 48B on x64; round up
        try:
            while user32.GetMessageW(msg, None, 0, 0) > 0:
                user32.TranslateMessage(msg)
                user32.DispatchMessageW(msg)
        except Exception:
            pass
        try:
            if _hook_handle:
                user32.UnhookWindowsHookEx(_hook_handle)
        except Exception:
            pass

    _hook_proc_ref = cb  # prevent GC
    _hook_thread = threading.Thread(target=_pump, name="crab-mouse-hook", daemon=True)
    _hook_thread.start()
    ready.wait(timeout=2.0)
    if not _hook_handle:
        # Failed to install hook — drop references so we can retry next time.
        _hook_thread = None
        _hook_thread_id = 0
        _hook_proc_ref = None


def _stop_user_click_hook() -> None:
    global _hook_thread, _hook_thread_id, _hook_handle, _hook_proc_ref
    if not _IS_WINDOWS or _hook_thread is None:
        return
    try:
        if _hook_thread_id:
            ctypes.windll.user32.PostThreadMessageW(
                _hook_thread_id, _WM_QUIT, 0, 0,
            )
    except Exception:
        pass
    try:
        _hook_thread.join(timeout=1.0)
    except Exception:
        pass
    _hook_thread = None
    _hook_thread_id = 0
    _hook_handle = 0
    _hook_proc_ref = None


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
            except Exception:
                pass
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
            except Exception:
                pass
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
