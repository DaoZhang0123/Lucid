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
2. **三级截图策略**（仅 action="screenshot" 时通过 level 参数选择）：
   - level="fullscreen"（默认）：整张虚拟桌面，用于建立全局认知、找窗口在哪。
   - level="active_window"：仅当前活动窗口，分辨率更高，用于在选定 App 内做事。
   - level="cursor_local"：鼠标周围的小块高细节图，用于点小按钮、读小字、确认是否选中前先看准。
   选完哪一级，后续 coordinate 就**用那张截图的坐标系**。系统会在每步动作后自动补一张 L1 截图。
3. 操作粒度尽量小：一次只做一步，做完后再截图验证。
4. 文本输入直接 action="type" + text="...". 本地驱动会用剪贴板粘贴，不受输入法影响，中文/英文/路径都可直接传。
5. **任何中间步骤都必须调用 `computer` 工具**；不要只输出"我将要…"或"Let me…"这样的旁白。
   唯一可以不调工具的场景：任务已经确认完成、或确认无法完成，此时请以 "任务完成:" 或 "任务失败:" 开头的文本总结。
6. 不要尝试关闭、重启系统或操作高权限窗口。
7. coordinate 必须是图片像素坐标（左上角原点），不要给百分比或相对坐标。

常用技巧：
- **保存/打开对话框定位文件**：优先在**文件名输入框**里直接 type 完整绝对路径（如 `C:\\Users\\xxx\\Desktop\\a.txt`）后按 Enter，
  或者点击对话框顶部的**地址栏**（路径面包屑）然后 type 目标路径回车——**不要**去左侧的"快速访问/此电脑"树里一层层点击导航，
  既慢又容易点错。
