"""The cross-run concept RATIFICATION stage — the background half of the agentic taxonomy steward.

WHAT WAS MISSING, AND WHY (read this before changing anything here).

The portfolio-level agentic merge already exists and already runs. `engine/concept_steward.py`
is the cross-run instance of `search/concept_map.py::consolidate_concepts`: an LLM reviews the
whole portfolio's concept vocabulary and PROPOSES merges/splits/purges, bounded and validated
against the live vocabulary. It is dispatched at finalize by `curation_protocol.py`'s at-most-once
paid transaction and its result is durably logged to `concept_curation_log.jsonl`. On the
development corpus it has been producing correct merges for weeks — e.g.
`experiment/validation/empirical` -> `validation/empirical_confirmation`, "direct semantic
equivalence".

Nothing has ever applied one. `concept_aliases.jsonl` does not exist in that memory dir.

The apply half WAS built with the steward (`bf259ba8`, 2026-07-15: "apply_concept_curation records
through the SAME deterministic, append-only, reversible record_concept_alias / record_concept_split
the operator CLI uses") and was DELETED on 2026-08-03 (`72ae8487`, item EM-14) for a stated and
correct reason:

    "`apply_concept_curation` is deleted. It bypassed the CAS discipline entirely (neither
     `expected_revision` nor `action_id` reached the writers) while the module's invariant is that
     the steward only PROPOSES and the operator applies through a typed action or owner HTTP
     governance. Dead code contradicting the invariant one import away is worse than dead code."

So the portfolio does not tidy itself because its applier was found concurrency-unsafe and removed,
leaving the paid proposer running with no consumer. This module is that half rebuilt with the
discipline it lacked: **every append carries both an `action_id` and an `expected_governance_revision`
that reach the writer**, and the writer is the same `record_concept_alias` the operator's CLI and
HTTP surfaces call. Nothing here mints an identity, rewrites history, or invents a taxonomy.

WHO DECIDES WHAT.

* The **agent** decides that two concepts are one. That judgement is already made, already paid for,
  already bounded by `_validate_curation` (both ends must be in the live vocabulary, no self-links,
  capped count) and already durable. This stage does not re-decide it and never calls a model.
* This **stage** decides only whether a recorded proposal is still applicable *now* — and it delegates
  even that to `record_concept_alias`'s own locked validator, which re-checks source/target liveness,
  portfolio membership and cycle-freedom inside the same critical section as the append.
* The **operator** keeps the veto, before and after: the stage is off by default, and every decision
  it lands is reversed by one existing typed action (`concept-alias-clear` / the HTTP
  `/api/cross-run/concept-alias-clear`).

MERGES ONLY. The steward also proposes SPLITs and PURGEs and this stage applies neither. A merge is
deduplication — it says two names are one thing, and clearing it restores exactly the previous view.
A purge removes a concept from every cross-run view (retrieval, novelty, the atlas) on an inference
about absence; a split mints provisional child ids that no run authored. Both are reversible, neither
is deduplication, and both deserve a human at the moment of the decision rather than a human
discovering it later. They are surfaced in this stage's receipt as pending operator work.

WHY THIS IS NOT `curation_protocol.py`'s THIRD DRIVER, AND NOT `steward_invocation.py`'s.

Both of those protocols exist for one reason: a provider call cannot participate in a JSONL
transaction, so a crash between "we paid" and "we wrote the receipt" must never be replayable. They
are elaborate because the effect they guard is NOT idempotent — buying the same answer twice costs
money twice.

This stage buys nothing. Its only effect is one `record_concept_alias` append per decision, and that
append is idempotent by construction: the `action_id` is a pure function of the semantic payload
(`from` + `to` + action), so `_append_governance`'s idempotency lookup — which runs BEFORE CAS and
validation — turns any repeat into a replay of the original receipt. A crash anywhere leaves a
prefix of decisions applied and the rest untouched, and re-running completes it. So this is an
AT-LEAST-ONCE-SAFE stage, where those two are AT-MOST-ONCE protocols, and joining either would mean
carrying a claim/recovery/scratch machinery whose whole purpose (making a non-idempotent effect
happen at most once) does not apply. It would also put a second writer on
`concept_curation_log.jsonl`, whose `_validate_curation_row` is documented as staying TOTAL over four
coexisting row generations; this stage is strictly a READER of that ledger.

The one property that idempotency buys and must not be lost: **an operator's undo is durable**.
`clear_concept_alias` pops the policy edge but the original SET row stays physically in the
append-only ledger, so the next pass matches its `action_id`, replays the old receipt and appends
nothing. A reversed decision stays reversed without any "do not re-propose" list.

WHEN IT RUNS, AND WHY NOT AS A RUN'S PARALLEL TASK.

It is a CROSS-RUN stage with a run-triggered entry point, not a run-scoped background task:

1. Engine invariant 1 forbids a background task appending folded domain events, and there is no
   folded event this stage could honestly write anyway. A `concept_merge_ratified` in run A's log is
   invisible to run B's replay while every run's read-time canonicalization obeys the decision, and
   invariant 5 forbids `fold` doing the I/O it would need to re-derive one. The gate for invariant 3
   is therefore the governance ledger itself, keyed by `action_id` — which is strictly better than a
   `<x>_requests`/`<x>s_done` counter pair here, because it survives the triggering run's deletion.
2. The drift is a property of the CORPUS. Any run would compute the same decisions from the same
   ledger, so triggering from a run is arbitrary; what makes finalize the right MOMENT is only that
   the portfolio just changed (this run's capsule and, right above, its own paid proposal landed).
3. Nothing here is on the run's critical path or in its failure domain: the stage never raises to its
   caller and its receipt lives in cross-run memory, not in the run's log.

Hence: called from `engine/finalize.py` on the MAIN task immediately after the concept steward
(so the proposal it may ratify is at most one step old), gated on `Settings.concept_tidy`, default
OFF; and exposed to the operator as `looplab governance concept-ratify` for on-demand use and dry
runs. One function, two entry points — unlike the paid protocols, there is no crash-window asymmetry
between the two to justify two writers.

Layering: engine-internal; imports `concept_registry` (the table), `governance_health` (the strict
reader) and stdlib. Never `serve`, never the orchestrator.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from looplab.engine.concept_registry import (
    ConceptGovernanceGlobalConflict,
    _append_governance,
    concept_governance_snapshot,
    normalize_key,
    prepare_concept_alias,
    record_concept_alias,
    resolve_slug,
)
from looplab.engine.governance_health import (
    GovernanceLedgerUnavailable,
    curation_ledger_file,
    read_curation_rows,
)

# The actor stamped on every alias row this stage writes. It is deliberately NOT "operator": `by` is
# what a governance reader (CLI, HTTP, a future UI lens) joins on to tell a RATIFIED agent judgement
# apart from a human's typed action — and both apart from the render-time similarity hints
# `ui/src/conceptForest.js::spellingVariants` reports, which are not decisions at all and never
# reach a ledger. Versioned so a later change to what auto-ratification means is legible in history
# rather than retroactively re-describing rows already on disk.
RATIFIER_ACTOR = "concept-ratifier/v1"
RATIFICATION_LEDGER = "concept_ratification_log.jsonl"
_ACTION_ID_PREFIX = "concept-ratify:v1:"
# A pass is bounded so one enormous curation history cannot turn a finalize into a long ledger walk.
# The remainder is simply ratified by the next pass; nothing is lost, because the input is durable.
MAX_RATIFICATIONS_PER_PASS = 32
# CAS retries per decision. One retry absorbs the ordinary race (a concurrent writer moved the
# governance revision between our snapshot and our append); beyond that, declining and letting the
# next pass re-derive is correct — a retry loop against a busy ledger is how a finalize hangs.
_MAX_CAS_ATTEMPTS = 2
_MAX_WHY = 200


@dataclass(frozen=True)
class ProposedMerge:
    """One agent-proposed merge, normalized to exactly what the alias writer would store.

    `source`/`target` come from `prepare_concept_alias`, i.e. the same normalization the durable
    write applies, so a proposal can never be displayed or receipted in one spelling and committed
    in another. `proposal_key` is the curation row's own durable identity (the finalize protocol's
    semantic `curation_key`, or the on-demand protocol's `action_id`) and is provenance ONLY — see
    `ratification_action_id` for why it is deliberately not part of the decision identity.
    """

    source: str
    target: str
    proposal_key: str
    why: str
    revision: int

    @property
    def action_id(self) -> str:
        return ratification_action_id(self.source, self.target)


def ratification_action_id(from_concept: str, to_concept: str) -> str:
    """The at-most-once key for ratifying one merge — a pure function of the SEMANTIC payload.

    It binds exactly the fields `concept_registry._idempotency_payload` compares (`v`/`action`/
    `from`/`to`; `v` and `action` are fixed for every row this stage writes), which is what makes two
    outcomes impossible:

    * a `ConceptGovernanceIdempotencyConflict` — that is raised when one action id carries two
      different payloads, and here equal ids imply equal payloads by construction;
    * a duplicate physical row for a decision already taken, however many proposals repeat it.

    Keying on the PROPOSAL instead (the curation row) would break the second: two finalize passes
    over different portfolio snapshots legitimately propose the same merge twice, and the second
    would append a fresh row — silently re-applying a merge the operator had cleared. Keyed on the
    payload, the cleared decision's original row is still found and replayed, and the clear stands.
    A LATER proposal naming a DIFFERENT target is a genuinely new decision and does apply; this
    module's undo is per-decision, not a permanent veto on the concept (see the module docstring).
    """
    prepared = prepare_concept_alias(from_concept, to_concept)
    digest = hashlib.sha256(
        f"set\x1f{prepared['from']}\x1f{prepared['to']}".encode("utf-8")).hexdigest()
    return _ACTION_ID_PREFIX + digest[:32]


def _proposal_key(row: dict) -> str:
    """The curation row's own durable identity, whichever of the two paid protocols wrote it."""
    for field in ("curation_key", "action_id", "invocation_id"):
        value = row.get(field)
        if isinstance(value, str) and value:
            return value[:160]
    return ""


