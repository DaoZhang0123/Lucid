"""Computer-use Tool 实现。

把 Anthropic computer_use_* 工具的 action 派发到本地 Screen + Input。
本地坐标系：动作前必先有一次截图（即 LLM 看到的画面），然后用该 Capture 的
scale 反算回物理坐标后再点。

参考：
https://docs.claude.com/en/docs/build-with-claude/computer-use
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .input_driver import InputDriver
from .screen import Capture, ScreenLevel, ScreenSensor


@dataclass
class ToolResult:
    output: str | None = None
    error: str | None = None
    image_png: bytes | None = None


class ComputerTool:
    """Anthropic computer_use_20250124 兼容动作派发器。"""

    def __init__(self, sensor: ScreenSensor, driver: InputDriver) -> None:
        self.sensor = sensor
        self.driver = driver
        # 最近一次发给 LLM 的截图（用于坐标反算）
        self.last_capture: Capture | None = None

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
                    f"Control the Windows desktop. The screenshots you receive are {screen_w}x{screen_h} pixels; "
                    "use that coordinate space when specifying `coordinate`. Always start with action='screenshot' "
                    "if you have not seen the screen recently."
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
                        "coordinate": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "minItems": 2,
                            "maxItems": 2,
                            "description": "[x, y] in screenshot pixel coordinates.",
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

        if a == "screenshot":
            cap = self.sensor.capture(ScreenLevel.L1)
            self.last_capture = cap
            return ToolResult(image_png=cap.png_bytes(), output=f"L1 {cap.sent_size[0]}x{cap.sent_size[1]}")

        if a == "mouse_move":
            x, y = self._coord(coord)
            d.mouse_move(x, y)
            return ToolResult(output=f"moved to {x},{y}")

        if a == "left_click":
            x, y = (self._coord(coord) if coord else d.cursor_position())
            d.left_click(x, y)
            return ToolResult(output=f"left_click {x},{y}")

        if a == "right_click":
            x, y = (self._coord(coord) if coord else d.cursor_position())
            d.right_click(x, y)
            return ToolResult(output=f"right_click {x},{y}")

        if a == "middle_click":
            x, y = (self._coord(coord) if coord else d.cursor_position())
            d.middle_click(x, y)
            return ToolResult(output=f"middle_click {x},{y}")

        if a == "double_click":
            x, y = (self._coord(coord) if coord else d.cursor_position())
            d.double_click(x, y)
            return ToolResult(output=f"double_click {x},{y}")

        if a == "triple_click":
            x, y = (self._coord(coord) if coord else d.cursor_position())
            d.left_click(x, y)
            d.left_click()
            d.left_click()
            return ToolResult(output=f"triple_click {x},{y}")

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
