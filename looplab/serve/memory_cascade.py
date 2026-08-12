"""What of the cross-run memory belongs to ONE run, and may therefore go when that run does.

Deleting a run removes its directory and nothing else: the lessons, cases, notes, claims and concept
capsules it contributed live in the shared cross-run store (`memory_dir`), keyed by `run_id`, and
they survive. That is the right default — the point of cross-run memory is that it outlives the run
— but it leaves the operator with rows whose provenance now points at nothing, and no way to clean
up after a toy or broken run without hand-editing JSONL.

So deletion may CASCADE, opt-in, under one rule:

    delete only what is attributable to this run ALONE.

The rule matters because these stores MERGE. A lesson row is not a run's private note: consolidation
(`lesson_hygiene.consolidate_lessons`) folds near-duplicates from several runs into one row that
keeps the NEWEST contributor's `run_id` and carries the others' support as `evidence_count` /
`evidence_refs`. Deleting such a row on the strength of its `run_id` would destroy corroboration
earned by runs that still exist — the store would silently lose evidence it never showed the
operator. The same shape appears in concept governance: once a curation decision merges
`optimization/analytical_solution` into `optimization/analytic`, the capsules carrying those
concepts are no longer a single run's account of its own work.

Hence every tier below states, in one predicate, what "this run alone" means for it, and everything
that fails the predicate is KEPT and COUNTED with a reason — a cascade that quietly skips rows is
indistinguishable from one that quietly deletes the wrong ones.

Two tiers are never cascaded at all:

* `skills/` — an auto-skill is promoted only once a SECOND, differently-fingerprinted task confirms
  it (`memory.write_auto_skill`), so a promoted skill is cross-run by construction, and its stored
  `fingerprints` list is the evidence. Candidates are left too: they are the record of what has been
  claimed once and awaits confirmation, and they name no run.
* the curation logs (`*_curation_log.jsonl`, `claim_decisions.jsonl`) — append-only audit. An audit
  that deletes its own entries is not an audit.

The purge is idempotent by construction: it is "remove every row attributable solely to R", so
running it twice is running it once, and a retry after a partial failure is safe.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable

from looplab.core.jsonlio import (
    read_jsonl_lenient, replace_jsonl_rows_atomic_preserving_quarantine)

# Bump when a tier's predicate changes meaning, so a stored receipt is never read under a rule it
# was not computed with.
MEMORY_CASCADE_SCHEMA = 1

# The stores that are never touched, and why — surfaced to the operator rather than left implicit.
PRESERVED_TIERS: tuple[tuple[str, str], ...] = (
    ("skills", "auto-skills are promoted only across two differently-fingerprinted tasks"),
    ("curation_logs", "append-only governance audit"),
)


# A row that was never this run's is not a "skip" — it is somebody else's row, and counting it as
# kept would tell the operator the cascade refused thousands of rows it was never asked about.
NOT_THIS_RUN = "written by another run"


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _rows(path: Path, *, loads=json.loads) -> list[dict]:
    try:
        return [row for row in read_jsonl_lenient(path, loads=loads) if isinstance(row, dict)]
    except Exception:  # noqa: BLE001 — an unreadable store contributes nothing and blocks nothing
        return []


# ---------------------------------------------------------------------------------------------
# The per-tier rules. Each returns either "" (delete: this run alone owns it) or the REASON it is
# kept, phrased for the operator.
# ---------------------------------------------------------------------------------------------

def lesson_keep_reason(row: dict, run_id: str) -> str:
    """A lesson is this run's alone only if consolidation never folded another run into it.

    `evidence_count` counts DISTINCT contributing runs (`lesson_hygiene._accumulated_evidence`), so
    anything above 1 means the row speaks for runs beyond this one. `evidence_refs` names them
    directly when the lineage is traceable; `evidence_untraceable_count` records support whose
    lineage predates the field — support we cannot attribute, and therefore must not discard.
    """
    if _text(row.get("run_id")) != run_id:
        return NOT_THIS_RUN
    try:
        evidence = int(row.get("evidence_count", 1) or 1)
    except (TypeError, ValueError):
        evidence = 2                                   # unreadable support is not absent support
    if evidence > 1:
        return "consolidated: it carries evidence from other runs"
    refs = row.get("evidence_refs")
    if isinstance(refs, list):
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            other = _text(ref.get("run_id")) or _text(ref.get("run_uid"))
            if other and other != run_id and other != _text(row.get("run_uid")):
                return "consolidated: it carries evidence from other runs"
    try:
        if int(row.get("evidence_untraceable_count", 0) or 0) > 0:
            return "carries support whose run of origin is not recorded"
    except (TypeError, ValueError):
        return "carries support whose run of origin is not recorded"
    return ""


def note_keep_reason(row: dict, run_id: str) -> str:
    """A meta-note is one run's account of finishing. Nothing merges into it."""
    return "" if _text(row.get("run_id")) == run_id else NOT_THIS_RUN


