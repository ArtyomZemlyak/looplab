"""PART IV cross-run Step 4 (§21.20) — evidence-grounded CLAIM assessments.

A pure read-model that projects the ALREADY-SHIPPED memory into verifiable claims: a distilled lesson
already carries `{statement, outcome, evidence:[node_ids], run_id, task_id}` (a verdict + its grounding
nodes), and a D8 deep-research memo carries `claims:[{statement, node_ids, urls}]`. This module UNIFIES
those two shapes (it does not fork a third): it groups by normalized statement and records support vs
oppose evidence refs plus an epistemic state, so the loop/UI can ask "what does the accumulated evidence
suggest, and what contradicts it?" — the §21.20.5 claim idea in lean form.

Deliberately pure/deterministic and off any live path: no new store, no LLM, no I/O. The verdict→stance
mapping reuses the shipped lesson vocabulary (`memory._NEGATIVE` / "supported"); a "noted"/unknown verdict
is neutral (it takes no stance), exactly as on the lesson read/write paths.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Callable
from typing import Optional

from looplab.engine.memory import _CLAIM_STANCES, _NEGATIVE, normalize_statement


def _node_ids(raw) -> list:
    """Evidence node-id refs from a lesson's `evidence` or a claim's `node_ids`: ints kept as ints,
    numeric strings coerced, everything else dropped (a URL/source belongs in `sources`, not evidence)."""
    if isinstance(raw, bool) or raw is None:
        return []
    if isinstance(raw, int):
        raw = [raw]
    elif not isinstance(raw, (list, tuple)):
        return []
    out = []
    for x in raw:
        if isinstance(x, bool):
            continue
        if isinstance(x, int):
            out.append(x)
        elif isinstance(x, str) and x.strip().lstrip("-").isdigit():
            out.append(int(x))
    return out


def _qualify_refs(run_id, node_ids) -> list[str]:
    """Run-QUALIFY evidence refs so (r1,node0) and (r2,node0) never collapse: a bare node id is run-local.
    "?" marks a ref whose run is unknown (e.g. a D8 claim without a run_id)."""
    r = str(run_id or "?")
    return [f"{r}:{n}" for n in node_ids]


_RESEARCH_VERDICTS = frozenset(("supported", "unsupported", "unclear", "cited", "unverified"))


def _lesson_claim_stance(row: dict) -> str:
    """Map lesson evidence to the literal claim while preserving legacy rows exactly.

    New producers write an explicit stance. Presence with an invalid value fails closed to neutral;
    absence is the migration discriminator and retains the historical outcome projection.
    """
    if "claim_stance" in row:
        stance = str(row.get("claim_stance") or "")
        return stance if stance in _CLAIM_STANCES else "neutral"
    outcome = str(row.get("outcome") or "")
    if outcome == "supported":
        return "support"
    if outcome in _NEGATIVE:
        return "oppose"
    return "neutral"


def _research_verification(row: dict) -> tuple[str, str, str]:
    """Return ``(verdict, method, note)`` for one persisted D8 claim.

    Older rows had no verifier payload.  They are intentionally ``unverified`` rather than implicitly
    supported: a numeric citation proves only that the memo named a node, not that the node establishes the
    claim.  The nested shape is the durable v2 contract; top-level fields are accepted for migration.
    """
    raw = row.get("verification") if isinstance(row.get("verification"), dict) else {}
    verdict = str(raw.get("verdict") or row.get("verification_verdict") or "unverified").lower()
    if verdict not in _RESEARCH_VERDICTS:
        verdict = "unverified"
    method = str(raw.get("method") or row.get("verification_method") or "")[:80]
    note = str(raw.get("note") or row.get("verification_note") or "")[:400]
    return verdict, method, note


def _metric_identity(row: dict) -> str:
    """Best available metric *name* for structured identity (never a numeric score)."""
    for key in ("metric_name", "metric_key", "objective_metric", "metric"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:_MAX_DECISION_METRIC]
    fingerprint = row.get("fingerprint")
    if isinstance(fingerprint, (list, tuple)):
        for token in fingerprint:
            if isinstance(token, str) and token.casefold().startswith("metric:"):
                return token.split(":", 1)[1][:_MAX_DECISION_METRIC]
    return ""


def _epistemic(support, oppose) -> str:
    """The evidence's current verdict on a claim. 'mixed' when both sides exist (a scoped disagreement,
    never newest-wins); 'inconclusive' when only neutral/unknown evidence remains — distinct from a
    supported/refuted claim (§21.20.1: absence is not failure)."""
    if support and oppose:
        return "mixed"
    if support:
        return "supported"
    if oppose:
        return "refuted"
    return "inconclusive"


def claim_evidence_digest(claim: dict) -> str:
    """Stable revision token for the evidence projection an operator actually reviewed.

    Governance metadata is deliberately excluded: ``expected_revision`` fences the decision ledger. This
    digest changes when proof, verification, provenance, or a live opposite-polarity assertion changes.
    """
    fields = (
        "claim_uid", "statement", "scope", "metric", "polarity", "epistemic", "support", "oppose",
        "unverified", "runs", "scopes", "sources", "verification", "contradicts",
    )
    payload = {key: claim.get(key) for key in fields}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "cev_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------------- #
# Operator claim DECISIONS (§22.4) — the ONLY write to cross-run MEANING an actor other than the engine
# may make. Append-only, keyed by normalized statement, overlaid on the machine-proposed assessment.
# --------------------------------------------------------------------------- #

CLAIM_DECISIONS = ("ratified", "rejected", "pinned")
CLAIM_DECISION_ACTIONS = CLAIM_DECISIONS + ("clear",)

_MAX_DECISION_STATEMENT = 4000
_MAX_DECISION_SCOPE = 500
_MAX_DECISION_METRIC = 200
_MAX_DECISION_NOTE = 4000
_MAX_DECISION_ACTOR = 120
_MAX_DECISION_AT = 120
_MAX_DECISION_ACTION_ID = 160
_MAX_EVIDENCE_DIGEST = 80


class ClaimDecisionConflict(ValueError):
    """Optimistic-concurrency conflict on the append-only claim-governance ledger."""

    def __init__(self, expected: int, current: int):
        super().__init__(f"claim governance revision conflict: expected {expected}, current {current}")
        self.expected_revision = expected
        self.current_revision = current


class ClaimDecisionIdempotencyConflict(ValueError):
    """An ``action_id`` was reused with a different semantic decision payload."""


def _bounded(value, name: str, maximum: int, *, required: bool = False) -> str:
    text = str(value or "")
    if required and not text.strip():
        raise ValueError(f"empty {name}")
    if len(text) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    return text


def _decision_payload(row: dict) -> tuple:
    """Semantic request identity for ``action_id`` replay.

    Actor and timestamp are receipt metadata: a transport retry may be served after the deployment's
    operator label changes, but it must still return the original durable receipt instead of conflicting.
    """
    return tuple(str(row.get(k) or "") for k in
                 ("statement", "scope", "metric", "decision", "note", "evidence_digest"))


def _logical_decision_rows(rows) -> list[dict]:
    """Quarantine malformed/id-colliding rows and assign one monotonic logical revision per action."""
    logical: list[dict] = []
    actions: dict[str, tuple] = {}
    for raw in rows or []:
        if not isinstance(raw, dict) or raw.get("decision") not in CLAIM_DECISION_ACTIONS:
            continue
        action_id = str(raw.get("action_id") or "")
        if action_id:
            if action_id in actions:
                # Exact duplicate or collision: either way the repeated physical row is not a new action.
                continue
            actions[action_id] = _decision_payload(raw)
        logical.append({**raw, "revision": len(logical) + 1})
    return logical


def claim_governance_revision(memory_dir) -> int:
    """Current logical claim-governance revision; valid legacy rows count in file order."""
    from pathlib import Path

    from looplab.events.eventstore import read_jsonl_lenient
    if not memory_dir:
        return 0
    path = Path(memory_dir) / "claim_decisions.jsonl"
    rows = read_jsonl_lenient(path, loads=json.loads, dicts_only=True) if path.exists() else []
    return len(_logical_decision_rows(rows))


def record_claim_decision(memory_dir, *, statement: str, decision: str, note: str = "",
                          by: str = "operator", at: str = "", scope: str = "", metric: str = "",
                          expected_revision: Optional[int] = None, action_id: str = "",
                          evidence_digest: str = "", validate: Optional[Callable[[], None]] = None) -> dict:
    """Persist an OPERATOR verdict on a claim (ratify / reject / pin). Append-only JSONL, keyed BOTH by the
    legacy `normalize_statement` (so the lean projection still overlays) AND by a structured `claim_uid`
    (scope+polarity-precise, so a decision in task A never reaches a same-worded claim in task B — CODEX).
    `scope` (task id) / `metric` qualify the structured key. This is the §22.4 governance write — agents
    never call it. Returns the record. Durable locked+fsynced append; raises on an invalid decision or
    missing memory dir (a real operator error)."""
    from pathlib import Path

    if decision not in CLAIM_DECISION_ACTIONS:
        raise ValueError(f"decision must be one of {CLAIM_DECISION_ACTIONS}, got {decision!r}")
    if not memory_dir:
        raise ValueError("no memory_dir")
    # Reject oversized identity fields instead of truncating them: the exact persisted statement/scope/metric
    # must always recompute the same UID after restart. The 4000 statement cap matches persisted D8 claims.
    s = _bounded(statement, "statement", _MAX_DECISION_STATEMENT, required=True).strip()
    sc = _bounded(scope, "scope", _MAX_DECISION_SCOPE)
    mt = _bounded(metric, "metric", _MAX_DECISION_METRIC)
    aid = _bounded(action_id, "action_id", _MAX_DECISION_ACTION_ID).strip()
    from looplab.engine.claim_key import CLAIM_KEY_VERSION, claim_uid
    rec = {"statement": s, "key": normalize_statement(s), "claim_key_version": CLAIM_KEY_VERSION,
           "claim_uid": claim_uid(s, scope=sc, metric=mt), "scope": sc, "metric": mt,
           "decision": decision, "note": _bounded(note, "note", _MAX_DECISION_NOTE),
           "by": _bounded(by or "operator", "by", _MAX_DECISION_ACTOR),
           "at": _bounded(at, "at", _MAX_DECISION_AT)}
    digest = _bounded(evidence_digest, "evidence_digest", _MAX_EVIDENCE_DIGEST).strip()
    if digest:
        rec["evidence_digest"] = digest
    if aid:
        rec["action_id"] = aid
    path = Path(memory_dir) / "claim_decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    from looplab.core.atomicio import best_effort_fsync
    from looplab.events.eventstore import _interprocess_lock, read_jsonl_lenient
    # Idempotency lookup, revision CAS, allocation and append are one critical section. A
    # pre-lock check lets two UI writers both accept revision N and silently create divergent policy.
    with _interprocess_lock(Path(str(path) + ".lock"), required=True):
        rows = read_jsonl_lenient(path, loads=json.loads, dicts_only=True) if path.exists() else []
        logical = _logical_decision_rows(rows)
        if aid:
            existing = next((r for r in logical if str(r.get("action_id") or "") == aid), None)
            if existing is not None:
                if _decision_payload(existing) == _decision_payload(rec):
                    return existing
                raise ClaimDecisionIdempotencyConflict(
                    f"action_id {aid!r} was already used for a different claim decision")
        current = len(logical)
        if expected_revision is not None:
            if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
                raise ValueError("expected_revision must be an integer")
            if expected_revision != current:
                raise ClaimDecisionConflict(expected_revision, current)
        if validate is not None:
            validate()
        stored = {**rec, "revision": current + 1}
        separator = ""
        if path.exists() and path.stat().st_size:
            with open(path, "rb") as existing:
                existing.seek(-1, 2)
                if existing.read(1) not in (b"\n", b"\r"):
                    # Preserve the torn forensic fragment but isolate the acknowledged valid row.
                    separator = "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(separator + json.dumps(stored) + "\n")
            f.flush()
            best_effort_fsync(f.fileno())
        return stored


def _global_key(legacy_key: str) -> str:
    """A DISTINCT index (in the same decisions dict) for the last SCOPE-LESS decision on a statement, so
    a later scoped decision that overwrites the plain legacy key can't hide the portfolio-wide verdict
    from the structured fallback. The control-char prefix won't collide with a claim_uid ("clm_"+hex) or,
    in practice, a normalize_statement key — the only way to collide is a statement literally beginning
    with a NUL byte, which argv, LLM text and engine-written JSON logs never carry. The dict is only ever
    read via `.get(key)`, never iterated, so the extra keys are safe."""
    return "\x00global\x00" + legacy_key


def _scoped_key(legacy_key: str, scope: str) -> str:
    """A lean-projection index for a scope-only decision.

    The structured UID remains authoritative.  This secondary key lets the default statement projection
    retrieve an exact task verdict without putting scoped policy back at the shared legacy key, where the
    latest task would overwrite every earlier task's decision.
    """
    return "\x00scope\x00" + str(scope) + "\x00" + legacy_key


def load_claim_decisions(memory_dir) -> dict:
    """Replay current decisions into safe global and structured namespaces.

    UIDs are recomputed with the current claim-key version, so durable v1 rows migrate on read. A scoped or
    metric-qualified row is indexed ONLY by its structured UID: it must never overwrite the global legacy
    statement key. Unscoped/unqualified rows remain the fallback for every scope. ``clear`` tombstones only
    the namespace it addresses. Last write wins within each exact namespace.
    """
    from pathlib import Path

    from looplab.events.eventstore import read_jsonl_lenient
    if not memory_dir:
        return {}
    path = Path(memory_dir) / "claim_decisions.jsonl"
    if not path.exists():
        return {}
    from looplab.engine.claim_key import CLAIM_KEY_VERSION, claim_uid
    out: dict = {}
    rows = read_jsonl_lenient(path, loads=json.loads, dicts_only=True)
    for r in _logical_decision_rows(rows):
        statement = str(r.get("statement") or "").strip()
        scope, metric = str(r.get("scope") or ""), str(r.get("metric") or "")
        k = str(r.get("key") or normalize_statement(statement))
        # A legacy scoped row without its statement cannot be migrated safely. Never fall back to its old UID:
        # that would silently replay a v1 token-set collision under the v2 role-aware contract.
        uid = claim_uid(statement, scope=scope, metric=metric) if statement else ""
        current = {**r, "claim_uid": uid, "claim_key_version": CLAIM_KEY_VERSION}
        keys = ([uid] if uid else [])
        if k and not scope and not metric:
            # Retain a distinct portfolio-wide fallback as well as the legacy lean key. A
            # caller may merge overlays that place a scoped decision at the plain key; that must not erase
            # the durable global verdict for every other scope.
            keys.extend((k, _global_key(k)))
        elif k and scope and not metric:
            keys.append(_scoped_key(k, scope))
        # One semantic UID may have several historical display spellings. Retire every index that points
        # at the same namespace before applying its newest row, so ``clear`` cannot be bypassed through an
        # older legacy statement key.
        if uid:
            for old_key, old in list(out.items()):
                if str(old.get("claim_uid") or "") == uid:
                    out.pop(old_key, None)
        for key in keys:
            if r.get("decision") == "clear":
                out.pop(key, None)
            else:
                out[key] = current
    return out


def _string_list(raw, *, maximum: int, item_maximum: int) -> list[str]:
    """Bounded JSON-list normalization; strings are scalar values, never character iterables."""
    if not isinstance(raw, (list, tuple)):
        return []
    return [x[:item_maximum] for x in raw[:maximum] if isinstance(x, str) and x]


# --------------------------------------------------------------------------- #
# D8 research claims persisted cross-run (§21.20 / CR1b) — so a deep-research memo's evidence-backed
# claims survive their run and can CONTEST/support lesson verdicts (contested is otherwise unreachable
# from newest-verdict-wins lessons alone). Written at finalize; read by the claim assessments callers.
# --------------------------------------------------------------------------- #

def record_research_claims(memory_dir, *, run_id: str, task_id: str, claims,
                           direction: str = "") -> int:
    """Upsert (by run_id) a run's D8 research claims into `research_claims.jsonl`. Each row:
    {run_id, task_id, statement, node_ids, urls}. Append-with-replace so a re-run doesn't double-count.
    Returns how many rows were written. Best-effort atomicity via the shared whole-file writer."""
    from pathlib import Path

    from looplab.events.eventstore import _interprocess_lock, read_jsonl_lenient, write_jsonl_atomic
    if not memory_dir:
        return 0
    rid = str(run_id or "")
    if not rid:
        return 0
    rows = []
    direction = str(direction or "")
    if direction not in ("min", "max"):
        direction = ""
    source = claims if isinstance(claims, (list, tuple)) else []
    for c in source[:256]:
        stmt = str((c.get("statement") if isinstance(c, dict) else "") or "").strip()
        if not stmt:
            continue
        verdict, method, note = _research_verification(c)
        rows.append({"v": 2, "run_id": rid, "task_id": str(task_id or "")[:500],
                     "direction": direction,
                     "statement": stmt[:4000],
                     "metric": _metric_identity(c),
                     "node_ids": _node_ids(c.get("node_ids"))[:64],
                     "urls": _string_list(c.get("urls"), maximum=32, item_maximum=2000),
                     "verification": {"verdict": verdict, "method": method, "note": note}})
    path = Path(memory_dir) / "research_claims.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Hold the same interprocess lock the case/capsule/decision sidecar stores use — and RE-READ inside it —
    # so two runs sharing memory_dir don't clobber each other's D8 claims in this whole-file read-modify-write
    # (write_jsonl_atomic is crash-atomic, NOT concurrency-safe; last-writer-wins would silently drop the
    # loser's claims and under-count `contested`) (CODEX). This closes the unlocked read-modify-replace the
    # review flagged: two finalizers reading E then replacing with E+r1 / E+r2 would erase each other.
    with _interprocess_lock(Path(str(path) + ".lock"), required=True):
        existing = read_jsonl_lenient(path, loads=json.loads, dicts_only=True) if path.exists() else []
        kept = [r for r in existing if str(r.get("run_id") or "") != rid]   # drop this run's old rows
        write_jsonl_atomic(path, kept + rows, dumps=json.dumps)
    return len(rows)


def load_research_claims(memory_dir) -> list[dict]:
    """The persisted cross-run D8 research claims (statement + run-qualified node evidence). [] when none."""
    from pathlib import Path

    from looplab.events.eventstore import read_jsonl_lenient
    if not memory_dir:
        return []
    path = Path(memory_dir) / "research_claims.jsonl"
    return read_jsonl_lenient(path, loads=json.loads, dicts_only=True) if path.exists() else []


def claims_for_memory(memory_dir, *, lessons=None, research_claims=None, decisions=None,
                      scope_task: str = "", fuzzy: bool = False,
                      structured: bool = False) -> list[dict]:
    """Convenience: `claim_assessments` over a memory dir — lessons.jsonl (or a pre-filtered `lessons`) +
    the persisted D8 research claims + the operator-decision overlay. One call so every read path applies
    research claims AND decisions consistently. `fuzzy` (opt-in) merges paraphrased claims (CR1b);
    `structured` (opt-in) uses the scope+polarity-safe structured claim key (the full CR); `scope_task`
    filters the D8 research claims to the bound task so a task-scoped caller does not re-read another task's
    research claims (mega-review) — the decisions overlay is applied scope-safely by `claim_assessments`."""
    from pathlib import Path

    from looplab.events.eventstore import read_jsonl_lenient
    base = Path(memory_dir) if memory_dir else None
    if lessons is None:
        lp = base / "lessons.jsonl" if base else None
        lessons = read_jsonl_lenient(lp, loads=json.loads, dicts_only=True) if (lp and lp.exists()) else []
    research = load_research_claims(memory_dir) if research_claims is None else list(research_claims)
    if scope_task:
        wanted = str(scope_task)
        lessons = [r for r in lessons if str(r.get("task_id") or "") == wanted]
        research = [r for r in research if str(r.get("task_id") or "") == wanted]
    dec = load_claim_decisions(memory_dir) if decisions is None else decisions
    return claim_assessments(lessons, research_claims=research, decisions=dec,
                             fuzzy=fuzzy, structured=structured)


def atlas_for_memory(memory_dir, *, lessons=None, capsules=None, research_claims=None,
                     decisions=None, scope_task: str = "", max_items: int = 8,
                     structured: bool = False) -> dict:
    """Convenience: `portfolio_atlas` over a memory dir with EVERY overlay loaded — lessons + D8 research
    claims + operator decisions + concept aliases + splits. One call so every atlas surface is consistent.
    `structured` keeps the claim projection consistent with the researcher advisory; `scope_task` filters
    the D8 research claims to the bound task so a task-scoped caller does not surface another task's
    claims/contradictions (mega-review)."""
    from pathlib import Path

    from looplab.engine.concept_registry import load_concept_aliases, load_concept_splits
    from looplab.engine.memory import ConceptCapsuleStore
    from looplab.events.eventstore import read_jsonl_lenient
    base = Path(memory_dir) if memory_dir else None
    if lessons is None:
        lp = base / "lessons.jsonl" if base else None
        lessons = read_jsonl_lenient(lp, loads=json.loads, dicts_only=True) if (lp and lp.exists()) else []
    if capsules is None:
        cp = base / "concept_capsules.jsonl" if base else None
        capsules = ConceptCapsuleStore(cp).all() if (cp and cp.exists()) else []
    research = load_research_claims(memory_dir) if research_claims is None else list(research_claims)
    if scope_task:
        wanted = str(scope_task)
        # Scope is an access boundary across every joined store, not just D8. Filtering only
        # research rows still leaked other tasks through lessons and concept capsules in the same response.
        lessons = [r for r in lessons if str(r.get("task_id") or "") == wanted]
        capsules = [r for r in capsules if str(r.get("task_id") or "") == wanted]
        research = [r for r in research if str(r.get("task_id") or "") == wanted]
    return portfolio_atlas(lessons, capsules, max_items=max_items,
                           decisions=(load_claim_decisions(memory_dir) if decisions is None else decisions),
                           research_claims=research,
                           aliases=load_concept_aliases(memory_dir),
                           splits=load_concept_splits(memory_dir), structured=structured)


_CLAIM_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def _stmt_tokens(s: str) -> frozenset:
    return frozenset(w for w in _CLAIM_WORD.findall((s or "").casefold()) if len(w) > 2)


def _fuzzy_merge_claims(claims: list[dict], *, threshold: float = 0.6) -> list[dict]:
    """Conservative opt-in paraphrase projection.

    Candidates must share scope, semantic polarity and governance maturity, and every member must clear the
    threshold (complete-link). A bounded token index avoids all-pairs and single-link bridge collapse.
    """
    n = len(claims)
    if n <= 1:
        return claims
    from looplab.engine.claim_key import claim_signature
    toks = [_stmt_tokens(c["statement"]) for c in claims]
    meta = [(tuple(c.get("scopes") or []), claim_signature(c["statement"])["polarity"],
             str(c.get("maturity") or "machine-proposed")) for c in claims]
    groups: list[list[int]] = []
    token_groups: dict[str, set[int]] = {}
    for i, token_set in enumerate(toks):
        candidates = sorted({gid for token in token_set for gid in token_groups.get(token, ())})[:64]
        chosen = None
        for gid in candidates:
            members = groups[gid]
            if len(members) >= 64 or any(meta[j] != meta[i] for j in members):
                continue
            complete = True
            for j in members:
                union, inter = token_set | toks[j], token_set & toks[j]
                if not inter or len(inter) / len(union) < threshold:
                    complete = False
                    break
            if complete:
                chosen = gid
                break
        if chosen is None:
            chosen = len(groups)
            groups.append([])
        groups[chosen].append(i)
        for token in token_set:
            token_groups.setdefault(token, set()).add(chosen)

    out = []
    for idxs in groups:
        members = [claims[i] for i in idxs]
        if len(members) == 1:
            out.append(members[0])
            continue
        sup = sorted({r for m in members for r in m["support"]})
        opp = sorted({r for m in members for r in m["oppose"]})
        unverified = sorted({r for m in members for r in m.get("unverified", [])})
        rep = max(members, key=lambda m: (m["n_support"] + m["n_oppose"], m["statement"]))
        mat = members[0].get("maturity", "machine-proposed")
        out.append({
            "statement": rep["statement"], "epistemic": _epistemic(sup, opp), "maturity": mat,
            "support": sup, "oppose": opp, "n_support": len(sup), "n_oppose": len(opp),
            "unverified": unverified, "n_unverified": len(unverified),
            "runs": sorted({r for m in members for r in m["runs"]}),
            "scopes": sorted({r for m in members for r in m["scopes"]}),
            "sources": sorted({s for m in members for s in m.get("sources", [])}),
            "verification": sorted({v for m in members for v in m.get("verification", [])}),
            "decision": members[0].get("decision"),
            "merged_from": sorted(m["statement"] for m in members),
        })
    out.sort(key=lambda c: (-(c["n_support"] + c["n_oppose"]), -c["n_oppose"], c["statement"]))
    return out


def _structured_assessments(lessons, research_claims, decisions) -> list[dict]:
    """The SCOPE+POLARITY-safe structured projection (full CR of the lean fuzzy merge). Identity is the
    `claim_signature` merge_key: (subject stems, scope=task, metric, polarity). Opposite-polarity claims
    sharing a `contra_key` are surfaced as a CONTRADICTION (they never merge, and each is marked contested).
    Governance overlays by the structured `claim_uid` (scope-precise)."""
    from looplab.engine.claim_key import claim_signature, claim_uid
    groups: dict[str, dict] = {}

    def _grp(statement, scope, metric=""):
        s = str(statement or "").strip()
        if not s:
            return None
        sig = claim_signature(s, scope=str(scope or ""), metric=str(metric or ""))
        if sig["polarity"] == 0:                     # no subject content -> not a claim
            return None
        g = groups.get(sig["merge_key"])
        if g is None:
            g = groups[sig["merge_key"]] = {
                "uid": sig["uid"], "contra_key": sig["contra_key"], "polarity": sig["polarity"],
                "scope": sig["scope"], "metric": sig["metric"],
                "support": set(), "oppose": set(), "unverified": set(),
                "runs": set(), "scopes": set(), "sources": set(), "verification": set(), "_ev": {}}
        g["_ev"][s] = g["_ev"].get(s, 0)             # candidate representative statements (evidence-weighted)
        return g

    for lz in lessons or []:
        g = _grp(lz.get("statement"), lz.get("task_id"), _metric_identity(lz))
        if g is None:
            continue
        if lz.get("run_id"):
            g["runs"].add(str(lz["run_id"]))
        if lz.get("task_id"):
            g["scopes"].add(str(lz["task_id"]))
        refs = _qualify_refs(lz.get("run_id"), _node_ids(lz.get("evidence")))
        stance = _lesson_claim_stance(lz)
        if stance == "support":
            g["support"].update(refs)
        elif stance == "oppose":
            g["oppose"].update(refs)
        g["_ev"][str(lz.get("statement") or "").strip()] += len(refs)

    for rc in research_claims or []:
        g = _grp(rc.get("statement"), rc.get("task_id"), _metric_identity(rc))
        if g is None:
            continue
        if rc.get("run_id"):
            g["runs"].add(str(rc["run_id"]))         # D8 rows DO register their run/scope now (CODEX)
        if rc.get("task_id"):
            g["scopes"].add(str(rc["task_id"]))
        refs = _qualify_refs(rc.get("run_id"), _node_ids(rc.get("node_ids")))
        verdict, method, _note = _research_verification(rc)
        g["verification"].add(f"{method}:{verdict}" if method else verdict)
        if verdict == "supported":
            g["support"].update(refs)
        else:
            # unsupported/unclear/cited/legacy-unverified evidence is not counter-evidence; it simply has
            # not established the claim.  Keep the refs drillable without promoting them to support.
            g["unverified"].update(refs)
        g["_ev"][str(rc.get("statement") or "").strip()] += len(refs)
        g["sources"].update(_string_list(rc.get("urls"), maximum=32, item_maximum=2000))

    # Contradiction map: a contra_key seen with BOTH polarities means two opposite claims about one subject
    # in one scope — the portfolio disagrees with itself at the ASSERTION level (unreachable from a single
    # merged statement). Each such claim is marked contested and carries its opposites' representative text.
    _dec = {"ratified": "operator-ratified", "rejected": "operator-rejected", "pinned": "operator-pinned"}

    def _decision_for(g: dict, rep: str):
        overlay = decisions or {}
        candidates = [g["uid"], claim_uid(rep, scope=g["scope"], metric=g["metric"])]
        if g["metric"]:
            candidates.append(claim_uid(rep, scope=g["scope"], metric=""))
        if g["metric"]:
            candidates.append(claim_uid(rep, scope="", metric=g["metric"]))
        candidates.append(claim_uid(rep, scope="", metric=""))
        seen = set()
        for uid in candidates:
            if uid and uid not in seen and uid in overlay:
                return overlay[uid]
            seen.add(uid)
        legacy_key = normalize_statement(rep)
        legacy = overlay.get(legacy_key)
        if (legacy and not str(legacy.get("scope") or "")
                and not str(legacy.get("metric") or "")):
            return legacy
        global_legacy = overlay.get(_global_key(legacy_key))
        if (global_legacy and not str(global_legacy.get("scope") or "")
                and not str(global_legacy.get("metric") or "")):
            return global_legacy
        return None

    prepared = []
    for g in groups.values():
        rep = max(g["_ev"], key=lambda s: (g["_ev"][s], s)) if g["_ev"] else ""
        sup, opp, unverified = sorted(g["support"]), sorted(g["oppose"]), sorted(g["unverified"])
        decision = _decision_for(g, rep)
        prepared.append({"group": g, "statement": rep, "support": sup, "oppose": opp,
                         "unverified": unverified, "decision": decision,
                         "maturity": _dec.get((decision or {}).get("decision"), "machine-proposed")})

    # Keep a governance-independent contradiction map for the evidence digest. The live projection below
    # may hide a rejected opposite, but rejecting it must not make the reviewed proof revision change by
    # itself; only source evidence should age a decision.
    raw_contra: dict[str, dict[int, list]] = {}
    contra: dict[str, dict[int, list]] = {}
    for item in prepared:
        if item["support"]:
            g = item["group"]
            raw_contra.setdefault(g["contra_key"], {}).setdefault(g["polarity"], []).append(item)
        if item["maturity"] != "operator-rejected" and item["support"]:
            contra.setdefault(g["contra_key"], {}).setdefault(g["polarity"], []).append(item)

    out = []
    for item in prepared:
        g, rep = item["group"], item["statement"]
        sup, opp, unverified = item["support"], item["oppose"], item["unverified"]
        opposites = ([] if item["maturity"] == "operator-rejected" else
                     [og for pol, gs in contra.get(g["contra_key"], {}).items() if pol != g["polarity"]
                      for og in gs])
        contradicts = sorted({o["statement"] for o in opposites})
        raw_opposites = [og for pol, gs in raw_contra.get(g["contra_key"], {}).items()
                         if pol != g["polarity"] for og in gs]
        raw_contradicts = sorted({o["statement"] for o in raw_opposites})
        row = {
            "statement": rep,
            # a polarity contradiction is the strongest contested signal -> mixed even if this side's own
            # evidence is one-directional (that is exactly what the structured key makes reachable).
            "epistemic": "mixed" if contradicts and sup else _epistemic(sup, opp),
            "maturity": item["maturity"],
            "support": sup, "oppose": opp, "n_support": len(sup), "n_oppose": len(opp),
            "unverified": unverified, "n_unverified": len(unverified),
            "runs": sorted(g["runs"]), "scopes": sorted(g["scopes"]), "sources": sorted(g["sources"]),
            "verification": sorted(g["verification"]),
            "claim_uid": g["uid"], "scope": g["scope"], "polarity": g["polarity"],
            "metric": g["metric"],
            "decision": item["decision"], "contradicts": contradicts,
        }
        digest_row = {**row,
                      "epistemic": "mixed" if raw_contradicts and sup else _epistemic(sup, opp),
                      "contradicts": raw_contradicts}
        row["evidence_digest"] = claim_evidence_digest(digest_row)
        decision_digest = str((item["decision"] or {}).get("evidence_digest") or "")
        row["decision_fresh"] = (decision_digest == row["evidence_digest"] if decision_digest else None)
        out.append(row)
    out.sort(key=lambda c: (-(c["n_support"] + c["n_oppose"]), -c["n_oppose"],
                            0 if c["contradicts"] else 1, c["statement"]))
    return out


def claim_assessments(lessons: list[dict], *, research_claims: Optional[list[dict]] = None,
                      decisions: Optional[dict] = None, fuzzy: bool = False,
                      structured: bool = False) -> list[dict]:
    """Project distilled `lessons` (+ optional D8 `research_claims`) into evidence-grounded claim
    assessments. Groups by normalized statement; each claim carries `support`/`oppose` node-id evidence,
    contributing `runs`/`scopes`, and an `epistemic` state. `decisions` (from `load_claim_decisions`)
    overlays an operator `maturity` (`operator-ratified`/`operator-rejected`/`operator-pinned`, else
    `machine-proposed`) — the §22.4 governance overlay. Sorted most-evidenced first. Pure.

    `structured` (opt-in, the full CR of the lean `fuzzy` merge) switches identity to the SCOPE+POLARITY-safe
    structured claim key (`claim_key.claim_signature`): claims from different tasks never merge, opposite
    polarity ("X helps" vs "X never helps") is a CONTRADICTION not a merge, and paraphrase/inflection
    variants collapse by exact structured key (O(n), no transitive over-merge). Mutually exclusive with the
    lean `fuzzy` path (structured wins)."""
    if structured:
        return _structured_assessments(lessons, research_claims, decisions)
    groups: dict[str, dict] = {}

    def _group(stmt: str) -> Optional[dict]:
        s = str(stmt or "").strip()
        if not s:
            return None
        # NOTE (CODEX): identity here is the normalized STATEMENT (the shipped lesson `normalize_statement`
        # key) — it can merge same-worded claims across incompatible scopes and the 160-char cap can
        # collide. A structured semantic claim key (subject/intervention/comparator/scope) is the CR1b TODO
        # (§21.20.13); this lean projection keeps scope/runs as metadata on the claim.
        return groups.setdefault(normalize_statement(s), {
            "statement": s, "support": set(), "oppose": set(),
            "unverified": set(), "runs": set(), "scopes": set(), "sources": set(),
            "verification": set()})

    def _qualify(run_id, node_ids) -> list[str]:
        # Run-QUALIFY evidence refs so (r1,node0) and (r2,node0) never collapse (CODEX): a bare node id is
        # run-local. "?" marks a ref whose run is unknown (e.g. a D8 claim without a run_id).
        r = str(run_id or "?")
        return [f"{r}:{n}" for n in node_ids]

    for lz in lessons or []:
        g = _group(lz.get("statement"))
        if g is None:
            continue
        if lz.get("run_id"):
            g["runs"].add(str(lz["run_id"]))
        if lz.get("task_id"):
            g["scopes"].add(str(lz["task_id"]))
        refs = _qualify(lz.get("run_id"), _node_ids(lz.get("evidence")))
        stance = _lesson_claim_stance(lz)
        if stance == "support":
            g["support"].update(refs)
        elif stance == "oppose":
            g["oppose"].update(refs)
        # "noted"/unknown -> neutral: still registers the run/scope, but takes NO stance.

    for rc in research_claims or []:
        g = _group(rc.get("statement"))
        if g is None:
            continue
        if rc.get("run_id"):
            g["runs"].add(str(rc["run_id"]))
        if rc.get("task_id"):
            g["scopes"].add(str(rc["task_id"]))
        refs = _qualify(rc.get("run_id"), _node_ids(rc.get("node_ids")))
        verdict, method, _note = _research_verification(rc)
        g["verification"].add(f"{method}:{verdict}" if method else verdict)
        if verdict == "supported":
            g["support"].update(refs)
        else:
            g["unverified"].update(refs)
        g["sources"].update(_string_list(rc.get("urls"), maximum=32, item_maximum=2000))

    _dec = {"ratified": "operator-ratified", "rejected": "operator-rejected", "pinned": "operator-pinned"}
    out = []
    for key, g in groups.items():
        sup, opp, unverified = sorted(g["support"]), sorted(g["oppose"]), sorted(g["unverified"])
        overlay = decisions or {}
        real_scopes = {str(scope) for scope in g["scopes"] if str(scope)}
        # A statement row spanning multiple tasks cannot safely receive any one task's policy.  For a
        # task-bound row, however, the exact scope-only decision outranks the portfolio-wide fallback.
        d = None
        if len(real_scopes) == 1:
            from looplab.engine.claim_key import claim_uid
            scope = next(iter(real_scopes))
            d = overlay.get(claim_uid(g["statement"], scope=scope, metric=""))
            # Compatibility for a custom lean overlay keyed by normalized statement+scope.
            if d is None:
                d = overlay.get(_scoped_key(key, scope))
        if d is None:
            d = overlay.get(key)
        # The lean projection groups by statement across tasks. A caller-supplied scoped decision may
        # therefore govern this row only when all contributing task scopes are that exact scope; unscoped
        # decisions remain the portfolio-wide fallback. The durable loader normally indexes scoped records
        # by structured UID only, but this guard also keeps custom/preloaded overlays fail-closed.
        if d is not None:
            _dscope = str(d.get("scope") or "")
            if _dscope:
                if not real_scopes or not real_scopes <= {_dscope}:
                    d = None
        if d is None:
            d = overlay.get(_global_key(key))
        out.append({
            "statement": g["statement"],
            "epistemic": _epistemic(sup, opp),
            "maturity": _dec.get((d or {}).get("decision"), "machine-proposed"),
            "support": sup, "oppose": opp,
            "n_support": len(sup), "n_oppose": len(opp),
            "unverified": unverified, "n_unverified": len(unverified),
            "runs": sorted(g["runs"]), "scopes": sorted(g["scopes"]),
            "sources": sorted(g["sources"]), "verification": sorted(g["verification"]),
            "decision": d,
        })
    # most-evidenced first (support+oppose), contested claims break ties toward visibility, then statement
    out.sort(key=lambda c: (-(c["n_support"] + c["n_oppose"]), -c["n_oppose"], c["statement"]))
    return _fuzzy_merge_claims(out) if fuzzy else out


# --------------------------------------------------------------------------- #
# Step 5 (§21.20.5): a BOUNDED context pack for a proposing agent — evidence AND counter-arguments.
# --------------------------------------------------------------------------- #

_CAVEAT_STATES = ("mixed", "refuted", "inconclusive")


def build_context_pack(claims: list[dict], *, concept_overview: Optional[dict] = None,
                       max_claims: int = 5) -> dict:
    """Assemble a CLAIM-COUNT-bounded cross-run context pack from claim assessments (+ an optional concept
    overview) for a proposing agent (§21.20.5, Step 5). ("Claim-count", not token/byte: the pack caps the
    number of claims + per-claim field lengths; a true serialized-token envelope is the CR2b TODO — see the
    NOTE below.) The design's hard rule is that positive hits must
    never crowd out caveats. Precedence is pinned → ratified → mixed → supported → refuted →
    inconclusive, and a **caveat slot is reserved** whenever it can be filled by replacing the weakest
    non-pinned positive. The hard claim cap is never exceeded; pins beyond it are reported as omitted.
    Pure/deterministic and
    'silent' by construction — it just returns structured data; promoting it to advisory prompt-grounding
    is a separate, gated step (never wired here). No LLM, no I/O."""
    # NOTE (CODEX): this bounds by CLAIM COUNT + per-claim field caps (below), not a serialized token/byte
    # budget — a true token envelope is the CR2b TODO. `max_claims<1` is normalized to 1.
    max_claims = max(1, int(max_claims))
    # Governance precedence is explicit: rejected is absent; pinned is retention-critical; ratified is the
    # next preference; then evidence ordering. A caveat may replace a non-pinned positive, never a pin.
    live = [c for c in (claims or []) if c.get("maturity") != "operator-rejected"]
    _kept = {"operator-pinned", "operator-ratified"}
    pinned = [c for c in live if c.get("maturity") == "operator-pinned"]
    ratified = [c for c in live if c.get("maturity") == "operator-ratified"]
    rest = [c for c in live if c.get("maturity") not in _kept]
    by_state: dict[str, list] = {"mixed": [], "supported": [], "refuted": [], "inconclusive": []}
    for c in rest:
        by_state.get(c["epistemic"], by_state["inconclusive"]).append(c)
    ordered = (pinned + ratified + by_state["mixed"] + by_state["supported"]
               + by_state["refuted"] + by_state["inconclusive"])
    picked = ordered[:max_claims]
    # Reserved caveat slot: if nothing picked carries a caveat but caveats exist, swap the weakest NON-kept
    # picked (a governance-retained claim is never evicted to make room) for the strongest available caveat —
    # opposition is never crowded out by a full slate of positives (§20.5). Kept caveats count as caveats too.
    if picked and not any(c["epistemic"] in _CAVEAT_STATES for c in picked):
        # Include RATIFIED caveats too: a ratified mixed/refuted/inconclusive claim pushed past max_claims by
        # the ratified block must still be able to fill the reserved slot, or a slate of ratified-supported
        # claims could crowd opposition out — the exact §20.5 rule this slot exists to protect (CODEX).
        caveats = ([c for c in pinned if c["epistemic"] in _CAVEAT_STATES]
                   + [c for c in ratified if c["epistemic"] in _CAVEAT_STATES]
                   + by_state["mixed"] + by_state["refuted"] + by_state["inconclusive"])
        # Evict the weakest non-pinned positive. Ratification raises priority but may still yield to a
        # caveat; a pin is the explicit retention guarantee and cannot be displaced. If the cutoff is all
        # pins there is no legal victim, so the caveat remains outside this bounded projection.
        victim = next((i for i in range(len(picked) - 1, -1, -1)
                       if picked[i].get("maturity") != "operator-pinned"), None)
        if caveats and victim is not None:
            picked = picked[:victim] + picked[victim + 1:] + [caveats[0]]

    def _slim(c: dict) -> dict:
        # Evidence refs are run-QUALIFIED ("run:node"), so the truncated support/oppose lists stay citable;
        # keep runs/scopes too so a reader can resolve the claim's provenance (CODEX).
        return {"statement": c["statement"][:300], "epistemic": c["epistemic"],
                "maturity": c.get("maturity", "machine-proposed"),
                "claim_uid": c.get("claim_uid", ""), "scope": c.get("scope", ""),
                "evidence_digest": c.get("evidence_digest", ""),
                "decision_fresh": c.get("decision_fresh"),
                "metric": c.get("metric", ""), "polarity": c.get("polarity"),
                "n_support": c["n_support"], "n_oppose": c["n_oppose"],
                "n_unverified": c.get("n_unverified", 0),
                "support": c["support"][:6], "oppose": c["oppose"][:6],
                "unverified": c.get("unverified", [])[:6],
                # Structured polarity contradictions are assertion-level counter-evidence,
                # not entries in ``oppose``. Keep their bounded text or a mixed claim renders as 1↑/0↓
                # with no visible reason for the disagreement.
                "contradicts": _string_list(c.get("contradicts"), maximum=4, item_maximum=300),
                "runs": c.get("runs", [])[:6], "scopes": c.get("scopes", [])[:6]}

    pack = {
        "claims": [_slim(c) for c in picked],
        "n_claims_total": len(claims or []),
        "n_contested": sum(1 for c in live if c.get("epistemic") == "mixed"),
        # Pins have highest priority but cannot override the hard prompt-size cap. Surface any overflow
        # explicitly so a bounded advisory never implies that it retained every operator pin.
        "n_pinned_total": len(pinned),
        "n_pinned_omitted": max(0, len(pinned) - sum(
            1 for c in picked if c.get("maturity") == "operator-pinned")),
    }
    if concept_overview:
        pack["coverage"] = {
            "n_runs": concept_overview.get("n_runs", 0),
            "n_concepts": concept_overview.get("n_concepts", 0),
            "top_concepts": [e["concept"] for e in (concept_overview.get("concepts") or [])[:max_claims]],
        }
    return pack


# Deterministic query-INTENT cues (CR2a eligibility). Kept ML-context-safe: ambiguous technique words
# ("negative", "loss") are NOT cues, so "hard negatives for retrieval" reads as neutral EXPLORE, not FAILED.
_INTENT_CUES = {
    "failed":    frozenset("fail failed failing avoid avoided pitfall pitfalls mistake mistakes wrong "
                           "broke broken regress regression hurt hurts degrade degrades harmful useless "
                           "ineffective".split()),
    "contested": frozenset("contested contradict contradiction conflict conflicting disagree disagreement "
                           "controversial controversy debate unclear uncertain".split()),
    "worked":    frozenset("best proven effective recommend recommended success successful reliable robust "
                           "winning champion".split()),
}
# The CONTRADICTION pool for the retrieval quota — claims that carry actual OPPOSITION (mixed=contested,
# refuted=negative verdict). This is DELIBERATELY narrower than build_context_pack's `_CAVEAT_STATES`
# (which also includes `inconclusive`): the context-pack reserves a slot so a clean slate of positives can't
# hide any NON-positive (§21.20.5 coverage), whereas the retrieval quota reserves slots specifically for
# COUNTER-EVIDENCE/contradictions — an inconclusive (no-stance) claim is neither. Two distinct mechanisms,
# not an accidental inconsistency (concept-conformance).
_CAVEAT = frozenset(("mixed", "refuted"))


def _classify_intent(query: str) -> str:
    """Map a free-text query to a retrieval INTENT (failed / contested / worked / explore) by cue overlap.
    Deterministic, no LLM. `explore` (neutral) when no cue fires — the safe default that reorders nothing."""
    toks = set(_CLAIM_WORD.findall(str(query or "").casefold()))
    scored = [(sum(1 for w in cues if w in toks), name) for name, cues in _INTENT_CUES.items()]
    best_n, best = max(scored, key=lambda t: (t[0], t[1]))
    return best if best_n else "explore"


def _eligible(kind: str, meta: dict, intent: str) -> bool:
    """Whether a doc is on-INTENT (a soft priority signal, never a hard exclusion — counter-evidence is
    still returned). Concepts are always eligible; a claim's eligibility depends on its epistemic/maturity."""
    if kind != "claim" or intent == "explore":
        return True
    ep, mat = meta.get("epistemic"), meta.get("maturity")
    if intent == "failed":
        return ep in _CAVEAT
    if intent == "contested":
        return ep == "mixed"
    if intent == "worked":
        return ep == "supported" or mat == "operator-ratified"
    return True


