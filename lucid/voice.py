"""Voice input — local ASR transcription.

See ``Docs/voice-input.md`` for design. This module owns the model lifecycle:
lazy-loaded on first call, kept resident across calls, dropped after an idle
window. The sidecar exposes it via the ``transcribe_audio`` RPC.

Engines (factory dispatched on ``VoiceConfig.engine``):
    - ``faster-whisper`` (default; MIT, CPU-friendly INT8)
    - ``sherpa-onnx``    (Phase 4 — currently raises NotImplementedError)
    - ``vosk``           (Phase 4 — currently raises NotImplementedError)

Audio bytes from the frontend (typically webm/opus from MediaRecorder) are
written to a temp file under ``~/.lucid/voice/`` and decoded by PyAV (bundled
by faster-whisper); we don't depend on a system FFmpeg.
"""
from __future__ import annotations

import os
import sys
import time
import threading
import tempfile
import gc
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Optional


# --- runtime resolution of the per-user voice cache dir ---

def _lucid_home() -> Path:
    base = os.environ.get("USERPROFILE") or os.environ.get("HOME")
    return (Path(base) if base else Path(".")) / ".lucid"


def voice_dir() -> Path:
    d = _lucid_home() / "voice"
    d.mkdir(parents=True, exist_ok=True)
    return d


def models_dir() -> Path:
    d = voice_dir() / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- result type ---

@dataclass
class TranscribeResult:
    text: str
    language: str = ""
    duration_ms: int = 0
    confidence: float | None = None
    engine: str = ""
    model: str = ""
    # Set when filtered out (silence, hallucination, too short).
    filtered_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # JSON-friendly: drop None for confidence to keep payload tidy.
        if d.get("confidence") is None:
            d.pop("confidence")
        return d


# --- mime → extension helper ---

_MIME_EXT = {
    "audio/webm": ".webm",
    "audio/webm;codecs=opus": ".webm",
    "audio/ogg": ".ogg",
    "audio/ogg;codecs=opus": ".ogg",
    "audio/wav": ".wav",
    "audio/wave": ".wav",
    "audio/x-wav": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
}


def _ext_for_mime(mime: str) -> str:
    if not mime:
        return ".webm"
    m = mime.strip().lower()
    if m in _MIME_EXT:
        return _MIME_EXT[m]
    base = m.split(";")[0].strip()
    return _MIME_EXT.get(base, ".webm")


# --- base class ---

class Transcriber:
    """Abstract base. Subclasses lazy-load on first ``.transcribe``."""

    engine: str = "?"

    def transcribe(self, audio_bytes: bytes, mime: str = "") -> TranscribeResult:
        raise NotImplementedError

    def unload(self) -> None:
        """Drop the model from RAM. Next .transcribe re-loads."""

    def status(self) -> dict[str, Any]:
        return {"engine": self.engine, "loaded": False}


# --- faster-whisper backend ---

