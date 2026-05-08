"""Minimal Windows UI Automation client — only what we need to ask
"what's the bounding rect of the smallest UI element under (x, y)".

Implemented in pure ``ctypes`` against ``UIAutomationCore.dll`` to avoid pulling
in ``comtypes`` / ``pywinauto`` (those drag in ~10 MB of generated wrappers and
slow down PyInstaller analysis a lot). The COM apartment is initialised lazily
on first use and reused for the lifetime of the sidecar process.

Public API:
    element_rect_at(x, y) -> (left, top, right, bottom) | None

Returns ``None`` on:
- non-Windows platforms
- COM init failure
- ElementFromPoint failure (e.g. secure desktop, remote app, hwnd not UIA-aware)

Used by ``screen.py::_capture_cursor_local`` to pick a smart crop window for L3
captures (Snipaste-style).
"""
from __future__ import annotations

import sys
import threading
from typing import Optional, Tuple

# COM identifiers — copied from UIAutomation.idl
# CLSID_CUIAutomation = {ff48dba4-60ef-4201-aa87-54103eef594e}
# IID_IUIAutomation   = {30cbe57d-d9d0-452a-ab13-7ac5ac4825ee}
_CLSID_CUIAutomation = b"\xa4\xdb\x48\xff\xef\x60\x01\x42\xaa\x87\x54\x10\x3e\xef\x59\x4e"
_IID_IUIAutomation = b"\x7d\xe5\xcb\x30\xd0\xd9\x2a\x45\xab\x13\x7a\xc5\xac\x48\x25\xee"

_COINIT_APARTMENTTHREADED = 0x2
_CLSCTX_INPROC_SERVER = 0x1

_lock = threading.Lock()
_uia: object | None = None  # cached IUIAutomation*
_init_failed: bool = False
_init_done: bool = False


def _ensure_init() -> bool:
    """Initialise COM + create the IUIAutomation singleton. Idempotent."""
    global _uia, _init_failed, _init_done
    if sys.platform != "win32":
        return False
    if _init_done:
        return _uia is not None
    with _lock:
        if _init_done:
            return _uia is not None
        _init_done = True
        try:
            import ctypes
            from ctypes import wintypes

            ole32 = ctypes.windll.ole32
            # CoInitializeEx — APARTMENTTHREADED is required for UIAutomation.
            # S_OK == 0, S_FALSE == 1 (already init'd). Both are fine.
            hr = ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
            if hr < 0:  # negative HRESULT = failure
                _init_failed = True
                return False

            # CoCreateInstance(CLSID_CUIAutomation, NULL, INPROC, IID_IUIAutomation, &p)
            class GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", ctypes.c_uint32),
                    ("Data2", ctypes.c_uint16),
                    ("Data3", ctypes.c_uint16),
                    ("Data4", ctypes.c_ubyte * 8),
                ]

            def _bytes_to_guid(b: bytes) -> GUID:
                g = GUID()
                ctypes.memmove(ctypes.byref(g), b, 16)
                return g

            clsid = _bytes_to_guid(_CLSID_CUIAutomation)
            iid = _bytes_to_guid(_IID_IUIAutomation)
            ptr = ctypes.c_void_p()
            hr = ole32.CoCreateInstance(
                ctypes.byref(clsid),
                None,
                _CLSCTX_INPROC_SERVER,
                ctypes.byref(iid),
                ctypes.byref(ptr),
            )
            if hr < 0 or not ptr.value:
                _init_failed = True
                return False
            _uia = ptr.value  # raw COM interface pointer
            return True
        except Exception:
            _init_failed = True
            return False


def _release_unknown(p: int) -> None:
    """Call IUnknown::Release on a raw interface pointer."""
    try:
        import ctypes
        # vtable[2] = Release
        vtbl_ptr = ctypes.cast(p, ctypes.POINTER(ctypes.c_void_p))[0]
        release_fn_ptr = ctypes.cast(vtbl_ptr, ctypes.POINTER(ctypes.c_void_p))[2]
        release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(release_fn_ptr)
        release(p)
    except Exception:
        pass


