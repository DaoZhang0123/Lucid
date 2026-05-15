"""Computer-use Tool 实现。

把 Anthropic computer_use_* 工具的 action 派发到本地 Screen + Input。
本地坐标系：动作前必先有一次截图（即 LLM 看到的画面），然后用该 Capture 的
scale 反算回物理坐标后再点。

参考：
https://docs.claude.com/en/docs/build-with-claude/computer-use
"""
from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from typing import Any

from .input_driver import InputDriver
from .cursor_indicator import pulse_click as _crab_pulse_click
from .screen import Capture, ScreenLevel, ScreenSensor


@dataclass
class ToolResult:
    output: str | None = None
    error: str | None = None
    image_png: bytes | None = None
    # Optional Capture that should become the new ``ComputerTool.last_capture``
    # so subsequent click coordinates are reverse-mapped against it. Set by
    # tools that hand the model a non-screenshot image (e.g. R2 launch_app L2,
    # R3 click-verify L2). Loop.py is responsible for installing it.
    attached_capture: Capture | None = None


_LEVEL_ALIASES = {
    None: ScreenLevel.L1,
    "": ScreenLevel.L1,
    "l1": ScreenLevel.L1,
    "fullscreen": ScreenLevel.L1,
    "full": ScreenLevel.L1,
    "screen": ScreenLevel.L1,
    "l2": ScreenLevel.L2,
    "active_window": ScreenLevel.L2,
    "active": ScreenLevel.L2,
    "window": ScreenLevel.L2,
    "l3": ScreenLevel.L3,
    "cursor_local": ScreenLevel.L3,
    "cursor": ScreenLevel.L3,
    "local": ScreenLevel.L3,
    "l0": ScreenLevel.L0,
    "icon_atlas": ScreenLevel.L0,
    "atlas": ScreenLevel.L0,
    "icons": ScreenLevel.L0,
}


def _parse_level(raw: Any) -> ScreenLevel:
    if isinstance(raw, ScreenLevel):
        return raw
    key = raw.lower().strip() if isinstance(raw, str) else raw
    return _LEVEL_ALIASES.get(key, ScreenLevel.L1)


