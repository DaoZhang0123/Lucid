"""Voice intent dispatcher.

After the local Whisper transcription returns text, we used to either send it
to the main agent loop (``mode=agent``) or paste it into the focused input
(``mode=dictation``) based on a settings toggle. In practice users mix all
three needs in the same session — so we route through a tiny LLM call that
picks one of:

    - ``thread_new``        — start a brand-new agent task
    - ``thread_abort``      — cancel the currently running task
    - ``dictation_append``  — append to the focused input box

See ``Docs/voice-input.md`` §5.2 for design rationale. This module owns the
prompt + JSON parsing + a regex fallback so the dispatcher never raises into
the front-end. Returns a ``DispatchResult`` the front-end converts into the
result-state chip(s).
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from dataclasses import dataclass, asdict
from typing import Any, Literal, Optional

from .config import Config
from .llm_client import LLMClient, build_llm_client


Intent = Literal["thread_new", "thread_abort", "dictation_append"]
Confidence = Literal["high", "medium", "low"]
VALID_INTENTS: tuple[Intent, ...] = ("thread_new", "thread_abort", "dictation_append")


@dataclass
class VoiceContext:
    """Runtime context the front-end provides with each dispatch call."""

    has_running_thread: bool = False
    active_input_focus: bool = False
    last_user_text: Optional[str] = None
    locale: str = "en"

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "VoiceContext":
        d = d or {}
        return cls(
            has_running_thread=bool(d.get("has_running_thread", False)),
            active_input_focus=bool(d.get("active_input_focus", False)),
            last_user_text=(d.get("last_user_text") or None),
            locale=str(d.get("locale", "en") or "en").lower(),
        )


@dataclass
class DispatchResult:
    intent: Intent
    confidence: Confidence
    reason: str
    cleaned_text: str
    source: Literal["llm", "regex", "rule"] = "llm"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Regex fallback — runs when the LLM call errors out or times out, and is
# also consulted as a fast pre-pass for obvious abort phrases (so the user
# yelling "stop!" never has to wait for a network round-trip).
# ---------------------------------------------------------------------------

_ABORT_RE = re.compile(
    r"^\s*(?:hey\s+)?(?:lucid[,\s]+)?"
    r"(stop(?:\s+(?:it|that|now|the\s+task))?|cancel(?:\s+(?:it|that|the\s+task))?|abort"
    r"|never\s*mind|nevermind|don'?t\s+do\s+(?:that|it)"
    r"|停下|停止|取消|算了|不要了|别跑了|别做了|住手"
    r"|arr[êe]te|annule|annuler|laisse\s+tomber|oublie\s+ç?a)"
    r"[\s.!?。！？]*$",
    re.IGNORECASE,
)# Broader "stop X" pattern: any imperative "stop / cancel / abort / 停 / 取消"
# at the very start of the utterance, regardless of what comes after. The
# strict ABORT_RE above only matches utterances that ARE the stop word; this
# catches "停止当天的Thread" / "停止所有的对话" / "stop the running task" etc.
_ABORT_PREFIX_RE = re.compile(
    r"^\s*(?:hey\s+)?(?:lucid[,\s，]+)?"
    r"(?:"
    r"stop|cancel|abort|halt|kill|terminate|end|quit|pause"
    r"|停止|停下|停掉|取消|中断|中止|终止|干掉|杀掉|关掉|退出"
    r"|arr[êé]te(?:r|z)?|annule(?:r|z)?|stoppe(?:r|z)?|halte"
    r")\b",
    re.IGNORECASE,
)_NEW_VERBS = re.compile(
    r"\b(open|launch|start|run|send|post|message|write|save|create|generate|"
    r"summari[sz]e|tell\s+me|find|search|look\s+up|read|fetch|"
    r"play|pause(?!\s+the\s+task)|skip|email|call)\b"
    r"|(?:打开|启动|运行|发(?:送)?|写一?[篇下条]?|保存|创建|生成|总结|"
    r"告诉我|查一?下|搜索|搜一?下|读一?下|播放|暂停|发邮件)"
    r"|\b(?:ouvre|lance|d[ée]marre|envoie|[ée]cris|sauvegarde|cr[ée]e|g[ée]n[èe]re|"
    r"r[ée]sume|dis[\s-]+moi|cherche|trouve|lis|joue)\b",
    re.IGNORECASE,
)
_DICT_HINTS = re.compile(
    r"^\s*(?:also|and|plus|oh|oops|wait|um+|uh+|"
    r"还有|另外|顺便|对了|另说|还有一句|嗯|呃"
    r"|aussi|et\s+aussi|euh+|hm+|attends|oh)\b",
    re.IGNORECASE,
)


def _strip_filler(text: str) -> str:
    """Drop common wake-word / filler prefixes for the LLM input."""
    cleaned = re.sub(
        r"^\s*(?:hey\s+|嘿\s*|嗨\s*)?lucid[\s,，。:：]+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return cleaned.strip()


def regex_classify(text: str, ctx: VoiceContext) -> DispatchResult:
    """Pure-regex classifier. Used as fallback and as fast-path for aborts."""
    cleaned = _strip_filler(text)
    if _ABORT_RE.match(cleaned) and ctx.has_running_thread:
        return DispatchResult(
            intent="thread_abort",
            confidence="high",
            reason="matched abort phrase",
            cleaned_text=cleaned,
            source="regex",
        )
    # Broader "stop / 停止 / cancel + <object>" prefix. Slightly lower
    # confidence than the strict full-utterance match above, but still high
    # enough that the front-end fires the abort directly when there's a
    # running thread.
    if _ABORT_PREFIX_RE.match(cleaned) and ctx.has_running_thread:
        return DispatchResult(
            intent="thread_abort",
            confidence="high",
            reason="matched abort prefix (stop/停止/cancel + object)",
            cleaned_text=cleaned,
            source="regex",
        )
    if _NEW_VERBS.search(cleaned):
        return DispatchResult(
            intent="thread_new",
            confidence="medium",
            reason="matched new-task verb",
            cleaned_text=cleaned,
            source="regex",
        )
    if ctx.active_input_focus and _DICT_HINTS.match(cleaned):
        return DispatchResult(
            intent="dictation_append",
            confidence="medium",
            reason="continuation phrase + input focused",
            cleaned_text=cleaned,
            source="regex",
        )
    if ctx.active_input_focus and not ctx.has_running_thread:
        return DispatchResult(
            intent="dictation_append",
            confidence="low",
            reason="default to dictation when an input is focused",
            cleaned_text=cleaned,
            source="regex",
        )
    return DispatchResult(
        intent="thread_new",
        confidence="low",
        reason="no abort / dictation signal",
        cleaned_text=cleaned,
        source="regex",
    )


# ---------------------------------------------------------------------------
# LLM classifier
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a voice-to-action router for the Lucid desktop assistant. Read the
transcript and classify the user's intent as exactly one of:

  - thread_new       : the user wants Lucid to start a NEW task
                       (open an app, send a message, summarise, write,
                       compute, look up, play). English verbs: open / launch /
                       send / write / summari?e / find / play / call.
                       Chinese: 打开 / 发 / 写 / 保存 / 总结 / 查 / 播放.
                       French: ouvre / envoie / écris / résume / cherche.
  - thread_abort     : the user wants to STOP / cancel the currently running
                       task. Triggers: stop / cancel / abort / never mind /
                       don't do that / 停下 / 停止 / 取消 / 算了 / 不要了 /
                       arrête / annule / laisse tomber.
                       ONLY pick this if has_running_thread = true; otherwise
                       fall back to thread_new.
  - dictation_append : the transcript is filler / continuation / correction
                       meant for the currently focused input box. Triggers:
                       starts mid-sentence, "also" / "and" / "one more
                       thing" / "oops" / 还有 / 另外 / 对了 / aussi / euh.
                       ONLY pick this if active_input_focus = true; otherwise
                       fall back to thread_new.

Set confidence:
  - high   : an unambiguous trigger word + the corresponding context flag is
             true.
  - medium : trigger word present but the context flag is missing, OR the
             trigger is implicit but the verb tense / phrasing is clear.
  - low    : ambiguous; the user can disambiguate from a 1-frame chip.

Return STRICT JSON, no prose, no markdown fences:

{"intent": "thread_new|thread_abort|dictation_append",
 "confidence": "high|medium|low",
 "reason": "<one short sentence explaining the pick>",
 "cleaned_text": "<transcript with wake-words / fillers stripped>"}
"""


