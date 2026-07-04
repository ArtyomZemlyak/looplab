"""Pluggable vector store (I17, ADR-16). `VectorStore` is the seam; LanceDB is the
production default — here we ship a dependency-free `InMemoryVectorStore` (brute-force
cosine) as the testable default so swapping in LanceDB/Qdrant is a config change.

`hash_embed` is a deterministic (hashlib-based) bag-of-words embedder for offline
tests; production embeddings go through LiteLLM (`ollama/nomic-embed-text`).
"""
from __future__ import annotations

import hashlib
import http.client
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

Vector = list[float]


@dataclass
class Item:
    id: str
    vector: Vector
    payload: dict = field(default_factory=dict)


@dataclass
class Hit:
    id: str
    score: float
    payload: dict


class VectorStore(Protocol):
    def upsert(self, index: str, items: list[Item]) -> None: ...
    def search(self, index: str, query: Vector, k: int) -> list[Hit]: ...
    def delete(self, index: str, ids: list[str]) -> None: ...
    def rebuild(self, index: str) -> None: ...


def hash_embed(text: str, dim: int = 64) -> Vector:
    v = [0.0] * dim
    for tok in text.lower().split():
        h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
        v[h % dim] += 1.0
    return v


class LLMEmbedder:
    """Real semantic embeddings over any OpenAI-compatible `/embeddings` endpoint (Ollama
    `nomic-embed-text`, vLLM/SGLang, OpenAI…), replacing the lexical `hash_embed` bag-of-words when a
    model is configured. Dependency-free (stdlib urllib, so it uses the same proxy/CA env the chat
    client does). **Robust by construction:** it commits to ONE vector dimension for its lifetime, so
    `_cosine` never sees a dim mismatch — if a call fails (endpoint down / offline box), it returns a
    `hash_embed` fallback PADDED to that same dimension, and if the very first call fails it degrades
    to pure `hash_embed` at `dim_fallback`. So retrieval is never crashed by a flaky endpoint; it just
    quietly loses semantic quality. A single embedder instance must build AND query one index (same
    dim)."""

    def __init__(self, model: str, base_url: str = "http://localhost:11434/v1",
                 api_key: str = "x", timeout: float = 30.0, dim_fallback: int = 64):
        self.model = model
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or "x"
        self.timeout = timeout
        self.dim_fallback = dim_fallback
        self._dim: Optional[int] = None     # committed on first success (or first fallback)
        self._live: Optional[bool] = None    # None=untried, True=endpoint works, False=degraded to hash

    def _call(self, texts: list[str]) -> Optional[list[Vector]]:
        """One batched POST /embeddings. Returns per-text vectors, or None on ANY failure (network,
        HTTP, bad body) so the caller degrades gracefully instead of crashing the run."""
        try:
            req = urllib.request.Request(
                f"{self.base_url}/embeddings",
                data=json.dumps({"model": self.model, "input": texts}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {self.api_key}"},
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError,
                json.JSONDecodeError, http.client.HTTPException):
            # HTTPException covers IncompleteRead/BadStatusLine — a server dying mid-response
            # must degrade to the hash fallback, not crash role construction.
            return None
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or len(data) != len(texts):
            return None
        vecs: list[Vector] = []
        try:
            for row in data:
                emb = row.get("embedding") if isinstance(row, dict) else None
                if not isinstance(emb, list) or not emb:
                    return None
                vecs.append([float(x) for x in emb])
        except (TypeError, ValueError):    # non-numeric entries in a malformed body
            return None
        return vecs

    def _fallback(self, texts: list[str]) -> list[Vector]:
        d = self._dim or self.dim_fallback
        self._dim = d
        return [hash_embed(t, dim=d) for t in texts]

    def embed_many(self, texts: list[str]) -> list[Vector]:
        if not texts:
            return []
        if self._live is not False:                      # untried or known-live -> try the endpoint
            vecs = self._call(texts)
            if vecs is not None:
                self._live = True
                dim = len(vecs[0])
                if self._dim is None:
                    self._dim = dim
                if dim == self._dim:                     # guard against a model that changes dim
                    return vecs
            elif self._live is None:                     # first-ever call failed -> degrade for good
                self._live = False
        return self._fallback(texts)

    def embed(self, text: str) -> Vector:
        return self.embed_many([text])[0]

    def __call__(self, text: str) -> Vector:             # so it drops in wherever `hash_embed` is used
        return self.embed(text)


def make_embedder(settings) -> Callable[[str], Vector]:
    """Return a text→vector callable from config: `hash_embed` (zero-dep default, dim 64) when no
    `embed_model` is set — byte-identical to prior behavior — else an `LLMEmbedder` over the
    configured endpoint (falling back to `embed_base_url` or the shared `llm_base_url`). Never raises:
    a misconfigured/offline endpoint degrades to `hash_embed` at call time (see `LLMEmbedder`)."""
    model = getattr(settings, "embed_model", None)
    if not model:
        return hash_embed
    base = getattr(settings, "embed_base_url", None) or getattr(settings, "llm_base_url", "") or ""
    key = getattr(settings, "llm_api_key", None)
    try:
        key = key.get_secret_value() if key is not None and hasattr(key, "get_secret_value") else (key or "x")
    except Exception:
        key = "x"
    return LLMEmbedder(model, base_url=base, api_key=key)


def cosine(a: Vector, b: Vector) -> float:
    if len(a) != len(b):
        # Mismatched dims (e.g. a store populated with hash_embed=64 then queried with a 768-dim
        # LLM embedder) would silently rank on the truncated overlap; refuse instead.
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


# Back-compat alias for pre-rename importers (orchestrator, tests).
_cosine = cosine


class InMemoryVectorStore:
    """Default offline impl. Brute-force cosine; fine at LoopLab's scale (hundreds–
    low-thousands of notes, per ADR-16's benchmark analysis)."""

    def __init__(self) -> None:
        self._idx: dict[str, dict[str, Item]] = {}

    def upsert(self, index: str, items: list[Item]) -> None:
        store = self._idx.setdefault(index, {})
        for it in items:
            store[it.id] = it

    def search(self, index: str, query: Vector, k: int) -> list[Hit]:
        store = self._idx.get(index, {})
        hits = [Hit(it.id, cosine(query, it.vector), it.payload) for it in store.values()]
        hits.sort(key=lambda h: (-h.score, h.id))
        return hits[:k]

    def get(self, index: str, id: str) -> Optional[Hit]:
        it = self._idx.get(index, {}).get(id)
        return Hit(it.id, 1.0, it.payload) if it else None

    def delete(self, index: str, ids: list[str]) -> None:
        store = self._idx.get(index, {})
        for i in ids:
            store.pop(i, None)

    def rebuild(self, index: str) -> None:
        # In-memory store has no derived state to rebuild; LanceDB re-derives from
        # canonical knowledge/*.md here.
        self._idx.setdefault(index, {})
