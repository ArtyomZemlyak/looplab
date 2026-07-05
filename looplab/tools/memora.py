"""Harmonic memory representation (idea import from microsoft/Memora, ICML'26): index a memory by a
short ABSTRACTION + a few CUE ANCHORS rather than by its raw content, CONSOLIDATE a new memory into an
existing entry under a matching abstraction instead of duplicating it, and EXPAND a search through the
anchors of its top hits to surface related-but-not-similar memories.

Opt-in and LLM-optional *by construction* — mirroring how `vectorstore.LLMEmbedder` degrades to
`hash_embed`: the abstractor degrades from a live chat model (`LLMAbstractor`) to a deterministic
lexical one (`lexical_abstraction`), so the three structural benefits work fully offline. With **no**
abstractor at all (`make_abstractor` returns None), the callers stay byte-identical to their pre-Memora
behavior — `memora` is ON by default (the abstractor is the live `LLMAbstractor` when `memora_llm` is
on and a client is wired — both default on — else the lexical fallback); set `memora=false` to restore
the pre-Memora raw-text index.

Nothing here is a source of truth: abstractions/anchors live only in *derived, rebuildable* indexes
(the in-memory `VectorStore` behind `KnowledgeTools`/`CaseLibrary`), never in the append-only event log
or the canonical `cases.jsonl`. Consolidation only ever collapses duplicates *inside* such an index.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from looplab.tools.vectorstore import Hit, Vector, VectorStore

# Deliberately a superset of memory._STOP: the abstractor sees free-form note/case text, not just task
# goals, so it strips a few more filler words. Kept local so this module has no cross-import coupling.
_STOP = {
    "the", "a", "an", "to", "of", "and", "or", "for", "on", "in", "with", "from", "predict", "using",
    "use", "data", "dataset", "model", "target", "column", "columns", "features", "given", "this",
    "that", "is", "are", "was", "were", "by", "your", "my", "it", "as", "at", "be", "we", "you", "i",
    "our", "their", "its", "so", "but", "if", "then", "than", "into", "over", "via", "per", "not",
    "no", "yes", "can", "will", "would", "should", "could", "may", "might", "do", "does", "did",
    "have", "has", "had", "get", "got", "make", "made", "when", "what", "which", "who", "how", "why",
    "best", "good", "better", "run", "runs", "node", "nodes", "case", "past", "task", "metric",
    "params", "param", "op", "operator", "reached", "used", "value", "note", "notes",
}


@dataclass
class Abstraction:
    """The scaffolding over a memory value: a `primary` abstraction (a short essence phrase) plus
    `anchors` (cue tags giving alternative retrieval paths). Only THIS is embedded/indexed — the rich
    memory value itself is stored unindexed alongside (Memora's storage/retrieval decoupling)."""

    primary: str
    anchors: list[str] = field(default_factory=list)

    def index_text(self) -> str:
        """The string that actually gets embedded: primary essence, then the anchors (repeated cue
        words carry extra weight in a bag-of-words / semantic embedding — that's intended)."""
        return " ".join([self.primary, *self.anchors]).strip()

    def merge(self, other: "Abstraction") -> "Abstraction":
        """Consolidate two abstractions of the same evolving topic: keep the richer primary and take
        the union of anchors (order-preserving dedupe), so a merged entry stays reachable by every cue
        either version had."""
        primary = self.primary if len(self.primary) >= len(other.primary) else other.primary
        seen: set[str] = set()
        anchors: list[str] = []
        for a in [*self.anchors, *other.anchors]:
            if a and a not in seen:
                seen.add(a)
                anchors.append(a)
        return Abstraction(primary=primary, anchors=anchors)


def _salient_tokens(text: str) -> list[str]:
    """Deterministic salient-token extraction: lowercased alnum tokens of length >2 that aren't
    stopwords, in first-seen order (dedup happens by the caller when it wants frequency)."""
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) > 2 and w not in _STOP]


