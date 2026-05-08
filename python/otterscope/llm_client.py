"""LLM 后端抽象 —— 三种 provider 都被映射成同一个 `chat()` 接口。

调用方（`loop.Agent`）始终用 OpenAI 风格的输入 / 输出：

  messages: list[dict]      # role/content[/tool_call_id/tool_calls] 的 OpenAI 兼容形态
  tools:    list[dict]      # OpenAI tool schema：{"type":"function","function":{...}}
  ->        LLMResponse     # 归一化的回复（text + tool_calls）

各 provider 的事情：
  - OpenAIChatClient  原样转发（LiteLLM/OpenClaw/任何 OpenAI 兼容代理 + 用户填的
                      base_url/api_key/extra_headers）
  - AnthropicClient   翻译成 Anthropic Messages API 形态
                      （system 抽出去；image_url data URL → image source.base64；
                       tool_calls/tool 结果 → tool_use/tool_result content blocks；
                       OpenAI tool schema → Anthropic tool schema）
  - CopilotClient     拿 Copilot 短时 token + 动态 base_url 后，复用
                      OpenAIChatClient（Copilot 后端是 OpenAI 兼容的 chat completions）
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

# 复用 auth.copilot 里探测出来的 Windows 系统代理（注册表 / 环境变量）。如果
# 不可用就退回 None，让 SDK 自己走 trust_env。
try:
    from .auth.copilot import _SYSTEM_PROXY as _SYS_PROXY  # type: ignore
except Exception:  # pragma: no cover
    _SYS_PROXY = None

# chat 调用的默认超时与 SDK 层重试。Copilot 多模态请求慢，给宽一点。
_CHAT_TIMEOUT_SEC = 90.0
_SDK_MAX_RETRIES = 4


def _build_openai_http_client():
    """返回一个 httpx.Client，带上系统代理 + truststore（如果可用）。

    OpenAI SDK 允许传 ``http_client=`` 参数；不传的话它只看 HTTPS_PROXY 环境变量，
    拿不到注册表里的系统代理（Clash/V2Ray 默认就是写注册表），造成 chat 调用走直连、中
    国大陆 connection reset。"""
    try:
        import httpx
    except ImportError:  # pragma: no cover
        return None
    kwargs: dict[str, Any] = {
        "timeout": _CHAT_TIMEOUT_SEC,
        "trust_env": True,
    }
    if _SYS_PROXY:
        kwargs["proxy"] = _SYS_PROXY
    try:
        import ssl
        import truststore
        kwargs["verify"] = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        pass
    return httpx.Client(**kwargs)


# ------------------------------ 归一化结果 ------------------------------

@dataclass
class ToolCall:
    id: str
    name: str
    arguments_json: str  # 必须是合法 JSON 字符串；解析失败时由调用方处理


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMClient(Protocol):
    model: str

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        *,
        temperature: float | None = None,
        top_p: float | None = None,
    ) -> LLMResponse: ...


# ------------------------------ OpenAI 兼容 ------------------------------

class OpenAIChatClient:
    """LiteLLM / OpenClaw / 任何 OpenAI 兼容 /v1/chat/completions 后端。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        from openai import OpenAI

        self.model = model
        self._client = OpenAI(
            base_url=base_url.rstrip("/"),
            api_key=api_key,
            default_headers=dict(extra_headers) if extra_headers else None,
            timeout=_CHAT_TIMEOUT_SEC,
            max_retries=_SDK_MAX_RETRIES,
            http_client=_build_openai_http_client(),
        )

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        *,
        temperature: float | None = None,
        top_p: float | None = None,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if top_p is not None:
            kwargs["top_p"] = top_p
        resp = self._client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        text = msg.content or ""
        calls: list[ToolCall] = []
        for tc in (msg.tool_calls or []):
            calls.append(ToolCall(
                id=tc.id,
                name=tc.function.name,
                arguments_json=tc.function.arguments or "{}",
            ))
        return LLMResponse(text=text, tool_calls=calls)


# ------------------------------ Anthropic 原生 ------------------------------