def case_keep_reason(row: dict, run_id: str) -> str:
    """A case row is one run's contribution to a task's champion group.

    Modern rows are per-`run_uid` contributions with an `active` winner chosen across the group
    (`memory.JsonlCaseLibrary._add_locked`), so removing this run's row can orphan the group's
    champion — `purge_attributable_memory` re-elects it rather than leaving a group with no active
    case.
    """
    return "" if _text(row.get("run_id")) == run_id else NOT_THIS_RUN


def claim_keep_reason(row: dict, run_id: str, *, curated_tasks: frozenset[str]) -> str:
    """A claim row is this run's, unless another run has already curated the shared pool it is in.

    Claim curation adjudicates every claim for a `task_id` at once and stores only a digest of its
    input (`claim_curation_log.input_digest`). Removing a claim another run's decision was computed
    over would leave that decision unverifiable against the store it claims to describe.
    """
    if _text(row.get("run_id")) != run_id:
        return NOT_THIS_RUN
    if _text(row.get("task_id")) in curated_tasks:
        return "another run's curation decision was computed over this claim pool"
    return ""


def capsule_keep_reason(row: dict, run_id: str, *, merged_concepts: frozenset[str]) -> str:
    """A concept capsule is this run's account of its own concepts — until governance merges them.

    Once a curation decision folds one concept id into another, the capsules holding those ids stop
    being a single run's private summary: the alias family they now belong to is shared, and the
    portfolio other runs read is computed from it.
    """
    if _text(row.get("run_id")) != run_id:
        return NOT_THIS_RUN
    concepts = row.get("concepts")
    if isinstance(concepts, list):
        for concept in concepts:
            if _text(concept) in merged_concepts:
                return "its concepts were merged into a shared concept family"
    return ""


def merged_concept_ids(memory_dir: str | Path) -> frozenset[str]:
    """Every concept id a curation decision has moved, in either direction.

    Both ends count: the source id disappears into the target, and the target now stands for more
    than the run that coined it. Splits and purges are governance edits to the shared vocabulary for
    the same reason.
    """
    ids: set[str] = set()
    for row in _rows(Path(memory_dir) / "concept_curation_log.jsonl"):
        proposals = row.get("proposals")
        if not isinstance(proposals, dict):
            continue
        for key in ("merges", "splits", "purges"):
            for item in proposals.get(key) or []:
                if not isinstance(item, dict):
                    continue
                for field in ("from_concept", "to_concept", "concept"):
                    value = _text(item.get(field))
                    if value:
                        ids.add(value)
                for value in item.get("into") or []:
                    if _text(value):
                        ids.add(_text(value))
    return frozenset(ids)


def _tasks_curated_by_other_runs(memory_dir: str | Path, run_id: str) -> frozenset[str]:
    return frozenset(
        _text(row.get("task_id"))
        for row in _rows(Path(memory_dir) / "claim_curation_log.jsonl")
        if _text(row.get("task_id")) and _text(row.get("run_id")) != run_id)


