"""ReAct loop —— 通过本地 LiteLLM 代理（OpenAI 兼容 /chat/completions）驱动 Claude。

工具：自定义的 OpenAI function tool `computer`，参数与 Anthropic computer_use_20250124
保持一致（action / coordinate / text / scroll_* / duration）。

图像通过 OpenAI 多模态 content 数组以 data URL 形式发送。
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any

from openai import APIError, OpenAI
from rich.console import Console

from .config import Config
from .input_driver import InputDriver
from .runlog import RunLogger
from .safety import SafetyLayer
from .screen import ScreenLevel, ScreenSensor
from .tools import ComputerTool, ToolResult

_console = Console()

SYSTEM_PROMPT = """\
你是一个运行在 Windows 桌面上的视觉 GUI Agent。
你只能通过 `computer` 工具与系统交互（截图、鼠标、键盘）。

工作原则：
1. 先用 action="screenshot" 看清当前屏幕，再决定下一步动作。
2. 操作粒度尽量小：一次只做一步，做完后再截图验证（系统会在每步动作后自动给你最新截图）。
3. 中文文本输入由本地驱动用剪贴板粘贴实现，请直接 action="type" + text="中文"。
4. **任何中间步骤都必须调用 `computer` 工具**；不要只输出“我将要…”或“Let me…”这样的旁白。
   唯一可以不调工具的场景：任务已经确认完成、或确认无法完成，此时请以 “任务完成:” 或 “任务失败:” 开头的文本总结。
