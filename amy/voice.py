"""Phase 5 — Voice layer: wake word + STT + TTS.

Two ways to use voice with Amy:
  1) Browser (works today): the dashboard uses the Web Speech API — press 🎤, speak,
     and answers are spoken back using the REDACTED `voice_safe` text. No installs.
  2) Native/on-device (this module): faster-whisper for STT, Piper for TTS.

All functions degrade gracefully: if a library is missing they raise a clear,
actionable error instead of crashing the import.

Privacy rule: NEVER speak text flagged sensitive. Feed TTS the engine's
`result.voice_safe` (account numbers / UPI ids already redacted).
"""
from __future__ import annotations
import io, wave
from .engine import get_engine

# ----- STT (faster-whisper) -----
_whisper = None
def _get_whisper(model_size: str = "base.en"):
    global _whisper
    if _whisper is None:
        try:
            from faster_whisper import WhisperModel
        except Exception as e:
            raise RuntimeError("pip install faster-whisper to enable STT") from e
        _whisper = WhisperModel(model_size, device="cpu", compute_type="int8")
    return _whisper

def transcribe(wav_path: str) -> str:
    model = _get_whisper()
    segments, _ = model.transcribe(wav_path)
    return " ".join(s.text for s in segments).strip()

# ----- TTS (Piper) -----
def speak_to_wav(text: str, out_path: str, voice_model: str | None = None) -> str:
    try:
        from piper.voice import PiperVoice
    except Exception as e:
        raise RuntimeError("pip install piper-tts and download a voice model") from e
    if not voice_model:
        raise RuntimeError("provide a Piper .onnx voice model path")
    voice = PiperVoice.load(voice_model)
    with wave.open(out_path, "wb") as wf:
        voice.synthesize(text, wf)
    return out_path

# ----- end-to-end one-shot: audio in -> spoken answer out -----
def handle_audio(wav_in: str, wav_out: str, voice_model: str) -> dict:
    text = transcribe(wav_in)
    result = get_engine().ask(text, channel="voice")
    spoken = result.voice_safe or result.answer      # redacted for speech
    speak_to_wav(spoken, wav_out, voice_model)
    return {"heard": text, "intent": result.intent,
            "spoken": spoken, "sensitive": result.sensitive, "model": result.model}

# ----- wake word (optional) -----
def listen_for_wake(keyword: str = "amy"):
    """Stub: integrate openWakeWord / Porcupine. On detection, record a clip and call handle_audio()."""
    raise NotImplementedError("Wire openWakeWord/Porcupine here; trigger handle_audio() on '%s'." % keyword)
