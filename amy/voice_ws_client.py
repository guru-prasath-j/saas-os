"""Phase 5 — live-mic voice client streaming over the Amy websocket.

Loop:  🎤 mic --(record-until-silence)--> wav --(faster-whisper)--> text
       --> ws /ws  {text, channel:"voice"}
       <-- {answer, voice_safe, sensitive}
       --(Piper TTS)--> speak voice_safe   (redacted; account #s never spoken)

Modes:
  python -m amy.voice_ws_client --mic --voice en_US-amy.onnx   # live microphone
  python -m amy.voice_ws_client --wav clip.wav                 # one wav file
  python -m amy.voice_ws_client                                # typed fallback

Deps for --mic: sounddevice, numpy, faster-whisper (+ piper-tts for spoken replies).
"""
from __future__ import annotations
import argparse, json, os, tempfile, wave

WS_URL = os.getenv("AMY_WS", "ws://127.0.0.1:8848/ws")
SR = 16000  # whisper-friendly sample rate


def record_until_silence(max_s: float = 15.0, silence_s: float = 1.2,
                         start_s: float = 0.3) -> str:
    """Record from the default mic until ~silence_s of quiet, return a wav path.
    Simple energy-based VAD — no extra services needed."""
    import sounddevice as sd
    import numpy as np

    block = int(SR * 0.1)              # 100 ms blocks
    frames: list[bytes] = []
    silent_blocks = 0
    voiced = False
    # calibrate noise floor from the first few blocks
    calib, n_calib = [], int(start_s / 0.1)
    threshold = None

    print("🎤 listening… (speak; pause to finish)")
    with sd.InputStream(samplerate=SR, channels=1, dtype="int16", blocksize=block) as stream:
        for _ in range(int(max_s / 0.1)):
            data, _ = stream.read(block)
            pcm = np.frombuffer(data, dtype="int16").astype("float32")
            energy = float((pcm ** 2).mean() ** 0.5)
            if threshold is None:
                calib.append(energy)
                if len(calib) >= n_calib:
                    threshold = max(300.0, (sum(calib) / len(calib)) * 3.0)
                continue
            frames.append(data.tobytes())
            if energy > threshold:
                voiced = True; silent_blocks = 0
            elif voiced:
                silent_blocks += 1
                if silent_blocks * 0.1 >= silence_s:
                    break

    path = tempfile.mktemp(suffix=".wav")
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(SR)
        wf.writeframes(b"".join(frames))
    return path


def _send(ws, payload: dict) -> dict:
    ws.send(json.dumps(payload)); return json.loads(ws.recv())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mic", action="store_true", help="capture live microphone")
    ap.add_argument("--wav", help="transcribe one wav file")
    ap.add_argument("--voice", help="Piper voice .onnx model for spoken replies")
    args = ap.parse_args()

    try:
        from websocket import create_connection
    except Exception:
        raise SystemExit("pip install websocket-client")

    ws = create_connection(WS_URL)
    print(f"connected: {WS_URL}\n")
    try:
        while True:
            if args.wav:
                from .voice import transcribe
                text = transcribe(args.wav); print(f"heard: {text}"); args.wav = None
            elif args.mic:
                input("press Enter to talk (or Ctrl-C to quit)…")
                wav = record_until_silence()
                from .voice import transcribe
                text = transcribe(wav); print(f"heard: {text}")
            else:
                text = input("speak (typed) > ").strip()
            if not text or text.lower() in {"exit", "quit"}:
                break

            res = _send(ws, {"text": text, "channel": "voice"})
            spoken = res.get("voice_safe") or res.get("answer", "")
            tag = " [SENSITIVE]" if res.get("sensitive") else ""
            print(f"\nAmy ({res.get('intent')} · {res.get('model')}{tag}) >\n{res.get('answer','')}\n")

            if args.voice:
                from .voice import speak_to_wav
                out = tempfile.mktemp(suffix=".wav")
                speak_to_wav(spoken, out, args.voice)
                try:
                    import sounddevice as sd, soundfile as sf
                    audio, sr = sf.read(out, dtype="float32"); sd.play(audio, sr); sd.wait()
                except Exception:
                    print(f"(spoken -> {out})")
    except KeyboardInterrupt:
        pass
    finally:
        ws.close()


if __name__ == "__main__":
    main()
