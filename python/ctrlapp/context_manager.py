"""Context Manager.

Two responsibilities:

1. **Image recompression (no LLM, pure code)**
   Old screenshots that have aged out of the per-level / global "keep" window
   are recompressed to small JPEG (e.g. 720px long edge @ Q35) instead of being
   replaced by a text placeholder. This preserves *some* visual context (the
   model can still glance at past screen state) while keeping prompt size
   bounded.

2. **Adaptive history summarisation (uses cheap LLM)**
   When estimated tokens of the outgoing request exceed `target_ratio` of the
   model's context window, the earliest non-prelude messages are condensed via
   a single summarisation call into one synthetic ``user`` message
   (``## Conversation summary so far``) and the originals are dropped. The
   most-recent ``keep_recent_messages`` messages are always preserved verbatim.

The module is deliberately stateless — pass a ``ContextConfig`` in and call
the two entry points around the main ``client.chat`` site.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
from typing import Any, Callable, Iterable

from PIL import Image

from .config import ContextConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iter_image_blocks(messages: list[dict[str, Any]]) -> list[tuple[int, int, str]]:
    """Return [(message_idx, content_idx, level_tag), ...] for every image_url
    block. The level tag is sniffed from the nearest preceding text part of the
    same content list (e.g. ``[level=L1]``). Defaults to ``L1``.
    """
    out: list[tuple[int, int, str]] = []
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
                if "[level=L0]" in last_text:
                    tag = "L0"
                elif "[level=L2]" in last_text:
                    tag = "L2"
                elif "[level=L3]" in last_text:
                    tag = "L3"
                out.append((mi, ci, tag))
    return out


def _data_url_to_bytes(url: str) -> bytes | None:
    if not isinstance(url, str) or not url.startswith("data:image"):
        return None
    try:
        return base64.b64decode(url.split(",", 1)[1])
    except Exception:
        return None


def _bytes_to_jpeg_data_url(png_or_jpeg: bytes, quality: int, max_long_edge: int) -> bytes | None:
    """Decode -> downscale -> re-encode as JPEG. Returns the new raw bytes."""
    try:
        img = Image.open(io.BytesIO(png_or_jpeg))
        img.load()
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        w, h = img.size
        long_edge = max(w, h)
        if max_long_edge > 0 and long_edge > max_long_edge:
            scale = max_long_edge / long_edge
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=int(quality), optimize=True)
        return buf.getvalue()
    except Exception:
        return None


def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Rough token estimate: text chars / 4 + per-image fixed cost.

    Per-image cost depends on whether it's a (small) JPEG or a (full) PNG. We
    approximate by base64 length / 4 byte → roughly 1 token per 4 bytes of
    base64, capped to a reasonable per-image ceiling for VLM tile counting.
    """
    total = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total += max(1, len(c) // 4)
        elif isinstance(c, list):
            for part in c:
                if not isinstance(part, dict):
                    continue
                t = part.get("type")
                if t == "text":
                    total += max(1, len(part.get("text", "") or "") // 4)
                elif t == "image_url":
                    url = (part.get("image_url") or {}).get("url", "")
                    if isinstance(url, str):
                        # base64 length / 4 ≈ tokens; cap so a giant PNG isn't 100k tokens.
                        total += min(2400, max(200, len(url) // 4))
                    else:
                        total += 800
                elif t == "image_ref":
                    total += 30
        # tool messages with stringified content
        if "tool_calls" in m:
            try:
                total += max(1, len(json.dumps(m.get("tool_calls"))) // 4)
            except Exception:
                pass
    return total


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class ContextManager:
    """Stateless helper; one instance per Agent is fine."""

    def __init__(self, cfg: ContextConfig) -> None:
        self.cfg = cfg

    # -- image recompression ------------------------------------------------

    def compress_old_images(
        self,
        messages: list[dict[str, Any]],
        *,
        keep_per_level: dict[str, int],
        keep_recent_global: int,
        image_names: dict[str, str] | None = None,
        run_dir: Any = None,
    ) -> tuple[int, int]:
        """Recompress (or, if disabled, drop to placeholder) old image blocks.

        Returns ``(recompressed, dropped_to_text)``.

        Keep policy (image survives untouched):
          * level == "L0" (icon atlases): always.
          * each of L1/L2/L3: most recent ``keep_per_level[level]`` blocks.
          * additionally: most recent ``keep_recent_global`` blocks overall.

        Everything else is recompressed to JPEG @ ``image_recompress_quality``
        and downscaled to ``image_recompress_max_long_edge``. If
        ``image_recompress_enabled`` is False or
        ``image_recompress_max_long_edge <= 0``, those blocks are replaced with
        a text placeholder instead (legacy behaviour).
        """
        entries = _iter_image_blocks(messages)
        if not entries:
            return (0, 0)

        keep_idx: set[int] = set()
        for i, (_mi, _ci, tag) in enumerate(entries):
            if tag == "L0":
                keep_idx.add(i)
        for level in ("L1", "L2", "L3"):
            k = max(0, int(keep_per_level.get(level, 1)))
            if k <= 0:
                continue
            seen = 0
            for i in range(len(entries) - 1, -1, -1):
                if entries[i][2] == level:
                    keep_idx.add(i)
                    seen += 1
                    if seen >= k:
                        break
        if keep_recent_global > 0:
            for i in range(max(0, len(entries) - keep_recent_global), len(entries)):
                keep_idx.add(i)

        recompressed = 0
        dropped = 0
        for i, (mi, ci, tag) in enumerate(entries):
            if i in keep_idx:
                continue
            part = messages[mi]["content"][ci]
            url = (part.get("image_url") or {}).get("url", "") if isinstance(part, dict) else ""
            raw = _data_url_to_bytes(url)
            if raw is None:
                continue

            do_recompress = (
                self.cfg.image_recompress_enabled
                and self.cfg.image_recompress_max_long_edge > 0
            )
            new_bytes = (
                _bytes_to_jpeg_data_url(
                    raw,
                    quality=self.cfg.image_recompress_quality,
                    max_long_edge=self.cfg.image_recompress_max_long_edge,
                )
                if do_recompress
                else None
            )

            # Only swap in the recompressed version if it's actually smaller.
            if new_bytes is not None and len(new_bytes) < len(raw):
                b64 = base64.b64encode(new_bytes).decode("ascii")
                messages[mi]["content"][ci] = {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64," + b64},
                }
                recompressed += 1
            else:
                # Fall back to text placeholder.
                name = None
                if image_names is not None:
                    name = image_names.get(hashlib.md5(raw).hexdigest())
                if name and run_dir is not None:
                    placeholder = (
                        f"[old screenshot omitted to control request size; "
                        f"level={tag}; file={name}; path={run_dir}\\{name}]"
                    )
                elif name:
                    placeholder = f"[old screenshot omitted; level={tag}; file={name}]"
                else:
                    placeholder = f"[old screenshot omitted; level={tag}]"
                messages[mi]["content"][ci] = {"type": "text", "text": placeholder}
                dropped += 1
        return (recompressed, dropped)

    # -- adaptive summarisation --------------------------------------------

    def estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
        return _estimate_tokens(messages)

    def needs_summary(self, messages: list[dict[str, Any]]) -> bool:
        if not self.cfg.auto_compress_enabled:
            return False
        budget = int(self.cfg.model_context_tokens * self.cfg.target_ratio)
        return _estimate_tokens(messages) > budget

    def maybe_summarize(
        self,
        messages: list[dict[str, Any]],
        *,
        prelude_len: int,
        summarizer: Callable[[list[dict[str, Any]], int], str],
        log_fn: Callable[[str], None] | None = None,
    ) -> bool:
        """If the request is over budget, summarise the early non-prelude tail.

        ``summarizer(text_messages, max_tokens) -> summary_text`` is supplied
        by the caller so this module stays unaware of which LLM client is in
        use. ``text_messages`` is the slice to summarise, with all images
        already stripped to ``[image: ...]`` placeholders.

        Returns True iff a summarisation actually happened (so the caller can
        update its bookkeeping like ``_current_messages``).

        Mutates ``messages`` in place: ``messages[prelude_len:cut]`` is
        replaced by a single ``user`` message containing the summary.
        """
        if not self.needs_summary(messages):
            return False
        keep_n = max(2, int(self.cfg.keep_recent_messages))
        if len(messages) - prelude_len <= keep_n + 2:
            # Not enough older history to be worth summarising.
            return False
        cut = len(messages) - keep_n
        if cut <= prelude_len:
            return False
        old_segment = messages[prelude_len:cut]
        text_segment = _strip_images_for_summary(old_segment)
        try:
            summary = summarizer(text_segment, self.cfg.summary_max_tokens)
        except Exception as e:  # pragma: no cover - defensive
            if log_fn:
                log_fn(f"context summarisation failed: {type(e).__name__}: {e}")
            return False
        if not summary:
            return False
        synthesized = {
            "role": "user",
            "content": (
                "## Conversation summary so far\n"
                "(Earlier messages were condensed by the context manager to fit the model's window. "
                "The summary preserves: user instructions, key actions taken, screen-state observations, "
                "errors encountered, and any partial progress.)\n\n"
                + summary.strip()
            ),
        }
        messages[prelude_len:cut] = [synthesized]
        if log_fn:
            removed = len(old_segment)
            log_fn(
                f"context manager: summarised {removed} older message(s) "
                f"into 1 (~{len(summary)} chars)"
            )
        return True


def _strip_images_for_summary(messages: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a copy with image_url blocks replaced by '[image]' text so the
    summariser doesn't burn vision tokens repeating screenshots it can't see."""
    out: list[dict[str, Any]] = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            new_c: list[Any] = []
            for part in c:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    new_c.append({"type": "text", "text": "[image]"})
                else:
                    new_c.append(part)
            out.append({**m, "content": new_c})
        else:
            out.append(m)
    return out


def build_summary_prompt(text_segment: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convenience builder for the summariser call. The Agent passes whatever
    LLM client it has; the prompt asks for a concise factual summary."""
    transcript_lines: list[str] = []
    for m in text_segment:
        role = m.get("role", "?")
        c = m.get("content")
        if isinstance(c, list):
            text = " ".join(
                (p.get("text", "") if isinstance(p, dict) and p.get("type") == "text" else "")
                for p in c
            ).strip()
        else:
            text = str(c or "").strip()
        if "tool_calls" in m:
            try:
                tc_brief = ", ".join(
                    f"{(tc.get('function') or {}).get('name', '?')}({(tc.get('function') or {}).get('arguments', '')[:120]})"
                    for tc in (m.get("tool_calls") or [])
                )
                text = (text + ("\n" if text else "") + f"[tool_calls: {tc_brief}]").strip()
            except Exception:
                pass
        if not text:
            continue
        # Truncate very long single messages so the summariser stays cheap.
        if len(text) > 4000:
            text = text[:4000] + " …(truncated)"
        transcript_lines.append(f"{role.upper()}: {text}")
    transcript = "\n\n".join(transcript_lines)
    system = (
        "You are summarising the earlier portion of an autonomous GUI Agent's transcript so the "
        "agent can keep working within its context window. Produce a concise, factual recap "
        "(<= 600 words) covering, in this order:\n"
        "  1. The user's original instruction(s) and any clarifications.\n"
        "  2. What the agent has actually done so far (key tool calls / windows opened / files touched).\n"
        "  3. The current screen / app state as last observed.\n"
        "  4. Errors or dead-ends encountered and how they were handled.\n"
        "  5. Anything still pending.\n"
        "Use plain bullet points. Do NOT speculate or add new steps. Do NOT include screenshots."
    )
    user = "Transcript to summarise:\n\n" + transcript
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
