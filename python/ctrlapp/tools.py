"""Computer-use Tool 实现。

把 Anthropic computer_use_* 工具的 action 派发到本地 Screen + Input。
本地坐标系：动作前必先有一次截图（即 LLM 看到的画面），然后用该 Capture 的
scale 反算回物理坐标后再点。

参考：
https://docs.claude.com/en/docs/build-with-claude/computer-use
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .input_driver import InputDriver
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
    # Optional active-app screen rect ``(left, top, right, bottom)`` published
    # by tools that just brought a known app to the foreground (launch_app /
    # focus_window / region). Loop.py installs this onto
    # ``ComputerTool.active_app_rect``; while set, post-step captures are
    # auto-narrowed to this rect so the model's coordinate frame stays sticky
    # across non-screenshot actions, AND any click whose reverse-mapped screen
    # point falls outside this rect is rejected by ``_check_coord_bounds``.
    attached_active_rect: tuple[int, int, int, int] | None = None


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
        # 最近一次发给 LLM 的截图（用于坐标反算）
        self.last_capture: Capture | None = None
        # 当前活动 App 的屏幕矩形 (left, top, right, bottom)。launch_app /
        # focus_window / region 成功后由 loop.py 装填；之后每步 post-screenshot
        # 自动收窄到这个矩形（保证坐标系不漂移），并对落到矩形外的 click
        # 直接拒绝。模型显式 screenshot(level='fullscreen') 时清除（意图离开当前 App）。
        self.active_app_rect: tuple[int, int, int, int] | None = None
        # 是否已经向模型展示过当前 active_app_rect 的 L2 全景图。第一次锁定一个
        # App 时 loop.py 会用 L2；之后切换到 L3（cursor_local）以节省 token，
        # 因为模型已经有了窗口的“总体地图”。每次 active_app_rect 变化时由
        # ``set_active_app_rect`` 复位为 False。
        self.active_app_l2_shown: bool = False

    def set_active_app_rect(self, rect: tuple[int, int, int, int] | None) -> None:
        """Pin / unpin the active-app rect. Resets the L2-shown flag whenever
        the rect changes so the next post-step shows a fresh L2 once."""
        if rect == self.active_app_rect:
            return
        self.active_app_rect = rect
        self.active_app_l2_shown = False

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
                            "enum": ["fullscreen", "active_window", "cursor_local"],
                            "description": (
                                "Only for action='screenshot'. "
                                "fullscreen = whole virtual desktop (default, coarse, good for orientation); "
                                "active_window = the currently focused window only (medium, good for in-app work); "
                                "cursor_local = a small high-detail patch around the mouse cursor (best for precise targeting before a click)."
                            ),
                        },
                        "coordinate": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "minItems": 2,
                            "maxItems": 2,
                            "description": "[x, y] in the most recent screenshot's pixel coordinates.",
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
        try:
            return self._dispatch(action, params)
        except Exception as e:  # 防御性兜底
            return ToolResult(error=f"{type(e).__name__}: {e}")

    # ----- R4: coordinate bounds hard-check -----
    _COORD_ACTIONS = {
        "mouse_move", "left_click", "right_click", "middle_click",
        "double_click", "triple_click", "left_mouse_down", "left_mouse_up",
        "left_click_drag", "scroll",
    }
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
            return ToolResult(error=(
                f"coordinate ({ix},{iy}) is outside the most recent screenshot "
                f"({sw}x{sh}, level={cap.level.value}). Take a fresh screenshot first "
                f"(e.g. action='screenshot' with the appropriate level), then re-issue "
                f"the action with coordinates inside the new image."
            ))
        # When an active-app rect is pinned, also enforce that the resolved
        # screen point lies inside that rect — catches the common case where
        # the model is reading an old (different-frame) screenshot or pulled
        # a coord from memory that refers to a window which has since moved.
        if self.active_app_rect is not None:
            try:
                sx, sy = cap.model_to_screen(ix, iy)
            except Exception:
                return None
            left, top, right, bottom = self.active_app_rect
            if not (left <= sx < right and top <= sy < bottom):
                return ToolResult(error=(
                    f"coordinate ({ix},{iy}) reverse-maps to screen ({sx},{sy}), which "
                    f"is outside the active app rect (left={left}, top={top}, "
                    f"right={right}, bottom={bottom}). Either pick a coordinate inside "
                    f"the app's current window, or call action='screenshot' with "
                    f"level='fullscreen' to re-orient (this also releases the active-app pin)."
                ))
        return None

    # ----- R3: click pre/post pixel-diff verify (Docs/screenshot.md §13.4) -----
    def _click_with_verify(self, action: str, x: int, y: int, do_click) -> ToolResult:
        """Run a click action and (when enabled) compare the pixels around
        ``(x, y)`` before vs. after; if almost nothing changed, surface a
        warning + an L2 of the active window so the model can re-decide
        without needing to issue a separate screenshot turn.
        """
        cfg = self.screenshot_cfg
        enabled = bool(getattr(cfg, "click_verify_enabled", False)) if cfg else False
        radius = int(getattr(cfg, "click_verify_radius_px", 100)) if cfg else 100
        sleep_ms = int(getattr(cfg, "click_verify_post_sleep_ms", 150)) if cfg else 150
        threshold = float(getattr(cfg, "click_no_change_threshold", 0.005)) if cfg else 0.005

        if not enabled:
            do_click()
            return ToolResult(output=f"{action} {x},{y}")

        try:
            pre_cap = self.sensor.capture_around(x, y, radius)
        except Exception:
            pre_cap = None

        do_click()

        if sleep_ms > 0:
            time.sleep(sleep_ms / 1000.0)

        if pre_cap is None:
            return ToolResult(output=f"{action} {x},{y} (verify skipped: pre-capture failed)")

        try:
            post_cap = self.sensor.capture_around(x, y, radius)
            ratio = ScreenSensor.pixel_diff_ratio(pre_cap.image, post_cap.image)
        except Exception as e:
            return ToolResult(output=f"{action} {x},{y} (verify error: {type(e).__name__}: {e})")

        if ratio < threshold:
            try:
                l2 = self.sensor.capture(ScreenLevel.L2)
                self.last_capture = l2
                image_png = l2.png_bytes()
                hint = (
                    f"{action} executed at ({x},{y}) but only {ratio:.2%} of pixels "
                    f"changed near the cursor (threshold {threshold:.2%}). "
                    f"The click may have missed its target, or the target was "
                    f"unresponsive. A fresh L2 active-window screenshot is attached "
                    f"and is now your active coordinate frame."
                )
                return ToolResult(output=hint, image_png=image_png, attached_capture=l2)
            except Exception:
                return ToolResult(output=(
                    f"{action} {x},{y} — only {ratio:.2%} pixels changed near cursor "
                    f"(possible miss); follow-up L2 capture failed"
                ))

        return ToolResult(output=f"{action} {x},{y} (post-click pixel-change {ratio:.2%})")

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

        if a == "screenshot":
            level = _parse_level(p.get("level"))
            cap = self.sensor.capture(level)
            self.last_capture = cap
            # Explicit fullscreen request = model wants to re-orient on the
            # whole desktop; release the active-app pin so subsequent clicks
            # are once again interpreted in the L1 frame.
            if level is ScreenLevel.L1:
                self.set_active_app_rect(None)
            elif level is ScreenLevel.L2 and self.active_app_rect is not None:
                # Model explicitly asked for the active-window L2 again;
                # treat it as a re-orientation and let loop.py emit the next
                # post-step as L2 too if needed.
                self.active_app_l2_shown = True
            tag = {
                ScreenLevel.L1: "L1/fullscreen",
                ScreenLevel.L2: "L2/active_window",
                ScreenLevel.L3: "L3/cursor_local",
            }[level]
            return ToolResult(
                image_png=cap.png_bytes(),
                output=f"{tag} {cap.sent_size[0]}x{cap.sent_size[1]} (raw {cap.raw_size[0]}x{cap.raw_size[1]} @ offset {cap.offset})",
            )

        if a == "mouse_move":
            x, y = self._coord(coord)
            d.mouse_move(x, y)
            return ToolResult(output=f"moved to {x},{y}")

        if a == "left_click":
            x, y = (self._coord(coord) if coord else d.cursor_position())
            return self._click_with_verify(a, x, y, lambda: d.left_click(x, y))

        if a == "right_click":
            x, y = (self._coord(coord) if coord else d.cursor_position())
            return self._click_with_verify(a, x, y, lambda: d.right_click(x, y))

        if a == "middle_click":
            x, y = (self._coord(coord) if coord else d.cursor_position())
            return self._click_with_verify(a, x, y, lambda: d.middle_click(x, y))

        if a == "double_click":
            x, y = (self._coord(coord) if coord else d.cursor_position())
            return self._click_with_verify(a, x, y, lambda: d.double_click(x, y))

        if a == "triple_click":
            x, y = (self._coord(coord) if coord else d.cursor_position())
            def _triple() -> None:
                d.left_click(x, y); d.left_click(); d.left_click()
            return self._click_with_verify(a, x, y, _triple)

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