_INTENTS = ("failed", "contested", "worked", "explore")

_RETRIEVAL_CORPUS_VERSION = 2
_INTENT_SCORE_BONUS = 0.001
_CAVEAT_SCORE_RATIO = 0.50
_CAVEAT_QUERY_COVERAGE = 0.10


def _retrieval_tokens(text: str) -> frozenset[str]:
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return frozenset(_CLAIM_WORD.findall(normalized))


def _lexical_relevance(query: str, text: str) -> tuple[int, float, float]:
    q, d = _retrieval_tokens(query), _retrieval_tokens(text)
    shared = len(q & d)
    coverage = shared / len(q) if q else 0.0
    jaccard = shared / len(q | d) if q or d else 0.0
    return shared, coverage, jaccard


def _json_digest(value, *, length: int = 20) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def _retrieval_doc(kind: str, text: str, meta: dict) -> tuple[str, str, dict]:
    identity = {"v": _RETRIEVAL_CORPUS_VERSION, "kind": kind,
                "claim_uid": str(meta.get("claim_uid") or ""),
                "metric": str(meta.get("metric") or ""),
                "text": " ".join(unicodedata.normalize("NFKC", str(text or "")).casefold().split())}
    stable_id = f"{kind[:1]}_{_json_digest(identity, length=16)}"
    return kind, str(text or ""), {**meta, "stable_id": stable_id}