def proposed_merges(memory_dir) -> list[ProposedMerge]:
    """Every agent-proposed MERGE recorded in the concept curation ledger, oldest first.

    Read through `governance_health.read_curation_rows`, i.e. the same strict reader the paid
    protocols use: a torn or unrecognized row fails the whole read rather than being skipped, because
    skipping one would silently change which decisions this stage believes were proposed. Rows are
    returned in physical ledger order and deduplicated by decision identity, keeping the EARLIEST
    proposal of a repeated merge — so the order in which conflicting proposals are attempted (and
    therefore which of a mutually-cyclic pair is rejected) is a function of durable history rather
    than of dict iteration.
    """
    if not memory_dir:
        return []
    path = Path(memory_dir) / curation_ledger_file("concept")
    if not path.exists():
        return []
    out: list[ProposedMerge] = []
    seen: set[str] = set()
    for row in read_curation_rows(path, kind="concept"):
        # `begun` claims carry no proposals, and an `error`/`empty`/`unavailable` terminal has none
        # either. Read the payload rather than the outcome string: the outcome vocabulary differs
        # between the two writers, the payload does not.
        merges = (row.get("proposals") or {}).get("merges")
        if not isinstance(merges, list):
            continue
        revision = row.get("revision")
        revision = revision if isinstance(revision, int) and not isinstance(revision, bool) else 0
        for merge in merges:
            if not isinstance(merge, dict):
                continue
            try:
                prepared = prepare_concept_alias(
                    merge.get("from_concept") or "", merge.get("to_concept") or "")
            except ValueError:
                # A proposal that the durable writer would refuse outright (empty source, self-link)
                # is not a decision this stage can carry forward. `_validate_curation` already drops
                # these at proposal time; a historical row may predate that.
                continue
            if not prepared["to"]:
                continue          # an empty target is a PURGE, which this stage never applies
            candidate = ProposedMerge(
                source=prepared["from"], target=prepared["to"],
                proposal_key=_proposal_key(row),
                why=str(merge.get("why") or "")[:_MAX_WHY], revision=revision)
            if candidate.action_id in seen:
                continue
            seen.add(candidate.action_id)
            out.append(candidate)
    return out


