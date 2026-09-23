"""EVERY cross-run memory store, one row each: what lives under `memory_dir`, what one of its rows is
keyed by, who writes it, and whether a run's deletion may take it (review 2026-09-22, ENG3-07).

`memory_dir` holds sixteen JSONL stores, the `skills/` tree, an abstraction cache and the paid-
curation claim receipts, and until this module NO module listed them. Five partial lists did, each
for its own purpose, and each was complete only for that purpose: `governance_health` named seven
ledgers (and the three governed sources a second time), `cross_run_context` the same three sources,
`serve/memory_cascade.py` five cascaded stores and two preserved groups — and the literal
`"lessons.jsonl"` was spelled in sixteen modules.

MEASURED on the tree this landed on, what that cost. Nine entries were in NEITHER deletion tier.
Two of them are a run's own measurements and name it on every row — `lesson_utility.jsonl`
(`run_id`/`run_uid`, one row per lesson a run's prior showed) and `regime_contrast.jsonl` (the run's
stamp beside its regime medians) — so deleting a run WITH the memory cascade left its citation
counts steering every later run's `lesson_rank_key` / `filter_useless` and its measurements steering
`regime_prior`, on the word of a run nobody could inspect any more; and `memory-orphans`, which
walked the same five-store list, could not see them either. The other seven
(`concept_ratification_log`, `task_facets`, `concept_aliases`, `concept_splits`, `exploits`, the
abstraction cache and the claim receipts) were never cascaded — correctly — but for a reason
nothing had written down, which is indistinguishable from having been forgotten.

ONE ROW PER STORE, and the partial lists are VIEWS of it, keeping their names so no caller churns:
`cascaded_tiers()` is `memory_cascade.CASCADED_TIERS`, `preserved_tiers()` its `PRESERVED_TIERS`,
`governance_ledger_files()` / `curation_ledger_scopes()` are `governance_health`'s
`_GOVERNANCE_LEDGER_FILES` / `_CURATION_LEDGER_SCOPES`, and `governed_source_names()` is both
`governance_health._GOVERNED_SOURCE_NAMES` and `cross_run_context.CROSS_RUN_SOURCE_NAMES`. A store
therefore gains a deletion policy, a strict-ledger name or a governed-source flag in exactly one
place. `tests/test_memory_stores.py` holds the registry to the tree BOTH ways: a name joined onto a
`memory_dir` path (or any bare `*.jsonl` literal) with no row here is red, and so is a row whose
declared `writer` no longer exists, no longer writes, or no longer names its store.

THE DELETION POLICY is the cascade's contract, stated per store:

* CASCADED — every row names the run that wrote it (`run_uid`; `run_id` on a row written before the
  uid existed — `core/run_identity.py::row_belongs_to_run`), and `serve/memory_cascade.py` holds one
  keep-predicate per cascaded store saying what "this run ALONE" means there. A cascaded store whose
  rows named no run could never be matched, so its purge would report a clean success having done
  nothing; the registry refuses that combination (`names_run`).
* PRESERVED — never touched by a deletion or by `memory-orphans`, with the REASON stated, because
  "the cascade skips this store" and "the cascade forgot this store" must not read alike. Rows
  sharing one reason share a `group`, and the group is what the operator is shown.

Pure data: this module imports nothing from `looplab`, so `governance_health` — an engine LEAF that
`tools/` imports at module level (`tests/test_package_layering.py::LEAF_ONLY`) — derives its tables
from it without pulling anything new into that import graph.
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "CASCADED", "PRESERVED", "POLICIES", "MemoryStore", "MEMORY_STORES", "memory_store",
    "cascaded_tiers", "preserved_tiers", "preserved_files", "governance_ledger_files",
    "curation_ledger_scopes", "governed_source_names",
]

CASCADED = "cascaded"
PRESERVED = "preserved"
POLICIES = (CASCADED, PRESERVED)


@dataclass(frozen=True)
class MemoryStore:
    """One entry directly under `memory_dir`.

    `name` is the file (or, for `skills`, directory) name. `label` is the operator's word for it on
    a survey or purge receipt. `key` is what ONE row is keyed by, as its writer stamps it — prose,
    because the keys are heterogeneous (a run, a claim uid, a content digest) and the one fact a
    machine must read off it is carried separately as `names_run`: whether a row names the run that
    wrote it, which is the precondition for cascading at all. `reason` is, for a CASCADED store, the
    rule that nonetheless KEEPS one of the run's rows (empty when nothing does) and, for a PRESERVED
    one, why the store is never touched. `writer` cites the function that writes it, as
    `<module>.py::<qualname>` — re-derived by `tests/test_memory_stores.py`, so it cannot outlive a
    rename. `ledger` is the strict governance ledger's public name (`governance_health`),
    `curation_kind` the paid steward whose history the file is, and `governed_source` marks the
    three evidence stores every live cross-run projection reads under the governance locks.
    """

    name: str
    label: str
    policy: str
    key: str
    names_run: bool
    reason: str
    writer: str
    group: str = ""
    ledger: str = ""
    curation_kind: str = ""
    governed_source: bool = False


_RUN = "run_uid (run_id on a row written before the uid existed)"

MEMORY_STORES: tuple[MemoryStore, ...] = (
    # ---- CASCADED: a run's deletion takes the rows it alone wrote. One keep-predicate each in
    # `serve/memory_cascade.py`; the order here is the order a survey and a purge walk them.
    MemoryStore(
        "lessons.jsonl", "lessons", CASCADED, key=f"{_RUN}; lesson_id", names_run=True,
        reason="kept when consolidation folded another run's evidence into it",
        writer="engine/lessons.py::LessonMemory.append_lessons", governed_source=True),
    MemoryStore(
        "meta_notes.jsonl", "notes", CASCADED, key=f"{_RUN}; finish_seq", names_run=True,
        reason="", writer="engine/lessons_distill.py::LessonDistillMixin.write_reflection_note"),
    MemoryStore(
        "cases.jsonl", "cases", CASCADED, key=f"{_RUN}, per (task_id, direction) group",
        names_run=True, reason="",
        writer="engine/lessons.py::LessonMemory.store_case"),
    MemoryStore(
        "research_claims.jsonl", "claims", CASCADED, key=_RUN, names_run=True,
        reason="kept when another run's curation decision was computed over its claim pool",
        writer="engine/claims.py::record_research_claims", governed_source=True),
    MemoryStore(
        "concept_capsules.jsonl", "concept capsules", CASCADED, key=_RUN, names_run=True,
        reason="kept when any of its concepts was merged into a shared concept family",
        writer="engine/lessons.py::LessonMemory.store_concept_capsule", governed_source=True),
    MemoryStore(
        "lesson_utility.jsonl", "lesson utility", CASCADED, key=f"{_RUN}; lesson_id",
        names_run=True, reason="",
        writer="engine/lessons_distill.py::LessonDistillMixin.write_reflection_note"),
    MemoryStore(
        "regime_contrast.jsonl", "regime contrasts", CASCADED,
        key=f"{_RUN}; task_id; seeded_from on a row seeded from an archived log", names_run=True,
        reason="kept when it was seeded from an archived run's log rather than written by a run",
        writer="engine/lessons_distill.py::LessonDistillMixin.write_reflection_note"),
    # ---- PRESERVED: never touched by a deletion or by `memory-orphans`.
    MemoryStore(
        "skills", "skills", PRESERVED,
        key="statement digest; the confirming task fingerprints in its frontmatter",
        names_run=False, group="skills",
        reason="auto-skills are promoted only across two differently-fingerprinted tasks",
        writer="engine/lessons_distill.py::LessonDistillMixin.promote_settled_skills"),
    MemoryStore(
        "concept_curation_log.jsonl", "concept curation log", PRESERVED,
        key="curation_key (the paid input's digest); run_id of the finalize that paid",
        names_run=True, group="curation_logs", reason="append-only governance audit",
        writer="engine/curation_protocol.py::CurationProtocolMixin._append_curation_once",
        ledger="concept_curation", curation_kind="concept"),
    MemoryStore(
        "claim_curation_log.jsonl", "claim curation log", PRESERVED,
        key="curation_key (the paid input's digest); run_id of the finalize that paid",
        names_run=True, group="curation_logs", reason="append-only governance audit",
        writer="engine/curation_protocol.py::CurationProtocolMixin._append_curation_once",
        ledger="claim_curation", curation_kind="claim"),
    MemoryStore(
        "task_facets_curation_log.jsonl", "facets curation log", PRESERVED,
        key="curation_key (per task); run_id of the finalize that paid",
        names_run=True, group="curation_logs", reason="append-only governance audit",
        writer="engine/curation_protocol.py::CurationProtocolMixin._append_curation_once",
        ledger="task_facets_curation", curation_kind="facets"),
    MemoryStore(
        "concept_ratification_log.jsonl", "ratification log", PRESERVED,
        key="none: one observation per ratification pass (by, at)", names_run=False,
        group="curation_logs", reason="append-only governance audit",
        writer="engine/concept_tidy.py::_append_ratification_receipt"),
    MemoryStore(
        "concept_aliases.jsonl", "concept aliases", PRESERVED,
        key="action_id + revision, per source concept", names_run=False,
        group="governance_ledgers",
        reason="operator policy, keyed by the concept, claim or task it governs — no run wrote it",
        writer="engine/concept_registry.py::record_concept_alias", ledger="concept_aliases"),
    MemoryStore(
        "concept_splits.jsonl", "concept splits", PRESERVED,
        key="action_id + revision, per source concept", names_run=False,
        group="governance_ledgers",
        reason="operator policy, keyed by the concept, claim or task it governs — no run wrote it",
        writer="engine/concept_registry.py::record_concept_split", ledger="concept_splits"),
    MemoryStore(
        "claim_decisions.jsonl", "claim decisions", PRESERVED,
        key="action_id + revision, per claim_uid", names_run=False,
        group="governance_ledgers",
        reason="operator policy, keyed by the concept, claim or task it governs — no run wrote it",
        writer="engine/claims.py::record_claim_decision", ledger="claim_decisions"),
    MemoryStore(
        "task_facets.jsonl", "task facets", PRESERVED,
        key="task_id (last write wins) + revision", names_run=False,
        group="governance_ledgers",
        reason="operator policy, keyed by the concept, claim or task it governs — no run wrote it",
        writer="engine/task_facets.py::record_task_facets", ledger="task_facets"),
    MemoryStore(
        ".curation_invocations", "curation receipts", PRESERVED,
        key="digest of (ledger, curation_key); run_id of the finalize that claimed it",
        names_run=True, group="curation_receipts",
        reason=("at-most-once receipts for paid steward calls: removing one lets a settled paid "
                "call be bought again"),
        writer="engine/curation_protocol.py::CurationProtocolMixin._write_curation_claim"),
    MemoryStore(
        "exploits.jsonl", "exploit suite", PRESERVED, key="the rule's regex pattern",
        names_run=False, group="exploit_suite",
        reason="the reward-hack ruleset `looplab harden` grows: operator-built, written by no run",
        writer="cli/export_cmds.py::harden"),
    MemoryStore(
        "memora_cache.json", "abstraction cache", PRESERVED,
        key="content hash of the abstracted text", names_run=False, group="abstraction_cache",
        reason="a content-addressed cache of model-written abstractions: no entry names a run",
        writer="tools/memora.py::CachedAbstractor._persist"),
)


def memory_store(name: str) -> MemoryStore:
    """The registry row for one store name. KeyError on a name nothing registered."""
    for store in MEMORY_STORES:
        if store.name == name:
            return store
    raise KeyError(name)


def cascaded_tiers() -> tuple[tuple[str, str], ...]:
    """`(file, label)` for every CASCADED store, in registry order (`memory_cascade.CASCADED_TIERS`)."""
    return tuple((s.name, s.label) for s in MEMORY_STORES if s.policy == CASCADED)


def preserved_tiers() -> tuple[tuple[str, str], ...]:
    """`(group, reason)` once per PRESERVED group, in first-appearance order
    (`memory_cascade.PRESERVED_TIERS`). One reason per group is a registry rule, not a guess here."""
    groups: dict[str, str] = {}
    for store in MEMORY_STORES:
        if store.policy == PRESERVED:
            groups.setdefault(store.group, store.reason)
    return tuple(groups.items())


def preserved_files(group: str) -> tuple[str, ...]:
    """The store names one PRESERVED group discloses."""
    return tuple(s.name for s in MEMORY_STORES if s.policy == PRESERVED and s.group == group)


def governance_ledger_files() -> dict[str, str]:
    """`{file: public ledger name}` for every strict governance ledger
    (`governance_health._GOVERNANCE_LEDGER_FILES`)."""
    return {s.name: s.ledger for s in MEMORY_STORES if s.ledger}


def curation_ledger_scopes() -> dict[str, tuple[str, str]]:
    """`{file: (steward kind, public ledger name)}` for every paid-curation history
    (`governance_health._CURATION_LEDGER_SCOPES`)."""
    return {s.name: (s.curation_kind, s.ledger) for s in MEMORY_STORES if s.curation_kind}


def governed_source_names() -> tuple[str, ...]:
    """The evidence stores every live cross-run projection reads under the governance locks, sorted
    (`cross_run_context.CROSS_RUN_SOURCE_NAMES`, `governance_health._GOVERNED_SOURCE_NAMES`)."""
    return tuple(sorted(s.name for s in MEMORY_STORES if s.governed_source))