def _retrieval_corpus_digest(docs) -> str:
    canonical = [{"kind": kind, "text": text, "meta": meta}
                 for kind, text, meta in sorted(docs, key=lambda d: d[2]["stable_id"])]
    return _json_digest({"v": _RETRIEVAL_CORPUS_VERSION, "docs": canonical}, length=20)


def _preselect_retrieval_docs(docs, query: str, limit: int):
    """Cheap query-aware cap with one best row per source kind before the expensive hybrid index."""
    cap = max(1, int(limit))
    if len(docs) <= cap:
        return list(docs)
    stats = [_lexical_relevance(query, d[1]) for d in docs]
    ranked = sorted(range(len(docs)),
                    key=lambda i: (-stats[i][0], -stats[i][1], -stats[i][2],
                                   docs[i][2]["stable_id"]))
    selected: list[int] = []
    kinds = sorted({d[0] for d in docs})
    if cap >= len(kinds):
        for kind in kinds:
            selected.append(next(i for i in ranked if docs[i][0] == kind))
    selected_set = set(selected)
    selected.extend(i for i in ranked if i not in selected_set)
    return [docs[i] for i in selected[:cap]]


def cross_run_retrieve(memory_dir, query: str, *, k: int = 8, lessons=None, capsules=None,
                       research_claims=None, scope_task: str = "", contradiction_quota: float = 0.34,
                       max_corpus: int = 2000, structured: bool = False, intent: Optional[str] = None) -> dict:
    """CR2a retrieval planner (§21.20.5, full CR): RRF-fuse the portfolio's cross-run KNOWLEDGE — claims
    (epistemic state / operator maturity) + concepts (#runs) — over the shipped `HybridRetriever`
    (lexical + BM25 + vector; reuses hybrid_merge, NO new fuser), then shape the ranked recall with:

    - INTENT classification (`failed`/`contested`/`worked`/`explore`) → an eligibility priority so an
      on-intent claim floats up (soft; never hides counter-evidence);
    - a CONTRADICTION QUOTA reserving ~`contradiction_quota` of the k slots for caveat (mixed/refuted)
      claims when they exist, so a positive-heavy recall never buries the counter-evidence (mirrors the
      context pack's caveat slot). `failed`/`contested` intents raise the quota;
    - a bounded corpus (`max_corpus`, truncation REPORTED not silent) + a why-recalled RECEIPT (intent,
      quota, corpus digest, degraded-channel note, per-hit rank).

    Every source is SCOPED before indexing: pass scoped `lessons`/`capsules`, and `scope_task` filters the
    D8 research claims to that task so a task-bound agent cannot retrieve another task's claims (CODEX).
    Operator-rejected claims never enter the corpus. Advisory; pure w.r.t. the passed/loaded stores."""
    from pathlib import Path

    from looplab.engine.concept_registry import (load_concept_aliases, load_concept_splits)
    from looplab.engine.memory import ConceptCapsuleStore, portfolio_concept_overview
    from looplab.events.eventstore import read_jsonl_lenient
    base = Path(memory_dir) if memory_dir else None
    if capsules is None:
        cp = base / "concept_capsules.jsonl" if base else None
        capsules = ConceptCapsuleStore(cp).all() if (cp and cp.exists()) else []
    if lessons is None:
        lp = base / "lessons.jsonl" if base else None
        lessons = read_jsonl_lenient(lp, loads=json.loads, dicts_only=True) if (lp and lp.exists()) else []
    # Scope EVERY source before joining. Decisions are a governance overlay; they never grant visibility.
    research = load_research_claims(memory_dir) if research_claims is None else list(research_claims)
    if scope_task:
        wanted = str(scope_task)
        lessons = [r for r in lessons if str(r.get("task_id") or "") == wanted]
        capsules = [r for r in capsules if str(r.get("task_id") or "") == wanted]
        research = [r for r in research if str(r.get("task_id") or "") == wanted]
    claims = [c for c in claim_assessments(lessons, research_claims=research,
                                           decisions=load_claim_decisions(memory_dir), structured=structured)
              if c.get("maturity") != "operator-rejected"]
    overview = portfolio_concept_overview(capsules, aliases=load_concept_aliases(memory_dir),
                                          splits=load_concept_splits(memory_dir))
    docs: list[tuple[str, str, dict]] = []
    for c in claims:
        evidence_digest = _json_digest({"support": c.get("support", []), "oppose": c.get("oppose", []),
                                        "unverified": c.get("unverified", []),
                                        "sources": c.get("sources", [])})
        docs.append(_retrieval_doc("claim", c["statement"], {
            "epistemic": c["epistemic"], "n_support": c["n_support"],
            "n_oppose": c["n_oppose"], "n_unverified": c.get("n_unverified", 0),
            "contradicts": _string_list(c.get("contradicts"), maximum=4, item_maximum=300),
            "maturity": c.get("maturity"), "claim_uid": c.get("claim_uid", ""),
            "metric": c.get("metric", ""), "scopes": c.get("scopes", []),
            "decision_revision": (c.get("decision") or {}).get("revision"),
            "governance_digest": _json_digest(c.get("decision") or {}),
            "evidence_digest": evidence_digest}))
    for e in overview["concepts"]:
        docs.append(_retrieval_doc("concept", e["concept"], {
            "n_runs": e["n_runs"], "runs": [r["run_id"] for r in e["runs"][:5]],
            "evidence_digest": _json_digest(e["runs"])}))

    n_total = len(docs)
    corpus_digest = _retrieval_corpus_digest(docs)
    indexed_docs = _preselect_retrieval_docs(docs, str(query or ""), max_corpus)
    truncated = n_total - len(indexed_docs)
    # The AGENT may pass an explicit `intent` (it knows why it is searching — genuinely agentic); otherwise
    # classify deterministically from the query text. An unknown value falls back to classification.
    intent = intent if intent in _INTENTS else _classify_intent(query)
    kk = max(1, int(k))                            # normalize k (0/-1 -> 1) before it reaches the receipt
    try:
        base_quota = float(contradiction_quota)
    except (TypeError, ValueError):
        base_quota = 0.34
    if not math.isfinite(base_quota):
        base_quota = 0.34
    base_quota = min(1.0, max(0.0, base_quota))
    q = max(base_quota, 0.5) if intent in ("failed", "contested") else base_quota
    target = min(math.ceil(kk * q), max(0, kk - 1))
    # A why-recalled receipt: corpus revision (content digest), the degraded vector-channel semantics, the
    # classified intent + quota, and (below) the per-hit rank — enough to explain/reproduce a result.
    receipt = {"query": str(query or ""), "k": kk, "n_corpus": n_total,
               "n_indexed": len(indexed_docs), "corpus_digest_version": _RETRIEVAL_CORPUS_VERSION,
               "channels": ["lexical", "bm25", "vector"], "intent": intent,
               "vector_channel": "hash_embed(64-bucket bag-of-words; lexical proxy, not semantic)",
               "corpus_digest": corpus_digest,
               "retrieval_digest": _retrieval_corpus_digest(indexed_docs), "truncated": truncated,
               "preselection": "query-overlap+one-per-source/v1",
               "contradiction_quota": round(base_quota, 3),
               "effective_quota": round(q, 3), "caveat_target": target,
               "caveat_score_ratio": _CAVEAT_SCORE_RATIO,
               "caveat_query_coverage": _CAVEAT_QUERY_COVERAGE,
               "intent_score_bonus": _INTENT_SCORE_BONUS}
    if not indexed_docs or not str(query or "").strip():
        return {"results": [], "receipt": {**receipt, "n_hits": 0, "n_caveats": 0}}

    from looplab.search.hybrid_merge import HybridRetriever
    # Retrieve a POOL larger than k so the intent priority + contradiction quota have room to reorder/swap
    # without extra queries; the vector channel is the `hash_embed` bag-of-words (a lexical proxy — declared
    # in the receipt, not passed off as semantic retrieval).
    pool_n = min(len(indexed_docs), max(kk * 4, kk + 12))
    pool = HybridRetriever([t for _, t, _ in indexed_docs]).candidates(str(query), k=pool_n)
    ranked = []
    for rel_rank, (i, score) in enumerate(pool):
        kind, text, meta = indexed_docs[i]
        shared, coverage, jaccard = _lexical_relevance(str(query), text)
        eligible = _eligible(kind, meta, intent)
        # Intent is a bounded tiebreak-like bonus scaled by actual query overlap, never a hard tier that can
        # lift an unrelated "failed" memory above a strongly relevant positive result.
        bonus = (_INTENT_SCORE_BONUS * min(1.0, coverage * 2.0)
                 if intent != "explore" and eligible and shared else 0.0)
        ranked.append({"idx": i, "kind": kind, "text": text, "score": round(float(score), 6),
                       "intent_bonus": round(bonus, 6), "query_overlap": shared,
                       "query_coverage": round(coverage, 4), "query_jaccard": round(jaccard, 4),
                       "rel_rank": rel_rank, **meta})
    ranked.sort(key=lambda h: (-(h["score"] + h["intent_bonus"]), h["rel_rank"], h["stable_id"]))
    picked = ranked[:kk]

    # CONTRADICTION QUOTA: guarantee ~quota of the k slots are caveat (mixed/refuted) claims when the pool
    # has them — swapping the LEAST-relevant non-caveat picks (from the bottom) for the most-relevant unpicked
    # caveats, so the top relevance hit is never displaced and opposition is never crowded out.
    # ceil(k*q) caveat slots, but capped at k-1 so the #1 relevance hit is NEVER evicted (at k=1 the target
    # is 0 — the single slot stays the top hit, as the swap contract promises; mega-review finding).
    have = [h for h in picked if h["kind"] == "claim" and h.get("epistemic") in _CAVEAT]
    if target > len(have):
        picked_ids = {h["idx"] for h in picked}
        top_score = max((h["score"] for h in ranked), default=0.0)
        extra = [h for h in ranked if h["idx"] not in picked_ids
                 and h["kind"] == "claim" and h.get("epistemic") in _CAVEAT
                 and h["query_coverage"] >= _CAVEAT_QUERY_COVERAGE
                 and h["score"] >= top_score * _CAVEAT_SCORE_RATIO]
        need = target - len(have)
        for cav in extra[:need]:
            # Keep the raw relevance winner (rel_rank 0). Quotas reserve relevant counter-evidence, not an
            # unrelated caveat selected solely for its epistemic label. Also NEVER evict an operator-PINNED
            # claim — the "pinned is retained" governance projection applies to EVERY consumer, not just the
            # context pack (concept-conformance: §22.4 / §21.20.5, mirroring build_context_pack).
            victim = next((h for h in reversed(picked)
                           if not (h["kind"] == "claim" and h.get("epistemic") in _CAVEAT)
                           and h["rel_rank"] != 0 and h.get("maturity") != "operator-pinned"), None)
            if victim is None:
                break
            picked[picked.index(victim)] = cav
        picked.sort(key=lambda h: (-(h["score"] + h["intent_bonus"]),
                                   h["rel_rank"], h["stable_id"]))

    n_caveats = sum(1 for h in picked if h["kind"] == "claim" and h.get("epistemic") in _CAVEAT)
    results = [{k2: v for k2, v in h.items() if k2 != "idx"} for h in picked]
    # Report the EFFECTIVE quota actually applied (raised for failed/contested) + the reserved caveat target,
    # so the receipt explains why a contested claim was (or wasn't) surfaced — not just the configured base.
    return {"results": results,
            "receipt": {**receipt, "n_hits": len(results), "n_caveats": n_caveats}}