# ---------------------------------------------------------------------------------------------
# Survey and purge
# ---------------------------------------------------------------------------------------------

def _tier_rules(memory_dir: str | Path, run_id: str) -> list[tuple[str, str, Callable, Any]]:
    """(store file, label, keep-reason predicate, json loader) for every cascaded tier.

    The loader is not decoration: `read_jsonl_lenient`'s docstring is explicit that a store must be
    parsed with the parser it was WRITTEN with. Cases are stdlib-written (`JsonlCaseLibrary` passes
    `json.loads`/`json.dumps`); the rest are orjson-written. Parsing one with the other can classify
    a readable row as quarantine and vice versa.
    """
    import orjson

    merged = merged_concept_ids(memory_dir)
    curated = _tasks_curated_by_other_runs(memory_dir, run_id)
    return [
        ("lessons.jsonl", "lessons", lesson_keep_reason, orjson.loads),
        ("meta_notes.jsonl", "notes", note_keep_reason, orjson.loads),
        ("cases.jsonl", "cases", case_keep_reason, json.loads),
        ("research_claims.jsonl", "claims",
         lambda row, rid: claim_keep_reason(row, rid, curated_tasks=curated), orjson.loads),
        ("concept_capsules.jsonl", "concept capsules",
         lambda row, rid: capsule_keep_reason(row, rid, merged_concepts=merged), orjson.loads),
    ]


def _survey_tier(rows: Iterable[dict], keep_reason: Callable, run_id: str) -> dict:
    deletable, kept, reasons = 0, 0, {}
    for row in rows:
        reason = keep_reason(row, run_id)
        if not reason:
            deletable += 1
        elif reason != NOT_THIS_RUN:
            kept += 1
            reasons[reason] = reasons.get(reason, 0) + 1
    return {"deletable": deletable, "kept": kept,
            "reasons": [{"reason": r, "rows": n} for r, n in sorted(reasons.items())]}


def attributable_memory(memory_dir: str | Path | None, run_id: str) -> dict:
    """Read-only survey: what a cascade WOULD delete, and what it would keep and why.

    Read without the store locks on purpose. This is a preview shown before a confirmation, not a
    decision anything is fenced on — taking every mutable store's interprocess lock to draw a
    dialog would let one hung reader block every concurrent run's finalize. The purge re-reads
    under the lock and re-applies the same predicates, so the preview being a moment stale can only
    change the NUMBER the operator saw, never what the purge is allowed to touch.
    """
    run = _text(run_id)
    base = Path(memory_dir) if memory_dir else None
    empty = {"schema": MEMORY_CASCADE_SCHEMA, "run_id": run, "available": False,
             "deletable": 0, "kept": 0, "stores": [],
             "preserved": [{"store": s, "reason": r} for s, r in PRESERVED_TIERS]}
    if not run or base is None or not base.is_dir():
        return empty
    stores, total_deletable, total_kept = [], 0, 0
    for filename, label, keep_reason, loads in _tier_rules(base, run):
        path = base / filename
        if not path.exists():
            continue
        tier = _survey_tier(_rows(path, loads=loads), keep_reason, run)
        total_deletable += tier["deletable"]
        total_kept += tier["kept"]
        stores.append({"store": label, "file": filename, **tier})
    return {**empty, "available": True, "deletable": total_deletable, "kept": total_kept,
            "stores": stores}


def _reelect_active_cases(rows: list[dict]) -> list[dict]:
    """Re-run the champion election over what survives, per (task_id, direction) group.

    `active` marks the best contribution in a group. Dropping a run's row can drop the group's only
    active member, and a task whose case bank has no champion is retrieved as if the task had never
    been solved — a silent regression for every run that still exists.
    """
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        if "active" not in row:
            continue                                   # legacy single-slot row: no election exists
        groups.setdefault((row.get("task_id"), row.get("direction", "min")), []).append(row)
    changed: list[dict] = []
    for (_task, direction), group in groups.items():
        if any(row.get("active") for row in group):
            continue
        measured = [row for row in group if isinstance(row.get("metric"), (int, float))
                    and not isinstance(row.get("metric"), bool)]
        if not measured:
            continue
        winner = (min(measured, key=lambda c: c["metric"]) if direction == "min"
                  else max(measured, key=lambda c: c["metric"]))
        winner["active"] = True
        changed.append(winner)
    return changed