def pending_curation_work(memory_dir) -> dict:
    """Counts of proposed SPLIT/PURGE work this stage deliberately leaves to the operator.

    Reported rather than acted on, so "the background stage ran and the taxonomy is still messy" can
    be told apart from "the background stage silently declined to act". Never raises on a missing
    ledger; a poisoned one propagates, because a health failure must not read as "no pending work".
    """
    if not memory_dir:
        return {"splits": 0, "purges": 0}
    path = Path(memory_dir) / curation_ledger_file("concept")
    if not path.exists():
        return {"splits": 0, "purges": 0}
    splits = purges = 0
    for row in read_curation_rows(path, kind="concept"):
        proposals = row.get("proposals") or {}
        for field, add in (("splits", 1), ("purges", 1)):
            value = proposals.get(field)
            if isinstance(value, list):
                if field == "splits":
                    splits += len(value) * add
                else:
                    purges += len(value) * add
    return {"splits": splits, "purges": purges}


def _still_applicable(merge: ProposedMerge, snapshot: dict) -> Optional[str]:
    """Cheap pre-checks against one governance snapshot; None means "attempt the write".

    These are NOT the authority — `record_concept_alias` re-runs the equivalent checks inside its own
    lock, which is the only place they can be atomic with the append. Their job is to keep the common
    cases (already ratified, already governed by the operator) out of the receipt's decline list,
    where they would read as failures rather than as work that was already done.
    """
    aliases = snapshot["aliases"]
    if merge.source in aliases:
        # Either this decision is already in force, or the operator has governed this source
        # differently. Both mean "do not write"; only the first is worth calling applied.
        resolved = resolve_slug(merge.source, aliases)
        return "already_applied" if resolved == merge.target else "source_already_governed"
    if resolve_slug(merge.target, aliases) != merge.target:
        return "target_not_canonical"
    return None