def portfolio_atlas(lessons: list[dict], capsules: list[dict], *, max_items: int = 8,
                    decisions: Optional[dict] = None, research_claims: Optional[list[dict]] = None,
                    aliases: Optional[dict] = None, splits: Optional[dict] = None,
                    structured: bool = False) -> dict:
    """The Research Atlas DATA payload (§21.20 Step 6): one structured bounded observation/mixed-evidence
    view, composing the concept overview (Step 3), the claim
    assessments (Step 4) and the bounded context pack (Step 5). Pure/deterministic — the read-model a
    Research Atlas UI (or an agent) would render; no LLM, no I/O.

    The legacy ``thin_coverage`` field means only "observed in one returned run". It is not a gap or coverage
    assertion: a true CoverageFrame (§20.6, unknown-vs-zero) needs a frozen scope, eligible denominator and
    health contract, which remain deferred full-CR3a work."""
    from looplab.engine.memory import portfolio_concept_overview
    max_items = max(1, int(max_items))                       # normalize (CODEX): 0/negative -> at least 1
    overview = portfolio_concept_overview(capsules, aliases=aliases, splits=splits)
    claims = claim_assessments(lessons, research_claims=research_claims, decisions=decisions,
                               structured=structured)
    # A contradiction the operator REJECTED is no longer live, consistent with build_context_pack and
    # cross_run_claims. Pin priority applies inside the embedded context pack; this human-facing contested
    # summary remains evidence-ordered and independently capped.
    contested = [c for c in claims if c["epistemic"] == "mixed" and c.get("maturity") != "operator-rejected"]
    thin = [e["concept"] for e in overview["concepts"] if e["n_runs"] == 1]
    # Run count spans BOTH sources — capsules AND the runs cited by lessons — so a lesson-only / legacy
    # memory (no opt-in capsules) is not reported as zero runs (CODEX). The authoritative scoped corpus
    # join (cross_run_index) is the full-CR TODO; this at least unions what the two memory stores know.
    run_ids = {c.get("run_id") for c in capsules if c.get("run_id")}
    for cl in claims:
        run_ids.update(cl.get("runs") or [])
    n_runs = len(run_ids)
    # Keep the embedded context-pack coverage n_runs CONSISTENT with the top-level count (both the union of
    # capsule + lesson-cited runs), so one atlas payload never reports two different run counts — otherwise a
    # lesson-only memory says n_runs>0 at the top but coverage.n_runs==0, the very "zero runs" artifact the
    # union set out to fix (CODEX).
    pack_overview = {**overview, "n_runs": n_runs}
    return {
        "n_runs": n_runs, "n_concepts": overview["n_concepts"],
        "n_claims": len(claims), "n_contested": len(contested),
        "explored": overview["concepts"][:max_items],        # what's been tried (concept × runs)
        "thin_coverage": thin[:max_items],                   # legacy key: observed in one returned run
        "contradictions": contested[:max_items],             # legacy key: mixed-evidence claim records
        "context_pack": build_context_pack(claims, concept_overview=pack_overview, max_claims=max_items),
    }


