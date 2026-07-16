"""Cross-run CONCEPT registry (§21.20.3 / CR1a) — stable concept identity + operator merge/purge/split.

The shipped per-run concept graph emits display SLUGS ("data/hard-negative-mining"); across runs the same
technology may be spelled differently, an operator may decide two slugs are one concept, or ONE coarse slug
really covers two distinct techniques. This module adds an identity layer WITHOUT rewriting history
(§21.20.1 "taxonomy changes do not rewrite history"):

- ONE versioned normalization contract (`CONCEPT_KEY_VERSION` / `normalize_key`) used for BOTH writes and
  reads — NFKC + casefold + whitespace-collapse — so `Hard-Neg`, `hard-neg`, and `  hard-neg  ` are one key
  (closing the CODEX gap where alias keys were only stripped while `concept_uid` casefolded).
- `concept_uid(slug, aliases=None)` — a stable opaque UID for a concept's CANONICAL identity (aliases
  resolved first), content-addressed so a display re-spelling that aliases to the same canonical keeps the
  UID; wide enough (64-bit) to be a durable identifier, not just a test fixture.
- an append-only `concept_aliases.jsonl` of operator-governed renames {from -> to}; a write that would close a
  CYCLE or self-link is REJECTED (a cycle has no canonical result, per CODEX), so the resolver always
  terminates at a real canonical slug.
- an append-only `concept_splits.jsonl` (SPLIT): one coarse concept -> several finer ones, re-tagged
  DETERMINISTICALLY at read time from each run's OWN sibling concepts/goal terms (the "needs re-tagging"
  full-CR of §21.20.13). Non-destructive: raw per-run tags are untouched; the split is a read-time rule.
- `canonicalize_concepts` maps a raw slug list to its canonical set — ALIAS the source, SPLIT the canonical
  source from sibling context, then ALIAS/PURGE the split target — applied at READ time (overview / atlas),
  so raw per-run tags stay intact for audit.

Merge/purge/split are operator actions (§22.4); explicit clear records reverse them without deleting history.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Optional

# Versioned identity contract. Bump only on a normalization change that would re-key existing concepts; a
# record carries no version today (the algorithm is the contract), but the constant pins the intent and
# lets a future migration detect a mode change (CODEX: "one versioned Unicode normalization/key contract").
CONCEPT_KEY_VERSION = 1

_TOMBSTONE = "\x00purged"   # canonical target that marks a concept purged (dropped from cross-run views)
_WORD = re.compile(r"[^\W_]+", re.UNICODE)   # unicode word tokens (Cyrillic-safe), for split-rule matching
_MAX_CONCEPT = 500
_MAX_ACTOR = 120
_MAX_AT = 120
_MAX_SPLIT_RULES = 64
_MAX_SPLIT_TERMS = 32
_MAX_SPLIT_TERM = 200
_MAX_ACTION_ID = 160


class ConceptGovernanceConflict(ValueError):
    """Optimistic-concurrency failure for an alias/split ledger mutation."""

    def __init__(self, path: Path, expected: int, actual: int):
        self.path = path
        self.expected = expected
        self.actual = actual
        super().__init__(f"stale governance revision for {path.name}: expected {expected}, current {actual}")


class ConceptGovernanceIdempotencyConflict(ValueError):
    """An action id was already committed with a different semantic payload in this ledger."""

    def __init__(self, path: Path, action_id: str):
        self.path = path
        self.action_id = action_id
        super().__init__(f"action_id {action_id!r} already exists with a different payload in {path.name}")


def normalize_key(s: str) -> str:
    """The ONE canonical key normalization used for every write and read (CODEX): NFKC (fold compatibility
    forms), casefold (case-insensitive incl. non-ASCII), strip, and collapse internal whitespace runs. A
    slug that differs only by case/spacing/compat-form maps to a single key. Preserves '/', '-' (the slug
    structure); the display label is whatever the caller stored — this is identity, not presentation."""
    t = unicodedata.normalize("NFKC", str(s or "")).casefold().strip()
    # Strip C0/C1 control chars (except the \t\n\r that the \s+ collapse handles) so NO untrusted slug can
    # normalize to a string containing the internal tombstone sentinel '\x00purged' and turn a MERGE into a
    # covert PURGE (mega-review finding). The sentinel is only ever assigned internally, never via this path.
    t = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", t)
    return re.sub(r"\s+", " ", t)


def _bounded_key(value, field: str, maximum: int = _MAX_CONCEPT, *, required: bool = False) -> str:
    """Normalize an identity-bearing field and reject oversize input instead of truncating it."""
    out = normalize_key(value)
    if required and not out:
        raise ValueError(f"empty {field}")
    if len(out) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return out


def _bounded_text(value, field: str, maximum: int) -> str:
    out = str(value or "")
    if len(out) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return out


def _validate_expected_revision(value: Optional[int]) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise ValueError("expected_revision must be a non-negative integer")


def _validated_action_id(value: str) -> str:
    action_id = _bounded_text(value, "action_id", _MAX_ACTION_ID).strip()
    if value and not action_id:
        raise ValueError("action_id must not be blank")
    return action_id


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
                         at: str = "", expected_revision: Optional[int] = None,
                         action_id: str = "") -> dict:
    """Operator MERGE (§22.4): declare `from_concept` is really `to_concept` (append-only, reversible by a
    later alias). Pass `to_concept=""` to PURGE/tombstone `from_concept` (dropped from cross-run views).
    Rejects an empty source, a self-link, or an edge that would close a cycle (CODEX: a cycle has no
    canonical result), and a missing dir — all real operator errors. The stored keys are normalized under
    the ONE versioned contract, so writes and reads agree."""
    src = _bounded_key(from_concept, "from_concept", required=True)
    dst = _bounded_key(to_concept, "to_concept")
    _validate_expected_revision(expected_revision)
    action_id = _validated_action_id(action_id)
    if not memory_dir:
        raise ValueError("no memory_dir")
    if dst and src == dst:
        raise ValueError("self-link: from_concept == to_concept")
    rec = {"action": "set" if dst else "purge", "from": src, "to": dst,
           "by": _bounded_text(by or "operator", "by", _MAX_ACTOR),
           "at": _bounded_text(at, "at", _MAX_AT), "v": CONCEPT_KEY_VERSION}
    if action_id:
        rec["action_id"] = action_id
    path = Path(memory_dir) / "concept_aliases.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)

    # Validate while holding the append lock.  A pre-lock check has a TOCTOU race: concurrent writers can
    # both observe an acyclic ledger and append a->b / b->a.
    def _validate_locked() -> None:
        if dst and _would_cycle(src, dst, load_concept_aliases(memory_dir)):
            raise ValueError(f"alias {src!r} -> {dst!r} would close a cycle")

    return _append_governance(path, rec, validate=_validate_locked, expected_revision=expected_revision)


def clear_concept_alias(memory_dir, *, from_concept: str, by: str = "operator", at: str = "",
                        expected_revision: Optional[int] = None, action_id: str = "") -> dict:
    """Undo the current MERGE/PURGE policy for one source without deleting history.

    A clear is an explicit append-only tombstone for the *policy edge*, distinct from a purge (whose empty
    target tombstones the concept itself). Replay removes the source from the alias map, exposing its raw
    normalized identity again.
    """
    src = _bounded_key(from_concept, "from_concept", required=True)
    _validate_expected_revision(expected_revision)
    action_id = _validated_action_id(action_id)
    if not memory_dir:
        raise ValueError("no memory_dir")
    rec = {"action": "clear", "from": src,
           "by": _bounded_text(by or "operator", "by", _MAX_ACTOR),
           "at": _bounded_text(at, "at", _MAX_AT),
           "v": CONCEPT_KEY_VERSION}
    if action_id:
        rec["action_id"] = action_id
    path = Path(memory_dir) / "concept_aliases.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return _append_governance(path, rec, expected_revision=expected_revision)


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
        if r.get("v", CONCEPT_KEY_VERSION) != CONCEPT_KEY_VERSION:
            continue
        src = normalize_key(r.get("from"))
        action = str(r.get("action") or "legacy")
        if not src:
            continue
        if len(src) > _MAX_CONCEPT:
            continue
        if action == "clear":
            out.pop(src, None)
        elif action == "purge":
            out[src] = _TOMBSTONE
        elif action == "set":
            dst = normalize_key(r.get("to"))
            if dst and len(dst) <= _MAX_CONCEPT:
                out[src] = dst
        elif action == "legacy":
            dst = normalize_key(r.get("to"))
            if len(dst) <= _MAX_CONCEPT:
                out[src] = _TOMBSTONE if not dst else dst
    return out


def resolve_slug(slug: str, aliases: dict) -> Optional[str]:
    """Follow the alias chain to the canonical slug (cycle-safe even on legacy/torn ledgers). Returns None
    for a PURGED concept so callers drop it. A slug with no alias resolves to itself (normalized)."""
    cur = normalize_key(slug)
    order: list[str] = []
    positions: dict[str, int] = {}
    while cur in aliases:
        if cur in positions:
            # A legacy/torn ledger can contain a cycle despite current write-time rejection. Choose from
            # the actual cycle only: an acyclic prefix must not create an entry-dependent identity.
            return min(order[positions[cur]:])
        positions[cur] = len(order)
        order.append(cur)
        nxt = aliases[cur]
        if nxt == _TOMBSTONE:
            return None            # purged
        cur = normalize_key(nxt)
    return cur or None


# --------------------------------------------------------------------------- #
# Splits (operator SPLIT with deterministic re-tagging, §21.20.13) — the "needs re-tagging" full-CR.
# One coarse concept -> several finer ones, chosen per run from that run's OWN sibling concepts/goal terms.
# --------------------------------------------------------------------------- #

def record_concept_split(memory_dir, *, from_concept: str, rules, default: str = "", by: str = "operator",
                         at: str = "", expected_revision: Optional[int] = None,
                         action_id: str = "") -> dict:
    """Operator SPLIT (§22.4): declare `from_concept` is too coarse and must be RE-TAGGED into finer
    concepts. `rules` is an ordered list of `{"to": slug, "when_any": [term, ...]}`: for a given run, the
    FIRST rule whose `when_any` terms appear among that run's sibling concept tokens wins; otherwise
    `default` (or the original slug if no default). The re-tagging is a pure read-time rule over each run's
    OWN context, so history is never rewritten (§21.20.1). Append-only, reversible by a later split.

    Rejects an empty source, a rule targeting the source (no progress / would re-split forever), an empty
    ruleset with no default, and a missing dir — real operator errors."""
    src = _bounded_key(from_concept, "from_concept", required=True)
    _validate_expected_revision(expected_revision)
    action_id = _validated_action_id(action_id)
    if not memory_dir:
        raise ValueError("no memory_dir")
    if not isinstance(rules, (list, tuple)):
        raise ValueError("rules must be a list")
    if len(rules) > _MAX_SPLIT_RULES:
        raise ValueError(f"rules exceeds {_MAX_SPLIT_RULES} items")
    norm_rules = []
    for r in rules:
        if not isinstance(r, dict):
            raise ValueError("each split rule must be an object")
        to = _bounded_key(r.get("to"), "rule.to")
        raw_terms = r.get("when_any") or []
        if not isinstance(raw_terms, (list, tuple)):
            raise ValueError("rule.when_any must be a list")
        if len(raw_terms) > _MAX_SPLIT_TERMS:
            raise ValueError(f"rule.when_any exceeds {_MAX_SPLIT_TERMS} items")
        terms = [_bounded_key(t, "rule.when_any item", _MAX_SPLIT_TERM) for t in raw_terms]
        terms = [t for t in terms if t]
        if not to or not terms:
            continue                   # a rule with no target or no trigger term is inert -> drop
        if to == src:
            raise ValueError(f"split rule targets its own source {src!r} (no progress)")
        norm_rules.append({"to": to, "when_any": sorted(set(terms))})
    # `default == src` is allowed: it is the natural "keep the original slug when no rule matches" fallback
    # (resolve_split is single-pass, so there is no re-split loop). An EMPTY default already means "keep it".
    dflt = _bounded_key(default, "default")
    if not norm_rules and (not dflt or dflt == src):
        raise ValueError("split needs at least one rule (a bare identity default is inert)")
    rec = {"action": "set", "from": src, "rules": norm_rules, "default": dflt,
           "by": _bounded_text(by or "operator", "by", _MAX_ACTOR),
           "at": _bounded_text(at, "at", _MAX_AT), "v": CONCEPT_KEY_VERSION}
    if action_id:
        rec["action_id"] = action_id
    path = Path(memory_dir) / "concept_splits.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return _append_governance(path, rec, expected_revision=expected_revision)


def clear_concept_split(memory_dir, *, from_concept: str, by: str = "operator", at: str = "",
                        expected_revision: Optional[int] = None, action_id: str = "") -> dict:
    """Undo the active split rule for one source through an append-only clear record."""
    src = _bounded_key(from_concept, "from_concept", required=True)
    _validate_expected_revision(expected_revision)
    action_id = _validated_action_id(action_id)
    if not memory_dir:
        raise ValueError("no memory_dir")
    rec = {"action": "clear", "from": src,
           "by": _bounded_text(by or "operator", "by", _MAX_ACTOR),
           "at": _bounded_text(at, "at", _MAX_AT),
           "v": CONCEPT_KEY_VERSION}
    if action_id:
        rec["action_id"] = action_id
    path = Path(memory_dir) / "concept_splits.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return _append_governance(path, rec, expected_revision=expected_revision)


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
        if r.get("v", CONCEPT_KEY_VERSION) != CONCEPT_KEY_VERSION:
            continue
        src = normalize_key(r.get("from"))
        if not src:
            continue
        action = str(r.get("action") or "legacy")
        if action == "clear":
            out.pop(src, None)
            continue
        if action not in {"legacy", "set"}:
            continue
        raw_rules = r.get("rules")
        if len(src) > _MAX_CONCEPT or not isinstance(raw_rules, list) or len(raw_rules) > _MAX_SPLIT_RULES:
            continue
        rules = []
        for raw_rule in raw_rules:
            if not isinstance(raw_rule, dict):
                continue
            target, raw_terms = normalize_key(raw_rule.get("to")), raw_rule.get("when_any")
            if (not target or len(target) > _MAX_CONCEPT or not isinstance(raw_terms, list)
                    or len(raw_terms) > _MAX_SPLIT_TERMS):
                continue
            terms = [normalize_key(term) for term in raw_terms]
            if any(len(term) > _MAX_SPLIT_TERM for term in terms):
                continue
            terms = [term for term in terms if term]
            if terms:
                rules.append({"to": target, "when_any": terms})
        default = normalize_key(r.get("default"))
        if len(default) > _MAX_CONCEPT:
            continue
        if rules or (default and default != src):
            out[src] = {"rules": rules, "default": default}
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
    # `when_any` entries are semantic trigger PHRASES, while `_ctx_tokens` deliberately splits slugs on
    # punctuation. Match a phrase when all of its Unicode tokens are present, so `hard-negative` and
    # `hard negative` both match sibling `loss/hard-negative` instead of becoming inert rules.
    ctx = _ctx_tokens(context_terms)
    for rule in spec.get("rules", []):
        triggers = [_ctx_tokens([t]) for t in (rule.get("when_any") or [])]
        if rule.get("to") and any(tokens and tokens <= ctx for tokens in triggers):
            return rule["to"]
    return spec.get("default") or s


def canonicalize_concept(slug: str, *, sibling_concepts=(), aliases: Optional[dict] = None,
                         splits: Optional[dict] = None) -> Optional[str]:
    """Canonicalize one raw slug with the governance order used by every portfolio consumer.

    Source aliases must resolve *before* split lookup (otherwise an alias pointing at a coarse canonical
    concept bypasses its split). The split target is resolved again so target aliases and purges apply. Split
    context contains canonicalized siblings only and never the source itself.
    """
    source = resolve_slug(slug, aliases) if aliases else normalize_key(slug)
    if not source:
        return None
    sibling_sources = []
    for sibling in sibling_concepts or []:
        resolved = resolve_slug(sibling, aliases) if aliases else normalize_key(sibling)
        # Exclude by canonical identity, not raw list position. A duplicate or alias of the
        # source is still the source and must not satisfy its own split trigger.
        if resolved and resolved != source:
            sibling_sources.append(resolved)
    split_target = resolve_split(source, _ctx_tokens(sibling_sources), splits) if splits else source
    return resolve_slug(split_target, aliases) if aliases else (normalize_key(split_target) or None)


def canonicalize_concepts(concepts, aliases: Optional[dict] = None,
                          splits: Optional[dict] = None) -> list[str]:
    """Map raw slugs to a sorted canonical set: ALIAS-SOURCE -> SPLIT -> ALIAS/PURGE-TARGET.

    No rules ({}/None) still applies the versioned normalization contract. Non-destructive: the input list
    is never mutated.
    """
    raw = list(concepts or [])
    out: set = set()
    for i, c in enumerate(raw):
        canonical = canonicalize_concept(c, sibling_concepts=raw[:i] + raw[i + 1:],
                                         aliases=aliases, splits=splits)
        if canonical:
            out.add(canonical)
    return sorted(out)


# --------------------------------------------------------------------------- #
# Durable governance append — a locked, fsynced append shared by alias/split writes (CODEX: these policy
# ledgers had no interprocess lock/fsync). `load_*` still applies last-write-wins, but the physical line is
# now written atomically under an exclusive lock so concurrent UI/CLI writers cannot interleave a line.
# --------------------------------------------------------------------------- #

def _ledger_revision(path: Path) -> int:
    """Current append revision, including legacy records that pre-date explicit revision fields."""
    from looplab.events.eventstore import read_jsonl_lenient

    rows = read_jsonl_lenient(path, loads=json.loads, dicts_only=True) if path.exists() else []
    explicit = [r.get("revision") for r in rows
                if isinstance(r.get("revision"), int) and not isinstance(r.get("revision"), bool)]
    return max([len(rows), *explicit], default=0)


def concept_governance_revision(memory_dir, kind: str) -> int:
    """Return the current per-ledger revision for `aliases` or `splits` (0 when absent)."""
    if kind not in {"aliases", "splits"}:
        raise ValueError("kind must be 'aliases' or 'splits'")
    if not memory_dir:
        return 0
    name = "concept_aliases.jsonl" if kind == "aliases" else "concept_splits.jsonl"
    return _ledger_revision(Path(memory_dir) / name)


def _idempotency_payload(rec: dict) -> str:
    """Canonical semantic payload; actor/timestamp/revision are receipt metadata, not mutation identity."""
    semantic = {k: rec.get(k) for k in ("v", "action", "from", "to", "rules", "default", "by") if k in rec}
    return json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _append_governance(path: Path, rec: dict, *, validate: Optional[Callable[[], None]] = None,
                       expected_revision: Optional[int] = None) -> dict:
    """Append one governance record under a required cross-platform interprocess lock.

    Lenient JSONL replay can tolerate a torn tail, but it cannot recover an interleaved or lost policy
    decision.  Governance therefore fails closed when the locking guarantee is unavailable.  `validate`,
    when supplied, runs in the same critical section as the append.
    """
    from looplab.core.atomicio import best_effort_fsync
    from looplab.events.eventstore import _interprocess_lock

    with _interprocess_lock(Path(str(path) + ".lock"), required=True):
        action_id = str(rec.get("action_id") or "")
        if action_id and path.exists():
            from looplab.events.eventstore import read_jsonl_lenient
            for existing in read_jsonl_lenient(path, loads=json.loads, dicts_only=True):
                if str(existing.get("action_id") or "") != action_id:
                    continue
                # Resolve idempotency before CAS/validation. A transport retry carrying the original stale
                # revision must return its first durable receipt, never append again or fail with a conflict.
                if _idempotency_payload(existing) == _idempotency_payload(rec):
                    return dict(existing)
                raise ConceptGovernanceIdempotencyConflict(path, action_id)
        current = _ledger_revision(path)
        _validate_expected_revision(expected_revision)
        if expected_revision is not None and expected_revision != current:
            raise ConceptGovernanceConflict(path, expected_revision, current)
        if validate is not None:
            validate()
        # Allocate the CAS revision inside the same required lock as validation and append.
        rec["revision"] = current + 1
        line = json.dumps(rec) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            best_effort_fsync(f.fileno())
    return rec