_DATA_URL_RE = re.compile(r"^data:(image/[a-zA-Z+]+);base64,(.+)$", re.DOTALL)


def _messages_have_image(messages: list[dict[str, Any]]) -> bool:
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return True
    return False


def _split_data_url(url: str) -> tuple[str, str]:
    m = _DATA_URL_RE.match(url.strip())
    if not m:
        raise ValueError("image_url 必须是 data:image/...;base64,... 形式")
    return m.group(1), m.group(2)


def _openai_to_anthropic_content(content: Any) -> list[dict[str, Any]]:
    """把 OpenAI message.content（str 或 list）翻译成 Anthropic content blocks。"""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}]
    blocks: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            txt = part.get("text", "") or ""
            if txt:
                blocks.append({"type": "text", "text": txt})
        elif ptype == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            try:
                media_type, b64 = _split_data_url(url)
            except ValueError:
                # 远程 URL —— Anthropic Messages API 也支持 type=image source.type=url
                blocks.append({
                    "type": "image",
                    "source": {"type": "url", "url": url},
                })
                continue
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64},
            })
    return blocks


def _openai_tools_to_anthropic(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in tools:
        if t.get("type") != "function":
            continue
        fn = t.get("function") or {}
        name = fn.get("name") or ""
        if not name:
            continue
        out.append({
            "name": name,
            "description": fn.get("description") or "",
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def _openai_messages_to_anthropic(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """切走所有 system 文本，其余按 user/assistant/tool 翻译成 Anthropic 序列。

    OpenAI 形态:
      {"role":"system","content":"..."}
      {"role":"user","content":"..." | [{type,text},{type,image_url,...}]}
      {"role":"assistant","content":"..." (可空), "tool_calls":[{id,function:{name,arguments}}]}
      {"role":"tool","tool_call_id":"...","content":"..."}

    Anthropic 形态:
      system: str
      messages: [
        {"role":"user","content":[{type:text}|{type:image,source}|{type:tool_result, tool_use_id, content}]},
        {"role":"assistant","content":[{type:text}|{type:tool_use, id, name, input}]},
        ...
      ]
    Anthropic 不允许同 role 连续两条；连续合并即可。
    """
    sys_chunks: list[str] = []
    out: list[dict[str, Any]] = []

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    for m in messages:
        role = m.get("role")
        if role == "system":
            c = m.get("content")
            if isinstance(c, str) and c.strip():
                sys_chunks.append(c)
            elif isinstance(c, list):
                for part in c:
                    if isinstance(part, dict) and part.get("type") == "text":
                        sys_chunks.append(part.get("text", ""))
            continue

        if role == "user":
            push("user", _openai_to_anthropic_content(m.get("content")))
            continue

        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            text = m.get("content")
            if isinstance(text, str) and text.strip():
                blocks.append({"type": "text", "text": text})
            elif isinstance(text, list):
                blocks.extend(_openai_to_anthropic_content(text))
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except json.JSONDecodeError:
                    args = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id"),
                    "name": fn.get("name") or "",
                    "input": args,
                })
            push("assistant", blocks)
            continue

        if role == "tool":
            tool_use_id = m.get("tool_call_id") or ""
            content = m.get("content")
            if isinstance(content, list):
                inner = _openai_to_anthropic_content(content)
            else:
                inner = [{"type": "text", "text": str(content or "")}]
            push("user", [{
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": inner,
            }])
            continue

    return "\n\n".join(s for s in sys_chunks if s).strip(), out


class AnthropicClient:
    """Anthropic 原生 Messages API。"""

    def __init__(self, *, api_key: str, model: str, base_url: str = "https://api.anthropic.com",
                 anthropic_version: str = "2023-06-01") -> None:
        from anthropic import Anthropic

        self.model = model
        self._client = Anthropic(
            api_key=api_key,
            base_url=base_url.rstrip("/") if base_url else None,
            default_headers={"anthropic-version": anthropic_version} if anthropic_version else None,
        )

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        *,
        temperature: float | None = None,
        top_p: float | None = None,
    ) -> LLMResponse:
        system_text, msgs = _openai_messages_to_anthropic(messages)
        ant_tools = _openai_tools_to_anthropic(tools)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": msgs,
        }
        if system_text:
            kwargs["system"] = system_text
        if ant_tools:
            kwargs["tools"] = ant_tools
        if temperature is not None:
            kwargs["temperature"] = temperature
        if top_p is not None:
            kwargs["top_p"] = top_p

        resp = self._client.messages.create(**kwargs)

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(getattr(block, "text", "") or "")
            elif btype == "tool_use":
                calls.append(ToolCall(
                    id=getattr(block, "id", "") or "",
                    name=getattr(block, "name", "") or "",
                    arguments_json=json.dumps(getattr(block, "input", {}) or {}),
                ))
        return LLMResponse(text="".join(text_parts), tool_calls=calls)


