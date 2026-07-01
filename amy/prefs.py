"""Voice registry + user preferences (default Amy). Voices are config-driven
(voices.json) so new ones can be added without code changes."""
from __future__ import annotations
import json
from . import config


def load_voices() -> dict:
    try:
        return json.loads(config.VOICES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"default": "amy", "voices": [{"id": "amy", "name": "Amy",
                "desc": "Friendly default voice", "gender": "female",
                "web_match": ["female"], "piper": "en_US-amy-medium", "rate": 1.04}]}


def _read_prefs() -> dict:
    try:
        return json.loads(config.PREFS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_prefs(p: dict):
    try:
        config.PREFS_FILE.write_text(json.dumps(p, indent=2), encoding="utf-8")
    except Exception:
        pass


def current_voice() -> str:
    reg = load_voices()
    ids = [v["id"] for v in reg.get("voices", [])]
    pref = _read_prefs().get("voice")
    if pref in ids:
        return pref
    return reg.get("default", ids[0] if ids else "amy")


def set_voice(voice_id: str) -> bool:
    ids = [v["id"] for v in load_voices().get("voices", [])]
    if voice_id not in ids:
        return False
    p = _read_prefs(); p["voice"] = voice_id; _write_prefs(p)
    return True


def voice_meta(voice_id: str) -> dict:
    for v in load_voices().get("voices", []):
        if v["id"] == voice_id:
            return v
    return {}
