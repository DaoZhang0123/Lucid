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


# Voice transcription is restricted to the same three languages Lucid's UI
# supports (English / Simplified Chinese / French). Anything outside this
# set — either explicitly configured or inferred from the system locale —
# is coerced to the closest match (system primary if it's in the set, else
# English) so Whisper's `language=` parameter is always one of these three.
SUPPORTED_LANGS: tuple[str, ...] = ("en", "zh", "fr")

# Whisper accepts ISO-639-1 two-letter codes (plus a few extras). We only need
# the primary tag from the system locale — strip region / script suffixes.
_WHISPER_LANG_ALIASES = {
    "iw": "he",   # legacy Hebrew
    "in": "id",   # legacy Indonesian
    "ji": "yi",   # legacy Yiddish
    "nb": "no",   # Norwegian Bokmål → Norwegian
    "nn": "no",   # Nynorsk → Norwegian
}


def _coerce_supported(tag: Optional[str]) -> str:
    """Map any 2-letter tag onto the supported set, defaulting to English."""
    if tag and tag in SUPPORTED_LANGS:
        return tag
    return "en"


def _system_language_tag() -> Optional[str]:
    """Return a Whisper-compatible 2-letter language tag from the OS locale.

    Used when ``[voice].language`` is left as ``auto`` / ``system`` so the
    transcriber doesn't drift between languages on short clips. Returns
    ``None`` if we can't determine a sensible tag (caller will fall back to
    Whisper's own auto-detect).
    """
    candidates: list[str] = []
    if sys.platform == "win32":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(85)
            # GetUserDefaultLocaleName → e.g. "zh-CN", "en-US", "fr-FR".
            n = ctypes.windll.kernel32.GetUserDefaultLocaleName(buf, 85)
            if n > 0:
                candidates.append(buf.value)
        except Exception:
            pass
    try:
        import locale
        loc = locale.getdefaultlocale()[0]
        if loc:
            candidates.append(loc)
    except Exception:
        pass
    for env in ("LC_ALL", "LC_MESSAGES", "LANG"):
        v = os.environ.get(env)
        if v:
            candidates.append(v)
    for raw in candidates:
        tag = raw.split(".", 1)[0].split("@", 1)[0].replace("_", "-")
        primary = tag.split("-", 1)[0].strip().lower()
        if len(primary) == 2 and primary.isalpha():
            return _WHISPER_LANG_ALIASES.get(primary, primary)
    return None


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
    try:
        # Use snapshot_download directly so we can target our cache root.
        from huggingface_hub import snapshot_download  # type: ignore[import-not-found]
    except ImportError as e:
        return {"ok": False, "error": f"huggingface_hub not installed: {e}"}

    # Try the user-supplied endpoint first, then fall back to common
    # mirrors. huggingface.co is intentionally last because it is
    # blocked on many networks where the user has set a mirror.
    candidates: list[str] = []
    seen_eps: set[str] = set()
    for ep in (hf_endpoint, "https://hf-mirror.com", "https://huggingface.tuna.tsinghua.edu.cn", "https://huggingface.co"):
        ep = (ep or "").strip().rstrip("/")
        if ep and ep not in seen_eps:
            candidates.append(ep)
            seen_eps.add(ep)

    def _patch_endpoint(ep: str) -> None:
        """Force every loaded huggingface_hub module to use ``ep``.

        ``huggingface_hub`` reads ``HF_ENDPOINT`` once at import time and
        caches it into module-level constants, so simply setting the env
        var here is a no-op when the module is already loaded (which is
        likely, because faster-whisper imports it during ``_load``).
        """
        os.environ["HF_ENDPOINT"] = ep
        os.environ["HF_HUB_ENDPOINT"] = ep
        try:
            from huggingface_hub import constants as _hf_const  # type: ignore[import-not-found]
            _hf_const.ENDPOINT = ep
            _hf_const.HUGGINGFACE_CO_URL_HOME = ep + "/"
            if hasattr(_hf_const, "HUGGINGFACE_CO_URL_TEMPLATE"):
                _hf_const.HUGGINGFACE_CO_URL_TEMPLATE = ep + "/{repo_id}/resolve/{revision}/{filename}"
            for _modname in ("huggingface_hub.file_download",
                             "huggingface_hub._snapshot_download",
                             "huggingface_hub.hf_api"):
                _m = sys.modules.get(_modname)
                if _m is not None and hasattr(_m, "ENDPOINT"):
                    setattr(_m, "ENDPOINT", ep)
        except Exception as e:
            print(f"[voice] HF endpoint patch failed for {ep} ({e})", file=sys.stderr)

    last_err: Exception | None = None
    last_ep: str = ""
    local_path: str = ""

    def _has_complete_weights(snap_dir: str) -> tuple[bool, int]:
        """Check the snapshot dir really contains the weight file.

        faster-whisper distributes its weights as ``model.bin`` (CTranslate2
        format). HuggingFace's ``snapshot_download`` has a misfeature where,
        if the connection drops mid-large-file but the small companion
        files (config/tokenizer/vocab) finished first, it can return
        successfully and leave a zero-byte ``*.incomplete`` blob behind.
        We have to verify by hand.
        """
        sd = Path(snap_dir)
        if not sd.is_dir():
            return False, 0
        candidates_w = list(sd.glob("model.bin")) + list(sd.glob("*.bin"))
        if not candidates_w:
            return False, 0
        # Pick the largest — for distil-* there may be multiple.
        sizes = []
        for w in candidates_w:
            try:
                sizes.append(w.resolve().stat().st_size)
            except OSError:
                sizes.append(0)
        biggest = max(sizes) if sizes else 0
        # Even tiny is ~75 MB; anything under 5 MB means the weight file
        # is just an LFS pointer or an incomplete blob.
        return biggest > 5 * 1024 * 1024, biggest

    for ep in candidates:
        _patch_endpoint(ep)
        try:
            print(f"[voice] downloading {repo} via {ep} ...", file=sys.stderr)
            local_path = snapshot_download(
                repo_id=repo,
                cache_dir=str(models_dir()),
                local_files_only=False,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[voice] download via {ep} raised: {type(e).__name__}: {e}", file=sys.stderr)
            last_err = e
            last_ep = ep
            continue
        ok_w, weight_bytes = _has_complete_weights(local_path)
        if not ok_w:
            print(f"[voice] download via {ep} returned but model.bin is missing/incomplete "
                  f"(largest weight: {weight_bytes} bytes); trying next mirror", file=sys.stderr)
            last_err = RuntimeError("model weights missing or incomplete after download")
            last_ep = ep
            continue
        last_ep = ep
        last_err = None
        break

    if last_err is not None:
        msg = f"{type(last_err).__name__}: {last_err}"
        text = str(last_err)
        if "missing or incomplete" in text:
            msg = (
                f"模型权重 model.bin 没下载完整（已尝试镜像：{', '.join(candidates)}）。"
                "通常是网络在传大文件时被中断。请检查网络/VPN 后重试，"
                "或在“HuggingFace 镜像”里换一个可用的镜像。"
            )
        elif "UNEXPECTED_EOF_WHILE_READING" in text or "SSLEOF" in text or "Max retries exceeded" in text:
            msg = (
                f"无法连接到模型镜像（{last_ep}）。已尝试镜像：{', '.join(candidates)}。"
                f"原始错误：{type(last_err).__name__}。"
                "请检查网络/VPN，或在“HuggingFace 镜像”里换一个可用的镜像后重试。"
            )
        return {"ok": False, "error": msg}
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

    def transcribe(self, audio_bytes: bytes, mime: str = "", ui_locale: str = "") -> TranscribeResult:
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

    def transcribe(self, audio_bytes: bytes, mime: str = "", ui_locale: str = "") -> TranscribeResult:
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
            language = (self.cfg.language or "auto").strip().lower()
            # Whisper's `language=` is always one of the three Lucid UI
            # languages (en / zh / fr). "auto" / "system" / empty resolves
            # against the **Lucid UI locale first** (so the user's chosen
            # interface language wins over the OS locale, which may differ
            # — e.g. Lucid set to zh-CN on an en-US Windows install). Falls
            # back to OS locale only if the front-end didn't pass one.
            # "detect" is intentionally treated the same way — we no longer
            # expose true multilingual auto-detect because <2s clips drift
            # between zh / ko / cy / etc. and we don't ship those models'
            # downstream pipelines anyway. An explicit code outside the set
            # is coerced to English with a one-line warning.
            def _ui_lang_to_whisper(s: str) -> Optional[str]:
                t = (s or "").strip().lower().replace("_", "-")
                if not t:
                    return None
                primary = t.split("-", 1)[0]
                return primary if primary in SUPPORTED_LANGS else None

            if language in ("", "auto", "system", "detect"):
                lang_param: Optional[str] = (
                    _ui_lang_to_whisper(ui_locale)
                    or _coerce_supported(_system_language_tag())
                )
            elif language in SUPPORTED_LANGS:
                lang_param = language
            else:
                lang_param = (
                    _ui_lang_to_whisper(ui_locale)
                    or _coerce_supported(_system_language_tag())
                )
                print(
                    f"[voice] language={language!r} is not in the supported set "
                    f"{SUPPORTED_LANGS}; falling back to {lang_param!r}.",
                    file=sys.stderr,
                )

            t0 = time.time()
            # Whisper's `zh` token covers both Mandarin scripts; the model
            # outputs Traditional more often than Simplified for short
            # clips. Bias the decoder with a Simplified-Chinese prompt and
            # post-convert anything that slips through with `zhconv`.
            # For en/fr we also pass a short language hint so the decoder
            # stays in-language on noisy clips.
            initial_prompt: Optional[str] = None
            if lang_param == "zh":
                initial_prompt = "以下是简体中文普通话的句子。请用简体中文输出。"
            elif lang_param == "en":
                initial_prompt = "The following is a sentence in English."
            elif lang_param == "fr":
                initial_prompt = "Ce qui suit est une phrase en français."
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
                initial_prompt=initial_prompt,
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
            # Whisper still emits Traditional Hanzi for ~zh fairly often even
            # with a Simplified prompt; force-convert when the user picked zh.
            if text and lang_param == "zh":
                try:
                    from zhconv import convert as _zh_convert  # type: ignore
                    text = _zh_convert(text, "zh-cn")
                except Exception as e:  # zhconv missing → leave text alone
                    print(f"[voice] zhconv unavailable ({e}); leaving Traditional output", file=sys.stderr)
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

    def transcribe(self, audio_bytes: bytes, mime: str = "", ui_locale: str = "") -> TranscribeResult:
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
