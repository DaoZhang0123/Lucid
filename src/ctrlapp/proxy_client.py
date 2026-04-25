"""本地 LiteLLM 代理（OpenAI 兼容 /chat/completions）的最小客户端。

用于 `--smoke-test`：验证能否通过本地代理（如 litellm-ghc-proxy-lite）
跟 Claude 通道通话。

主 ReAct 循环不走这里，而是用 openai SDK；二者打的都是同一个端点。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib import request as _urlreq
from urllib.error import HTTPError, URLError

from .config import ProxyConfig


def _resolve_key(cfg: ProxyConfig) -> str:
    return cfg.api_key or os.environ.get("LITELLM_MASTER_KEY", "")


def chat_once(
    cfg: ProxyConfig,
    prompt: str,
    *,
    timeout: float = 60.0,
    retries: int = 2,
    retry_delay: float = 1.5,
) -> str:
    """打一次 /chat/completions，返回 assistant 的文本。

    对 5xx / 网络错误最多重试 `retries` 次（GitHub Copilot 上游偶发
    `Connection error` 时尤其有用）。其它错误立即抛 RuntimeError。
    """
    key = _resolve_key(cfg)
    if not key:
        raise RuntimeError("缺少 api_key（config [llm.proxy].api_key 或 LITELLM_MASTER_KEY 环境变量）。")

    url = cfg.base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg.model,
        "messages": [{"role": "user", "content": prompt}],
    }
    data = json.dumps(payload).encode("utf-8")

    last_err: str = ""
    for attempt in range(retries + 1):
        req = _urlreq.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
        )
        try:
            with _urlreq.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            break
        except HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            last_err = f"HTTP {e.code} {e.reason}: {err_body[:500]}"
            # 5xx 才重试；4xx 直接抛
            if e.code >= 500 and attempt < retries:
                time.sleep(retry_delay)
                continue
            raise RuntimeError(last_err) from None
        except URLError as e:
            last_err = f"连接失败：{e.reason}（base_url={cfg.base_url}）"
            if attempt < retries:
                time.sleep(retry_delay)
                continue
            raise RuntimeError(last_err) from None
    else:  # pragma: no cover
        raise RuntimeError(last_err or "unknown error")

    try:
        obj: dict[str, Any] = json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(f"代理返回非 JSON：{body[:500]}") from None

    if "error" in obj:
        raise RuntimeError(f"代理报错：{obj['error']}")

    try:
        return obj["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"无法解析响应：{body[:500]}") from None
