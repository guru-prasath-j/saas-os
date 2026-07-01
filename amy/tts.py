"""Server-side Piper TTS. Synthesizes text to WAV bytes per persona model.

Degrades gracefully: callers should fall back to browser TTS if this raises
(e.g., piper not installed or a voice model is missing)."""
from __future__ import annotations
import io, wave, re
from . import config, prefs

_cache = {}


def _clean(text: str) -> str:
    t = re.sub(r"```[\s\S]*?```", " ", str(text))
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)
    t = re.sub(r"\*([^*]+)\*", r"\1", t)
    t = re.sub(r"[#*_`>|~]", " ", t)
    return re.sub(r"\s{2,}", " ", t).strip()


def model_path(voice_id: str):
    meta = prefs.voice_meta(voice_id) or {}
    name = meta.get("piper", "")
    return config.VOICES_MODELS_DIR / f"{name}.onnx" if name else None


def available(voice_id: str) -> bool:
    p = model_path(voice_id)
    return bool(p and p.exists())


def _voice(model_file):
    key = str(model_file)
    if key not in _cache:
        from piper import PiperVoice  # raises if piper-tts not installed
        _cache[key] = PiperVoice.load(key)
    return _cache[key]


def synth(text: str, voice_id: str) -> bytes:
    """Return WAV audio bytes for the given persona. Raises if unavailable."""
    mp = model_path(voice_id)
    if not mp or not mp.exists():
        raise FileNotFoundError(f"voice model missing: {mp}")
    v = _voice(mp)
    clean = _clean(text)[:1500] or "."
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        v.synthesize(clean, wf)
    return buf.getvalue()