class FasterWhisperTranscriber(Transcriber):
    engine = "faster-whisper"

    def __init__(self, cfg: "VoiceConfig"):
        self.cfg = cfg
        self._model = None  # type: ignore[assignment]
        self._model_lock = threading.Lock()
        self._last_use_ts = 0.0
        self._load_started_ts = 0.0
        self._loaded_ts = 0.0
        self._loading = False

    # ---- model loading ----

    def _model_id(self) -> str:
        # faster-whisper accepts:
        #   - HuggingFace repo id     (e.g. "Systran/faster-whisper-small")
        #   - shorthand size          (e.g. "small", "distil-small.en")
        #   - local model directory   (containing model.bin)
        return (self.cfg.model_size or "small").strip()

    def _load(self) -> None:
        try:
            from faster_whisper import WhisperModel  # type: ignore[import-not-found]
        except ImportError as e:
            raise RuntimeError(
                "faster-whisper is not installed. Run `pip install faster-whisper` "
                "(adds ~80MB; CTranslate2 + tokenizers + av) or change "
                "[voice].engine to a backend you have installed."
            ) from e
        # Optional HF mirror for users behind the great firewall.
        if self.cfg.hf_endpoint:
            os.environ.setdefault("HF_ENDPOINT", self.cfg.hf_endpoint)
        # Cache models inside ~/.lucid/voice/models/<size>/.
        # faster-whisper passes this through to huggingface_hub.snapshot_download.
        cache_root = str(models_dir())
        self._loading = True
        self._load_started_ts = time.time()
        try:
            self._model = WhisperModel(
                self._model_id(),
                device=self.cfg.device or "cpu",
                compute_type=self.cfg.compute_type or "int8",
                download_root=cache_root,
                cpu_threads=max(0, int(self.cfg.cpu_threads or 0)),
                num_workers=1,
            )
            self._loaded_ts = time.time()
        finally:
            self._loading = False

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        with self._model_lock:
            if self._model is None:
                self._load()

    # ---- public API ----

    def transcribe(self, audio_bytes: bytes, mime: str = "") -> TranscribeResult:
        if not audio_bytes:
            return TranscribeResult(text="", engine=self.engine, model=self._model_id(),
                                    filtered_reason="empty")
        self._ensure_loaded()
        assert self._model is not None
        self._last_use_ts = time.time()

        ext = _ext_for_mime(mime)
        # Persist into ~/.lucid/voice/ — `keep_audio` decides whether to keep
        # the file after transcription (default false: delete).
        tmp_path = voice_dir() / f"recording-{int(time.time()*1000)}{ext}"
        tmp_path.write_bytes(audio_bytes)

        try:
            language = (self.cfg.language or "auto").strip()
            lang_param: Optional[str] = None if language in ("", "auto") else language

            t0 = time.time()
            segments_iter, info = self._model.transcribe(
                str(tmp_path),
                language=lang_param,
                beam_size=max(1, int(self.cfg.beam_size or 5)),
                vad_filter=bool(self.cfg.vad_filter),
                vad_parameters={"min_silence_duration_ms": 400} if self.cfg.vad_filter else None,
                # Whisper hallucinates "Thank you" on silence; suppress with
                # a low log-prob threshold.
                no_speech_threshold=0.6,
                condition_on_previous_text=False,
            )
            # segments_iter is a generator; materialise it to drain decoder.
            parts: list[str] = []
            avg_log_probs: list[float] = []
            for seg in segments_iter:
                txt = (seg.text or "").strip()
                if txt:
                    parts.append(txt)
                if getattr(seg, "avg_logprob", None) is not None:
                    avg_log_probs.append(float(seg.avg_logprob))
            elapsed_ms = int((time.time() - t0) * 1000)

            text = " ".join(parts).strip()
            confidence: float | None = None
            if avg_log_probs:
                # avg_logprob is negative (closer to 0 = more confident).
                # Map to [0,1] via simple exp; clamp.
                import math as _math
                m = sum(avg_log_probs) / len(avg_log_probs)
                confidence = max(0.0, min(1.0, _math.exp(m)))

            # Hallucination / silence filter: too few characters → drop.
            filtered = ""
            stripped_words = [w for w in text.split() if w]
            if len(text) < 2 or len(stripped_words) < 1:
                filtered = "too_short"
                text = ""

            return TranscribeResult(
                text=text,
                language=getattr(info, "language", "") or "",
                duration_ms=elapsed_ms,
                confidence=confidence,
                engine=self.engine,
                model=self._model_id(),
                filtered_reason=filtered,
            )
        finally:
            if not self.cfg.keep_audio:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def unload(self) -> None:
        with self._model_lock:
            self._model = None
            self._loaded_ts = 0.0
        gc.collect()

    def status(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "model": self._model_id(),
            "loaded": self._model is not None,
            "loading": self._loading,
            "loaded_ts": self._loaded_ts or None,
            "last_use_ts": self._last_use_ts or None,
            "device": self.cfg.device,
            "compute_type": self.cfg.compute_type,
            "cache_dir": str(models_dir()),
        }


# --- placeholder backends for Phase 4 ---

class _StubTranscriber(Transcriber):
    def __init__(self, name: str):
        self.engine = name

    def transcribe(self, audio_bytes: bytes, mime: str = "") -> TranscribeResult:
        raise NotImplementedError(
            f"Voice engine '{self.engine}' is not implemented yet. "
            "Set [voice].engine = 'faster-whisper' for now."
        )


# --- factory ---

def build_transcriber(cfg: "VoiceConfig") -> Transcriber:
    eng = (cfg.engine or "faster-whisper").strip().lower()
    if eng in ("faster-whisper", "faster_whisper", "fw"):
        return FasterWhisperTranscriber(cfg)
    if eng in ("sherpa-onnx", "sherpa_onnx", "sherpa"):
        return _StubTranscriber("sherpa-onnx")
    if eng in ("vosk",):
        return _StubTranscriber("vosk")
    raise ValueError(f"unknown voice engine: {cfg.engine!r}")


# Re-export for convenient import: `from .config import VoiceConfig` keeps the
# config schema in one place (config.py); this module keeps the runtime side.
from .config import VoiceConfig  # noqa: E402  (intentional late import)
