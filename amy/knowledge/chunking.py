"""Split a note into retrieval-sized chunks.

Splits on blank lines (paragraphs/headings) and packs them up to ~max_chars,
so a chunk stays topically coherent without being too large to embed.
"""
from __future__ import annotations

import re


def chunk_text(text: str, max_chars: int = 800, overlap: int = 100) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    chunks: list[str] = []
    buf = ""
    for b in blocks:
        if len(b) > max_chars:  # a very long block: hard-split it
            if buf:
                chunks.append(buf); buf = ""
            for i in range(0, len(b), max_chars - overlap):
                chunks.append(b[i:i + max_chars])
            continue
        if len(buf) + len(b) + 2 <= max_chars:
            buf = f"{buf}\n\n{b}" if buf else b
        else:
            chunks.append(buf)
            buf = b
    if buf:
        chunks.append(buf)
    return chunks