def lexical_abstraction(text: str, *, max_anchors: int = 6, max_primary_words: int = 8) -> Abstraction:
    """The LLM-free abstractor: a deterministic, offline stand-in for a model-written abstraction.
    `primary` = the first few salient words in order (a rough essence); `anchors` = the most frequent
    salient tokens (the topic's recurring cues). Pure and reproducible — same input, same output — so
    consolidation and anchor-expansion behave identically across runs and in tests."""
    toks = _salient_tokens(text)
    # primary: first-seen salient words, order preserved, deduped, capped.
    primary_words: list[str] = []
    seen: set[str] = set()
    for t in toks:
        if t not in seen:
            seen.add(t)
            primary_words.append(t)
        if len(primary_words) >= max_primary_words:
            break
    # anchors: by frequency desc, then token asc (stable) — the topic's recurring cues.
    counts: dict[str, int] = {}
    for t in toks:
        counts[t] = counts.get(t, 0) + 1
    anchors = [t for t, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))][:max_anchors]
    return Abstraction(primary=" ".join(primary_words), anchors=sorted(anchors))


class LLMAbstractor:
    """Model-written abstraction over any `complete(prompt) -> str` callable (adapt a chat client with
    `chat_completer`). Robust like `LLMEmbedder`: any failure — dead endpoint, non-JSON reply, empty
    fields — falls back to `lexical_abstraction`, and a first-call failure sticks (`_live=False`) so a
    known-dead endpoint is never hammered per memory. So enabling LLM abstraction can only *improve*
    quality, never crash or block indexing."""

    _PROMPT = (
        "Summarize the memory below as JSON for a retrieval index. Return ONLY a JSON object:\n"
        '{"abstraction": "<6-8 word essence phrase>", "anchors": ["<cue tag>", ...]}\n'
        "Anchors are 3-6 short lowercase tags (entities, techniques, outcomes) that let a later, "
        "differently-worded query still find this. No prose, JSON only.\n\nMEMORY:\n"
    )

    def __init__(self, complete: Callable[[str], str], *, max_anchors: int = 6):
        self.complete = complete
        self.max_anchors = max_anchors
        self._live: Optional[bool] = None   # None=untried, True=works, False=degraded to lexical

    def __call__(self, text: str) -> Abstraction:
        if self._live is False:
            return lexical_abstraction(text, max_anchors=self.max_anchors)
        try:
            raw = self.complete(self._PROMPT + (text or "")[:4000])
            ab = self._parse(raw)
        except Exception:  # noqa: BLE001 — an abstractor must never crash the caller's write path
            ab = None
        if ab is None:
            if self._live is None:      # first-ever call failed -> degrade for the lifetime
                self._live = False
            return lexical_abstraction(text, max_anchors=self.max_anchors)
        self._live = True
        return ab

    def _parse(self, raw: str) -> Optional[Abstraction]:
        """Extract the JSON object from a possibly chatty reply. Returns None on anything unusable so
        the caller degrades to lexical."""
        if not raw:
            return None
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return None
        try:
            o = json.loads(m.group(0))
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(o, dict):
            return None
        primary = str(o.get("abstraction") or "").strip()
        raw_anchors = o.get("anchors")
        anchors: list[str] = []
        if isinstance(raw_anchors, list):
            seen: set[str] = set()
            for a in raw_anchors:
                t = str(a).strip().lower()
                if t and t not in seen:
                    seen.add(t)
                    anchors.append(t)
        anchors = sorted(anchors[:self.max_anchors])
        if not primary and not anchors:
            return None
        return Abstraction(primary=primary or " ".join(anchors), anchors=anchors)