def purge_attributable_memory(memory_dir: str | Path | None, run_id: str) -> dict:
    """Delete exactly the rows `attributable_memory` reports as deletable. Idempotent.

    Each store is rewritten under its own interprocess lock — the one its own writers take — with
    `replace_jsonl_rows_atomic_preserving_quarantine`, so every line this run does not solely own is
    retained BYTE FOR BYTE, including rows the reader could not parse. A cascade is not a licence to
    launder damage out of a shared store.

    Per-store failures are reported, not raised: the run itself is already gone by the time this
    runs, and a locked store is a reason to try again later, not a reason to report the deletion as
    failed.
    """
    run = _text(run_id)
    base = Path(memory_dir) if memory_dir else None
    result = {"schema": MEMORY_CASCADE_SCHEMA, "run_id": run, "ok": True,
              "deleted": 0, "kept": 0, "stores": [], "failures": []}
    if not run or base is None or not base.is_dir():
        result["ok"] = False
        result["failures"].append({"store": "memory_dir", "error": "no cross-run memory directory"})
        return result

    from looplab.events.eventstore import _interprocess_lock

    for filename, label, keep_reason, loads in _tier_rules(base, run):
        path = base / filename
        if not path.exists():
            continue
        dumps = json.dumps if loads is json.loads else None
        try:
            with _interprocess_lock(Path(f"{path}.lock"), required=True):
                rows = _rows(path, loads=loads)
                tier = _survey_tier(rows, keep_reason, run)
                if not tier["deletable"]:
                    result["kept"] += tier["kept"]
                    continue
                survivors = [row for row in rows if keep_reason(row, run)]
                rewritten = (_reelect_active_cases(survivors)
                             if filename == "cases.jsonl" else [])
                # Two things go: the rows this run solely owns, and — for cases — the stale copies of
                # rows whose `active` we just re-elected, which are appended back in their new form.
                superseded = {id(row) for row in rewritten}
                kept_identity = {_row_identity(row) for row in survivors
                                 if id(row) not in superseded}
                codec = {"loads": loads} if dumps is None else {"loads": loads, "dumps": dumps}
                replace_jsonl_rows_atomic_preserving_quarantine(
                    path, rewritten,
                    replace_if=lambda row: _row_identity(row) not in kept_identity,
                    **codec)
            result["deleted"] += tier["deletable"]
            result["kept"] += tier["kept"]
            result["stores"].append({"store": label, "file": filename,
                                     "deleted": tier["deletable"], "kept": tier["kept"]})
        except Exception as exc:  # noqa: BLE001 — one locked store must not hide the others' work
            result["ok"] = False
            result["failures"].append({"store": label, "file": filename,
                                       "error": f"{type(exc).__name__}: {exc}"[:200]})
    return result


def _row_identity(row: Any) -> str:
    """A stable identity for a decoded row, so the rewrite can name exactly which lines to drop.

    Content-addressed rather than positional: `replace_jsonl_rows_atomic_preserving_quarantine`
    re-reads and re-decodes the file itself, so the predicate it calls sees rows we cannot match by
    object identity. Sorted keys make two encodings of one row agree.
    """
    if not isinstance(row, dict):
        return ""
    try:
        return json.dumps(row, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001 — an unencodable row is not one we claim to own
        return ""


__all__ = [
    "MEMORY_CASCADE_SCHEMA", "NOT_THIS_RUN", "PRESERVED_TIERS", "attributable_memory",
    "purge_attributable_memory", "merged_concept_ids",
    "lesson_keep_reason", "note_keep_reason", "case_keep_reason",
    "claim_keep_reason", "capsule_keep_reason",
]