5. 不要尝试关闭、重启系统或操作高权限窗口。
6. coordinate 必须是图片像素坐标（左上角原点），不要给百分比或相对坐标。
"""


def _data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _prune_old_images(messages: list[dict[str, Any]], keep_n: int) -> int:
    """把对话历史里"最旧的若干张截图"替换成占位文字，仅保留最近 keep_n 张。

    - keep_n <= 0 时不做裁剪。
    - 只动 list 形式的多模态 content；纯字符串 content 原样保留。
    - tool 消息（screenshot 工具结果）目前都是文本，不受影响。
    返回被裁掉的图片数量（用于日志）。
    """
    if keep_n <= 0:
        return 0

    # 收集所有 image_url 块的 (msg_idx, content_idx) 位置，按出现顺序。
    positions: list[tuple[int, int]] = []
    for mi, m in enumerate(messages):
        c = m.get("content")
        if not isinstance(c, list):
            continue
        for ci, part in enumerate(c):
            if isinstance(part, dict) and part.get("type") == "image_url":
                positions.append((mi, ci))

    if len(positions) <= keep_n:
        return 0

    drop = positions[: len(positions) - keep_n]
    for mi, ci in drop:
        messages[mi]["content"][ci] = {
            "type": "text",
            "text": "[旧截图已省略以控制请求大小]",
        }
    return len(drop)


def _resolve_proxy_key(api_key_in_cfg: str) -> str:
    return (
        api_key_in_cfg
        or os.environ.get("LITELLM_MASTER_KEY", "")
        or os.environ.get("OPENAI_API_KEY", "")
    )


class Agent:
    def __init__(self, cfg: Config, api_key: str | None = None) -> None:
        self.cfg = cfg
        proxy = cfg.llm.proxy
        key = api_key or _resolve_proxy_key(proxy.api_key)
        if not key:
            raise RuntimeError(
                "缺少代理 API Key：请设置 LITELLM_MASTER_KEY 环境变量，"
                "或在 config.toml 的 [llm.proxy].api_key 填入。"
            )
        self.client = OpenAI(base_url=proxy.base_url.rstrip("/"), api_key=key)
        self.model = proxy.model
        self.sensor = ScreenSensor(cfg.screenshot)
        self.driver = InputDriver(cfg.input)
        self.tool = ComputerTool(self.sensor, self.driver)
        self.safety = SafetyLayer(cfg.safety)

    def run(self, instruction: str) -> str:
        log = RunLogger(self.cfg.logging, instruction)
        log.info(
            f"proxy={self.cfg.llm.proxy.base_url} model={self.model} "
            f"max_steps={self.cfg.llm.max_steps} autonomy={self.cfg.safety.autonomy}"
        )
        try:
            return self._run(instruction, log)
        except Exception as e:
            log.error(f"{type(e).__name__}: {e}")
            log.close(status="error")
            raise

    def _run(self, instruction: str, log: RunLogger) -> str:
        # 起手一张 L1 截图
        first = self.sensor.capture(ScreenLevel.L1)
        self.tool.last_capture = first
        screen_w, screen_h = first.sent_size
        tool_schema = ComputerTool.openai_tool_schema(screen_w, screen_h)
        log.info(f"initial screenshot {screen_w}x{screen_h}")
        log.save_image(first.png_bytes(), "step-000-init", level="INFO")

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"任务：{instruction}\n\n"
                            f"以下是当前桌面截图（已缩放到 {screen_w}x{screen_h}，"
                            f"`computer` 工具的 coordinate 用这个坐标系）。"
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": _data_url(first.png_bytes())}},
                ],
            },
        ]

        nudges_left = 1  # 模型只输文本不调工具时的最多推一把次数
        for step in range(self.cfg.llm.max_steps):
            _console.rule(f"[cyan]Step {step + 1}/{self.cfg.llm.max_steps}")
            log.info(f"--- step {step + 1}/{self.cfg.llm.max_steps} ---")
            dropped = _prune_old_images(messages, self.cfg.llm.keep_recent_screenshots)
            if dropped:
                log.debug(f"pruned {dropped} old screenshot(s) from prompt")
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,  # type: ignore[arg-type]
                    tools=[tool_schema],  # type: ignore[arg-type]
                    max_tokens=self.cfg.llm.max_tokens,
                )
            except APIError as e:
                _console.print(f"[red]API 错误：{e}[/red]")
                log.error(f"API error: {e}")
                log.close(status="api_error")
                return f"(API error) {e}"

            msg = resp.choices[0].message
            tool_calls = list(msg.tool_calls or [])
            text_content = msg.content or ""

            if text_content.strip():
                _console.print(f"[white]{text_content}[/white]")
                log.info(f"assistant text: {text_content.strip()}")

            # 把 assistant 消息原样回塞
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": text_content}
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments or "{}",
                        },
                    }
                    for tc in tool_calls
                ]
            messages.append(assistant_msg)

            if not tool_calls:
                stripped = text_content.strip()
                completion_markers = ("任务完成", "任务失败", "无法完成", "task complete", "task failed", "cannot complete")
                looks_done = any(m in stripped.lower() if m.isascii() else m in stripped for m in completion_markers)
                if looks_done or nudges_left <= 0:
                    final = stripped or "(无文本输出)"
                    log.step_record({"step": step + 1, "final_text": final})
                    log.close(status="ok", final_text=final)
                    return final
                # 推一把：追加一条 user 消息，要求继续调工具或明确声明完成。
                nudges_left -= 1
                log.warning("assistant returned text without tool_call; nudging to continue")
                messages.append({
                    "role": "user",
                    "content": (
                        "请不要只输出旁白。要么调用 `computer` 工具执行下一步，"
                        "要么以 “任务完成:” 或 “任务失败:” 开头总结并结束。"
                    ),
                })
                continue
            nudges_left = 1  # 只要有动作就重置推一把预算

            # 派发每个 tool_call
            had_screenshot = False
            step_tool_records: list[dict[str, Any]] = []
            for tc in tool_calls:
                fn_name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                action = args.pop("action", "")
                _console.print(f"[magenta]→ {fn_name}.{action}[/magenta] {args}")
                log.info(f"tool_call {fn_name}.{action} {args}")

                if fn_name != "computer":
                    tr = ToolResult(error=f"unknown tool: {fn_name}")
                elif self.safety.should_confirm(action, args) and not self.safety.confirm(action, args):
                    tr = ToolResult(error="user declined this action")
                    log.warning(f"user declined {action} {args}")
                else:
                    tr = self.tool.dispatch(action, args)

                if action == "screenshot":
                    had_screenshot = True

                parts: list[str] = []
                if tr.output:
                    parts.append(tr.output)
                if tr.error:
                    parts.append(f"ERROR: {tr.error}")
                    log.error(f"tool error: {tr.error}")
                content_str = " | ".join(parts) if parts else "(ok)"

                # WARNING 级图像：供 image_level=WARNING 时仅错误时落盘使用
                if tr.error and tr.image_png:
                    log.save_image(tr.image_png, f"step-{step + 1:03d}-error-{action}", level="WARNING")
                elif action == "screenshot" and tr.image_png:
                    log.save_image(tr.image_png, f"step-{step + 1:03d}-shot", level="DEBUG")

                step_tool_records.append({"action": action, "args": args, "result": content_str})

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": content_str,
                    }
                )

            # 每步动作后追一张 L1 截图作为下一轮视觉输入。
            post = self.sensor.capture(ScreenLevel.L1)
            self.tool.last_capture = post
            tag = "动作后截图" if not had_screenshot else "最新截图"
            post_png = post.png_bytes()
            saved_name = log.save_image(post_png, f"step-{step + 1:03d}-post", level="INFO")
            log.step_record(
                {
                    "step": step + 1,
                    "assistant_text": text_content,
                    "tools": step_tool_records,
                    "post_image": saved_name,
                }
            )
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"{tag}（{post.sent_size[0]}x{post.sent_size[1]}）："},
                        {"type": "image_url", "image_url": {"url": _data_url(post_png)}},
                    ],
                }
            )

        log.warning("max_steps reached")
        log.close(status="max_steps")
        return "(达到最大步数预算，任务可能未完成)"
