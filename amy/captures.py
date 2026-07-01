"""Photo capture ingestion for PersonalOS / Amy.

The mobile app sends a photo (+ time/GPS). We:
  1. save the image into the vault under 08_Captures/attachments/
  2. ask a vision model for a caption + OCR text (OpenAI gpt-4o-mini)
  3. optionally reverse-geocode the GPS to a place name
  4. write a markdown "capture note" into 08_Captures/YYYY/MM/
The note is a normal vault note, so the existing index/agents pick it up and
Amy can answer about your photos later.

Privacy: ingestion is disabled in PUBLIC mode (see app.py). Images never leave
your backend except the single vision call to OpenAI for caption/OCR.
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import config

CAPTURES_REL = "08_Captures"


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #
def _vault(vault=None) -> Path:
    return Path(vault or config.VAULT)


def captures_dir(vault=None) -> Path:
    return _vault(vault) / CAPTURES_REL


def attachments_dir(vault=None) -> Path:
    return captures_dir(vault) / "attachments"


# --------------------------------------------------------------------------- #
# vision: caption + OCR in one OpenAI call
# --------------------------------------------------------------------------- #
_VISION_SYS = (
    "You describe photos for a personal memory assistant. Return STRICT JSON only: "
    '{"caption": "<one or two sentence description of the scene>", '
    '"ocr": "<all readable text in the image, verbatim; empty string if none>", '
    '"tags": ["<3-6 short lowercase keyword tags>"]}'
)


def _ext_for(filename: str, content_type: str | None) -> str:
    if filename and "." in filename:
        return filename.rsplit(".", 1)[-1].lower()
    if content_type and "/" in content_type:
        return content_type.split("/")[-1].lower().replace("jpeg", "jpg")
    return "jpg"


def _mime_for(ext: str) -> str:
    ext = ext.lower()
    return "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"


def analyze_image(image_bytes: bytes, ext: str = "jpg", api_key=None) -> dict:
    """Return {'caption', 'ocr', 'tags', 'model'}. Degrades gracefully.

    api_key: pass None to use the global key (personal app); pass the user's key
    in SaaS; pass "" to force no captioning (SaaS user without a key) so a shared
    key is never used.
    """
    key = api_key if api_key is not None else config.OPENAI_API_KEY
    if not key:
        return {"caption": "", "ocr": "", "tags": [], "model": "none"}
    try:
        from openai import OpenAI

        client = OpenAI(api_key=key)
        b64 = base64.b64encode(image_bytes).decode("ascii")
        data_uri = f"data:{_mime_for(ext)};base64,{b64}"
        r = client.chat.completions.create(
            model=config.OPENAI_MODEL,
            max_tokens=500,
            messages=[
                {"role": "system", "content": _VISION_SYS},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this photo and extract any text."},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                },
            ],
        )
        raw = r.choices[0].message.content or "{}"
        data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        tags = data.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        return {
            "caption": str(data.get("caption", "")).strip(),
            "ocr": str(data.get("ocr", "")).strip(),
            "tags": [str(t).strip().lower() for t in tags if str(t).strip()][:8],
            "model": config.OPENAI_MODEL,
        }
    except Exception as e:  # never let a vision failure block ingestion
        return {"caption": "", "ocr": "", "tags": [], "model": f"error:{type(e).__name__}"}


# --------------------------------------------------------------------------- #
# optional reverse-geocode (best effort, no hard dependency)
# --------------------------------------------------------------------------- #
def reverse_geocode(lat: float, lon: float) -> str:
    try:
        import urllib.request

        url = (
            "https://nominatim.openstreetmap.org/reverse?format=json"
            f"&lat={lat}&lon={lon}&zoom=14"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "PersonalOS-Amy/1.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        a = data.get("address", {})
        parts = [
            a.get("suburb") or a.get("neighbourhood") or a.get("village"),
            a.get("city") or a.get("town") or a.get("county"),
            a.get("state"),
        ]
        place = ", ".join([p for p in parts if p])
        return place or data.get("display_name", "")
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# ingestion
# --------------------------------------------------------------------------- #
@dataclass
class CaptureResult:
    note_path: str       # vault-relative path to the .md note
    image_path: str      # vault-relative path to the image
    title: str
    caption: str
    ocr: str
    place: str
    created: str
    hash: str
    duplicate: bool = False


def _safe(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40]


def _parse_dt(taken_at: str | None) -> _dt.datetime:
    if taken_at:
        try:
            return _dt.datetime.fromisoformat(taken_at.replace("Z", "+00:00"))
        except Exception:
            pass
    return _dt.datetime.now().astimezone()


def ingest(
    image_bytes: bytes,
    filename: str = "",
    content_type: str | None = None,
    taken_at: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    source: str = "mobile",
    note: str = "",
    tags: list[str] | None = None,
    vault=None,
    openai_api_key=None,
) -> CaptureResult:
    """Write image + capture note into the vault. Idempotent by image hash.

    vault: target vault root (per-user in SaaS; defaults to global config.VAULT).
    openai_api_key: caption/OCR key (None=global, user's key in SaaS, ""=skip).
    """
    h = hashlib.sha1(image_bytes).hexdigest()[:12]
    ext = _ext_for(filename, content_type)
    created = _parse_dt(taken_at)
    stamp = created.strftime("%Y-%m-%d_%H%M")

    attachments_dir(vault).mkdir(parents=True, exist_ok=True)
    img_name = f"{stamp}_{h}.{ext}"
    img_abs = attachments_dir(vault) / img_name
    img_rel = f"{CAPTURES_REL}/attachments/{img_name}"

    note_dir = captures_dir(vault) / created.strftime("%Y") / created.strftime("%m")
    note_abs = note_dir / f"{stamp}_{h}.md"
    note_rel = f"{CAPTURES_REL}/{created.strftime('%Y')}/{created.strftime('%m')}/{stamp}_{h}.md"

    # dedup: same image already ingested
    if note_abs.exists():
        return CaptureResult(
            note_path=note_rel, image_path=img_rel, title=note_abs.stem,
            caption="", ocr="", place="", created=created.isoformat(),
            hash=h, duplicate=True,
        )

    # save image
    img_abs.write_bytes(image_bytes)

    # analyze
    vis = analyze_image(image_bytes, ext, api_key=openai_api_key)
    caption, ocr = vis["caption"], vis["ocr"]
    all_tags = sorted(set((tags or []) + vis["tags"]))

    place = reverse_geocode(lat, lon) if (lat is not None and lon is not None) else ""

    # build the note
    pretty = created.strftime("%d %b %Y, %I:%M %p").lstrip("0")
    title = f"Capture - {pretty}" + (f" - {place}" if place else "")

    fm = ["---", "type: capture", "category: captures",
          f"created: {created.isoformat()}",
          f"ingested: {_dt.datetime.now().astimezone().isoformat()}",
          f"source: {source}", f"image: attachments/{img_name}", f"hash: {h}"]
    if lat is not None and lon is not None:
        fm += ["location:", f"  lat: {lat}", f"  lon: {lon}"]
        if place:
            fm.append(f'  place: "{place}"')
    if all_tags:
        fm.append("tags: [" + ", ".join(all_tags) + "]")
    fm.append(f'title: "{title}"')
    fm.append("---")

    body = [f"# {title}", ""]
    body.append(f"![[{img_rel}]]")
    body.append("")
    if caption:
        body += [f"**Caption:** {caption}", ""]
    if ocr:
        body += ["**Text (OCR):**", ""] + [f"> {ln}" for ln in ocr.splitlines() if ln.strip()] + [""]
    if place:
        body += [f"**Location:** {place}", ""]
    if note:
        body += [f"**Note:** {note}", ""]

    note_dir.mkdir(parents=True, exist_ok=True)
    note_abs.write_text("\n".join(fm) + "\n\n" + "\n".join(body) + "\n", encoding="utf-8")

    return CaptureResult(
        note_path=note_rel, image_path=img_rel, title=title, caption=caption,
        ocr=ocr, place=place, created=created.isoformat(), hash=h, duplicate=False,
    )


def list_captures(notes, limit: int = 50) -> list[dict]:
    """Recent capture notes from the loaded vault notes (newest first)."""
    caps = [n for n in notes if n.path.startswith(CAPTURES_REL + "/") and n.path.endswith(".md")]
    caps.sort(key=lambda n: n.meta.get("created", n.path), reverse=True)
    out = []
    for n in caps[:limit]:
        loc = n.meta.get("location") or {}
        out.append({
            "path": n.path,
            "title": n.title,
            "created": n.meta.get("created", ""),
            "image": n.meta.get("image", ""),
            "place": (loc.get("place") if isinstance(loc, dict) else "") or "",
            "tags": n.tags,
        })
    return out