class CachedAbstractor:
    """Memoize an abstractor by content hash so a re-built index (roles are reconstructed often)
    doesn't re-abstract unchanged notes/cases — the fix that makes LLM abstraction affordable as a
    default. In-memory always; also persisted to `path` (a JSON map) when given, so the cache survives
    across runs/processes. Best-effort and never raises: an unreadable/corrupt cache starts empty, an
    unwritable one just skips persistence. Namespaced by model id so switching `llm_model` doesn't
    serve stale abstractions."""

    def __init__(self, inner: Callable[[str], Abstraction], path: Optional[str] = None,
                 namespace: str = ""):
        self.inner = inner
        self.path = Path(path) if path else None
        self.namespace = namespace or ""
        self._cache: dict[str, dict] = {}
        if self.path and self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._cache = raw
            except (OSError, ValueError, json.JSONDecodeError):
                self._cache = {}

    def _key(self, text: str) -> str:
        h = hashlib.sha256()
        h.update(self.namespace.encode("utf-8"))
        h.update(b"\x00")
        h.update((text or "").encode("utf-8"))
        return h.hexdigest()

    def __call__(self, text: str) -> Abstraction:
        key = self._key(text)
        hit = self._cache.get(key)
        if isinstance(hit, dict) and ("primary" in hit or "anchors" in hit):
            return Abstraction(str(hit.get("primary", "")), list(hit.get("anchors", [])))
        ab = self.inner(text)
        self._cache[key] = {"primary": ab.primary, "anchors": list(ab.anchors)}
        self._persist()
        return ab

    def _persist(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            from looplab.core.atomicio import atomic_write_text
            atomic_write_text(self.path, json.dumps(self._cache))
        except Exception:  # noqa: BLE001 — a cache we can't persist is a perf miss, not an error
            pass


def chat_completer(client) -> Callable[[str], str]:
    """Adapt a LoopLab chat client (`.chat(messages, tools, tool_choice)`) into the plain
    `complete(prompt) -> str` callable `LLMAbstractor` wants — one user turn, no tools."""
    def _complete(prompt: str) -> str:
        msg = client.chat([{"role": "user", "content": prompt}], [], tool_choice="none")
        return (msg or {}).get("content") or ""
    return _complete


def make_abstractor(settings, complete: Optional[Callable[[str], str]] = None,
                    cache_path: Optional[str] = None) -> Optional[Callable[[str], Abstraction]]:
    """Config-driven abstractor, or None to keep callers byte-identical to pre-Memora behavior.
    Returns None unless `settings.memora` is on. When on: an `LLMAbstractor` (wrapped in a
    `CachedAbstractor` so a re-built index doesn't re-call the model on unchanged content) if
    `settings.memora_llm` is set AND a `complete` callable was supplied, else the deterministic
    `lexical_abstraction` (so the harmonic index still works with zero LLM calls). Never raises."""
    if not getattr(settings, "memora", False):
        return None
    max_anchors = int(getattr(settings, "memora_anchors", 6) or 6)
    if complete is not None and getattr(settings, "memora_llm", False):
        inner = LLMAbstractor(complete, max_anchors=max_anchors)
        return CachedAbstractor(inner, path=cache_path,
                                namespace=str(getattr(settings, "llm_model", "")))
    return lambda text: lexical_abstraction(text, max_anchors=max_anchors)


def expand_by_anchors(store: VectorStore, index: str, seed_hits: list[Hit],
                      embed: Callable[[str], Vector], *, k: int = 3,
                      exclude: Optional[set[str]] = None) -> list[Hit]:
    """The one extra retrieval hop: from the anchors carried by `seed_hits`, surface
    related-but-not-similar entries (a different primary, but a shared cue). Uses only the public
    `VectorStore.search` — embed the seeds' pooled anchors and search, then keep only entries whose
    OWN anchors actually intersect the seed anchors (precision guard: an anchor query shouldn't drag in
    a merely lexically-close note). Returns [] when the seeds carry no anchors, so a non-harmonic index
    (no abstractor) yields no expansion and the caller stays legacy."""
    seed_anchors: set[str] = set()
    seed_ids: set[str] = set(exclude or set())
    for h in seed_hits:
        seed_ids.add(h.id)
        for a in (h.payload.get("anchors") or []):
            seed_anchors.add(a)
    if not seed_anchors:
        return []
    query = embed(" ".join(sorted(seed_anchors)))
    out: list[Hit] = []
    for h in store.search(index, query, k + len(seed_ids) + k):
        if h.id in seed_ids:
            continue
        hit_anchors = set(h.payload.get("anchors") or [])
        if not (hit_anchors & seed_anchors):
            continue                       # semantically close but not anchor-linked -> skip
        out.append(h)
        if len(out) >= k:
            break
    return out
