"""Download the 4 Piper voice models for PersonalOS / Amy HD voices.

Run once on your machine (needs internet):
    cd _Amy
    pip install piper-tts
    python -m scripts.setup_voices

Downloads ~80 MB into _Amy/voices_models/. After this, toggle 'HD voice' in the
dashboard and the four personas sound exactly as designed.
"""
import urllib.request
from pathlib import Path

BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
OUT = Path(__file__).resolve().parent.parent / "voices_models"

# (huggingface relative path without extension) -> local file stem
MODELS = {
    "en/en_US/amy/medium/en_US-amy-medium": "en_US-amy-medium",
    "en/en_GB/jenny_dioco/medium/en_GB-jenny_dioco-medium": "en_GB-jenny_dioco-medium",
    "en/en_US/ryan/high/en_US-ryan-high": "en_US-ryan-high",
    "en/en_GB/alan/medium/en_GB-alan-medium": "en_GB-alan-medium",
}


def fetch(url, dest):
    if dest.exists() and dest.stat().st_size > 0:
        print("  exists:", dest.name); return
    print("  downloading:", dest.name)
    urllib.request.urlretrieve(url, dest)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for rel, stem in MODELS.items():
        for ext in (".onnx", ".onnx.json"):
            fetch(BASE + rel + ext, OUT / (stem + ext))
    print("\nDone. Models in", OUT)
    print("Restart the server and toggle 'HD voice' in the dashboard.")


if __name__ == "__main__":
    main()