# ------------------------------ GitHub Copilot ------------------------------

class CopilotClient:
    """GitHub Copilot —— OpenAI 兼容形态，但要动态拿 token + base_url。

    每次 chat() 前问一下 token manager；如果 token 即将过期，manager 会自动刷新。
    """

    # IDE 伪装头，对齐 openclaw / copilot.vim 等已知良好实现
    _IDE_HEADERS = {
        "Editor-Version": "vscode/1.96.2",
        "Editor-Plugin-Version": "copilot-chat/0.35.0",
        "User-Agent": "GitHubCopilotChat/0.26.7",
        "Copilot-Integration-Id": "vscode-chat",
        "Openai-Organization": "github-copilot",
    }

    def __init__(self, *, token_manager: Any, model: str) -> None:
        # token_manager: otterscope.auth.copilot.CopilotTokenManager
        self._tm = token_manager
        self.model = model

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        *,
        temperature: float | None = None,
        top_p: float | None = None,
    ) -> LLMResponse:
        token, base_url = self._tm.get_active()
        # 不缓存 OpenAI client：base_url/token 可能在 30 分钟后变更
        from openai import OpenAI

        headers = dict(self._IDE_HEADERS)
        # Copilot vision 必须显式开关
        if _messages_have_image(messages):
            headers["Copilot-Vision-Request"] = "true"
        client = OpenAI(
            base_url=base_url.rstrip("/"),
            api_key=token,
            default_headers=headers,
            timeout=_CHAT_TIMEOUT_SEC,
            max_retries=_SDK_MAX_RETRIES,
            http_client=_build_openai_http_client(),
        )
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if top_p is not None:
            kwargs["top_p"] = top_p
        resp = client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        text = msg.content or ""
        calls: list[ToolCall] = []
        for tc in (msg.tool_calls or []):
            calls.append(ToolCall(
                id=tc.id,
                name=tc.function.name,
                arguments_json=tc.function.arguments or "{}",
            ))
        return LLMResponse(text=text, tool_calls=calls)


__all__ = [
    "LLMClient",
    "LLMResponse",
    "ToolCall",
    "OpenAIChatClient",
    "AnthropicClient",
    "CopilotClient",
    "build_llm_client",
]


def _resolve_proxy_key(api_key_in_cfg: str) -> str:
    return (
        api_key_in_cfg
        or os.environ.get("LITELLM_MASTER_KEY", "")
        or os.environ.get("OPENAI_API_KEY", "")
    )


def build_llm_client(cfg: "Any", api_key_override: str | None) -> LLMClient:
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


# Lightweight self-test (only when executed directly)
if __name__ == "__main__":  # pragma: no cover
    import io
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 200, 200)).save(buf, "PNG")
    data_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    msgs = [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": [
            {"type": "text", "text": "what's on screen?"},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]},
        {"role": "assistant", "content": "let me check", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "computer", "arguments": '{"action":"screenshot"}'}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "ok 1024x768"},
    ]
    sys_text, ant_msgs = _openai_messages_to_anthropic(msgs)
    print("SYSTEM:", sys_text)
    print("MSGS:", json.dumps(ant_msgs, indent=2)[:600])