def _safe_text(s, limit: int = 120) -> str:
    """Sanitize UNTRUSTED memory text (claim statements / concept slugs — LLM/repo-derived) before it enters
    an agent prompt: strip control chars + collapse newlines/whitespace to a single space, then bound the
    length. Prevents newline/control-char prompt-injection through the cross-run advisory pack (mega-review)."""
    t = re.sub(r"[\x00-\x1f\x7f-\x9f]+", " ", str(s or ""))
    return re.sub(r"\s+", " ", t).strip()[:limit]


def render_context_pack(pack: dict) -> str:
    """Render a context pack as a compact, bounded text block for a proposing agent (the advisory form).
    Deterministic; retains mixed evidence so the agent sees counter-arguments, not only positives.
    All memory-derived text is sanitized (control chars/newlines stripped) — quoted DATA, not instructions
    (mega-review prompt-injection hardening)."""
    if not pack.get("claims") and not pack.get("coverage"):
        return ""
    _mark = {"supported": "✓", "refuted": "✗", "mixed": "⚖", "inconclusive": "·"}
    lines = [f"Cross-run evidence ({pack.get('n_claims_total', 0)} claim records, "
             f"{pack.get('n_contested', 0)} mixed-evidence) — bounded observations, with counter-evidence:"]
    if pack.get("n_pinned_omitted", 0):
        lines.append(
            f"  WARNING: {int(pack['n_pinned_omitted'])} operator-pinned claim(s) omitted by the "
            "hard context limit; consult the full claims ledger.")
    for c in pack.get("claims", []):
        statement = _safe_text(c.get("statement"), 120)
        contradicts = "; ".join(
            repr(_safe_text(value, 160))
            for value in (c.get("contradicts") or [])[:3])
        lines.append(f"  {_mark.get(c['epistemic'], '?')} [{c['n_support']}↑/{c['n_oppose']}↓] "
                     f"UNTRUSTED_MEMORY={statement!r}"
                     + (f"; contradicts={contradicts}" if contradicts else ""))
    cov = pack.get("coverage")
    if cov:
        top = ", ".join(repr(_safe_text(x, 100))
                        for x in cov.get("top_concepts", [])[:6])
        lines.append(f"Bounded live concept observations (not coverage): {cov.get('n_runs', 0)} returned "
                     f"run(s), {cov.get('n_concepts', 0)} concept(s)"
                     f"{'; UNTRUSTED_MEMORY_CONCEPTS=' + top if top else ''}.")
    return "\n".join(lines)