def _decline_reason(exc: Exception) -> str:
    """Map a writer refusal onto a closed, content-free reason class for the receipt.

    The receipt is durable cross-run data an operator reads, so it carries the CLASS of refusal and
    never the exception text, which interpolates concept slugs and (for storage failures) paths.
    """
    if isinstance(exc, ConceptGovernanceGlobalConflict):
        return "revision_conflict"
    if isinstance(exc, GovernanceLedgerUnavailable):
        return "governance_unavailable"
    message = str(exc)
    for needle, reason in (
            ("would close a cycle", "would_close_cycle"),
            ("is purged", "purged"),
            ("does not exist in the current portfolio", "absent_from_portfolio"),
            ("is not live canonical", "target_not_canonical"),
            ("is aliased to", "source_already_governed"),
    ):
        if needle in message:
            return reason
    return "refused"


def _ratify_one(memory_dir, merge: ProposedMerge, *, by: str, at: str) -> tuple[str, dict]:
    """Apply one proposed merge under per-decision CAS. Returns `(outcome, detail)`.

    The CAS token is taken fresh for EACH decision and passed to the writer, which is the discipline
    whose absence retired this stage's predecessor (EM-14). A batch-level token would be worse than
    none: one concurrent operator edit would abort every remaining decision, and the natural fix for
    that — dropping the token — is the original defect. Per decision, a conflict costs exactly one
    re-derivation, and a decision another writer has meanwhile made is recognized as applied rather
    than fought over.
    """
    for attempt in range(_MAX_CAS_ATTEMPTS):
        snapshot = concept_governance_snapshot(memory_dir)
        skip = _still_applicable(merge, snapshot)
        if skip is not None:
            return skip, {}
        try:
            record = record_concept_alias(
                memory_dir, from_concept=merge.source, to_concept=merge.target,
                by=by, at=at, action_id=merge.action_id, require_existing=True,
                expected_governance_revision=snapshot["governance_revision"])
        except ConceptGovernanceGlobalConflict:
            # Someone wrote between our snapshot and our append. Re-derive once: the decision is
            # usually either now already in force (recognized by `_still_applicable` above) or still
            # perfectly valid against the new revision.
            if attempt + 1 < _MAX_CAS_ATTEMPTS:
                continue
            return "revision_conflict", {}
        except (ValueError, GovernanceLedgerUnavailable) as exc:
            return _decline_reason(exc), {}
        return "applied", {"revision": record.get("revision"),
                           "governance_revision": record.get("governance_revision")}
    return "revision_conflict", {}


