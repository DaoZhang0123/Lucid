"""Voice intent dispatcher.

After the local Whisper transcription returns text, route it via a small LLM
call (no regex rule-book -- see commit history for the previous regex-based
classifier and why it was scrapped: too rigid, mis-classified obvious abort
utterances like "stop everything" in Chinese because Python's word-boundary
does not fire between CJK ideographs).

The LLM picks one of:

    - ``thread_new``        -- start a brand-new agent task
    - ``thread_abort``      -- cancel the currently running task
    - ``dictation_append``  -- append to the focused input box

...and additionally tells us:

    - ``priority``     : 0 = urgent (preempts a running task), 1 = normal,
                         2 = background (listener-style; lowest). For
                         ``thread_abort`` this is always coerced to 0.
    - ``next_action``  : short human-readable description of what the
                         dispatcher thinks should happen next. Used for
                         logging and the overlay confirm chip.

See ``Docs/internal/voice-input.md`` sections 5.2 and 10.9 for design.
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
VALID_PRIORITIES: tuple[int, ...] = (0, 1, 2)


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
    priority: int = 1
    next_action: str = ""
    source: Literal["llm", "rule"] = "llm"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WAKEWORD_RE = re.compile(
    r"^\s*(?:hey\s+|\u563f\s*|\u55e8\s*)?lucid[\s,\uff0c\u3002:\uff1a]+",
    re.IGNORECASE,
)


def _strip_filler(text: str) -> str:
    """Drop common wake-word / filler prefixes for the LLM input."""
    return _WAKEWORD_RE.sub("", text).strip()


# ---------------------------------------------------------------------------
# LLM classifier
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are the voice-to-action router for the Lucid desktop assistant. Read
the transcript and return STRICT JSON with these fields:

  intent       : one of "thread_new" | "thread_abort" | "dictation_append"
  priority     : integer 0, 1, or 2  (see below)
  next_action  : short imperative sentence describing what should happen
                 next, written for a human log line (max ~80 chars)
  confidence   : "high" | "medium" | "low"
  reason       : one sentence explaining the pick
  cleaned_text : the transcript with wake-words / fillers removed, kept in
                 its original language

# Intent meanings

  - thread_new       : the user wants Lucid to start a NEW task -- open
                       an app, send a message, summarise, write, look up,
                       play, compute, etc. Default when nothing else fits.
  - thread_abort     : the user wants to STOP / cancel the currently
                       running task or queued tasks. Triggers in any
                       language: stop / cancel / abort / never mind /
                       don't do that / 停 / 停止 / 取消 / 算了 /
                       不要了 / 别跑了 / arrete / annule / laisse tomber.
                       IMPORTANT: faster-whisper frequently mis-hears
                       "取消" (qǔ xiāo) as one of its homophones —
                       "取效", "取笑", "取销", "曲消", "去消", "取小",
                       "取下". When the rest of the sentence makes it
                       obvious the user wants to cancel (e.g. "取效刚刚
                       的任务" / "取笑当前任务"), treat it as thread_abort
                       with high confidence. Do NOT downgrade just because
                       the character is "wrong".
                       ONLY pick this when has_running_thread = true OR
                       when the user explicitly says "cancel everything
                       in the queue". Otherwise fall back to thread_new.
  - dictation_append : the transcript is a continuation / correction /
                       filler that belongs in the currently focused input
                       box. Triggers: starts mid-sentence, "also" /
                       "and" / "one more thing" / "oops" / 还有 / 另外 /
                       对了 / aussi. ONLY pick this when
                       active_input_focus = true.

# Priority

Use 0/1/2 with the same semantics as the sidecar queue:

  0 = urgent     : the user wants this NOW; it should preempt whatever is
                   running. Always pick 0 for thread_abort. Also pick 0
                   for thread_new when the user uses panic words
                   ("urgent", "right now", "drop everything", "立刻",
                   "马上", "现在就", "快点", "tout de suite") OR when
                   the request is safety-critical (mute mic, close
                   camera, stop sharing, kill app).
  1 = normal     : ordinary new task. Default for thread_new.
  2 = background : the utterance is low-importance, can wait behind
                   normal work -- typically a casual "remind me later",
                   "when you get a chance", "顺便", "有空时". Rare; only
                   pick when the user explicitly defers.
  For dictation_append, set priority = 1 (the field is required but
  ignored downstream).

# next_action

Concise, imperative, English. Examples:

  - "Spawn an urgent thread and let the LLM decide what to cancel."
  - "Enqueue a normal new task to open WeChat and message Mom."
  - "Paste the continuation into the focused textarea."
  - "Enqueue a background reminder for later."

# Confidence

  high   : an unambiguous trigger word + the corresponding context flag
           is true (or no context flag is needed).
  medium : the verb is clear but a context flag is missing, OR the
           trigger is implicit but the phrasing is unmistakable.
  low    : ambiguous. The overlay will surface a 1-frame confirm chip.

Return ONLY the JSON object -- no prose, no markdown fences."""


