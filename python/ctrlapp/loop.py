"""ReAct loop —— 通过本地 LiteLLM 代理（OpenAI 兼容 /chat/completions）驱动 Claude。

工具：自定义的 OpenAI function tool `computer`，参数与 Anthropic computer_use_20250124
保持一致（action / coordinate / text / scroll_* / duration）。

图像通过 OpenAI 多模态 content 数组以 data URL 形式发送。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from typing import Any, Callable

from openai import APIError, APIConnectionError, APIStatusError
from rich.console import Console

from .config import Config
from .input_driver import InputDriver
from .llm_client import (
    AnthropicClient,
    CopilotClient,
    LLMClient,
    OpenAIChatClient,
)
from .runlog import ThreadLog
from .safety import SafetyLayer
from .screen import ScreenLevel, ScreenSensor
from .tools import ComputerTool, ToolResult
from . import memory as memory_mod
from . import tooltips as tooltips_mod

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

操作技巧库（动态学习）：
- 下方"## 操作技巧"段是 tools.md 注入的**动态技巧库**，包含针对各类 App / 对话框 / 控件
  的稳妥操作方式。开始任务前先扫一眼，遇到对应场景照着做；尤其注意"不要覆盖用户正在编辑的
  内容"、"先 alt+tab 看是否已开着"、"保存对话框直接 type 绝对路径"这些通用准则。
- 当你**总结出一条值得长期保留的经验**（成功路径或失败教训）时，调用独立函数工具
  `learn_tip(text, kind)` 把它追加进 tools.md：
  - `kind` 取 `"success"` / `"failure"` / `"tip"` 之一。
  - 例：`learn_tip(text="Outlook 用 Ctrl+R 回复比鼠标点回复按钮更稳", kind="success")`
  - 例：`learn_tip(text="WeChat 输入框 Enter 直接发送，需要换行用 Shift+Enter", kind="failure")`
  - 写入前先看下方"## 操作技巧"已有内容，避免重复。
- **不要**把单次任务的临时事实（"刚把文件存到 D:\\tmp"）当成技巧；那既不是 tools.md 该收的，
  也不是 memory.md 该收的——直接在回答里说一下即可。

长期记忆：
- 通过 `remember(text)` 工具把值得长期保留的信息写入 memory.md。**不要**当成 computer 的 action，
  这是独立的一个 function，参数为单一字符串 `text`。
- 应当记下的内容（满足任一即可）：
  1) 用户**明确**要求："记住…/以后都…/我喜欢…/我的 X 是 Y"。
  2) 用户的**称呼/身份**：自报姓名、昵称、希望被怎么叫（"叫我老张"、"我是 zhang"）、职业 / 角色。
  3) 用户的**操作习惯/偏好**：常用浏览器、常用编辑器、常用快捷键、惯用语言（中文/英文）、
     默认保存路径、常用文件命名风格、惯用搜索引擎、是否喜欢深色模式等。
  4) 用户的**环境事实**且短期内不会变：常用的工作目录、常驻开着的 App、双屏 / 主屏分辨率
     的特殊点、桌面快捷方式排布约定，等等。
  写入前**先检索一下当前 memory.md 已有内容**（已注入在 system prompt 末尾），避免重复或冲突；
  若发现冲突（用户改变偏好），写一条新的覆盖性记录而不要保留旧的。
- 不应当记的：单次任务的临时事实（"刚刚把文件存到了 D:\\tmp"）、过程性中间结果、
  对屏幕一次性观察、密码 / token / 银行账号 / 验证码等隐私敏感信息。
- 形式要求：每条记忆**单行、不超过 200 字**、写陈述句而非命令式；可以一次任务里
  写 0~多条，但不要把同一意思拆成多条。
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


def _build_llm_client(cfg: Config, api_key_override: str | None) -> LLMClient:
    """按 cfg.llm.provider 选三种后端之一。

    "proxy"     -> OpenAI 兼容代理 (LiteLLM / OpenClaw / 任意 OpenAI 兼容端点)
    "anthropic" -> Anthropic 原生 Messages API
    "copilot"   -> GitHub Copilot OAuth

    显式设置但凭据缺失时，会按 anthropic > copilot > proxy 的优先级回退
    （避免“点了 copilot 却悄悄走 proxy”这种隐形错配）。
    """
    raw_provider = (cfg.llm.provider or "anthropic").strip().lower()

    # 候选探测（按用户指定的优先级）
    a = cfg.llm.anthropic
    anthropic_key = api_key_override or a.api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    has_anthropic = bool(anthropic_key)

    has_copilot = False
    copilot_tm = None
    try:
        from .auth.copilot import CopilotTokenManager  # 延迟导入避免循环
        copilot_tm = CopilotTokenManager(cfg.llm.copilot.state_file or None)
        has_copilot = bool(copilot_tm.status().get("logged_in"))
    except Exception:
        copilot_tm = None
        has_copilot = False

    proxy = cfg.llm.proxy
    proxy_key = api_key_override or _resolve_proxy_key(proxy.api_key)
    has_proxy = bool(proxy_key)

    # 把"auto" / 未知值，或显式 provider 但凭据缺失的情况，按优先级回退
    def _auto_pick() -> str:
        if has_anthropic:
            return "anthropic"
        if has_copilot:
            return "copilot"
        if has_proxy:
            return "proxy"
        return "anthropic"  # 没有任何凭据时，让下面 anthropic 分支报详细错

    if raw_provider not in ("anthropic", "copilot", "proxy"):
        provider = _auto_pick()
    elif raw_provider == "anthropic" and not has_anthropic and (has_copilot or has_proxy):
        provider = _auto_pick()
    elif raw_provider == "copilot" and not has_copilot and (has_anthropic or has_proxy):
        provider = _auto_pick()
    elif raw_provider == "proxy" and not has_proxy and (has_anthropic or has_copilot):
        provider = _auto_pick()
    else:
        provider = raw_provider

    if provider == "anthropic":
        if not anthropic_key:
            raise RuntimeError(
                "缺少 Anthropic API Key：请设置 ANTHROPIC_API_KEY 环境变量，"
                "或在设置页 Anthropic.api_key 填入。"
            )
        return AnthropicClient(
            api_key=anthropic_key,
            model=a.model,
            base_url=a.base_url,
            anthropic_version=a.anthropic_version,
        )
    if provider == "copilot":
        from .auth.copilot import CopilotAuthError, CopilotTokenManager
        c = cfg.llm.copilot
        tm = copilot_tm or CopilotTokenManager(c.state_file or None)
        if not tm.status().get("logged_in"):
            raise RuntimeError(
                "未登录 GitHub Copilot。请到设置页点 “登录 GitHub Copilot” 完成设备码授权。"
            )
        # 预热一次：提前发现 token 到期 / 账号被吃
        try:
            tm.get_active()
        except CopilotAuthError as e:
            raise RuntimeError(str(e))
        return CopilotClient(token_manager=tm, model=c.model)
    # proxy
    if not proxy_key:
        raise RuntimeError(
            "缺少代理 API Key：请设置 LITELLM_MASTER_KEY 环境变量，"
            "或在 config.toml 的 [llm.proxy].api_key 填入。"
        )
    return OpenAIChatClient(
        base_url=proxy.base_url,
        api_key=proxy_key,
        model=proxy.model,
        extra_headers=dict(proxy.extra_headers) if proxy.extra_headers else None,
    )


class CancelledError(RuntimeError):
    """外部取消驱动时抛出（例如 sidecar.cancel）。"""


EventSink = Callable[[dict[str, Any]], None]


class Agent:
    def __init__(
        self,
        cfg: Config,
        api_key: str | None = None,
        *,
        event_sink: EventSink | None = None,
        cancel_event: threading.Event | None = None,
        thread_log: ThreadLog | None = None,
    ) -> None:
        self.cfg = cfg
        self.client = _build_llm_client(cfg, api_key)
        self.model = self.client.model
        self.sensor = ScreenSensor(cfg.screenshot)
        self.driver = InputDriver(cfg.input)
        self.tool = ComputerTool(self.sensor, self.driver)
        self.safety = SafetyLayer(cfg.safety)
        self.event_sink = event_sink
        self.cancel_event = cancel_event
        self.thread_log = thread_log
        # md5(png) -> 代表该截图的文件名，用于 context.log 里用文件名替换 base64 图。
        self._image_names: dict[str, str] = {}
        self._current_step: int = 0

    # 事件上报：sidecar 模式下避免使用 rich 控制台（会污染 stdout JSONRPC 通道）
    def _emit(self, kind: str, **payload: Any) -> None:
        if self.event_sink is None:
            return
        try:
            evt = {"event": kind, **payload}
            self.event_sink(evt)
        except Exception:
            # event sink 出错不应该干扰主流程
            pass

    def _print(self, *args: Any, **kw: Any) -> None:
        """sidecar 模式下静默；CLI 模式下走 rich console。"""
        if self.event_sink is None:
            _console.print(*args, **kw)

    # ---- 图片名记账 + 调试用 context.log ----

    def _record_img(self, png: bytes, name: str | None) -> None:
        if name:
            self._image_names[hashlib.md5(png).hexdigest()] = name

    def _img_block(self, png: bytes, name: str | None = None) -> dict[str, Any]:
        """返回发送给 LLM 的 image_url 块，并顺带把 png→文件名 记入 mapping。"""
        self._record_img(png, name)
        return {"type": "image_url", "image_url": {"url": _data_url(png)}}

    def _sanitize_for_log(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """复制一份 messages，把所有 image_url 的 base64 数据替换成 [image: <文件名>]。"""
        out: list[dict[str, Any]] = []
        for m in messages:
            c = m.get("content")
            if isinstance(c, list):
                new_c: list[Any] = []
                for part in c:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        url = (part.get("image_url") or {}).get("url", "")
                        name = "(unknown)"
                        if isinstance(url, str) and url.startswith("data:image"):
                            try:
                                b64 = url.split(",", 1)[1]
                                png = base64.b64decode(b64)
                                name = self._image_names.get(
                                    hashlib.md5(png).hexdigest(),
                                    f"sha1:{hashlib.sha1(png).hexdigest()[:10]}.png",
                                )
                            except Exception:
                                name = "(unparsable data url)"
                        new_c.append({"type": "image_ref", "file": name})
                    else:
                        new_c.append(part)
                out.append({**m, "content": new_c})
            else:
                out.append(m)
        return out

    def _dump_context(self, messages: list[dict[str, Any]], tools: Any, step: int) -> None:
        """把每次发给 LLM 的完整 context（图片用文件名替换）追加到 thread 目录下的 context.log。"""
        if self.thread_log is None or self.thread_log.run_dir is None:
            return
        try:
            path = self.thread_log.run_dir / "context.log"
            sanitized = self._sanitize_for_log(messages)
            tool_names = []
            try:
                for t in (tools or []):
                    if isinstance(t, dict):
                        fn = t.get("function") or {}
                        tool_names.append(fn.get("name") or t.get("name") or "?")
            except Exception:
                tool_names = ["<unavailable>"]
            header = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "step": step,
                "model": self.model,
                "provider": (self.cfg.llm.provider or "proxy").lower(),
                "tools": tool_names,
                "messages_count": len(sanitized),
            }
            with path.open("a", encoding="utf-8") as f:
                f.write(f"\n===== step {step} @ {header['ts']} =====\n")
                f.write(json.dumps(header, ensure_ascii=False) + "\n")
                f.write(json.dumps(sanitized, ensure_ascii=False, indent=2) + "\n")
        except Exception:
            pass

    def _rule(self, *args: Any, **kw: Any) -> None:
        if self.event_sink is None:
            _console.rule(*args, **kw)

    def _check_cancel(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise CancelledError("cancelled by user")

    def run(self, instruction: str) -> str:
        log = self.thread_log or ThreadLog.create(self.cfg.logging, instruction)
        log.info(
            f"proxy={self.cfg.llm.proxy.base_url} model={self.model} "
            f"max_steps={self.cfg.llm.max_steps} autonomy={self.cfg.safety.autonomy}"
        )
        run_dir = str(log.run_dir) if log.run_dir is not None else None
        thread_id = self.thread_log.id if self.thread_log is not None else None
        self._emit("run_start", instruction=instruction, run_dir=run_dir,
                   model=self.model, max_steps=self.cfg.llm.max_steps,
                   autonomy=self.cfg.safety.autonomy, thread_id=thread_id)
        try:
            return self._run(instruction, log)
        except CancelledError:
            log.warning("cancelled")
            log.close(status="cancelled")
            self._emit("final", status="cancelled", text="")
            return "(已取消)"
        except Exception as e:
            log.error(f"{type(e).__name__}: {e}")
            log.close(status="error")
            self._emit("error", message=f"{type(e).__name__}: {e}")
            raise

    def _build_tool_schemas(self, tool_schema: dict, remember_schema: dict, learn_tip_schema: dict) -> list[dict]:
        schemas = [tool_schema]
        if self.cfg.memory.enabled:
            schemas.append(remember_schema)
        if self.cfg.tools.enabled:
            schemas.append(learn_tip_schema)
        return schemas

    def _chat_with_retry(self, messages, raw_tools, log) -> Any:
        """在 SDK 重试之外再加一层应用层重试，专门处理 connection/timeout/5xx。

        SDK 默认 max_retries=2，但 multimodal 请求走代理 / Copilot 上游抽风时仍然容易报
        ``APIConnectionError``。上层看到的体验就是“任务到一半突然 connection error”。这里再重
        试 2 轮，退避 1.5s/3s；401/403/4xx 不重试。任一轮成功即返回。
        """
        tools = self._build_tool_schemas(*raw_tools) if isinstance(raw_tools[0], dict) else raw_tools
        # 在发送前记一笔完整 context（图片用文件名代替）便于 debug
        self._dump_context(messages, tools, self._current_step)
        backoff = [1.5, 3.0]
        last_exc: Exception | None = None
        for attempt in range(len(backoff) + 1):
            try:
                return self.client.chat(
                    messages=messages,
                    tools=tools,
                    max_tokens=self.cfg.llm.max_tokens,
                )
            except APIConnectionError as e:
                last_exc = e
                kind = "connection"
            except APIStatusError as e:
                # 仅重试 5xx（8xx 不存在但以防万一）
                status = getattr(e, "status_code", 0) or 0
                if status < 500:
                    raise
                last_exc = e
                kind = f"http{status}"
            if attempt >= len(backoff):
                break
            wait = backoff[attempt]
            log.warning(f"chat retry due to {kind}: {last_exc!r}; sleeping {wait}s")
            self._emit("warning", message=f"连接抖动，{wait}s 后第 {attempt + 2} 次重试…")
            time.sleep(wait)
        assert last_exc is not None
        raise last_exc

    def _run(self, instruction: str, log: ThreadLog) -> str:
        # 起手一张 L1 截图
        first = self.sensor.capture(ScreenLevel.L1)
        self.tool.last_capture = first
        screen_w, screen_h = first.sent_size
        tool_schema = ComputerTool.openai_tool_schema(screen_w, screen_h)
        remember_schema = {
            "type": "function",
            "function": {
                "name": "remember",
                "description": (
                    "把一条值得长期保留的事实写入 memory.md（覆盖式追加，下次任务起手会注入到 system prompt）。"
                    "适用：用户明确要求记住的偏好；用户的称呼 / 身份；用户的操作习惯（常用浏览器、编辑器、"
                    "快捷键、保存路径、命名风格、深色模式偏好等）；以及短期内不会变的环境事实（如常用工作目录）。"
                    "**不要**记单次任务的临时事实、过程中间结果、密码 token 等敏感信息。"
                    "写入前先看 system prompt 末尾的『长期记忆』段，避免重复或冲突。"
                    "格式要求：单行、不超过 200 字、陈述句。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "要写入的一条记忆，单行陈述句。"}
                    },
                    "required": ["text"],
                },
            },
        }
        learn_tip_schema = {
            "type": "function",
            "function": {
                "name": "learn_tip",
                "description": (
                    "把一条**操作技巧**（针对某个 App / 对话框 / 控件 怎么做更稳）追加进 tools.md，"
                    "下次任务起手会注入到 system prompt。适用：任务中发现 成功路径 或 失败教训，"
                    "且该经验在同类场景下可复用。写入前先看下方『操作技巧』已有条目避免重复。"
                    "**不要**记单次任务的临时事实（那不是技巧）、也不要记用户偏好（那是 memory.md）。"
                    "格式：单行陈述句、不超 200 字；带上 App / 场景标签以便检索。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "要写入的技巧。例：「Outlook 用 Ctrl+R 回复比鼠标点回复按钮更稳」"},
                        "kind": {"type": "string", "enum": ["success", "failure", "tip"], "description": "成功经验 / 失败教训 / 一般提示。默认 success。"},
                    },
                    "required": ["text"],
                },
            },
        }
        log.info(f"initial screenshot {screen_w}x{screen_h}")
        init_name = log.save_image(first.png_bytes(), "step-000-init", level="INFO")
        self._record_img(first.png_bytes(), init_name)
        if init_name:
            self._emit("step_image", step=0, level=first.level.value,
                       width=screen_w, height=screen_h,
                       path=str(log.run_dir / init_name) if log.run_dir else None,
                       file=init_name,
                       thread_id=log.id if log else None,
                       phase="init")

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT + memory_mod.memory_for_prompt(self.cfg.memory) + tooltips_mod.tools_for_prompt(self.cfg.tools)},
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
            self._check_cancel()
            self._current_step = step + 1
            self._rule(f"[cyan]Step {step + 1}/{self.cfg.llm.max_steps}")
            log.info(f"--- step {step + 1}/{self.cfg.llm.max_steps} ---")
            self._emit("step_start", step=step + 1, max_steps=self.cfg.llm.max_steps)
            dropped = _prune_old_images(messages, self.cfg.llm.keep_recent_screenshots)
            if dropped:
                log.debug(f"pruned {dropped} old screenshot(s) from prompt")
            try:
                resp = self._chat_with_retry(messages, [tool_schema, remember_schema, learn_tip_schema], log)
            except APIError as e:
                self._print(f"[red]API 错误：{e}[/red]")
                log.error(f"API error: {e}")
                log.close(status="api_error")
                self._emit("error", message=f"API error: {e}")
                return f"(API error) {e}"
            except Exception as e:
                # Anthropic / Copilot SDK 有自己的错误类型，用 base Exception 兜住
                self._print(f"[red]LLM 调用错误：{type(e).__name__}: {e}[/red]")
                log.error(f"LLM error: {type(e).__name__}: {e}")
                log.close(status="api_error")
                self._emit("error", message=f"LLM error: {e}")
                return f"(LLM error) {e}"

            tool_calls = resp.tool_calls
            text_content = resp.text or ""

            if text_content.strip():
                self._print(f"[white]{text_content}[/white]")
                log.info(f"assistant text: {text_content.strip()}")
                self._emit("assistant_text", step=step + 1, text=text_content)

            # 把 assistant 消息原样回塞
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": text_content}
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": tc.arguments_json or "{}",
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
                    self._emit("final", status="ok", text=final)
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
                fn_name = tc.name
                try:
                    args = json.loads(tc.arguments_json or "{}")
                except json.JSONDecodeError:
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                action = args.pop("action", "")
                self._print(f"[magenta]→ {fn_name}.{action}[/magenta] {args}")
                log.info(f"tool_call {fn_name}.{action} {args}")
                self._emit("tool_call", step=step + 1, name=fn_name, action=action, args=args)

                if fn_name != "computer":
                    if fn_name == "remember" and self.cfg.memory.enabled:
                        text = (args.get("text") or "").strip()
                        ok = memory_mod.append_memory(self.cfg.memory, text, source="agent")
                        tr = ToolResult(
                            output=f"已写入记忆：{text[:80]}" if ok else "",
                            error=None if ok else "memory disabled or empty",
                        )
                    elif fn_name == "learn_tip" and self.cfg.tools.enabled:
                        text = (args.get("text") or "").strip()
                        kind = (args.get("kind") or "success").strip().lower() or "success"
                        ok = tooltips_mod.append_tip(self.cfg.tools, text, kind=kind, source="agent")
                        tr = ToolResult(
                            output=f"已写入技巧({kind})：{text[:80]}" if ok else "",
                            error=None if ok else "tools disabled or empty",
                        )
                    else:
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
                self._emit("tool_result", step=step + 1, action=action,
                           ok=tr.error is None, output=tr.output or "", error=tr.error or "")

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
            self._record_img(post_png, saved_name)
            self._emit("step_image", step=step + 1, level=level_tag,
                       width=post.sent_size[0], height=post.sent_size[1],
                       path=str(log.run_dir / saved_name) if (log.run_dir and saved_name) else None,
                       file=saved_name,
                       thread_id=log.id if log else None,
                       phase="post")

            # 抓 L3 落点取证图（如果本步有点击且当前 post 不是 L3 本身）
            verify_capture = None
            verify_png: bytes | None = None
            if click_happened and post.level is not ScreenLevel.L3:
                try:
                    verify_capture = self.sensor.capture(ScreenLevel.L3)
                    verify_png = verify_capture.png_bytes()
                    verify_name = log.save_image(verify_png, f"step-{step + 1:03d}-verify-cursor_local", level="INFO")
                    self._record_img(verify_png, verify_name)
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
        self._emit("final", status="max_steps", text="")
        return "(达到最大步数预算，任务可能未完成)"
