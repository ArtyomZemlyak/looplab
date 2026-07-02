"""Pluggable vector store (I17, ADR-16). `VectorStore` is the seam; LanceDB is the
production default — here we ship a dependency-free `InMemoryVectorStore` (brute-force
cosine) as the testable default so swapping in LanceDB/Qdrant is a config change.

`hash_embed` is a deterministic (hashlib-based) bag-of-words embedder for offline
tests; production embeddings go through LiteLLM (`ollama/nomic-embed-text`).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional, Protocol

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


def _cosine(a: Vector, b: Vector) -> float:
    if len(a) != len(b):
        # Mismatched dims (e.g. a store populated with hash_embed=64 then queried with a 768-dim
        # LLM embedder) would silently rank on the truncated overlap; refuse instead.
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


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
        hits = [Hit(it.id, _cosine(query, it.vector), it.payload) for it in store.values()]
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
