"""Vector index over the vault.

Embeddings:
  - EMBED_BACKEND="ollama" -> Ollama with EMBED_MODEL (e.g. nomic-embed-text)
  - EMBED_BACKEND="st"     -> sentence-transformers
Falls back to a dependency-free keyword (TF-IDF) index if Chroma/embeddings unavailable.
Builds incrementally: only embeds notes not already in the collection (fast restarts)."""
from __future__ import annotations
import math, re
from collections import Counter
from . import config, vault as vaultmod

_WORD = re.compile(r"[a-z0-9]+")
def _tok(s): return _WORD.findall(s.lower())
def _doc(n): return f"{n.title}\n{n.body}"


class _OllamaEF:
    """Chroma EmbeddingFunction backed by Ollama (e.g. nomic-embed-text)."""
    def __init__(self, model: str, host: str):
        import ollama
        self._c = ollama.Client(host=host)
        self._model = model
        self._c.embeddings(model=model, prompt="ping")  # raises if model/daemon missing

    def name(self): return f"ollama:{self._model}"

    def _embed(self, input):
        texts = input if isinstance(input, list) else [input]
        return [self._c.embeddings(model=self._model, prompt=t)["embedding"] for t in texts]

    def __call__(self, input): return self._embed(input)
    def embed_documents(self, input): return self._embed(input)
    def embed_query(self, input): return self._embed(input)


class KeywordIndex:
    """Dependency-free TF-IDF fallback."""
    def __init__(self):
        self.docs = []; self.df = Counter(); self.N = 0

    def build(self, notes):
        self.docs = []; self.df = Counter()
        for n in notes:
            tf = Counter(_tok(_doc(n)))
            self.docs.append({"note": n, "tf": tf})
            for w in tf:
                self.df[w] += 1
        self.N = len(self.docs)

    def _score(self, q, d):
        s = 0.0
        for w, c in q.items():
            if w in d:
                idf = math.log((self.N + 1) / (self.df[w] + 1)) + 1
                s += c * d[w] * idf * idf
        return s

    def search(self, query, k=5, allow=None):
        q = Counter(_tok(query)); scored = []
        for d in self.docs:
            if allow and not allow(d["note"]):
                continue
            sc = self._score(q, d["tf"])
            if sc > 0:
                scored.append((sc, d))
        scored.sort(key=lambda x: x[0], reverse=True)
        hits = [d["note"] for _, d in scored[:k]]
        if hits:
            return hits
        return [d["note"] for d in self.docs if (not allow or allow(d["note"]))][:k]


class ChromaIndex:
    def __init__(self, index_dir=None, collection="vault"):
        import chromadb
        self._client = chromadb.PersistentClient(path=str(index_dir or config.INDEX_DIR))
        if config.EMBED_BACKEND == "ollama":
            ef = _OllamaEF(config.EMBED_MODEL, config.OLLAMA_HOST)
        else:
            from chromadb.utils import embedding_functions
            ef = embedding_functions.SentenceTransformerEmbeddingFunction(model_name=config.EMBED_MODEL)
        self._col = self._client.get_or_create_collection(collection, embedding_function=ef)
        self._by_path = {}

    def build(self, notes):
        for n in notes:
            self._by_path[n.path] = n
        try:
            existing = set(self._col.get().get("ids", []))
        except Exception:
            existing = set()
        todo = [n for n in notes if n.path not in existing]
        if todo:
            self._col.upsert(
                ids=[n.path for n in todo],
                documents=[_doc(n) for n in todo],
                metadatas=[{"path": n.path, "title": n.title, "category": n.category,
                            "owner": n.owner, "sensitive": n.sensitive} for n in todo])

    def search(self, query, k=5, allow=None):
        res = self._col.query(query_texts=[query], n_results=max(k * 3, 10))
        out = []
        for path in res["ids"][0]:
            n = self._by_path.get(path)
            if n and (not allow or allow(n)):
                out.append(n)
            if len(out) >= k:
                break
        return out


def build_index(notes, index_dir=None, collection="vault"):
    try:
        idx = ChromaIndex(index_dir, collection); idx.build(notes); return idx, f"chroma+{config.EMBED_BACKEND}"
    except Exception:
        idx = KeywordIndex(); idx.build(notes); return idx, "keyword"


def drop_index(index_dir, collection="vault"):
    """Delete a per-user Chroma collection (used on vault delete / account wipe).
    No-op for the keyword fallback (nothing persisted)."""
    try:
        import chromadb
        client = chromadb.PersistentClient(path=str(index_dir))
        client.delete_collection(collection)
    except Exception:
        pass
