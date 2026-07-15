"""Cross-run CONCEPT registry (§21.20.3 / CR1a) — stable concept identity + operator merge/purge/split.

The shipped per-run concept graph emits display SLUGS ("data/hard-negative-mining"); across runs the same
technology may be spelled differently, an operator may decide two slugs are one concept, or ONE coarse slug
really covers two distinct techniques. This module adds an identity layer WITHOUT rewriting history
(§21.20.1 "taxonomy changes do not rewrite history"):

- ONE versioned normalization contract (`CONCEPT_KEY_VERSION` / `normalize_key`) used for BOTH writes and
  reads — NFKC + casefold + whitespace-collapse — so `Hard-Neg`, `hard-neg`, and ` hard  neg ` are one key
  (closing the CODEX gap where alias keys were only stripped while `concept_uid` casefolded).
- `concept_uid(slug, aliases=None)` — a stable opaque UID for a concept's CANONICAL identity (aliases
  resolved first), content-addressed so a display re-spelling that aliases to the same canonical keeps the
  UID; wide enough (64-bit) to be a durable identifier, not just a test fixture.
- an append-only `concept_aliases.jsonl` of operator/auto renames {from -> to}; a write that would close a
  CYCLE or self-link is REJECTED (a cycle has no canonical result, per CODEX), so the resolver always
  terminates at a real canonical slug.
- an append-only `concept_splits.jsonl` (SPLIT): one coarse concept -> several finer ones, re-tagged
  DETERMINISTICALLY at read time from each run's OWN sibling concepts/goal terms (the "needs re-tagging"
  full-CR of §21.20.13). Non-destructive: raw per-run tags are untouched; the split is a read-time rule.
- `canonicalize_concepts` maps a raw slug list to its canonical set — SPLIT (sibling-context) then ALIAS
  then PURGE — applied at READ time (overview / atlas), so raw per-run tags stay intact for audit.

Merge/purge/split are operator actions (§22.4); all are reversible by a later record (append-only ledger).
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Optional

# Versioned identity contract. Bump only on a normalization change that would re-key existing concepts; a
# record carries no version today (the algorithm is the contract), but the constant pins the intent and
# lets a future migration detect a mode change (CODEX: "one versioned Unicode normalization/key contract").
CONCEPT_KEY_VERSION = 1

_TOMBSTONE = "\x00purged"   # canonical target that marks a concept purged (dropped from cross-run views)
_WORD = re.compile(r"[^\W_]+", re.UNICODE)   # unicode word tokens (Cyrillic-safe), for split-rule matching


def normalize_key(s: str) -> str:
    """The ONE canonical key normalization used for every write and read (CODEX): NFKC (fold compatibility
    forms), casefold (case-insensitive incl. non-ASCII), strip, and collapse internal whitespace runs. A
    slug that differs only by case/spacing/compat-form maps to a single key. Preserves '/', '-' (the slug
    structure); the display label is whatever the caller stored — this is identity, not presentation."""
    t = unicodedata.normalize("NFKC", str(s or "")).casefold().strip()
    return re.sub(r"\s+", " ", t)


# Back-compat alias: earlier code called the (strip-only) helper `_norm`. It is now the versioned contract.
_norm = normalize_key


def concept_uid(slug: str, aliases: Optional[dict] = None) -> str:
    """A stable opaque UID for a concept's CANONICAL identity. Aliases are resolved FIRST (so a re-spelling
    merged onto the canonical concept shares the UID), then the canonical slug is content-addressed under the
    versioned key contract. 64-bit hex — wide enough to be a durable identifier. Returns "" for a purged
    concept (no stable identity). NOT the display slug."""
    canon = resolve_slug(slug, aliases) if aliases else normalize_key(slug)
    if not canon:                      # purged / empty -> no identity
        return ""
    return "c_" + hashlib.sha1(f"{CONCEPT_KEY_VERSION}\x1f{canon}".encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Aliases (operator MERGE / PURGE, §22.4) — append-only, cycle-rejecting.
# --------------------------------------------------------------------------- #

def _would_cycle(src: str, dst: str, aliases: dict) -> bool:
    """True if adding src -> dst closes a cycle given existing aliases (dst already resolves back to src)."""
    cur, seen = dst, {src}
    while cur in aliases and cur not in seen:
        seen.add(cur)
        nxt = aliases[cur]
        if nxt == _TOMBSTONE:
            return False               # a purge chain terminates, never a cycle
        cur = nxt
    return cur == src


def record_concept_alias(memory_dir, *, from_concept: str, to_concept: str, by: str = "operator",
                         at: str = "") -> dict:
    """Operator MERGE (§22.4): declare `from_concept` is really `to_concept` (append-only, reversible by a
    later alias). Pass `to_concept=""` to PURGE/tombstone `from_concept` (dropped from cross-run views).
    Rejects an empty source, a self-link, or an edge that would close a cycle (CODEX: a cycle has no
    canonical result), and a missing dir — all real operator errors. The stored keys are normalized under
    the ONE versioned contract, so writes and reads agree."""
    src = normalize_key(from_concept)
    dst = normalize_key(to_concept)
    if not src:
        raise ValueError("empty from_concept")
    if not memory_dir:
        raise ValueError("no memory_dir")
    if dst and src == dst:
        raise ValueError("self-link: from_concept == to_concept")
    if dst and _would_cycle(src, dst, load_concept_aliases(memory_dir)):
        raise ValueError(f"alias {src!r} -> {dst!r} would close a cycle")
    rec = {"from": src, "to": dst, "by": str(by or "operator"), "at": str(at or ""), "v": CONCEPT_KEY_VERSION}
    path = Path(memory_dir) / "concept_aliases.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    _append_governance(path, rec)      # locked, fsynced append (see helper)
    return rec


def load_concept_aliases(memory_dir) -> dict:
    """`{from_key -> to_key}` from `concept_aliases.jsonl` (last write per source wins). A `to` of "" is
    kept as the tombstone marker so `resolve_slug` can drop purged concepts. {} when none/unreadable."""
    from looplab.events.eventstore import read_jsonl_lenient
    if not memory_dir:
        return {}
    path = Path(memory_dir) / "concept_aliases.jsonl"
    if not path.exists():
        return {}
    out: dict = {}
    for r in read_jsonl_lenient(path, loads=json.loads, dicts_only=True):
        src = normalize_key(r.get("from"))
        if src:
            out[src] = _TOMBSTONE if normalize_key(r.get("to")) == "" else normalize_key(r.get("to"))
    return out


def resolve_slug(slug: str, aliases: dict) -> Optional[str]:
    """Follow the alias chain to the canonical slug (cycle-safe even on legacy/torn ledgers). Returns None
    for a PURGED concept so callers drop it. A slug with no alias resolves to itself (normalized)."""
    cur = normalize_key(slug)
    seen: set[str] = set()
    while cur in aliases and cur not in seen:
        seen.add(cur)
        nxt = aliases[cur]
        if nxt == _TOMBSTONE:
            return None            # purged
        cur = nxt
    return cur or None


# --------------------------------------------------------------------------- #
# Splits (operator SPLIT with deterministic re-tagging, §21.20.13) — the "needs re-tagging" full-CR.
# One coarse concept -> several finer ones, chosen per run from that run's OWN sibling concepts/goal terms.
# --------------------------------------------------------------------------- #

def record_concept_split(memory_dir, *, from_concept: str, rules, default: str = "", by: str = "operator",
                         at: str = "") -> dict:
    """Operator SPLIT (§22.4): declare `from_concept` is too coarse and must be RE-TAGGED into finer
    concepts. `rules` is an ordered list of `{"to": slug, "when_any": [term, ...]}`: for a given run, the
    FIRST rule whose `when_any` terms appear among that run's sibling concept tokens wins; otherwise
    `default` (or the original slug if no default). The re-tagging is a pure read-time rule over each run's
    OWN context, so history is never rewritten (§21.20.1). Append-only, reversible by a later split.

    Rejects an empty source, a rule targeting the source (no progress / would re-split forever), an empty
    ruleset with no default, and a missing dir — real operator errors."""
    src = normalize_key(from_concept)
    if not src:
        raise ValueError("empty from_concept")
    if not memory_dir:
        raise ValueError("no memory_dir")
    norm_rules = []
    for r in (rules or []):
        to = normalize_key((r or {}).get("to"))
        terms = [normalize_key(t) for t in ((r or {}).get("when_any") or []) if normalize_key(t)]
        if not to or not terms:
            continue                   # a rule with no target or no trigger term is inert -> drop
        if to == src:
            raise ValueError(f"split rule targets its own source {src!r} (no progress)")
        norm_rules.append({"to": to, "when_any": sorted(set(terms))})
    # `default == src` is allowed: it is the natural "keep the original slug when no rule matches" fallback
    # (resolve_split is single-pass, so there is no re-split loop). An EMPTY default already means "keep it".
    dflt = normalize_key(default)
    if not norm_rules and (not dflt or dflt == src):
        raise ValueError("split needs at least one rule (a bare identity default is inert)")
    rec = {"from": src, "rules": norm_rules, "default": dflt, "by": str(by or "operator"),
           "at": str(at or ""), "v": CONCEPT_KEY_VERSION}
    path = Path(memory_dir) / "concept_splits.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    _append_governance(path, rec)
    return rec


def load_concept_splits(memory_dir) -> dict:
    """`{from_key -> {"rules": [...], "default": key}}` from `concept_splits.jsonl` (last write per source
    wins). {} when none/unreadable."""
    from looplab.events.eventstore import read_jsonl_lenient
    if not memory_dir:
        return {}
    path = Path(memory_dir) / "concept_splits.jsonl"
    if not path.exists():
        return {}
    out: dict = {}
    for r in read_jsonl_lenient(path, loads=json.loads, dicts_only=True):
        src = normalize_key(r.get("from"))
        if not src:
            continue
        rules = [{"to": normalize_key(x.get("to")), "when_any": [normalize_key(t) for t in (x.get("when_any") or [])]}
                 for x in (r.get("rules") or []) if isinstance(x, dict) and normalize_key(x.get("to"))]
        out[src] = {"rules": rules, "default": normalize_key(r.get("default"))}
    return out


def _ctx_tokens(concepts) -> set:
    """The unicode word tokens across a run's concept slugs — the CONTEXT a split rule matches against.
    e.g. ["data/hard-negative-mining", "loss/mnr"] -> {data, hard, negative, mining, loss, mnr}."""
    ctx: set = set()
    for c in concepts or []:
        ctx.update(_WORD.findall(normalize_key(c)))
    return ctx


def resolve_split(slug: str, context_terms, splits: Optional[dict]) -> str:
    """Re-tag `slug` per the split rules given a run's `context_terms` (from `_ctx_tokens`). First matching
    rule wins; else the split's default; else the slug unchanged. No-op when `splits` is empty."""
    s = normalize_key(slug)
    spec = (splits or {}).get(s)
    if not spec:
        return s
    ctx = set(context_terms or [])
    for rule in spec.get("rules", []):
        if rule.get("to") and any(t in ctx for t in (rule.get("when_any") or [])):
            return rule["to"]
    return spec.get("default") or s


