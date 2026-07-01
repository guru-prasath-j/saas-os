"""Embedders + the embedding engine.

HashingEmbedder: deterministic, dependency-free, offline (a real local fallback and
great for tests). OpenAIEmbedder: higher quality when a key is configured.
EmbeddingEngine: chunk each note, embed, and store in vector.db.
"""
from __future__ import annotations

import hashlib
import json
import math
import re

from .chunking import chunk_text

_W = re.compile(r"[a-z0-9]+")


class HashingEmbedder:
    name = "hashing"

    def __init__(self, dim: int = 256):
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in _W.findall((text or "").lower()):
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            v[h % self.dim] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]


class OpenAIEmbedder:
    name = "openai"

    def __init__(self, api_key: str, model: str = "text-embedding-3-small"):
        from openai import OpenAI
        self._c = OpenAI(api_key=api_key)
        self._model = model
        self.dim = 1536

    def embed(self, text: str) -> list[float]:
        r = self._c.embeddings.create(model=self._model, input=text[:8000])
        return r.data[0].embedding


class NvidiaEmbedder:
    """Free NVIDIA NIM embeddings (nv-embedqa-e5-v5, 1024-dim), OpenAI-compatible.
    Needs an `input_type` (query|passage), passed via extra_body."""
    name = "nvidia"

    def __init__(self, api_key: str, model: str | None = None, base_url: str | None = None):
        from openai import OpenAI
        from .. import config
        self._c = OpenAI(api_key=api_key, base_url=base_url or config.NVIDIA_BASE_URL)
        self._model = model or config.NVIDIA_EMBED_MODEL
        self.dim = 1024

    def _embed(self, text: str, input_type: str) -> list[float]:
        r = self._c.embeddings.create(
            model=self._model, input=[text[:8000]],
            extra_body={"input_type": input_type, "truncate": "END"})
        return r.data[0].embedding

    def embed(self, text: str) -> list[float]:          # documents/passages
        return self._embed(text, "passage")

    def embed_query(self, text: str) -> list[float]:    # queries
        return self._embed(text, "query")


class STEmbedder:
    """Local sentence-transformers embedder (offline)."""
    name = "sentence-transformers"

    def __init__(self, model: str | None = None):
        from sentence_transformers import SentenceTransformer
        from .. import config
        self._m = SentenceTransformer(model or config.ST_EMBED_MODEL)
        self.dim = self._m.get_sentence_embedding_dimension()

    def embed(self, text: str) -> list[float]:
        return self._m.encode(text[:8000]).tolist()


def make_embedder(provider: str | None = None, openai_key: str | None = None):
    """Pick an embedder. provider: auto|nvidia|openai|st|hashing.
    auto order: nvidia (if key) -> openai (if key) -> sentence-transformers -> hashing."""
    from .. import config
    p = (provider or config.EMBED_PROVIDER or "auto").lower()

    def _nvidia():
        return NvidiaEmbedder(config.NVIDIA_API_KEY) if config.NVIDIA_API_KEY else None

    def _openai():
        key = openai_key or config.OPENAI_API_KEY
        return OpenAIEmbedder(key) if key else None

    def _st():
        try:
            return STEmbedder()
        except Exception:
            return None

    if p == "nvidia":
        return _nvidia() or HashingEmbedder()
    if p == "openai":
        return _openai() or HashingEmbedder()
    if p == "st":
        return _st() or HashingEmbedder()
    if p == "hashing":
        return HashingEmbedder()
    # auto
    return _nvidia() or _openai() or _st() or HashingEmbedder()


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


class EmbeddingEngine:
    def __init__(self, dbs, embedder):
        self.dbs = dbs
        self.embedder = embedder

    def build_for_note(self, note_id: str, text: str, max_chars: int = 800):
        cur = self.dbs.vector
        cur.execute("DELETE FROM chunks WHERE note_id=?", (note_id,))
        rows = []
        for i, ch in enumerate(chunk_text(text, max_chars=max_chars)):
            emb = self.embedder.embed(ch)
            rows.append((f"{note_id}:{i}", note_id, i, ch,
                         json.dumps(emb), len(emb), self.embedder.name))
        if rows:
            cur.executemany(
                "INSERT OR REPLACE INTO chunks "
                "(id, note_id, chunk_index, text, embedding, dim, model) "
                "VALUES (?,?,?,?,?,?,?)", rows)
        cur.commit()
        return len(rows)

    def query_embedding(self, query: str) -> list[float]:
        # use embed_query when the embedder distinguishes queries (e.g. NVIDIA)
        fn = getattr(self.embedder, "embed_query", None)
        return fn(query) if callable(fn) else self.embedder.embed(query)
