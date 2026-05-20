"""Taskbar notification monitor — UIA channel.

Companion to :mod:`lucid.taskbar_monitor` (visual / pixel-diff channel).
This module watches the Windows shell's *accessibility* tree for the signals
that apps publish when they get a new notification:

* **Name** changes on taskbar buttons (Win11 ``Taskbar.TaskListButtonAutomationPeer``)
  and tray icons (``SystemTray.NormalButton``). Teams, Outlook, Slack, Discord
  all do this — Name goes from ``'Teams - 1 个运行窗口'`` to
  ``'Teams - 1 个运行窗口 1 条未读消息'`` or similar.

* **HelpText** changes — set to ``'已请求注意'`` ("Attention Requested") when
  the app calls ``FlashWindowEx`` to request attention. This is how
  **微信 / WeChat** signals unread, since WeChat paints its red badge as a
  bitmap overlay (not in the Name).

Why a separate channel:
  Reading the shell tree is event-driven (zero CPU between events) and exposes
  *structured* signals — we know **which app** fired and **what changed**.
  Compare with the visual channel which sees only a pixel diff and has to call
  an LLM to interpret it. UIA fires first; on a hit we bump the visual
  monitor's cooldown so the visual LLM never runs for the same event.

Architecture:
  * One dedicated background thread runs the UIA event loop (COM apartment is
    initialised by the ``uiautomation`` library on first call in that thread).
  * Property-changed handlers fire from a COM-managed thread; the handler
    enqueues a normalised record and returns immediately. A second worker
    thread drains the queue, decides "is this a new-unread transition?",
    consults per-app preferences, and emits ``taskbar_notify_confirmed`` /
    ``taskbar_uia_signal`` events to the same sink the visual monitor uses.

Doze learning interface:
  * Every raw signal is appended to ``logs/taskbar-monitor/uia-events.jsonl``
    (with prev/now Name+HelpText + app candidate). Doze can mine this file to
    figure out per-app reliability and write preferences via
    :mod:`lucid.taskbar_sources` (see ``set_app_pref``).
  * On startup we read ``~/.lucid/taskbar_sources.json`` to know which apps
    have been pinned to ``"uia"`` only / ``"visual"`` only / ``"both"`` —
    arbitration honours those preferences before deciding to emit.
"""
from __future__ import annotations

import json
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import LoggingConfig, TaskbarUiaConfig
from .runlog import resolve_logs_root
from . import taskbar_sources


EventSink = Callable[[dict[str, Any]], None]
OnConfirmedCallback = Callable[[dict[str, Any]], None]
ExternalCooldownBump = Callable[[str, list[str], float], None]
"""Called as ``bump(reason, app_candidates, suppress_sec)`` so the visual
monitor can skip LLM step-2 for ``suppress_sec`` after a UIA confirmation."""


# Per-button class names that meaningfully encode notification state.
# Everything else (TextBlock, Image, inner controls) is filtered out at the
# handler boundary to avoid log floods.
_TASKBAR_BUTTON_CLASSES: frozenset[str] = frozenset({
    "Taskbar.TaskListButtonAutomationPeer",  # Win11 pinned/running app buttons
    "SystemTray.NormalButton",               # Win11 tray icons
})

# Tray classes we ignore: clock ticks every minute, sound/network change with
# device state. They do not carry app-unread state.
_TRAY_NOISE_CLASSES: frozenset[str] = frozenset({
    "SystemTray.OmniButton",         # clock
    "SystemTray.OmniButtonRight",    # volume
    "SystemTray.AccentButton",       # network
    "SystemTray.ShowDesktopButton",  # show-desktop sliver
    "SystemTray.ChevronSystemTrayIcon",
})

# Substrings inside Name that almost certainly mean "new unread", regardless
# of app locale. The exact words used in the wild on this machine:
#   Teams (zh)   : '|新活动'
#   WeChat (zh)  : (no Name change — only HelpText '已请求注意')
#   Outlook (en) : 'unread'
#   Discord      : (#) prefix in Name
_UNREAD_NAME_HINTS: tuple[str, ...] = (
    "未读", "新消息", "条新", "条消息", "新活动", "新通知",
    "unread", "new message", "new messages", "new activity", "new notification",
)

# HelpText values that mean "flash requested" — `FlashWindowEx` puts this here.
# Locale variants:
_FLASH_HELPTEXT_HINTS: tuple[str, ...] = (
    "已请求注意",       # zh-CN
    "请求注意",         # zh-CN short
    "需要關注",         # zh-TW
    "attention required",
    "attention requested",
    "flashing",
)


def _ts_ms() -> int:
    return int(time.time() * 1000)