class ComputerTool:
    """Anthropic computer_use_20250124 兼容动作派发器。"""

    def __init__(self, sensor: ScreenSensor, driver: InputDriver,
                 screenshot_cfg: Any = None) -> None:
        self.sensor = sensor
        self.driver = driver
        # ScreenshotConfig (used for R3 click-verify thresholds). May be None
        # in legacy / test contexts; in that case R3 is a no-op.
        self.screenshot_cfg = screenshot_cfg
        # 最近一次发给 LLM 的截图（用于坐标反算）。为 None 表示本任务还
        # 没有任何可点击的坐标系。L0 (icon_atlas) 不会被装入这里 — 它是
        # 合成图，没有屏幕偏移。
        self.last_capture: Capture | None = None
        # Separately remember the most recent L1/L2 ("macro") capture so a
        # tiny L3 cursor_local taken just to *verify* a click doesn't shrink
        # the validator's accepted coordinate domain. Without this, the
        # model after an L3 has to either re-screenshot a full window or
        # restrict every click to a 893x112 tile around the previous
        # cursor position — which is almost never what it actually wants.
        self.last_macro_capture: Capture | None = None

    # ----- 暴露给 LLM 的工具 schema -----
    def tool_param(self, screen_w: int, screen_h: int) -> dict[str, Any]:
        """Anthropic 原生 Messages API 用（保留供未来直连官方 API 时使用）。"""
        return {
            "type": "computer_20250124",
            "name": "computer",
            "display_width_px": screen_w,
            "display_height_px": screen_h,
            "display_number": 1,
        }

    @staticmethod
    def openai_tool_schema(screen_w: int, screen_h: int) -> dict[str, Any]:
        """OpenAI Chat Completions function tool schema（走 LiteLLM 代理时用）。"""
        return {
            "type": "function",
            "function": {
                "name": "computer",
                "description": (
                    f"Control the Windows desktop. The default screenshots you receive are {screen_w}x{screen_h} pixels (level=fullscreen); "
                    "use that coordinate space when specifying `coordinate`. You can request a more detailed screenshot by calling "
                    "action='screenshot' with level='active_window' (the focused window only) or level='cursor_local' (a small patch around the mouse). "
                    "Coordinates you give afterwards are interpreted in the most recent screenshot's coordinate space."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": [
                                "screenshot",
                                "mouse_move",
                                "left_click",
                                "right_click",
                                "middle_click",
                                "double_click",
                                "triple_click",
                                "left_mouse_down",
                                "left_mouse_up",
                                "left_click_drag",
                                "scroll",
                                "type",
                                "key",
                                "hold_key",
                                "wait",
                                "cursor_position",
                            ],
                            "description": "The desktop action to perform.",
                        },
                        "level": {
                            "type": "string",
                            "enum": ["fullscreen", "active_window", "cursor_local", "icon_atlas"],
                            "description": (
                                "Only for action='screenshot'. "
                                "fullscreen = whole virtual desktop (~150 KB JPEG, use only to find a window or pick between apps); "
                                "active_window = the currently focused window only (~150 KB, default for in-app work); "
                                "cursor_local = a small high-detail patch around the mouse cursor (~10 KB, use to confirm a click landed on the right element); "
                                "icon_atlas = labelled grid of all installed app icons (NOT a screen — you cannot click on its coordinates; use only to identify an unfamiliar small icon)."
                            ),
                        },
                        "coordinate": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "minItems": 2,
                            "maxItems": 2,
                            "description": (
                                "[x, y] in the most recent screenshot's pixel coordinates. "
                                "For `left_click_drag` this is the DESTINATION of the drag — the cursor's "
                                "CURRENT position is used as the start, so you MUST issue a `mouse_move` "
                                "(or a left_click that lands you on the start) just before the drag; calling "
                                "`left_click_drag` without first moving the cursor will silently drag from "
                                "wherever the cursor happens to be (often nowhere useful)."
                            ),
                        },
                        "text": {
                            "type": "string",
                            "description": "Text to type, or a key combo like 'ctrl+c' for action='key'.",
                        },
                        "scroll_direction": {
                            "type": "string",
                            "enum": ["up", "down", "left", "right"],
                        },
                        "scroll_amount": {"type": "integer", "description": "Number of scroll ticks."},
                        "duration": {"type": "number", "description": "Seconds, for wait/hold_key."},
                        "confirmed": {
                            "type": "boolean",
                            "description": (
                                "Only meaningful for click actions "
                                "(left_click / right_click / middle_click / double_click / triple_click / left_click_drag) "
                                "when a `coordinate` is supplied. Default false. "
                                "When false (or omitted), the click is NOT performed; instead, the system captures a high-detail L3 "
                                "(`cursor_local`) tile around the target screen coordinate and returns it as the tool result, so you can "
                                "verify what is actually under the cursor at that pixel right now. After inspecting the tile, if you still "
                                "want to click, re-issue the SAME action with the SAME coordinate and `confirmed=true` to actually perform the click. "
                                "If the area is wrong, change your plan instead."
                            ),
                        },
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
            },
        }

    # ----- 派发 -----
    def dispatch(self, action: str, params: dict[str, Any]) -> ToolResult:
        # Wall-clock watchdog around every computer-tool action. Win32 input
        # APIs (pyautogui → SendInput / AttachThreadInput, UIA accessors) can
        # block indefinitely when a modal steals focus, an app's UI thread
        # hangs, or the system enters a Ctrl-Alt-Del / secure-desktop state.
        # See run 20260512-182234 O2: a `key Delete` issued after closing a
        # save-prompt dialog produced no further events for 11+ minutes and
        # wedged the entire sidecar. The watchdog returns a `ToolResult`
        # error to the agent loop after `timeout_s` instead of hanging,
        # letting the LLM recover (try screenshot, alt approach, etc.).
        result_box: dict[str, Any] = {}

        def _runner() -> None:
            try:
                result_box["value"] = self._dispatch(action, params)
            except BaseException as exc:  # noqa: BLE001 — re-raised on main
                result_box["error"] = exc

        # Generous: long type_text + per-char delay can legitimately take a
        # while; everything else should be sub-second. 20s catches a wedge
        # quickly without false-positiving on slow IME / multi-line typing.
        timeout_s = 20.0 if action == "type" else 15.0
        # 蟹钳点击脉冲：click / drag 类动作执行期间将光标换为闭合态，
        # ~120ms 后还原为张开态。脱离 CrabCursor block 时自动 no-op。
        _PULSE_ACTIONS = {
            "left_click", "right_click", "middle_click",
            "double_click", "triple_click", "left_click_drag",
        }
        pulse_ctx = _crab_pulse_click() if action in _PULSE_ACTIONS else None
        t = threading.Thread(
            target=_runner, daemon=True, name=f"computer_tool_{action}"
        )
        if pulse_ctx is not None:
            pulse_ctx.__enter__()
        try:
            t.start()
            t.join(timeout=timeout_s)
        finally:
            if pulse_ctx is not None:
                try:
                    pulse_ctx.__exit__(None, None, None)
                except Exception:
                    pass
        if t.is_alive():
            return ToolResult(
                error=(
                    f"action={action!r} timed out after {timeout_s:.0f}s — "
                    f"the input layer is wedged (focus-stealing modal, hung "
                    f"UI thread, or secure-desktop). Try `action=screenshot` "
                    f"to see current state."
                )
            )
        if "error" in result_box:
            exc = result_box["error"]
            return ToolResult(error=f"{type(exc).__name__}: {exc}")
        return result_box.get("value") or ToolResult(
            error=f"action={action!r} returned no result"
        )

    # ----- R4: coordinate bounds hard-check -----
    _COORD_ACTIONS = {
        "mouse_move", "left_click", "right_click", "middle_click",
        "double_click", "triple_click", "left_mouse_down", "left_mouse_up",
        "left_click_drag", "scroll",
    }

    @staticmethod
    def _maybe_unscreen_coord(cap: "Capture", ix: int, iy: int) -> tuple[int, int] | None:
        """If ``(ix, iy)`` looks like **screen-pixel** coordinates that fall
        inside the capture's screen rect, translate them back into image
        pixel coordinates and return the adjusted ``(px, py)``. Returns
        ``None`` if the point is not inside the screen rect or already a
        valid in-image coordinate.

        Why: the model is told to give image-pixel coords, but it sometimes
        slips and gives the corresponding screen coords (e.g. for an L2 of
        an active window with offset (1224, 375), it might say ``(1420, 556)``
        which is screen-frame, when the in-image equivalent is ``(196, 181)``).
        Both forms unambiguously identify the same pixel — accept either.
        """
        ox, oy = cap.offset
        rw, rh = cap.raw_size
        sw, sh = cap.sent_size
        # Already a valid in-image coord? Don't touch.
        if 0 <= ix < sw and 0 <= iy < sh:
            return None
        # Falls inside the capture's screen rect? Treat as screen coord.
        if ox <= ix < ox + rw and oy <= iy < oy + rh:
            scale_x = sw / rw if rw else 1.0
            scale_y = sh / rh if rh else 1.0
            px = int(round((ix - ox) * scale_x))
            py = int(round((iy - oy) * scale_y))
            # Clamp to image bounds (rounding can land on the edge).
            px = max(0, min(sw - 1, px))
            py = max(0, min(sh - 1, py))
            return px, py
        return None

    # Click actions also accept "no coordinate = use current cursor pos", so
    # only enforce bounds when a coordinate IS provided. mouse_move + drag
    # always require a coordinate, but we still gate on presence to avoid
    # double-erroring (the lower handler will surface "missing coordinate").
    def _check_coord_bounds(self, action: str, params: dict[str, Any]) -> ToolResult | None:
        if action not in self._COORD_ACTIONS:
            return None
        coord = params.get("coordinate")
        if not (isinstance(coord, (list, tuple)) and len(coord) == 2):
            return None
        try:
            ix, iy = int(coord[0]), int(coord[1])
        except (TypeError, ValueError):
            return ToolResult(error=f"coordinate must be two integers, got {coord!r}")
        cap = self.last_capture
        if cap is None:
            return ToolResult(error=(
                "no screenshot taken yet; call action='screenshot' first so coordinates "
                "have a known reference frame."
            ))
        sw, sh = cap.sent_size
        if not (0 <= ix < sw and 0 <= iy < sh):
            # Tolerance: accept screen-pixel coords if they fall inside the
            # capture's screen rect — see ``_maybe_unscreen_coord`` docstring.
            adj = self._maybe_unscreen_coord(cap, ix, iy)
            if adj is not None:
                params["coordinate"] = [adj[0], adj[1]]
                # Breadcrumb: the model gave screen-pixel coords; we
                # silently translated them to image-pixel. Surface this
                # in stderr (-> run.log) so a stale-local-variable
                # regression like the 20260511 WeChat click bug is
                # caught at a glance instead of needing math.
                print(
                    f"[coord_unscreened] {action} ({ix},{iy}) screen-pixel "
                    f"-> ({adj[0]},{adj[1]}) image-pixel "
                    f"(cap level={cap.level.value} sent={cap.sent_size} "
                    f"raw={cap.raw_size} offset={cap.offset})",
                    file=sys.stderr,
                    flush=True,
                )
                return None
            # L3 cursor_local fallback: a tiny verification tile must NOT
            # shrink the accepted coordinate domain. If the click doesn't
            # fit the L3 frame, transparently retry against the most
            # recent L1/L2 capture — that's almost always what the model
            # was actually targeting (it took the L3 just to "look at the
            # cursor", not to redefine where it can click).
            macro = self.last_macro_capture
            if (
                cap.level is ScreenLevel.L3
                and macro is not None
                and macro is not cap
            ):
                msw, msh = macro.sent_size
                if 0 <= ix < msw and 0 <= iy < msh:
                    self.last_capture = macro
                    print(
                        f"[coord_l3_fallback] {action} ({ix},{iy}) outside "
                        f"L3 {cap.sent_size} but inside L{macro.level.value} "
                        f"macro frame {macro.sent_size}; reverting active "
                        f"frame to the macro capture.",
                        file=sys.stderr,
                        flush=True,
                    )
                    return None
                madj = self._maybe_unscreen_coord(macro, ix, iy)
                if madj is not None:
                    params["coordinate"] = [madj[0], madj[1]]
                    self.last_capture = macro
                    print(
                        f"[coord_l3_fallback] {action} ({ix},{iy}) screen-pixel "
                        f"-> ({madj[0]},{madj[1]}) image-pixel against macro "
                        f"capture (level={macro.level.value} sent={macro.sent_size} "
                        f"offset={macro.offset}).",
                        file=sys.stderr,
                        flush=True,
                    )
                    return None
            ox, oy = cap.offset
            rw, rh = cap.raw_size
            return ToolResult(error=(
                f"coordinate ({ix},{iy}) is outside the most recent screenshot "
                f"({sw}x{sh}, level={cap.level.value}, screen rect "
                f"left={ox}, top={oy}, right={ox + rw}, bottom={oy + rh}). "
                f"Coordinates must be image-pixel (0..{sw - 1}, 0..{sh - 1}); "
                f"screen-pixel coords inside the rect above are also accepted "
                f"and auto-translated. If neither matches your target, take a "
                f"fresh screenshot first (action='screenshot' with the appropriate "
                f"level), then re-issue the action with coordinates inside the new image."
            ))
        # When a click coordinate is provided, also check it against the
        # **real-time** foreground window rect (Docs/screenshot.md v2 §4).
        # Catches the common bug where the model points at the previous
        # foreground app's old coords after focus has switched.
        if action in {
            "left_click", "right_click", "middle_click",
            "double_click", "triple_click", "left_click_drag",
        }:
            try:
                sx, sy = cap.model_to_screen(ix, iy)
            except Exception:
                return None
            try:
                err = self._validate_click_in_foreground(sx, sy)
            except Exception:
                # Guard must never turn into a fake tool error — fail open.
                err = None
            if err is not None:
                return ToolResult(error=err)
        return None

    @staticmethod
    def _validate_click_in_foreground(sx: int, sy: int) -> str | None:
        """Real-time foreground-window guard for clicks. Returns ``None`` if
        the click is allowed; otherwise an explanation string.

        Per Docs/screenshot.md v2 §4 we no longer rely on a pinned
        ``active_app_rect`` — every click queries ``GetForegroundWindow`` /
        ``GetWindowRect`` afresh (< 1 ms) so it stays correct even when the
        user dragged / resized / maximised the window mid-task.
        """
        try:
            from .window import active_window
        except Exception:
            return None
        try:
            win = active_window()
        except Exception:
            return None
        if win is None:
            return None
        # Read rect via getattr so a stale build that's missing the
        # `right`/`bottom` properties does NOT raise AttributeError and turn a
        # legitimate click into a fake "tool error" sent back to the model.
        # If anything is off, fail-open (return None == allow the click).
        try:
            left = int(win.left)
            top = int(win.top)
            width = int(win.width)
            height = int(win.height)
            right = int(getattr(win, "right", left + width))
            bottom = int(getattr(win, "bottom", top + height))
        except Exception:
            return None
        if width <= 0 or height <= 0:
            # Lock screen / secure desktop / no foreground — don't block.
            return None
        if left <= sx < right and top <= sy < bottom:
            return None
        return (
            f"click ({sx},{sy}) is outside foreground window "
            f"'{getattr(win, 'title', '?')}' rect ({left},{top})-({right},{bottom}); refusing. "
            f"If this is intentional (e.g. clicking a notification or another window), "
            f"call action='screenshot' with level='fullscreen' first to re-orient."
        )

    def _do_click(self, action: str, x: int, y: int, do_click) -> ToolResult:
        """Execute a click action and return a simple ack ToolResult."""
        do_click()
        return ToolResult(output=f"{action} {x},{y}")

    def _coord(self, raw: list[int] | tuple[int, int]) -> tuple[int, int]:
        """LLM 给的截图内坐标 → 物理屏幕坐标。"""
        cap = self.last_capture
        if cap is None:
            # 没截图过，直接当作物理坐标
            return int(raw[0]), int(raw[1])
        return cap.model_to_screen(int(raw[0]), int(raw[1]))

    def _dispatch(self, action: str, p: dict[str, Any]) -> ToolResult:
        a = action
        d = self.driver
        coord = p.get("coordinate")
        text = p.get("text")

        # R4: bounds-check coordinate before doing anything that uses it.
        bounds_err = self._check_coord_bounds(a, p)
        if bounds_err is not None:
            return bounds_err
        # _check_coord_bounds may have rewritten p["coordinate"] (e.g.
        # _maybe_unscreen_coord translating a screen-pixel guess back into
        # image-pixel). Re-read so the click handlers below see the fixed
        # value, not the stale original we captured a few lines up.
        coord = p.get("coordinate")

        if a == "screenshot":
            level = _parse_level(p.get("level"))
            cap = self.sensor.capture(level)
            # L0 (icon_atlas) is a synthetic image, not a screen region; do
            # NOT install it as the active coordinate frame, otherwise the
            # next click would reverse-map atlas-pixel coords to nonsense
            # screen coords. Per Docs/screenshot.md v2 §3.7 the loop
            # detects this via cap.level / cap.offset is None.
            if level is not ScreenLevel.L0:
                self.last_capture = cap
                # Track macro frames separately so click validation can
                # fall back to them after a tiny L3 verification capture.
                if level in (ScreenLevel.L1, ScreenLevel.L2):
                    self.last_macro_capture = cap
            tag = {
                ScreenLevel.L0: "L0/icon_atlas",
                ScreenLevel.L1: "L1/fullscreen",
                ScreenLevel.L2: "L2/active_window",
                ScreenLevel.L3: "L3/cursor_local",
            }[level]
            return ToolResult(
                image_png=cap.png_bytes(),
                output=f"{tag} {cap.sent_size[0]}x{cap.sent_size[1]} (raw {cap.raw_size[0]}x{cap.raw_size[1]} @ offset {cap.offset})",
                attached_capture=cap,
            )

        if a == "mouse_move":
            x, y = self._coord(coord)
            d.mouse_move(x, y)
            return ToolResult(output=f"moved to {x},{y}")

        if a == "left_click":
            x, y = (self._coord(coord) if coord else d.cursor_position())
            return self._do_click(a, x, y, lambda: d.left_click(x, y))

        if a == "right_click":
            x, y = (self._coord(coord) if coord else d.cursor_position())
            return self._do_click(a, x, y, lambda: d.right_click(x, y))

        if a == "middle_click":
            x, y = (self._coord(coord) if coord else d.cursor_position())
            return self._do_click(a, x, y, lambda: d.middle_click(x, y))

        if a == "double_click":
            x, y = (self._coord(coord) if coord else d.cursor_position())
            return self._do_click(a, x, y, lambda: d.double_click(x, y))

        if a == "triple_click":
            x, y = (self._coord(coord) if coord else d.cursor_position())
            def _triple() -> None:
                d.left_click(x, y); d.left_click(); d.left_click()
            return self._do_click(a, x, y, _triple)

        if a == "left_mouse_down":
            x, y = (self._coord(coord) if coord else d.cursor_position())
            d.left_mouse_down(x, y)
            return ToolResult(output=f"left_mouse_down {x},{y}")

        if a == "left_mouse_up":
            x, y = (self._coord(coord) if coord else d.cursor_position())
            d.left_mouse_up(x, y)
            return ToolResult(output=f"left_mouse_up {x},{y}")

        if a == "left_click_drag":
            x, y = self._coord(coord)
            d.left_click_drag(x, y)
            return ToolResult(output=f"drag to {x},{y}")

        if a == "scroll":
            direction = p.get("scroll_direction", "down")
            amount = int(p.get("scroll_amount", 3))
            x, y = (self._coord(coord) if coord else (None, None))
            d.scroll(x, y, direction, amount)
            return ToolResult(output=f"scroll {direction} {amount}")

        if a == "type":
            d.type_text(text or "")
            return ToolResult(output=f"typed {len(text or '')} chars")

        if a == "key":
            d.key(text or "")
            return ToolResult(output=f"key {text!r}")

        if a == "hold_key":
            duration = float(p.get("duration", 0.5))
            # 简化：按下指定键 duration 秒后释放
            import pyautogui
            keys = [k.lower() for k in (text or "").split("+") if k]
            for k in keys:
                pyautogui.keyDown(k)
            d.wait(duration)
            for k in reversed(keys):
                pyautogui.keyUp(k)
            return ToolResult(output=f"hold_key {text!r} {duration}s")

        if a == "wait":
            duration = float(p.get("duration", 1.0))
            d.wait(duration)
            return ToolResult(output=f"waited {duration}s")

        if a == "cursor_position":
            x, y = d.cursor_position()
            return ToolResult(output=f"cursor at {x},{y}")

        return ToolResult(error=f"unknown action: {action}")