- 桌面路径：`%USERPROFILE%\\Desktop` 或 `C:\\Users\\<用户名>\\Desktop`，可以直接当成绝对路径 type 进去。
- 对话框里若文件名框已有默认值（如 `*.txt`），先 Ctrl+A 全选再 type 新路径，避免拼接出错。
"""


def _data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _prune_old_images(messages: list[dict[str, Any]], keep_n: int) -> int:
    """按级别 + 全局最近 N 张做滑窗裁剪。

    规则（保留集 = 下面两者的并集）：
      A) **每级最近一张**：L1/L2/L3 各自最近的一张图都保留，确保模型能拿到任一
         级别上最新的视觉证据；
      B) **全局最近 keep_n 张**：再额外保留出现顺序最末的 keep_n 张。

    其余图片在 content list 里被原地替换成占位文字（保留位置，避免破坏多模态结构）。
    keep_n <= 0 时不做裁剪。识别级别的方式：同一条 message 的 content list 里
    紧邻 image_url 之前的 text 片段（loop 在生成 user 多模态消息时会注入
    "[level=Lx] ..." 标记）；找不到时按 L1 处理。

    返回被裁掉的图片数量。
    """
    if keep_n <= 0:
        return 0

    # 收集所有 image_url 块及其级别标签，按消息出现顺序。
    entries: list[tuple[int, int, str]] = []  # (msg_idx, content_idx, level_tag)
    for mi, m in enumerate(messages):
        c = m.get("content")
        if not isinstance(c, list):
            continue
        last_text = ""
        for ci, part in enumerate(c):
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                last_text = part.get("text", "") or ""
            elif ptype == "image_url":
                tag = "L1"
                if "[level=L2]" in last_text:
                    tag = "L2"
                elif "[level=L3]" in last_text:
                    tag = "L3"
                entries.append((mi, ci, tag))

    if not entries:
        return 0

    keep_idx: set[int] = set()
    # 规则 A：每级最新一张
    for level in ("L1", "L2", "L3"):
        for i in range(len(entries) - 1, -1, -1):
            if entries[i][2] == level:
                keep_idx.add(i)
                break
    # 规则 B：全局最近 keep_n 张
    for i in range(max(0, len(entries) - keep_n), len(entries)):
        keep_idx.add(i)

    dropped = 0
    for i, (mi, ci, _) in enumerate(entries):
        if i in keep_idx:
            continue
        messages[mi]["content"][ci] = {
            "type": "text",
            "text": "[旧截图已省略以控制请求大小]",
        }
        dropped += 1
    return dropped


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
                            f"[level=L1] 以下是当前桌面截图（已缩放到 {screen_w}x{screen_h}，"
                            f"`computer` 工具的 coordinate 用这个坐标系）。"
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": _data_url(first.png_bytes())}},
                ],
            },
        ]

        nudges_left = 1  # 模型只输文本不调工具时的最多推一把次数
        # 行为兜底：保存/打开对话框激活态。看到 ctrl+s / ctrl+shift+s / F12 / ctrl+o 触发；
        # 命中后给若干步预算，超出或检测到 Enter/Escape 视为对话框已结束。
        save_dialog_armed_steps = 0
        # 哪些点击类动作需要架构层 L3 兜底（落点高清取证）
        click_actions = {
            "left_click", "right_click", "middle_click",
            "double_click", "triple_click", "left_click_drag",
        }
        save_dialog_triggers = {"ctrl+s", "ctrl+shift+s", "f12", "ctrl+o"}
        save_dialog_close_keys = {"return", "enter", "esc", "escape"}

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

            # ---- 行为兜底（保存对话框 sidebar guard）状态机 ----
            #  1) 本步出现保存触发键 → armed
            #  2) armed 状态下出现 Return/Esc → 视为已离开对话框，清状态
            #  3) armed 状态下若有点击且坐标位于截图左侧 ~25% → 触发纠正提示
            saved_dialog_hint: str | None = None
            if self.cfg.safety.save_dialog_sidebar_guard:
                sw = self.tool.last_capture.sent_size[0] if self.tool.last_capture else 1
                click_threshold_x = int(sw * 0.25)
                for rec in step_tool_records:
                    act = rec["action"]
                    args = rec["args"]
                    if act == "key":
                        keytxt = (args.get("text") or "").lower().replace(" ", "")
                        if keytxt in save_dialog_triggers:
                            save_dialog_armed_steps = 6
                        elif keytxt in save_dialog_close_keys and save_dialog_armed_steps > 0:
                            save_dialog_armed_steps = 0
                    if save_dialog_armed_steps > 0 and act in click_actions:
                        coord = args.get("coordinate")
                        if isinstance(coord, (list, tuple)) and len(coord) == 2:
                            cx = int(coord[0])
                            if cx < click_threshold_x:
                                saved_dialog_hint = (
                                    "提示：你刚刚在保存/打开对话框的左侧导航区点击了。"
                                    "下次请优先在文件名输入框 type 完整绝对路径并回车，"
                                    "或先点击对话框顶部的地址栏（路径面包屑）再 type 路径，"
                                    "**不要**在左侧『快速访问/此电脑』树里逐层点击。"
                                )
                if save_dialog_armed_steps > 0:
                    save_dialog_armed_steps -= 1

            # 架构层兜底：若本步执行了点击动作，额外抓一张 L3 落点高清图，
            # 让模型在下一轮能看清"我刚刚到底点中了什么"。可由 [safety].verify_click_with_l3 关闭。
            click_happened = (
                self.cfg.safety.verify_click_with_l3
                and any(rec["action"] in click_actions for rec in step_tool_records)
            )

            # 每步动作后给模型一张视觉输入：
            #   - 如果模型本步调用了 screenshot（可能是 L2/L3）：把那张原样回送，
            #     这样三级金字塔才真正起作用。
            #   - 否则补一张 L1 全屏，作为粗粒度的"动作后变化"验证图。
            if had_screenshot and self.tool.last_capture is not None:
                post = self.tool.last_capture
                tag = f"模型请求的截图 ({post.level.value})"
            else:
                post = self.sensor.capture(ScreenLevel.L1)
                self.tool.last_capture = post
                tag = "动作后截图 (L1)"
            level_tag = {
                ScreenLevel.L1: "L1",
                ScreenLevel.L2: "L2",
                ScreenLevel.L3: "L3",
            }[post.level]
            post_png = post.png_bytes()
            saved_name = log.save_image(post_png, f"step-{step + 1:03d}-post-{post.level.value}", level="INFO")

            # 抓 L3 落点取证图（如果本步有点击且当前 post 不是 L3 本身）
            verify_capture = None
            verify_png: bytes | None = None
            if click_happened and post.level is not ScreenLevel.L3:
                try:
                    verify_capture = self.sensor.capture(ScreenLevel.L3)
                    verify_png = verify_capture.png_bytes()
                    log.save_image(verify_png, f"step-{step + 1:03d}-verify-cursor_local", level="INFO")
                except Exception as e:  # 截边缘可能 mss 越界
                    log.warning(f"L3 verify capture failed: {e}")
                    verify_capture = None
                    verify_png = None

            log.step_record(
                {
                    "step": step + 1,
                    "assistant_text": text_content,
                    "tools": step_tool_records,
                    "post_image": saved_name,
                    "verify_image": "step-{:03d}-verify-cursor_local".format(step + 1) if verify_png else None,
                    "save_dialog_hint": bool(saved_dialog_hint),
                }
            )

            # 组装回送消息：post 主图（L1/L2/L3）+ 可选 L3 取证图 + 可选纠正提示
            content: list[dict[str, Any]] = [
                {"type": "text", "text": f"[level={level_tag}] {tag}（{post.sent_size[0]}x{post.sent_size[1]}）："},
                {"type": "image_url", "image_url": {"url": _data_url(post_png)}},
            ]
            if verify_png and verify_capture is not None:
                content.append({
                    "type": "text",
                    "text": (
                        f"[level=L3] 落点取证（鼠标周边 {verify_capture.sent_size[0]}x{verify_capture.sent_size[1]}）："
                        "用于核对你刚才点击/拖拽到底命中了什么。"
                        "**坐标系仍以上方主图为准**（这张 L3 仅供视觉验证，不要用它的像素坐标作为下一次 coordinate 的参考）。"
                    ),
                })
                content.append({"type": "image_url", "image_url": {"url": _data_url(verify_png)}})
            if saved_dialog_hint:
                content.append({"type": "text", "text": saved_dialog_hint})
                log.warning("save_dialog sidebar guard triggered; hint injected")

            messages.append({"role": "user", "content": content})

        log.warning("max_steps reached")
        log.close(status="max_steps")
        return "(达到最大步数预算，任务可能未完成)"