def _build_user_msg(text: str, ctx: VoiceContext) -> str:
    last = (ctx.last_user_text or "").strip()
    if len(last) > 240:
        last = last[:240] + "…"
    return (
        f"transcript: {text!r}\n"
        f"locale: {ctx.locale}\n"
        f"has_running_thread: {str(ctx.has_running_thread).lower()}\n"
        f"active_input_focus: {str(ctx.active_input_focus).lower()}\n"
        f"last_user_text: {last!r}\n"
    )


def _parse_llm_json(raw: str) -> dict[str, Any]:
    """Tolerate markdown fences / leading prose around the JSON object."""
    raw = raw.strip()
    if not raw:
        raise ValueError("empty LLM response")
    # Strip ``` ``` json fences if present.
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL | re.IGNORECASE)
    if fence:
        raw = fence.group(1)
    # Otherwise fall back to the first {...} block.
    if not raw.startswith("{"):
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            raw = m.group(0)
    return json.loads(raw)


def _coerce_result(payload: dict[str, Any], cleaned_default: str) -> DispatchResult:
    intent = str(payload.get("intent") or "").strip()
    if intent not in VALID_INTENTS:
        raise ValueError(f"invalid intent: {intent!r}")
    conf = str(payload.get("confidence") or "low").strip().lower()
    if conf not in ("high", "medium", "low"):
        conf = "low"
    cleaned = str(payload.get("cleaned_text") or cleaned_default).strip() or cleaned_default
    reason = str(payload.get("reason") or "").strip()[:200]
    return DispatchResult(
        intent=intent,  # type: ignore[arg-type]
        confidence=conf,  # type: ignore[arg-type]
        reason=reason,
        cleaned_text=cleaned,
        source="llm",
    )