def _safe(getter: Callable[[], Any], default: Any = "") -> Any:
    try:
        v = getter()
        return v if v is not None else default
    except Exception:
        return default


def _has_any(text: str, needles: tuple[str, ...]) -> bool:
    if not text:
        return False
    lower = text.lower()
    return any(n.lower() in lower for n in needles)


# Pull the bare app name out of taskbar/tray Name. Examples we want to map:
#   '微信 - 1 个运行窗口'                  -> '微信'
#   'Microsoft Teams - 1 个运行窗口 已固定' -> 'Microsoft Teams'
#   ' Microsoft Teams |新活动'             -> 'Microsoft Teams'
#   ' 微信'                                -> '微信'
#   ' 微信 (3条新消息)'                     -> '微信'
_APP_NAME_STRIP_RE = re.compile(
    r"\s*[-—–|(（]\s*\d.*$"      # tail like " - 1 个运行窗口" or " (3 条新消息)"
    r"|\s*\|.*$"                  # tail like " |新活动"
    r"|\s*-\s*\d.*$"
)


def _extract_app_name(raw_name: str) -> str:
    if not raw_name:
        return ""
    name = raw_name.strip()
    # Special case: tray icon Name is sometimes "App Name App Name |新活动"
    # (duplicated) — collapse.
    parts = name.split(" ")
    half = len(parts) // 2
    if half >= 1 and parts[:half] == parts[half:half * 2]:
        name = " ".join(parts[:half] + parts[half * 2:])
    name = _APP_NAME_STRIP_RE.sub("", name).strip()
    return name


# --------------------------------------------------------------------------
# Per-button cached state. Two property-changed events arrive at potentially
# different timings (Name then HelpText, or vice versa), so we track both and
# only emit a transition when the *combined* unread judgement flips.
# --------------------------------------------------------------------------
@dataclass
class _ButtonState:
    runtime_id: tuple[int, ...]
    class_name: str
    app_name: str
    last_name: str = ""
    last_helptext: str = ""
    last_unread: bool = False
    last_unread_ts_ms: int = 0
    seen_count: int = 0


def _looks_unread(name: str, helptext: str) -> bool:
    return _has_any(name, _UNREAD_NAME_HINTS) or _has_any(helptext, _FLASH_HELPTEXT_HINTS)