def element_rect_at(x: int, y: int) -> Optional[Tuple[int, int, int, int]]:
    """Return the screen-space ``(left, top, right, bottom)`` of the smallest
    UIA element under the given screen coordinates. ``None`` on any failure.
    """
    if not _ensure_init():
        return None
    if _uia is None:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        # POINT struct
        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        pt = POINT(int(x), int(y))

        # IUIAutomation vtable layout (relevant slots):
        #   3  ElementFromPoint(POINT pt, IUIAutomationElement** found)
        # IUIAutomationElement vtable:
        #   2  Release
        #   ... CurrentBoundingRectangle (slot 12)  → RECT*
        # We use raw vtable indexing because we don't have IDL-generated stubs.
        uia_p = ctypes.c_void_p(_uia)
        vtbl = ctypes.cast(uia_p, ctypes.POINTER(ctypes.c_void_p))[0]
        ElementFromPoint_ptr = ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[7]
        ElementFromPoint = ctypes.WINFUNCTYPE(
            ctypes.c_long,           # HRESULT
            ctypes.c_void_p,         # this
            POINT,                   # pt (passed by value)
            ctypes.POINTER(ctypes.c_void_p),  # IUIAutomationElement** out
        )(ElementFromPoint_ptr)

        elem_ptr = ctypes.c_void_p()
        hr = ElementFromPoint(uia_p, pt, ctypes.byref(elem_ptr))
        if hr < 0 or not elem_ptr.value:
            return None

        try:
            elem_vtbl = ctypes.cast(elem_ptr, ctypes.POINTER(ctypes.c_void_p))[0]
            # IUIAutomationElement vtable: IUnknown (0..2) + 41 element methods.
            # get_CurrentBoundingRectangle is the 41st member, vtable slot 43.
            # Reference: UIAutomationClient.h, DECLARE_INTERFACE_(IUIAutomationElement)
            # Order: SetFocus(3), GetRuntimeId(4), FindFirst(5), FindAll(6),
            # FindFirstBuildCache(7), FindAllBuildCache(8), BuildUpdatedCache(9),
            # GetCurrentPropertyValue(10), GetCurrentPropertyValueEx(11),
            # GetCachedPropertyValue(12), GetCachedPropertyValueEx(13),
            # GetCurrentPatternAs(14), GetCachedPatternAs(15),
            # GetCurrentPattern(16), GetCachedPattern(17),
            # GetCachedParent(18), GetCachedChildren(19),
            # get_CurrentProcessId(20), get_CurrentControlType(21),
            # get_CurrentLocalizedControlType(22), get_CurrentName(23),
            # get_CurrentAcceleratorKey(24), get_CurrentAccessKey(25),
            # get_CurrentHasKeyboardFocus(26), get_CurrentIsKeyboardFocusable(27),
            # get_CurrentIsEnabled(28), get_CurrentAutomationId(29),
            # get_CurrentClassName(30), get_CurrentHelpText(31),
            # get_CurrentCulture(32), get_CurrentIsControlElement(33),
            # get_CurrentIsContentElement(34), get_CurrentIsPassword(35),
            # get_CurrentNativeWindowHandle(36), get_CurrentItemType(37),
            # get_CurrentIsOffscreen(38), get_CurrentOrientation(39),
            # get_CurrentFrameworkId(40), get_CurrentIsRequiredForForm(41),
            # get_CurrentItemStatus(42), get_CurrentBoundingRectangle(43)
            BoundingRect_ptr = ctypes.cast(elem_vtbl, ctypes.POINTER(ctypes.c_void_p))[43]
            GetBounds = ctypes.WINFUNCTYPE(
                ctypes.c_long,           # HRESULT
                ctypes.c_void_p,         # this
                ctypes.POINTER(RECT),    # out
            )(BoundingRect_ptr)
            r = RECT()
            hr = GetBounds(elem_ptr, ctypes.byref(r))
            if hr < 0:
                return None
            left, top, right, bottom = int(r.left), int(r.top), int(r.right), int(r.bottom)
            if right <= left or bottom <= top:
                return None
            return (left, top, right, bottom)
        finally:
            _release_unknown(elem_ptr.value)
    except Exception:
        return None