def _apply_context_guards(res: DispatchResult, ctx: VoiceContext) -> DispatchResult:
    """Downgrade impossible picks (model hallucinates abort with no thread, etc.)."""
    if res.intent == "thread_abort" and not ctx.has_running_thread:
        return DispatchResult(
            intent="thread_new",
            confidence="low",
            reason=f"abort downgraded (no running thread): {res.reason}",
            cleaned_text=res.cleaned_text,
            source=res.source,
        )
    if res.intent == "dictation_append" and not ctx.active_input_focus:
        return DispatchResult(
            intent="thread_new",
            confidence="low",
            reason=f"dictation downgraded (no input focus): {res.reason}",
            cleaned_text=res.cleaned_text,
            source=res.source,
        )
    return res


# ---------------------------------------------------------------------------
# Dispatcher entry point
# ---------------------------------------------------------------------------

class VoiceDispatcher:
    """Lazy-built LLM client + a thread-safe call wrapper.

    The same provider as the main agent loop is reused (Copilot / Anthropic),
    so OAuth tokens / API keys are shared. We keep our own client instance to
    avoid contention with a long-running agent ``chat`` call — the dispatcher
    request is small (200 in / 80 out tokens) and should not wait on the main
    loop's wall-clock timeout.
    """

    # Hard wall-clock cap. Above this we fall back to regex.
    _LLM_TIMEOUT_S = 6.0

    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._client: Optional[LLMClient] = None
        self._client_lock = threading.Lock()

    def reload_config(self, cfg: Config) -> None:
        with self._client_lock:
            self._cfg = cfg
            self._client = None

    def _ensure_client(self) -> LLMClient:
        with self._client_lock:
            if self._client is None:
                self._client = build_llm_client(self._cfg, None)
            return self._client

    def classify(self, text: str, ctx: VoiceContext) -> DispatchResult:
        text = (text or "").strip()
        if not text:
            return DispatchResult(
                intent="dictation_append" if ctx.active_input_focus else "thread_new",
                confidence="low",
                reason="empty transcript",
                cleaned_text="",
                source="rule",
            )
        # Fast-path obvious aborts so panic stops never wait on the network.
        cleaned = _strip_filler(text)
        if _ABORT_RE.match(cleaned) and ctx.has_running_thread:
            return DispatchResult(
                intent="thread_abort",
                confidence="high",
                reason="abort phrase fast-path",
                cleaned_text=cleaned,
                source="regex",
            )
        # Broader "stop / 停止 / cancel + <object>" prefix is also a fast-path
        # for aborts when there's something to abort. Prevents the LLM from
        # getting confused by short utterances like "停止当天的Thread"
        # (where the word "Thread" might bias it toward thread_new).
        if _ABORT_PREFIX_RE.match(cleaned) and ctx.has_running_thread:
            return DispatchResult(
                intent="thread_abort",
                confidence="high",
                reason="abort prefix fast-path",
                cleaned_text=cleaned,
                source="regex",
            )

        result_box: dict[str, Any] = {}

        def _runner() -> None:
            try:
                client = self._ensure_client()
                resp = client.chat(
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": _build_user_msg(text, ctx)},
                    ],
                    tools=[],
                    max_tokens=200,
                    temperature=0.0,
                    top_p=1.0,
                )
                result_box["text"] = resp.text or ""
            except BaseException as exc:  # noqa: BLE001
                result_box["error"] = exc

        t = threading.Thread(target=_runner, daemon=True, name="voice_dispatch")
        t0 = time.time()
        t.start()
        t.join(timeout=self._LLM_TIMEOUT_S)
        elapsed = time.time() - t0

        if t.is_alive():
            print(
                f"[voice_dispatch] LLM wall-clock timeout after {elapsed:.1f}s; falling back to regex",
                file=sys.stderr,
            )
            fb = regex_classify(text, ctx)
            return _apply_context_guards(fb, ctx)
        if "error" in result_box:
            print(
                f"[voice_dispatch] LLM error: {type(result_box['error']).__name__}: "
                f"{result_box['error']}; falling back to regex",
                file=sys.stderr,
            )
            fb = regex_classify(text, ctx)
            return _apply_context_guards(fb, ctx)

        try:
            payload = _parse_llm_json(result_box.get("text", ""))
            res = _coerce_result(payload, cleaned_default=cleaned)
        except Exception as e:
            print(
                f"[voice_dispatch] could not parse LLM JSON ({e}); falling back to regex",
                file=sys.stderr,
            )
            fb = regex_classify(text, ctx)
            return _apply_context_guards(fb, ctx)

        return _apply_context_guards(res, ctx)


__all__ = [
    "Intent",
    "Confidence",
    "VALID_INTENTS",
    "VoiceContext",
    "DispatchResult",
    "VoiceDispatcher",
    "regex_classify",
]