# --------------------------------------------------------------------------
# Public monitor
# --------------------------------------------------------------------------
class TaskbarUiaMonitor:
    """Event-driven taskbar notification detector via Windows UI Automation.

    Lifecycle: ``start()`` spawns the UIA worker; ``stop()`` tears it down.
    Both are idempotent.

    Emits to ``event_sink``:
      * ``taskbar_uia_started`` / ``taskbar_uia_stopped`` / ``taskbar_uia_error``
      * ``taskbar_uia_signal``       — every raw transition (for doze learning)
      * ``taskbar_notify_confirmed`` — when a transition crosses into "unread"
                                       and per-app preference allows it
                                       (shape compatible with visual channel)
    """

    def __init__(
        self,
        cfg: TaskbarUiaConfig,
        *,
        logging_cfg: LoggingConfig,
        event_sink: EventSink,
        on_confirmed: OnConfirmedCallback | None = None,
        external_cooldown_bump: ExternalCooldownBump | None = None,
    ) -> None:
        self.cfg = cfg
        self._sink = event_sink
        self._on_confirmed = on_confirmed
        self._bump = external_cooldown_bump

        self._stop = threading.Event()
        self._uia_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self._signal_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=200)
        self._state: dict[int, _ButtonState] = {}
        self._state_lock = threading.Lock()
        # Suppress repeated emits for the same app within this window. The
        # downstream `_on_confirmed` (auto_chat) already has its own dedupe,
        # but this dampens the raw signal too so doze logs aren't spammed.
        self._last_emit_app_ms: dict[str, int] = {}
        # v2.2: per-app "last sweep unread" state — drives the sweep's edge
        # detection (False→True transition emits a confirmed event). Without
        # this we'd re-emit on every sweep tick while the icon is flashing.
        self._sweep_prev_unread: dict[str, bool] = {}
        self._log_path = (resolve_logs_root(logging_cfg)
                          / "taskbar-monitor" / "uia-events.jsonl")
        # v2 snapshot-sweep miss log. Doze mines this to recommend which
        # apps need a visual fallback (their UIA event never fires even though
        # the taskbar button currently *looks* unread).
        self._misses_path = (resolve_logs_root(logging_cfg)
                             / "taskbar-monitor" / "uia-misses.jsonl")
        # Diagnostic probe — records every COM callback BEFORE any filtering,
        # plus attach outcomes. Lets us tell apart "COM never fires" from
        # "COM fires but sender class gets filtered out".
        self._probe_path = (resolve_logs_root(logging_cfg)
                            / "taskbar-monitor" / "uia-probe.jsonl")
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        # Roots we attached watchers to — kept so we can detach in stop().
        # `_watched_roots` keeps the high-level uiautomation Control objects
        # (purely for diagnostics); `_watched_native_elements` keeps the raw
        # IUIAutomationElement pointers actually used to detach.
        self._watched_roots: list[Any] = []
        self._watched_native_elements: list[Any] = []
        # Raw IUIAutomation interface + our COM handler — created on the UIA
        # thread inside `_uia_main` once the apartment is initialised.
        self._iuia: Any = None
        self._com_handler: Any = None
        # v2.1: Win11 22H2+ does NOT propagate XAML-island PropertyChanged
        # events up to the Shell_TrayWnd root through TreeScope_Subtree (the
        # root is a Win32 HWND, the buttons are XAML — there's a UIA proxy
        # seam that swallows the events). Workaround is to ALSO attach a
        # TreeScope_Element handler on each taskbar button individually, and
        # re-attach on every sweep tick so newly-launched apps' buttons are
        # covered. ``_per_button_attached`` is a set of hash(runtime_id) used
        # to avoid double-attaching the same button.
        self._per_button_attached: set[int] = set()
        self._attach_prop_array: Any = None  # built once in _uia_main
        self._attach_prop_count: int = 0
        self._attach_tree_scope_element: int = 0
        self._prefs = taskbar_sources.load()
        # v2: per-app timestamp of the most recent raw UIA signal that made
        # it through `_handle_signal`. The snapshot sweep diffs this against
        # the live UIA tree to detect "taskbar looks unread but event channel
        # never spoke" apps. Updated under `_recent_apps_lock` from the worker
        # thread; read under the same lock from the UIA thread sweep.
        self._recent_event_apps_ms: dict[str, int] = {}
        self._recent_apps_lock = threading.Lock()

    # ----- lifecycle -----

    def start(self) -> None:
        if not self.cfg.enabled:
            self._emit({"event": "taskbar_uia_skipped", "reason": "disabled_in_config"})
            return
        if self._uia_thread and self._uia_thread.is_alive():
            return
        self._stop.clear()
        self._uia_thread = threading.Thread(
            target=self._uia_main, name="lucid-taskbar-uia", daemon=True
        )
        self._worker_thread = threading.Thread(
            target=self._worker_main, name="lucid-taskbar-uia-worker", daemon=True
        )
        self._uia_thread.start()
        self._worker_thread.start()

    def stop(self) -> None:
        self._stop.set()
        # Best-effort detach. The handlers are COM-managed and the apartment
        # tears down when the thread exits anyway, so failures here are fine.
        try:
            iuia = self._iuia
            handler = self._com_handler
            if iuia is not None and handler is not None:
                for elem in self._watched_native_elements:
                    try:
                        iuia.RemovePropertyChangedEventHandler(elem, handler)
                    except Exception:
                        pass
            self._watched_native_elements.clear()
        except Exception:
            pass
        if self._uia_thread:
            self._uia_thread.join(timeout=2)
        if self._worker_thread:
            self._worker_thread.join(timeout=2)
        self._emit({"event": "taskbar_uia_stopped"})

    def reload_prefs(self) -> None:
        """Re-read ``~/.lucid/taskbar_sources.json``. Call after doze writes."""
        self._prefs = taskbar_sources.load()

    # ----- UIA thread -----

    def _uia_main(self) -> None:
        try:
            import uiautomation as uia  # type: ignore
            import comtypes  # type: ignore
            import comtypes.client as cc  # type: ignore
            from comtypes.gen.UIAutomationClient import (  # type: ignore
                CUIAutomation,
                IUIAutomation,
                IUIAutomationPropertyChangedEventHandler,
                UIA_NamePropertyId,
                UIA_HelpTextPropertyId,
                TreeScope_Subtree,
                TreeScope_Element,
            )
        except ImportError as exc:
            self._emit({
                "event": "taskbar_uia_error",
                "stage": "import",
                "message": f"uiautomation/comtypes missing: {exc}",
            })
            return
        try:
            # Initialise COM in MTA mode for this thread. UIA event handlers
            # *must* be attached from an MTA thread (per MSDN) — STA threads
            # silently never receive callbacks. uiautomation's helper does
            # CoInitializeEx(MULTITHREADED) for us.
            try:
                uia.InitializeUIAutomationInCurrentThread()
            except Exception:
                pass

            # Raw IUIAutomation interface — `uiautomation.Control` doesn't
            # expose the event-subscription methods, so we drop down here.
            self._iuia = cc.CreateObject(CUIAutomation, interface=IUIAutomation)

            # Build the COM handler ONCE; reuse for every root.
            self._com_handler = _make_property_handler(
                IUIAutomationPropertyChangedEventHandler,
                self._on_property_changed,
                self._append_probe,
            )

            roots = self._find_roots(uia)
            if not roots:
                self._emit({
                    "event": "taskbar_uia_error",
                    "stage": "find_roots",
                    "message": "Shell_TrayWnd not found",
                })
                return
            # Seed state cache so the first real change has a meaningful "prev".
            self._seed_cache(uia, roots)
            # Capture TreeScope_Element value for the per-button helper +
            # sweep re-attach paths (numeric COM enum, stable).
            self._attach_tree_scope_element = int(TreeScope_Element)
            self._attach_handlers_raw(
                roots,
                tree_scope=TreeScope_Subtree,
                property_ids=[UIA_NamePropertyId, UIA_HelpTextPropertyId],
            )
            self._emit({
                "event": "taskbar_uia_started",
                "roots": len(roots),
                "buttons_seen": len(self._state),
            })
            # Park here so the COM apartment stays alive for callbacks.
            # Piggy-back the snapshot sweep on this thread — it reuses the
            # already-initialised MTA + IUIAutomation, and re-walking the
            # subtree once a minute is cheap (a few hundred microseconds).
            sweep_interval_sec = float(
                getattr(self.cfg, "snapshot_sweep_interval_sec", 60.0)
            )
            next_sweep_mono = time.monotonic() + sweep_interval_sec if sweep_interval_sec > 0 else 0.0
            while not self._stop.wait(0.5):
                if sweep_interval_sec > 0 and time.monotonic() >= next_sweep_mono:
                    try:
                        self._run_sweep(uia, roots, sweep_interval_sec)
                    except Exception as exc:
                        self._append_probe({
                            "kind": "sweep_error",
                            "error": f"{type(exc).__name__}: {exc}",
                            "ts_ms": _ts_ms(),
                        })
                    next_sweep_mono = time.monotonic() + sweep_interval_sec
        except Exception as exc:
            self._emit({
                "event": "taskbar_uia_error",
                "stage": "loop",
                "message": f"{type(exc).__name__}: {exc}",
            })
            self._append_probe({
                "kind": "uia_main_crash",
                "error": f"{type(exc).__name__}: {exc}",
                "ts_ms": _ts_ms(),
            })
        finally:
            try:
                import uiautomation as _uia2  # type: ignore
                _uia2.UninitializeUIAutomationInCurrentThread()
            except Exception:
                pass

    def _find_roots(self, uia: Any) -> list[Any]:
        root = uia.GetRootControl()
        out: list[Any] = []
        for cls_name in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            p = root.PaneControl(ClassName=cls_name)
            if p.Exists(maxSearchSeconds=1):
                out.append(p)
        return out

    def _seed_cache(self, uia: Any, roots: list[Any]) -> None:
        for root in roots:
            for ctrl, _depth in uia.WalkControl(root, includeTop=False, maxDepth=12):
                cls = _safe(lambda c=ctrl: c.ClassName)
                if cls not in _TASKBAR_BUTTON_CLASSES:
                    continue
                self._update_state(ctrl, cls, force_seed=True)

    def _attach_handlers_raw(
        self,
        roots: list[Any],
        *,
        tree_scope: int,
        property_ids: list[int],
    ) -> None:
        """Attach the COM handler to each root via the raw IUIAutomation API.

        ``roots`` are still uiautomation ``Control`` objects (because that's
        what we use to find Shell_TrayWnd); we pull the underlying native
        ``IUIAutomationElement`` out of ``.Element`` to feed the raw call.

        Also attaches a per-button ``TreeScope_Element`` handler on every
        already-seeded taskbar button. The subtree subscription on the root
        is kept as a best-effort safety net but Win11 22H2+ does not reliably
        propagate XAML-button events up to it, so the per-button handlers
        are the ones that actually fire in practice.
        """
        import ctypes
        # Use the *NativeArray* variant of the method — it takes a plain
        # LP_c_long + count instead of a SAFEARRAY, which is much easier to
        # marshal from Python than the SAFEARRAY-flavoured overload.
        count = len(property_ids)
        ArrType = ctypes.c_long * count
        prop_array = ArrType(*property_ids)
        # Stash so the sweep can re-attach handlers on newly-discovered
        # buttons without rebuilding the array every time.
        self._attach_prop_array = prop_array
        self._attach_prop_count = count
        # Note: TreeScope_Element is captured by value in the parent _uia_main
        # via the closure, but we also need it later in _run_sweep — store it.
        # The numeric value is part of the COM enum and is stable.
        for root in roots:
            root_cls = _safe(lambda r=root: r.ClassName)
            try:
                native = root.Element  # IUIAutomationElement pointer
                if native is None:
                    raise RuntimeError("root.Element is None")
                self._iuia.AddPropertyChangedEventHandlerNativeArray(
                    native,
                    tree_scope,
                    None,             # cacheRequest
                    self._com_handler,
                    prop_array,
                    count,
                )
                self._watched_roots.append(root)
                self._watched_native_elements.append(native)
                self._append_probe({
                    "kind": "attach_ok",
                    "root_class": root_cls,
                    "seeded_buttons": len(self._state),
                    "ts_ms": _ts_ms(),
                })
            except Exception as exc:
                self._append_probe({
                    "kind": "attach_fail",
                    "root_class": root_cls,
                    "error": f"{type(exc).__name__}: {exc}",
                    "ts_ms": _ts_ms(),
                })
                self._emit({
                    "event": "taskbar_uia_error",
                    "stage": "attach",
                    "message": f"{type(exc).__name__}: {exc}",
                })

        # Per-button TreeScope_Element handlers — the real workhorse on
        # Win11 22H2+. Walk all roots once and attach to each taskbar /
        # tray button we find.
        import uiautomation as _uia  # already imported in caller; re-import safe
        attached = 0
        for root in roots:
            for ctrl, _depth in _uia.WalkControl(root, includeTop=False, maxDepth=12):
                cls = _safe(lambda c=ctrl: c.ClassName)
                if cls not in _TASKBAR_BUTTON_CLASSES:
                    continue
                if self._attach_one_button(ctrl, cls):
                    attached += 1
        self._append_probe({
            "kind": "per_button_attach_summary",
            "attached_count": attached,
            "total_tracked": len(self._per_button_attached),
            "ts_ms": _ts_ms(),
        })

    def _attach_one_button(self, ctrl: Any, cls: str) -> bool:
        """Attach a TreeScope_Element PropertyChanged handler to a single
        taskbar/tray button. Idempotent (uses runtime_id dedup). Returns True
        if a new attach happened.
        """
        if self._iuia is None or self._com_handler is None:
            return False
        if self._attach_prop_array is None or self._attach_prop_count <= 0:
            return False
        rid_seq = _safe(lambda: ctrl.GetRuntimeId(), default=None)
        if rid_seq is None:
            return False
        try:
            runtime_id = tuple(int(x) for x in rid_seq)
        except TypeError:
            return False
        key = hash(runtime_id)
        if key in self._per_button_attached:
            return False
        native = _safe(lambda: ctrl.Element, default=None)
        if native is None:
            return False
        try:
            self._iuia.AddPropertyChangedEventHandlerNativeArray(
                native,
                self._attach_tree_scope_element,
                None,
                self._com_handler,
                self._attach_prop_array,
                self._attach_prop_count,
            )
            self._per_button_attached.add(key)
            self._watched_native_elements.append(native)
            return True
        except Exception as exc:
            self._append_probe({
                "kind": "per_button_attach_fail",
                "class_name": cls,
                "name": _safe(lambda c=ctrl: c.Name),
                "runtime_id": list(runtime_id),
                "error": f"{type(exc).__name__}: {exc}",
                "ts_ms": _ts_ms(),
            })
            return False

    # ----- COM-thread callback (keep tiny, hand off to worker) -----

    def _on_property_changed(self, sender, propertyId, propertyValue):  # noqa: N802 (COM API)
        # ``sender`` is a raw IUIAutomationElement (comtypes pointer); its
        # accessors are CurrentXxx, not the Pythonic Xxx on uiautomation.Control.
        try:
            cls = _safe(lambda: sender.CurrentClassName)
            name = _safe(lambda: sender.CurrentName)
            helptext = _safe(lambda: sender.CurrentHelpText)
            # Probe FIRST — unconditional record so we can tell whether the
            # callback fires at all and what classes actually come through.
            try:
                self._append_probe({
                    "kind": "callback",
                    "class_name": cls,
                    "name": name,
                    "helptext": helptext,
                    "prop_id": int(propertyId),
                    "prop_value": str(propertyValue)[:120],
                    "ts_ms": _ts_ms(),
                })
            except Exception:
                pass
            if cls not in _TASKBAR_BUTTON_CLASSES:
                return
            # Drop tray noise (clock etc.) — these are NOT in _TASKBAR_BUTTON_CLASSES
            # but defensive.
            if cls in _TRAY_NOISE_CLASSES:
                return
            rid_seq = _safe(lambda: sender.GetRuntimeId(), default=None)
            if rid_seq is None:
                return
            # GetRuntimeId returns a SAFEARRAY of int — comtypes unwraps it to
            # a Python list/tuple already.
            try:
                runtime_id = tuple(int(x) for x in rid_seq)
            except TypeError:
                runtime_id = (int(rid_seq),)
            try:
                self._signal_queue.put_nowait({
                    "runtime_id": runtime_id,
                    "class_name": cls,
                    "name": name,
                    "helptext": helptext,
                    "changed_property_id": int(propertyId),
                    "ts_ms": _ts_ms(),
                })
            except queue.Full:
                # Drop — worker will pick up the *next* change anyway since we
                # always re-read both props on the next event.
                pass
        except Exception:
            # Never throw from a COM callback.
            pass

    # ----- worker thread: judge + emit -----

    def _worker_main(self) -> None:
        suppress_ms = int(max(0.0, float(getattr(self.cfg, "per_app_emit_cooldown_sec", 8.0))) * 1000)
        while not self._stop.is_set():
            try:
                evt = self._signal_queue.get(timeout=0.3)
            except queue.Empty:
                continue
            try:
                self._handle_signal(evt, suppress_ms)
            except Exception as exc:
                self._emit({
                    "event": "taskbar_uia_error",
                    "stage": "handle",
                    "message": f"{type(exc).__name__}: {exc}",
                })

    def _update_state(self, ctrl, cls: str, *, force_seed: bool = False) -> _ButtonState | None:
        rid_seq = _safe(lambda: ctrl.GetRuntimeId(), default=None)
        if rid_seq is None:
            return None
        runtime_id = tuple(rid_seq)
        name = _safe(lambda: ctrl.Name)
        helptext = _safe(lambda: ctrl.HelpText)
        key = hash(runtime_id)
        with self._state_lock:
            st = self._state.get(key)
            if st is None:
                st = _ButtonState(
                    runtime_id=runtime_id,
                    class_name=cls,
                    app_name=_extract_app_name(name),
                )
                self._state[key] = st
            st.last_name = name
            st.last_helptext = helptext
            st.last_unread = _looks_unread(name, helptext)
            if not st.app_name:
                st.app_name = _extract_app_name(name)
            if force_seed:
                st.seen_count = 1
        return st

    def _handle_signal(self, evt: dict[str, Any], suppress_ms: int) -> None:
        runtime_id = evt["runtime_id"]
        key = hash(runtime_id)
        name = evt["name"]
        helptext = evt["helptext"]
        cls = evt["class_name"]
        now_ms = evt["ts_ms"]

        with self._state_lock:
            st = self._state.get(key)
            if st is None:
                st = _ButtonState(
                    runtime_id=runtime_id,
                    class_name=cls,
                    app_name=_extract_app_name(name),
                )
                self._state[key] = st
            prev_name = st.last_name
            prev_help = st.last_helptext
            prev_unread = st.last_unread
            cur_unread = _looks_unread(name, helptext)
            st.last_name = name
            st.last_helptext = helptext
            st.last_unread = cur_unread
            st.seen_count += 1
            if not st.app_name:
                st.app_name = _extract_app_name(name)
            app_name = st.app_name

        if name == prev_name and helptext == prev_help:
            return  # purely a duplicate event

        # Always persist the raw signal for doze.
        raw_record = {
            "ts_ms": now_ms,
            "class_name": cls,
            "app_name": app_name,
            "prev_name": prev_name,
            "now_name": name,
            "prev_helptext": prev_help,
            "now_helptext": helptext,
            "prev_unread": prev_unread,
            "now_unread": cur_unread,
            "changed_property_id": evt.get("changed_property_id"),
        }
        self._emit({"event": "taskbar_uia_signal", **raw_record})
        self._append_event_log(raw_record)
        # v2: remember that the event channel spoke for this app so the
        # snapshot sweep won't flag it as a miss this interval.
        if app_name:
            with self._recent_apps_lock:
                self._recent_event_apps_ms[app_name] = now_ms

        # Only act on transitions into the "unread" state.
        if not (cur_unread and not prev_unread):
            return

        # Per-app preference gate.
        decision = taskbar_sources.decide_source_allowed(self._prefs, app_name, "uia")
        if decision == "deny":
            self._emit({
                "event": "taskbar_uia_suppressed_by_pref",
                "app_name": app_name,
            })
            return

        last = self._last_emit_app_ms.get(app_name, 0)
        if suppress_ms > 0 and now_ms - last < suppress_ms:
            return
        self._last_emit_app_ms[app_name] = now_ms

        self._emit_confirmed(
            app_name=app_name,
            cls=cls,
            runtime_id=list(runtime_id),
            reason=self._build_reason(prev_name, name, prev_help, helptext),
            now_ms=now_ms,
        )

    def _emit_confirmed(
        self,
        *,
        app_name: str,
        cls: str,
        runtime_id: list[int],
        reason: str,
        now_ms: int,
    ) -> None:
        """Build + emit the ``taskbar_notify_confirmed`` payload and run the
        downstream side-effects (visual cooldown bump + ``_on_confirmed``).

        Shared between the COM event path (`_handle_signal`) and the polling
        sweep path (`_run_sweep`). Both paths gate by cooldown / pref upstream;
        this helper only does the emit + dispatch.
        """
        candidates = [app_name] if app_name else []
        payload = {
            "event": "taskbar_notify_confirmed",
            "source": "uia",
            "app_candidates": candidates,
            "confidence": 0.95,
            "reason": reason,
            "has_new_message": True,
            "raw": "",
            "uia_class": cls,
            "uia_runtime_id": runtime_id,
            "ts_ms": now_ms,
        }
        self._emit(payload)

        # Tell the visual monitor to back off — it would otherwise still see
        # the pixel change a tick later and pay for a step-2 LLM call.
        if self._bump is not None:
            try:
                self._bump("uia_confirmed",
                           candidates,
                           float(getattr(self.cfg, "visual_suppress_after_uia_sec", 20.0)))
            except Exception:
                pass

        # Fire the same downstream hook the visual monitor uses, so the
        # sidecar's auto-chat logic doesn't need to know which channel won.
        if self._on_confirmed is not None:
            try:
                self._on_confirmed(payload)
            except Exception as exc:
                self._emit({
                    "event": "taskbar_uia_error",
                    "stage": "on_confirmed",
                    "message": f"{type(exc).__name__}: {exc}",
                })

    @staticmethod
    def _build_reason(prev_name: str, name: str,
                      prev_help: str, helptext: str) -> str:
        bits = []
        if name != prev_name:
            bits.append(f"Name: {prev_name!r} -> {name!r}")
        if helptext != prev_help:
            bits.append(f"HelpText: {prev_help!r} -> {helptext!r}")
        return "; ".join(bits) if bits else "uia-detected unread"

    # ----- helpers -----

    def _emit(self, payload: dict[str, Any]) -> None:
        try:
            self._sink(payload)
        except Exception:
            pass

    def _run_sweep(self, uia: Any, roots: list[Any], interval_sec: float) -> None:
        """Snapshot-vs-event reconciliation. Runs on the UIA (MTA) thread.

        Walks every taskbar/tray button under the watched roots, computes
        ``_looks_unread`` from current Name+HelpText, and compares against
        the per-app event-channel timestamps in ``_recent_event_apps_ms``.
        Any app that *looks* unread now but had no event-channel signal in
        the last ``interval_sec`` seconds is appended to ``uia-misses.jsonl``
        as a miss record \u2014 doze mines this file to recommend turning on the
        visual fallback for that app.
        """
        now_ms = _ts_ms()
        snapshot: dict[str, dict[str, str]] = {}
        newly_attached = 0

        for root in roots:
            for ctrl, _depth in uia.WalkControl(root, includeTop=False, maxDepth=12):
                cls = _safe(lambda c=ctrl: c.ClassName)
                if cls not in _TASKBAR_BUTTON_CLASSES:
                    continue
                # v2.1: attach a per-button handler on any new button that
                # appeared since last sweep (e.g. user launched a new app).
                if self._attach_one_button(ctrl, cls):
                    newly_attached += 1
                nm = _safe(lambda c=ctrl: c.Name)
                ht = _safe(lambda c=ctrl: c.HelpText)
                if not _looks_unread(nm, ht):
                    continue
                app = _extract_app_name(nm)
                if not app:
                    continue
                # First-seen Name wins (avoids tray duplicate overwriting the
                # taskbar entry which usually has the cleaner Name).
                snapshot.setdefault(app, {
                    "current_name": nm,
                    "current_helptext": ht,
                    "class_name": cls,
                })

        if newly_attached > 0:
            self._append_probe({
                "kind": "sweep_attach",
                "newly_attached": newly_attached,
                "total_tracked": len(self._per_button_attached),
                "ts_ms": now_ms,
            })

        interval_ms = int(interval_sec * 1000)
        with self._recent_apps_lock:
            recent = dict(self._recent_event_apps_ms)

        # v2.2: edge-detection + emit. For every app the sweep currently
        # sees as unread, decide whether this is a NEW transition
        # (prev_sweep_unread=False) AND the event channel didn't already
        # cover it within the last interval. If so, treat the sweep as the
        # authoritative detection (Win11 22H2+ events are unreliable on the
        # XAML taskbar — see Docs/internal/visual-notify-taskbar.md) and
        # emit a normal `taskbar_notify_confirmed` so the auto-chat path
        # fires immediately. Cooldown via `_last_emit_app_ms` prevents
        # spamming the same app while the flash toggles on/off.
        misses: list[dict[str, Any]] = []
        suppress_ms = int(
            max(0.0, float(getattr(self.cfg, "per_app_emit_cooldown_sec", 8.0))) * 1000
        )
        for app, info in snapshot.items():
            last_event_ms = recent.get(app, 0)
            event_covered_recently = (
                last_event_ms > 0 and (now_ms - last_event_ms) <= interval_ms
            )
            prev_sweep_unread = self._sweep_prev_unread.get(app, False)

            # Only log to misses.jsonl on the RISING EDGE — when this app
            # first appears as unread in a sweep AND the event channel
            # didn't already cover it within the last interval. Without the
            # edge gate, an app that flashes unread for N minutes writes
            # ~2 rows/sec for the whole flash duration (one tracked WeChat
            # window can fill the file with ~50 KB of identical rows in a
            # single afternoon — exactly the bloat the user flagged). With
            # the gate, one real miss = one row, which matches the semantics
            # of `_emit_confirmed` promotion below and keeps the file useful
            # as a diagnostic.
            if not event_covered_recently and not prev_sweep_unread:
                misses.append({
                    "ts_ms": now_ms,
                    "app_name": app,
                    "class_name": info["class_name"],
                    "current_name": info["current_name"],
                    "current_helptext": info["current_helptext"],
                    "sweep_interval_sec": interval_sec,
                    "last_event_age_ms": (now_ms - last_event_ms) if last_event_ms else None,
                })

            # Promote to a real confirmed event only on the RISING edge.
            # If the sweep already saw the app as unread last tick, the
            # event was already emitted then (or pre-empted by cooldown).
            if prev_sweep_unread:
                continue
            # Per-app preference gate (same as event path).
            decision = taskbar_sources.decide_source_allowed(self._prefs, app, "uia")
            if decision == "deny":
                self._emit({
                    "event": "taskbar_uia_suppressed_by_pref",
                    "app_name": app,
                    "via": "sweep",
                })
                continue
            # Cooldown gate — shared with event path so the two never
            # double-emit for the same flash.
            last_emit = self._last_emit_app_ms.get(app, 0)
            if suppress_ms > 0 and (now_ms - last_emit) < suppress_ms:
                continue
            self._last_emit_app_ms[app] = now_ms
            self._emit_confirmed(
                app_name=app,
                cls=info["class_name"],
                runtime_id=[],
                reason=(
                    f"sweep: HelpText={info['current_helptext']!r} "
                    f"Name={info['current_name']!r}"
                ),
                now_ms=now_ms,
            )

        # Update prev-unread state: keep True for apps still seen as unread,
        # drop everything else (so the next True for a dropped app counts as
        # a fresh rising edge).
        self._sweep_prev_unread = {app: True for app in snapshot.keys()}

        if not misses:
            return

        try:
            with open(self._misses_path, "a", encoding="utf-8") as f:
                for m in misses:
                    f.write(json.dumps(m, ensure_ascii=False) + "\n")
        except OSError:
            pass

        self._emit({
            "event": "taskbar_uia_misses_detected",
            "count": len(misses),
            "apps": [m["app_name"] for m in misses],
            "sweep_interval_sec": interval_sec,
        })

    def _append_event_log(self, record: dict[str, Any]) -> None:
        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _append_probe(self, record: dict[str, Any]) -> None:
        """Best-effort diagnostic write. Never raises."""
        try:
            with open(self._probe_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass


def _make_property_handler(
    iface: Any,
    callback: Callable[[Any, int, Any], None],
    probe: Callable[[dict[str, Any]], None],
) -> Any:
    """Build a `comtypes.COMObject` subclass implementing
    ``IUIAutomationPropertyChangedEventHandler`` and instantiate it.

    Defined at module scope (not inline) so PyInstaller can introspect it.
    """
    import comtypes  # type: ignore

    class _PropertyChangedHandler(comtypes.COMObject):
        _com_interfaces_ = [iface]

        # COM method name follows comtypes convention: <iface>_<method>.
        # Returning 0 = S_OK.
        def IUIAutomationPropertyChangedEventHandler_HandlePropertyChangedEvent(  # noqa: N802
            self, sender, propertyId, newValue,
        ):
            try:
                callback(sender, propertyId, newValue)
            except Exception as exc:
                try:
                    probe({
                        "kind": "callback_error",
                        "error": f"{type(exc).__name__}: {exc}",
                        "ts_ms": _ts_ms(),
                    })
                except Exception:
                    pass
            return 0

    return _PropertyChangedHandler()


__all__ = ["TaskbarUiaMonitor"]