def _build_user_msg(text: str, ctx: VoiceContext) -> str:
    last = (ctx.last_user_text or "").strip()
    if len(last) > 240:
        last = last[:240] + "..."
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


def _coerce_priority(value: Any, intent: str) -> int:
    """Clamp priority to {0,1,2}. Force 0 for thread_abort."""
    if intent == "thread_abort":
        return 0
    try:
        p = int(value)
    except Exception:
        return 1
    if p < 0:
        return 0
    if p > 2:
        return 2
    return p


def _coerce_result(payload: dict[str, Any], cleaned_default: str) -> DispatchResult:
    intent = str(payload.get("intent") or "").strip()
    if intent not in VALID_INTENTS:
        raise ValueError(f"invalid intent: {intent!r}")
    conf = str(payload.get("confidence") or "low").strip().lower()
    if conf not in ("high", "medium", "low"):
        conf = "low"
    cleaned = str(payload.get("cleaned_text") or cleaned_default).strip() or cleaned_default
    reason = str(payload.get("reason") or "").strip()[:200]
    priority = _coerce_priority(payload.get("priority"), intent)
    next_action = str(payload.get("next_action") or "").strip()[:160]
    return DispatchResult(
        intent=intent,  # type: ignore[arg-type]
        confidence=conf,  # type: ignore[arg-type]
        reason=reason,
        cleaned_text=cleaned,
        priority=priority,
        next_action=next_action,
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
            priority=1,
            next_action="Enqueue a normal new task (abort had nothing to stop).",
            source=res.source,
        )
    if res.intent == "dictation_append" and not ctx.active_input_focus:
        return DispatchResult(
            intent="thread_new",
            confidence="low",
            reason=f"dictation downgraded (no input focus): {res.reason}",
            cleaned_text=res.cleaned_text,
            priority=res.priority if res.priority != 0 else 1,
            next_action="Enqueue a normal new task (no input focus for dictation).",
            source=res.source,
        )
    return res


def _safe_default(text: str, ctx: VoiceContext, reason: str) -> DispatchResult:
    """Last-resort result when the LLM completely fails.

    Treats the utterance as a normal new task unless the user is clearly
    mid-typing into a focused input -- in which case we paste it instead
    of guessing.
    """
    cleaned = _strip_filler(text)
    if ctx.active_input_focus and not ctx.has_running_thread:
        return DispatchResult(
            intent="dictation_append",
            confidence="low",
            reason=reason,
            cleaned_text=cleaned,
            priority=1,
            next_action="LLM unavailable -> paste into focused textarea (safest default).",
            source="rule",
        )
    return DispatchResult(
        intent="thread_new",
        confidence="low",
        reason=reason,
        cleaned_text=cleaned,
        priority=1,
        next_action="LLM unavailable -> enqueue as normal new task (safest default).",
        source="rule",
    )


# ---------------------------------------------------------------------------
# Dispatcher entry point
# ---------------------------------------------------------------------------

class VoiceDispatcher:
    """Lazy-built LLM client + a thread-safe call wrapper.

    The same provider as the main agent loop is reused (Copilot / Anthropic),
    so OAuth tokens / API keys are shared. We keep our own client instance
    to avoid contention with a long-running agent ``chat`` call -- the
    dispatcher request is small (~250 in / 120 out tokens) and should not
    wait on the main loop's wall-clock timeout.
    """

    # Hard wall-clock cap. Above this we return a safe default and log.
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
                priority=1,
                next_action="No-op (empty transcript).",
                source="rule",
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
                    max_tokens=300,
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
                f"[voice_dispatch] LLM wall-clock timeout after {elapsed:.1f}s; using safe default",
                file=sys.stderr,
            )
            return _apply_context_guards(
                _safe_default(text, ctx, "llm timeout"),
                ctx,
            )
        if "error" in result_box:
            print(
                f"[voice_dispatch] LLM error: {type(result_box['error']).__name__}: "
                f"{result_box['error']}; using safe default",
                file=sys.stderr,
            )
            return _apply_context_guards(
                _safe_default(text, ctx, f"llm error: {type(result_box['error']).__name__}"),
                ctx,
            )

        try:
            payload = _parse_llm_json(result_box.get("text", ""))
            res = _coerce_result(payload, cleaned_default=_strip_filler(text))
        except Exception as e:
            print(
                f"[voice_dispatch] could not parse LLM JSON ({e}); using safe default",
                file=sys.stderr,
            )
            return _apply_context_guards(
                _safe_default(text, ctx, f"json parse failed: {e}"),
                ctx,
            )

        return _apply_context_guards(res, ctx)


__all__ = [
    "Intent",
    "Confidence",
    "VALID_INTENTS",
    "VALID_PRIORITIES",
    "VoiceContext",
    "DispatchResult",
    "VoiceDispatcher",
]
