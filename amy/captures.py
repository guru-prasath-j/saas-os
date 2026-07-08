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


# --------------------------------------------------------------------------- #
# photo memory — search over ingested captures (AI memory layer)
#
# Reads 08_Captures/**/*.md straight from the vault on every call (same
# always-fresh policy as amy/memory/recall.py), so a photo taken a minute ago
# is already searchable. Used by:
#   * CollabMaster._captures_context   (chat context injection)
#   * tools search_captures / recent_captures   (assistant console)
#   * the capture_digest automation job (daily/weekly comparison)
# --------------------------------------------------------------------------- #
_STOPWORDS = {
    "a", "an", "and", "the", "of", "in", "on", "at", "is", "was", "are",
    "were", "it", "that", "this", "these", "those", "i", "me", "my", "we",
    "you", "your", "do", "did", "does", "what", "which", "who", "when",
    "where", "how", "about", "with", "for", "from", "to", "have", "has",
    "had", "there", "be", "been", "can", "could", "tell", "show", "any",
    "some", "one", "or", "took", "take", "taken", "saved", "photo",
    "picture", "pic", "image", "capture", "captured", "amy",
}

_CAPTION_RE = re.compile(r"\*\*Caption:\*\*\s*(.+)")
_LOCATION_RE = re.compile(r"\*\*Location:\*\*\s*(.+)")
_NOTE_RE = re.compile(r"\*\*Note:\*\*\s*(.+)")

# Reverse-geocoding returns official city names; people ask with the common
# ones. Both directions are added at tokenization so either side matches.
_CITY_ALIASES = {
    "bangalore": "bengaluru", "bombay": "mumbai", "madras": "chennai",
    "calcutta": "kolkata", "gurgaon": "gurugram", "mysore": "mysuru",
    "poona": "pune", "trivandrum": "thiruvananthapuram", "cochin": "kochi",
    "baroda": "vadodara", "benares": "varanasi", "allahabad": "prayagraj",
}
_CITY_ALIASES.update({v: k for k, v in list(_CITY_ALIASES.items())})


def _cap_tokens(text: str) -> set:
    toks = {t for t in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(t) > 1 and t not in _STOPWORDS}
    toks |= {_CITY_ALIASES[t] for t in toks if t in _CITY_ALIASES}
    return toks


def _body_field(rx, body: str) -> str:
    m = rx.search(body)
    return m.group(1).strip() if m else ""


def _parse_capture_note(rel: str, text: str) -> dict:
    """One capture .md → a flat record. Tolerant of hand-edited notes."""
    from .vault import _tiny_parse
    meta, body = _tiny_parse(text)
    loc = meta.get("location")
    place = ((loc.get("place", "") if isinstance(loc, dict) else "")
             or meta.get("place", "") or _body_field(_LOCATION_RE, body))
    ocr = "\n".join(ln.lstrip("> ").strip() for ln in body.splitlines()
                    if ln.startswith(">"))
    tags = meta.get("tags") or []
    if not isinstance(tags, list):
        tags = [str(tags)]
    created = str(meta.get("created", ""))
    return {
        "path": rel,
        "title": str(meta.get("title", "")) or rel.rsplit("/", 1)[-1],
        "created": created, "date": created[:10],
        "place": str(place), "caption": _body_field(_CAPTION_RE, body),
        "ocr": ocr, "note": _body_field(_NOTE_RE, body),
        "tags": [str(t).strip() for t in tags if str(t).strip()],
        "image": str(meta.get("image", "")),
        "source": str(meta.get("source", "")),
    }


def load_capture_records(vault=None, limit: int = 400) -> list[dict]:
    """All capture records, newest first, read fresh from disk (capped)."""
    root = captures_dir(vault)
    if not root.exists():
        return []
    files = sorted(root.rglob("*.md"), key=lambda f: f.name, reverse=True)
    out = []
    for f in files[:limit]:
        try:
            rel = str(f.relative_to(_vault(vault))).replace("\\", "/")
            out.append(_parse_capture_note(
                rel, f.read_text(encoding="utf-8", errors="ignore")))
        except Exception:
            continue
    out.sort(key=lambda r: r["created"] or r["path"], reverse=True)
    return out


def _date_hint(query: str):
    """Map 'today'/'yesterday' in the query to a YYYY-MM-DD boost target."""
    q = query.lower()
    today = _dt.date.today()
    if "yesterday" in q:
        return (today - _dt.timedelta(days=1)).isoformat()
    if "today" in q or "this morning" in q or "tonight" in q:
        return today.isoformat()
    return None


def search_captures(query: str, vault=None, limit: int = 5,
                    min_score: float = 0.2) -> list[dict]:
    """Rank captures by weighted token overlap between the query and what we
    know about each photo (place > tags/note/title > caption > OCR).
    Score is normalized by query length; 'yesterday'/'today' in the query
    boosts captures from that day. Returns records + 'score', [] if nothing
    clears min_score."""
    qtok = _cap_tokens(query)
    if not qtok:
        return []
    hint = _date_hint(query)
    scored = []
    for r in load_capture_records(vault):
        fields = (
            (3.0, _cap_tokens(r["place"])),
            (2.0, _cap_tokens(" ".join(r["tags"]))),
            (2.0, _cap_tokens(r["note"])),
            (2.0, _cap_tokens(r["title"])),
            (1.5, _cap_tokens(r["caption"])),
            (1.0, _cap_tokens(r["ocr"])),
        )
        score = sum(max((w for w, toks in fields if t in toks), default=0.0)
                    for t in qtok) / max(len(qtok), 1)
        if hint and r["date"] == hint:
            score += 0.5
        if score >= min_score:
            scored.append((score, r))
    scored.sort(key=lambda x: (x[0], x[1]["created"]), reverse=True)
    return [dict(r, score=round(s, 3)) for s, r in scored[:limit]]


def captures_between(start: str, end: str, vault=None) -> list[dict]:
    """Capture records with start <= date <= end (YYYY-MM-DD, inclusive)."""
    return [r for r in load_capture_records(vault, limit=1000)
            if r["date"] and start <= r["date"] <= end]


def context_block(query: str, vault=None, k: int = 3) -> str:
    """Chat-context block describing the captures relevant to this query, or
    '' when nothing clears the relevance gate (never pollute a reply)."""
    hits = search_captures(query, vault=vault, limit=k)
    if not hits:
        return ""
    lines = ["## Photo memory (captures from your vault)"]
    for h in hits:
        bits = [f"[{h['date'] or 'undated'}] {h['title']}"]
        if h["place"]:
            bits.append(f"place: {h['place']}")
        if h["caption"]:
            bits.append(f"caption: {h['caption']}")
        if h["note"]:
            bits.append(f"user note: {h['note']}")
        if h["ocr"]:
            bits.append("text in photo: " +
                        " / ".join(h["ocr"].splitlines())[:400])
        if h["tags"]:
            bits.append("tags: " + ", ".join(h["tags"]))
        lines.append("- " + " · ".join(bits) + f" (note: {h['path']})")
    return "\n".join(lines)


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
