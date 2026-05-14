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


# Map faster-whisper short name → HuggingFace repo id, used both for our
# bundled-model lookup and for explicit downloads from the Settings page.
_FW_REPO = {
    "tiny": "Systran/faster-whisper-tiny",
    "tiny.en": "Systran/faster-whisper-tiny.en",
    "base": "Systran/faster-whisper-base",
    "base.en": "Systran/faster-whisper-base.en",
    "small": "Systran/faster-whisper-small",
    "small.en": "Systran/faster-whisper-small.en",
    "medium": "Systran/faster-whisper-medium",
    "medium.en": "Systran/faster-whisper-medium.en",
    "large-v1": "Systran/faster-whisper-large-v1",
    "large-v2": "Systran/faster-whisper-large-v2",
    "large-v3": "Systran/faster-whisper-large-v3",
    "distil-small.en": "Systran/faster-distil-whisper-small.en",
    "distil-medium.en": "Systran/faster-distil-whisper-medium.en",
    "distil-large-v2": "Systran/faster-distil-whisper-large-v2",
    "distil-large-v3": "Systran/faster-distil-whisper-large-v3",
}


def _hf_cache_subdir(model_size: str) -> str:
    """HF on-disk layout: ``models--{owner}--{name}``."""
    repo = _FW_REPO.get(model_size, model_size)
    return "models--" + repo.replace("/", "--")


def _bundled_voice_models_root() -> Optional[Path]:
    """Return the in-bundle voice_models dir if PyInstaller bundled one."""
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return None
    p = Path(meipass) / "lucid" / "voice_models"
    return p if p.is_dir() else None


def _seed_user_cache_from_bundle() -> list[str]:
    """Copy any pre-bundled HF model directories from the PyInstaller bundle
    into ``~/.lucid/voice/models/`` so users can see/manage them like any
    other downloaded model.

    Idempotent: only copies model dirs that don't already exist in the user
    cache. Returns the list of model subdir names that were freshly copied.
    """
    bundled = _bundled_voice_models_root()
    if bundled is None:
        return []
    user_root = models_dir()
    copied: list[str] = []
    import shutil
    # Each top-level entry in the bundled root is either CACHEDIR.TAG or a
    # `models--<owner>--<name>` directory.
    for entry in bundled.iterdir():
        if not entry.is_dir() or not entry.name.startswith("models--"):
            continue
        dst = user_root / entry.name
        if dst.exists():
            continue
        try:
            shutil.copytree(entry, dst)
            copied.append(entry.name)
        except Exception:  # noqa: BLE001 — non-fatal; load() falls back to bundled
            pass
    return copied


def model_is_cached(model_size: str) -> bool:
    """True iff the requested model is in the user cache or the bundle."""
    sub = _hf_cache_subdir(model_size)
    if (models_dir() / sub).is_dir():
        return True
    bundled = _bundled_voice_models_root()
    if bundled and (bundled / sub).is_dir():
        return True
    return False


def model_cache_location(model_size: str) -> str:
    """Return ``"user"`` or ``""`` (not present).

    Note: bundled models are seeded into the user dir on startup (see
    ``_seed_user_cache_from_bundle``), so by the time the UI asks they live
    under ``~/.lucid/voice/models/`` like any other model.
    """
    sub = _hf_cache_subdir(model_size)
    if (models_dir() / sub).is_dir():
        return "user"
    bundled = _bundled_voice_models_root()
    if bundled and (bundled / sub).is_dir():
        # Race: not yet seeded (sidecar started but didn't run seeding yet).
        return "bundled"
    return ""


def download_voice_model(model_size: str, hf_endpoint: str = "") -> dict[str, Any]:
    """Pre-download a faster-whisper model into the user cache.

    Triggered from the Settings page so the first PTT keypress is fast (no
    surprise network stall). Honours ``hf_endpoint`` (e.g. https://hf-mirror.com)
    for users behind blocked HuggingFace.

    Returns ``{ok, size_mb, location, error?}``.
    """
    size = (model_size or "tiny").strip()
    repo = _FW_REPO.get(size)
    if not repo:
        return {"ok": False, "error": f"unknown model size: {size}"}
    if hf_endpoint:
        os.environ["HF_ENDPOINT"] = hf_endpoint
        os.environ["HF_HUB_ENDPOINT"] = hf_endpoint
    try:
        # Use snapshot_download directly so we can target our cache root.
        from huggingface_hub import snapshot_download  # type: ignore[import-not-found]
    except ImportError as e:
        return {"ok": False, "error": f"huggingface_hub not installed: {e}"}
    try:
        local_path = snapshot_download(
            repo_id=repo,
            cache_dir=str(models_dir()),
            local_files_only=False,
        )
    except Exception as e:  # noqa: BLE001 — surface whatever HF threw at the UI
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    # Compute on-disk size of the snapshot dir (resolved symlinks count once).
    total = 0
    seen: set[Path] = set()
    for p in Path(local_path).rglob("*"):
        try:
            real = p.resolve()
        except OSError:
            real = p
        if real in seen or not real.is_file():
            continue
        seen.add(real)
        try:
            total += real.stat().st_size
        except OSError:
            pass
    return {
        "ok": True,
        "size_mb": round(total / 1024 / 1024, 1),
        "location": str(Path(local_path)),
    }


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
        # IMPORTANT: set HF_ENDPOINT *before* importing faster_whisper —
        # huggingface_hub caches `endpoint` as a module-level constant at
        # import time, so a later assignment is silently ignored. Also use
        # plain `=` (not setdefault) so a user-configured mirror always wins
        # over an inherited shell env var.
        if self.cfg.hf_endpoint:
            os.environ["HF_ENDPOINT"] = self.cfg.hf_endpoint
            os.environ["HF_HUB_ENDPOINT"] = self.cfg.hf_endpoint
        try:
            from faster_whisper import WhisperModel  # type: ignore[import-not-found]
        except ImportError as e:
            raise RuntimeError(
                "faster-whisper is not installed. Run `pip install faster-whisper` "
                "(adds ~80MB; CTranslate2 + tokenizers + av) or change "
                "[voice].engine to a backend you have installed."
            ) from e
        # Seed any bundled models (e.g. tiny shipped with the installer) into
        # ~/.lucid/voice/models/ so users see them in one place. No-op if the
        # user already has them.
        _seed_user_cache_from_bundle()
        # Resolve where to look. After seeding we expect the user cache to
        # have everything; fall back to the in-bundle path only if the seed
        # failed (e.g. user wiped the model dir between seeding and load).
        size = self._model_id()
        sub = _hf_cache_subdir(size)
        cache_root = str(models_dir())
        if not (Path(cache_root) / sub).is_dir():
            bundled_root = _bundled_voice_models_root()
            if bundled_root and (bundled_root / sub).is_dir():
                cache_root = str(bundled_root)
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