def canonicalize_concepts(concepts, aliases: Optional[dict] = None,
                          splits: Optional[dict] = None) -> list[str]:
    """Map a raw slug list to its canonical set, sorted. Order: SPLIT (using the slug list itself as the
    sibling context) -> ALIAS -> PURGE. No rules ({}/None) returns the sorted deduped normalized slugs, so
    this is safe to always call at read time. Non-destructive: the input list is never mutated."""
    if not aliases and not splits:
        return sorted({normalize_key(c) for c in (concepts or []) if normalize_key(c)})
    ctx = _ctx_tokens(concepts) if splits else set()
    out: set = set()
    for c in concepts or []:
        s = resolve_split(c, ctx, splits) if splits else normalize_key(c)
        r = resolve_slug(s, aliases) if aliases else (s or None)
        if r:
            out.add(r)
    return sorted(out)


# --------------------------------------------------------------------------- #
# Durable governance append — a locked, fsynced append shared by alias/split writes (CODEX: these policy
# ledgers had no interprocess lock/fsync). `load_*` still applies last-write-wins, but the physical line is
# now written atomically under an exclusive lock so concurrent UI/CLI writers cannot interleave a line.
# --------------------------------------------------------------------------- #

def _append_governance(path: Path, rec: dict) -> None:
    """Append one JSON record under an exclusive advisory lock, then fsync. Falls back to a plain append if
    fcntl is unavailable (non-POSIX) — the lock is best-effort exclusion, not a correctness dependency of
    the read model (which tolerates torn/duplicate lines via `read_jsonl_lenient`)."""
    line = json.dumps(rec) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        try:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        except Exception:  # noqa: BLE001 — no fcntl (e.g. non-POSIX): best-effort append, lenient reader copes
            fcntl = None   # type: ignore
        try:
            f.write(line)
            f.flush()
            import os
            os.fsync(f.fileno())
        finally:
            if fcntl is not None:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except Exception:  # noqa: BLE001
                    pass
