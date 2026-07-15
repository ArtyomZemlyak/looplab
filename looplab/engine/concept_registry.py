"""Cross-run CONCEPT registry (§21.20.3 / CR1a, lean): stable concept identity + operator merge/purge.

The shipped per-run concept graph emits display SLUGS ("data/hard-negative-mining"); across runs the same
technology may be spelled differently or an operator may decide two slugs are one concept. This module adds
a lean identity layer WITHOUT rewriting history (§21.20.1 "taxonomy changes do not rewrite history"):

- `concept_uid(slug)` — a deterministic, stable opaque UID for a canonical slug (never the display slug).
- an append-only `concept_aliases.jsonl` of operator/auto renames {from -> to}; `load_concept_aliases`
  reads them (last write wins) and `resolve_slug` follows the chain (cycle-safe) to the canonical slug.
- `canonicalize_concepts` maps a raw slug list to its canonical set — applied at READ time (overview /
  atlas), so the raw per-run tags stay intact for audit while cross-run views merge aliases.
- `record_concept_alias` (merge) and a `purge`/tombstone alias-to-"" are the operator writes (§22.4).

Merge/purge are operator actions; a `split` (one concept -> two) needs bounded re-tagging and is deferred.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

_TOMBSTONE = "\x00purged"   # canonical target that marks a concept purged (dropped from cross-run views)


def _norm(s: str) -> str:
    return str(s or "").strip()


def concept_uid(slug: str) -> str:
    """A stable opaque UID for a (canonical) concept slug — content-addressed so it never depends on
    insertion order or a counter. Deterministic across runs/machines; NOT the display slug."""
    return "c_" + hashlib.sha1(_norm(slug).casefold().encode("utf-8")).hexdigest()[:10]


def record_concept_alias(memory_dir, *, from_concept: str, to_concept: str, by: str = "operator",
                         at: str = "") -> dict:
    """Operator MERGE (§22.4): declare `from_concept` is really `to_concept` (append-only, reversible by a
    later alias). Pass `to_concept=""` to PURGE/tombstone `from_concept` (dropped from cross-run views).
    Raises on an empty source or missing dir (a real operator error)."""
    src = _norm(from_concept)
    if not src:
        raise ValueError("empty from_concept")
    if not memory_dir:
        raise ValueError("no memory_dir")
    rec = {"from": src, "to": _norm(to_concept), "by": str(by or "operator"), "at": str(at or "")}
    path = Path(memory_dir) / "concept_aliases.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    from looplab.events.eventstore import _interprocess_lock
    # Same interprocess lock the other operator-write sidecars (claim_decisions) use, so a UI merge racing the
    # `concept-merge` CLI on one memory_dir can't interleave/tear this append or silently lose an alias (CODEX).
    with _interprocess_lock(Path(str(path) + ".lock")):
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    return rec


def load_concept_aliases(memory_dir) -> dict:
    """`{from_slug -> to_slug}` from `concept_aliases.jsonl` (last write per source wins). A `to` of "" is
    kept as the tombstone marker so `resolve_slug` can drop purged concepts. {} when none/unreadable."""
    from looplab.events.eventstore import read_jsonl_lenient
    if not memory_dir:
        return {}
    path = Path(memory_dir) / "concept_aliases.jsonl"
    if not path.exists():
        return {}
    out: dict = {}
    for r in read_jsonl_lenient(path, loads=json.loads, dicts_only=True):
        src = _norm(r.get("from"))
        if src:
            out[src] = _TOMBSTONE if _norm(r.get("to")) == "" else _norm(r.get("to"))
    return out


def resolve_slug(slug: str, aliases: dict) -> Optional[str]:
    """Follow the alias chain to the canonical slug (cycle-safe). Returns None for a PURGED concept so
    callers drop it. A slug with no alias resolves to itself."""
    cur = _norm(slug)
    seen: set[str] = set()
    while cur in aliases and cur not in seen:
        seen.add(cur)
        nxt = aliases[cur]
        if nxt == _TOMBSTONE:
            return None            # purged
        cur = nxt
    return cur or None


def canonicalize_concepts(concepts, aliases: Optional[dict] = None) -> list[str]:
    """Map a raw slug list to its canonical set (aliases merged, purged dropped), sorted. No-op ({}/None)
    returns the sorted deduped raw slugs, so this is safe to always call at read time."""
    if not aliases:
        return sorted({_norm(c) for c in (concepts or []) if _norm(c)})
    out = set()
    for c in concepts or []:
        r = resolve_slug(c, aliases)
        if r:
            out.add(r)
    return sorted(out)