def ratify_concept_merges(memory_dir, *, at: str = "", by: str = RATIFIER_ACTOR,
                          limit: int = MAX_RATIFICATIONS_PER_PASS,
                          dry_run: bool = False) -> dict:
    """The stage: apply every still-applicable agent-proposed merge, then leave a receipt.

    Returns `{"applied": [...], "skipped": [...], "pending": {...}, "receipt": bool}` where each
    entry is `{"from", "to", "outcome", ...}`. Never raises for an absent ledger; a POISONED
    governance ledger does raise, because "the policy is unreadable" must not be reported as "there
    was nothing to do" (the same fail-closed stance `read_curation_rows` takes).

    `dry_run` performs every read and pre-check and writes nothing at all — not even the receipt —
    so the operator's preview and the background pass share one code path rather than two that agree.
    """
    if not memory_dir:
        return {"applied": [], "skipped": [], "pending": {"splits": 0, "purges": 0},
                "receipt": False}
    merges = proposed_merges(memory_dir)
    applied: list[dict] = []
    skipped: list[dict] = []
    budget = max(0, int(limit))
    for merge in merges:
        entry = {"from": merge.source, "to": merge.target,
                 "proposal_key": merge.proposal_key, "why": merge.why}
        if dry_run:
            snapshot = concept_governance_snapshot(memory_dir)
            outcome = _still_applicable(merge, snapshot) or "would_apply"
            detail: dict = {}
        elif len(applied) >= budget:
            outcome, detail = "deferred_budget", {}
        else:
            outcome, detail = _ratify_one(memory_dir, merge, by=by, at=at)
        entry.update(detail)
        entry["outcome"] = outcome
        (applied if outcome == "applied" else skipped).append(entry)
    result = {"applied": applied, "skipped": skipped,
              "pending": pending_curation_work(memory_dir), "receipt": False}
    if not dry_run and (applied or skipped):
        # The receipt is written AFTER the decisions, and only describes what actually landed. A
        # receipt written first would be a claim, and this stage has nothing to claim — its
        # decisions are already idempotent. A failed receipt therefore costs audit detail and never
        # policy correctness, which is why it is best-effort.
        result["receipt"] = _append_ratification_receipt(memory_dir, result, by=by, at=at)
    return result


def _append_ratification_receipt(memory_dir, result: dict, *, by: str, at: str) -> bool:
    """Append one bounded audit row per pass to `concept_ratification_log.jsonl`.

    Audit output, not policy: nothing reads it to decide anything, `load_concept_aliases` never sees
    it, and it therefore uses `_append_governance`'s lenient receipt-log path rather than joining the
    strict operator-ledger vocabulary in `governance_health._PUBLIC_LEDGERS`. It carries no
    `action_id` on purpose — every pass is a new observation, even when it changed nothing.

    What it is FOR: the proposal provenance that deliberately does not ride on the alias row.
    `concept_aliases.jsonl` stays exactly the four-field policy record every reader already parses,
    and the join from a ratified alias back to the agent's reasoning is `action_id` -> this ledger.
    """
    path = Path(memory_dir) / RATIFICATION_LEDGER
    row = {
        "v": 1, "action": "concept-ratification", "by": by, "at": at,
        "applied": [{"from": e["from"], "to": e["to"], "action_id":
                     ratification_action_id(e["from"], e["to"]),
                     "proposal_key": e.get("proposal_key", ""), "why": e.get("why", ""),
                     "revision": e.get("revision"),
                     "governance_revision": e.get("governance_revision")}
                    for e in result["applied"]],
        "skipped": [{"from": e["from"], "to": e["to"], "outcome": e["outcome"]}
                    for e in result["skipped"]],
        "pending": result["pending"],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _append_governance(path, row, require_durable=True)
        return True
    except Exception:  # noqa: BLE001 — an audit row must never be able to fail a finalization
        return False


def read_ratification_receipts(memory_dir) -> list[dict]:
    """The audit rows this stage has written, oldest first ({} -> [] when absent)."""
    from looplab.events.eventstore import read_jsonl_lenient

    if not memory_dir:
        return []
    path = Path(memory_dir) / RATIFICATION_LEDGER
    if not path.exists():
        return []
    return [r for r in read_jsonl_lenient(path, loads=json.loads, dicts_only=True)
            if r.get("action") == "concept-ratification"]


def ratified_alias_sources(memory_dir) -> set[str]:
    """The alias sources whose CURRENT policy edge was written by this stage, not by an operator.

    The read side of "a governed merge and an invented one must never look alike": a surface that
    shows a merged concept can say WHO decided it. Last-write-per-source, exactly as
    `load_concept_aliases` projects the same file — an operator who overwrites a ratified edge owns
    it from then on, and one who clears it removes it from this set.
    """
    from looplab.engine.concept_registry import _read_alias_rows

    if not memory_dir:
        return set()
    path = Path(memory_dir) / "concept_aliases.jsonl"
    if not path.exists():
        return set()
    owner: dict[str, str] = {}
    for row in _read_alias_rows(path):
        source = normalize_key(row.get("from"))
        if not source:
            continue
        if str(row.get("action") or "legacy") == "clear":
            owner.pop(source, None)
            continue
        owner[source] = str(row.get("by") or "")
    return {source for source, actor in owner.items() if actor.startswith("concept-ratifier/")}
