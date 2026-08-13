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


class _Everything(frozenset):
    """A membership test that answers True for anything.

    Returned when a governance log cannot be read. Both governance reads NARROW what the cascade may
    delete — "these concepts were merged", "another run curated these task pools" — so an unreadable
    log must protect EVERYTHING, not nothing. Returning an empty set there would have made every
    concept look unmerged and every claim pool uncurated, i.e. an I/O error would silently WIDEN a
    destructive operation. A sentinel value inside an ordinary frozenset does not do this: the
    predicates ask "is this row's concept in the set", and a row's concepts are never the sentinel.
    """

    def __contains__(self, _item) -> bool:
        return True


# The one instance, so a caller can also test identity if it ever needs to distinguish
# "everything is protected because we are blind" from "everything really was merged".
_UNREADABLE_GOVERNANCE = _Everything()


class RunIdentity:
    """WHICH run a row belongs to — and `run_id` alone cannot answer it.

    `run_id` is the run DIRECTORY NAME (`orchestrator.py`: `self.run_dir.name`). It is reused the
    moment a run is deleted and re-created, it is `demo`/`baseline` on half the corpus, and it is
    identical across two checkouts sharing the default `~/.looplab/memory`.
    `engine/concept_capsules.py` states the rule outright: "`run_id` is only a run-root-local label…
    key by a persisted globally unique run-incarnation UID". Every writer already does —
    `memory.py`, `concept_capsules.py`, `claims.py`, `lesson_hygiene.py` all carry `run_uid`.

    Keying the cascade on `run_id` therefore meant deleting a DIFFERENT, still-existing run's memory,
    which is the one outcome this feature exists to prevent. So: match on `run_uid` whenever the row
    has one, and fall back to `run_id` ONLY for rows written before that field existed — where it is
    the best identity there is, and where the ambiguity is disclosed rather than hidden.
    """

    def __init__(self, run_id: str, run_uid: str = "") -> None:
        self.run_id = _text(run_id)
        self.run_uid = _text(run_uid)

    def owns(self, row: dict) -> bool:
        row_uid = _text(row.get("run_uid"))
        if row_uid:
            # A row that names its incarnation is matched ONLY on that. Falling back to `run_id` here
            # is what destroyed the other run's rows: two incarnations named `demo` are two runs.
            return bool(self.run_uid) and row_uid == self.run_uid
        # The LEGACY fallback, and it fires even for a uid-keyed caller — deliberately, because a
        # row written before `run_uid` existed has no other identity and would otherwise be
        # uncascadable forever. What it costs is real and is NOT hypothetical: two checkouts sharing
        # `~/.looplab/memory`, both holding a run directory named `demo`, and deleting the one that
        # has a uid also deletes the other's uid-less rows.
        #
        # That residual is disclosed rather than hidden — see `name_matched` on the receipt and
        # `identity: "mixed"`. Before, `identity` keyed on the CALLER alone and asserted `run_uid`
        # for a purge that had in fact matched rows by directory name, so the class docstring's
        # "the ambiguity is disclosed" promise held only in the all-legacy case.
        return bool(self.run_id) and _text(row.get("run_id")) == self.run_id

    def name_matched(self, row: dict) -> bool:
        """Was this row attributed by directory NAME while a uid was available to key on?

        The disclosure predicate. False for a legacy caller (`identity` already says `run_id`, so
        every match is by name and counting them adds nothing) and false for any row carrying a
        uid. True exactly for the mixed case the receipt used to misreport."""
        return (bool(self.run_uid) and not _text(row.get("run_uid"))
                and bool(self.run_id) and _text(row.get("run_id")) == self.run_id)

    @property
    def legacy_only(self) -> bool:
        """True when we could not learn this run's uid, so every match is by directory name alone."""
        return not self.run_uid


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def run_memory_identity(run_dir: str | Path, *, fallback_memory_dir: str = "") -> dict:
    """The run's own `{run_id, run_uid, memory_dir}`, read from the run directory.

    MUST be called BEFORE the run is deleted, and that is the whole reason it exists as a separate
    step. Both facts live in the run and nowhere else:

    * `run_uid` is stamped once into `run_started` and is the only globally unique name for this
      incarnation (see `RunIdentity`);
    * `memory_dir` is a per-RUN `Settings` field recorded in `config.snapshot.json`. Reading the
      SERVER's current global instead means a run launched with `--memory-dir`, or one predating a
      settings change, has its rows in a store the cascade never opens — while the cascade rewrites
      the current store, deleting whatever happens to match there.

    Returns the fallback memory dir when the snapshot names none, and an empty `run_uid` when the log
    predates the field — both disclosed, never guessed at.
    """
    rd = Path(run_dir)
    identity = {"run_id": _text(rd.name), "run_uid": "", "memory_dir": _text(fallback_memory_dir)}
    try:
        cfg = json.loads((rd / "config.snapshot.json").read_text(encoding="utf-8"))
        if isinstance(cfg, dict) and _text(cfg.get("memory_dir")):
            identity["memory_dir"] = _text(cfg["memory_dir"])
    except Exception:  # noqa: BLE001 — a missing/damaged snapshot falls back, it does not refuse
        pass
    # Through the shared reader and the type CONSTANT, not a hand-rolled scan for a byte literal.
    # `iter_event_jsonl` expands the batch envelopes `EventStore.append_many` writes (whose physical
    # `type` is a list, so a `b'"run_started"'` prefilter would skip one) and applies the torn-tail
    # rule; `EV_RUN_STARTED` is the registry spelling, which invariant 7 exists for — a typo'd
    # literal here does not fail, it silently yields no uid, and an empty uid is what degrades the
    # purge to matching a REUSED directory name. Today `run_started` is only ever written by a single
    # `append`, so the envelope case is not reachable; that is a property of the writer, not of this
    # reader, and it is not one this module should depend on.
    try:
        from looplab.events.eventstore import iter_event_jsonl
        from looplab.events.types import EV_RUN_STARTED
        for event in iter_event_jsonl(rd / "events.jsonl"):
            if (event or {}).get("type") == EV_RUN_STARTED:
                identity["run_uid"] = _text((event.get("data") or {}).get("run_uid"))
                break
    except Exception:  # noqa: BLE001 — same: no uid means legacy matching, not a refusal
        pass
    return identity


def known_memory_dirs(runs_root: str | Path, *, fallback_memory_dir: str = "") -> set[str]:
    """Every cross-run store this SERVER is configured to manage, resolved.

    The purge takes `memory_dir` from the request body, because after the run is deleted its own
    record is gone and the receipt is the only place the value survives (see `run_memory_identity`).
    That is a caller-supplied absolute PATH reaching a destructive rewrite: `purge_attributable_memory`
    checks only `base.is_dir()` and then rewrites five `.jsonl` files under it, and with no `run_uid`
    the ownership test degrades to matching a row's bare `run_id`. A body naming a directory this
    server was never told about could therefore delete another operator's legacy rows — which matters
    most on the shared-origin deployment `serve/server.py` itself warns about, where one browser
    principal reaches the control plane and the cross-origin guard does not apply. Every OTHER path
    input on that router goes through the deletion transaction's containment guard; this one had none,
    and a containment guard would not have helped anyway, since an absolute path to somebody else's
    store contains no traversal.

    So the store is checked against what the server itself can name, not against the shape of the
    string. A CROSS-RUN store is shared by construction, so the legitimate retry — finishing a cascade
    for a run launched with `--memory-dir` — still resolves as long as any surviving run cites it, and
    when none does the server refuses rather than guessing, which is the rule the route already states
    for identity.
    """
    known: set[str] = set()

    def _add(value: str) -> None:
        if not _text(value):
            return
        try:
            known.add(str(Path(value).resolve()))
        except Exception:  # noqa: BLE001 — an unresolvable spelling is simply not a known store
            pass

    _add(fallback_memory_dir)
    root = Path(runs_root)
    try:
        entries = sorted(root.iterdir()) if root.is_dir() else []
    except OSError:
        entries = []
    for rd in entries:
        if not rd.is_dir() or rd.name.startswith("."):
            continue
        try:
            cfg = json.loads((rd / "config.snapshot.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — a run with no readable snapshot names no store
            continue
        if isinstance(cfg, dict):
            _add(_text(cfg.get("memory_dir")))
    return known


def memory_dir_is_known(candidate: str, known: set[str]) -> bool:
    """Whether `candidate` resolves to one of `known`. Compared as RESOLVED paths, never as text —
    `/a/../a/memory`, `/a/memory/` and a symlinked spelling are the same store, and a purge that
    refused them would push an operator toward passing the one spelling that skipped the check."""
    if not _text(candidate):
        return False
    try:
        return str(Path(candidate).resolve()) in known
    except Exception:  # noqa: BLE001
        return False


class StoreUnreadable(Exception):
    """The store exists and could not be read. NOT the same as "it holds nothing of ours"."""


def _rows(path: Path, *, loads=json.loads) -> list[dict]:
    """Every store is read with the STDLIB parser, deliberately.

    `read_jsonl_lenient`'s contract is that a store must be parsed with the parser it was written
    with — and the two are not interchangeable in one direction only: stdlib accepts the
    `NaN`/`Infinity` literals stdlib emits and orjson rejects. For a pass that CLASSIFIES rows,
    stdlib is therefore the strictly safer choice: it can misread a row as understood, never as
    quarantine, and a row misread as quarantine is one this cascade silently fails to delete while
    reporting success. (Measured: `research_claims.jsonl` and `concept_capsules.jsonl` are
    stdlib-written — `claims.py`, `concept_capsules.py` — and were being read with orjson.)

    An OSError is RAISED, not swallowed. An unreadable store returning `[]` reads as "nothing of ours
    here" and the purge reports a clean success having done nothing — routing straight around the
    `failures[]` and the retry that exist for exactly this.
    """
    try:
        return [row for row in read_jsonl_lenient(path, loads=loads) if isinstance(row, dict)]
    except OSError as exc:
        raise StoreUnreadable(f"{type(exc).__name__}: {exc}") from exc


# ---------------------------------------------------------------------------------------------
# The per-tier rules. Each returns either "" (delete: this run alone owns it) or the REASON it is
# kept, phrased for the operator.
# ---------------------------------------------------------------------------------------------

def lesson_keep_reason(row: dict, run: "RunIdentity") -> str:
    """A lesson is this run's alone only if consolidation never folded another run into it.

    `evidence_count` counts DISTINCT contributing runs (`lesson_hygiene._accumulated_evidence`), so
    anything above 1 means the row speaks for runs beyond this one. `evidence_refs` names them
    directly when the lineage is traceable; `evidence_untraceable_count` records support whose
    lineage predates the field — support we cannot attribute, and therefore must not discard.
    """
    if not run.owns(row):
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
            # `run_uid` FIRST. `lesson_hygiene._evidence_lineage` writes BOTH on every ref precisely
            # so a reader can use the durable one, and reading `run_id` first meant a ref naming a
            # SURVIVING run by uid — but sharing this run's directory name — compared equal and the
            # row was deleted. That is "destroy corroboration earned by runs that still exist,
            # silently", i.e. the exact outcome the predicate exists to prevent.
            other = _text(ref.get("run_uid")) or _text(ref.get("run_id"))
            if not other:
                continue
            # REVIEW (mega-review 2026-08-13): the membership test below mixes namespaces — a
            # legacy ref that names an OLDER same-named incarnation by bare `run_id` compares equal
            # to `run.run_id` and is misread as a self-reference, so its corroboration is discarded
            # and (with evidence_count == 1 and no untraceable count) the row can be deleted despite
            # cross-run support. Narrow in practice (consolidation usually bumps the counters
            # first), but it is the residual of exactly the ref-ordering defect the comment above
            # records; a run_id-namespace ref should only match when this run itself is
            # legacy-keyed.
            # ONE rule: a ref naming any run that is not this one is corroboration, and a row with
            # corroboration survives. There used to be a `continue` above this line, guarded on
            # `other in {run.run_uid, run.run_id} and other == row's own uid` — unreachable BY
            # EFFECT, because the two branches are exhaustive on `other in {…}`: whenever that guard
            # held, the test below was already False and the loop iterated anyway. It read as a
            # load-bearing rule about a ref naming this run's own uid, which is precisely the case
            # the comment above says was destroying other runs' corroboration, so a maintainer
            # hardening this predicate would have been editing a clause that never ran.
            if other not in {run.run_uid, run.run_id}:
                return "consolidated: it carries evidence from other runs"
    try:
        if int(row.get("evidence_untraceable_count", 0) or 0) > 0:
            return "carries support whose run of origin is not recorded"
    except (TypeError, ValueError):
        return "carries support whose run of origin is not recorded"
    return ""


def note_keep_reason(row: dict, run: "RunIdentity") -> str:
    """A meta-note is one run's account of finishing. Nothing merges into it."""
    return "" if run.owns(row) else NOT_THIS_RUN


def case_keep_reason(row: dict, run: "RunIdentity") -> str:
    """A case row is one run's contribution to a task's champion group.

    Modern rows are per-`run_uid` contributions with an `active` winner chosen across the group
    (`memory.JsonlCaseLibrary._add_locked`), so removing this run's row can orphan the group's
    champion — `purge_attributable_memory` re-elects it rather than leaving a group with no active
    case.
    """
    return "" if run.owns(row) else NOT_THIS_RUN


def claim_keep_reason(row: dict, run: "RunIdentity", *, curated_tasks: frozenset[str]) -> str:
    """A claim row is this run's, unless another run has already curated the shared pool it is in.

    Claim curation adjudicates every claim for a `task_id` at once and stores only a digest of its
    input (`claim_curation_log.input_digest`). Removing a claim another run's decision was computed
    over would leave that decision unverifiable against the store it claims to describe.
    """
    if not run.owns(row):
        return NOT_THIS_RUN
    if _text(row.get("task_id")) in curated_tasks:
        return "another run's curation decision was computed over this claim pool"
    return ""


def capsule_keep_reason(row: dict, run: "RunIdentity", *, merged_concepts: frozenset[str]) -> str:
    """A concept capsule is this run's account of its own concepts — until governance merges them.

    Once a curation decision folds one concept id into another, the capsules holding those ids stop
    being a single run's private summary: the alias family they now belong to is shared, and the
    portfolio other runs read is computed from it.
    """
    if not run.owns(row):
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
    try:
        rows = _rows(Path(memory_dir) / "concept_curation_log.jsonl")
    except StoreUnreadable:
        # Same fail-closed rule: if we cannot see which concepts were merged, we must not conclude
        # that none were.
        return _UNREADABLE_GOVERNANCE
    for row in rows:
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


def _tasks_curated_by_other_runs(memory_dir: str | Path, run: "RunIdentity") -> frozenset[str]:
    """Task ids some OTHER run has curated. A governance log that cannot be read makes every task
    look uncurated, which would widen what the cascade deletes — so an unreadable log fails closed
    by returning nothing deletable rather than nothing protected."""
    try:
        rows = _rows(Path(memory_dir) / "claim_curation_log.jsonl")
    except StoreUnreadable:
        rows = None
    if rows is None:
        return _UNREADABLE_GOVERNANCE
    return frozenset(
        _text(row.get("task_id"))
        for row in rows
        if _text(row.get("task_id")) and not run.owns(row))


# ---------------------------------------------------------------------------------------------
# Survey and purge
# ---------------------------------------------------------------------------------------------

# (store file, operator label). One list, so the survey and the purge cannot walk different tiers.
CASCADED_TIERS: tuple[tuple[str, str], ...] = (
    ("lessons.jsonl", "lessons"),
    ("meta_notes.jsonl", "notes"),
    ("cases.jsonl", "cases"),
    ("research_claims.jsonl", "claims"),
    ("concept_capsules.jsonl", "concept capsules"),
)


def _tier_rules(memory_dir: str | Path, run: "RunIdentity") -> list[tuple[str, str, Callable]]:
    """(store file, label, keep-reason predicate) for every cascaded tier.

    The two governance reads are done HERE, once, and are deliberately passed down rather than
    re-read per tier: they decide what is off-limits, and re-reading them mid-purge would let a
    curation decision landing between two tiers apply to one and not the other.

    REVIEW (mega-review 2026-08-13): the single read is consistent ACROSS tiers but is taken
    without the curation logs' locks and BEFORE any store lock — a curation decision that lands
    after this read and before a tier's locked rewrite is invisible to the predicates, so a capsule
    whose concept was just merged (or a claim a just-landed decision was computed over) can still
    be deleted in that window. The lenient reader also skips a torn in-flight append row silently
    (only an OSError widens to `_UNREADABLE_GOVERNANCE`). Small window, destructive direction —
    worth either taking the curation-log locks around this read or re-checking under each store's
    lock.
    """
    merged = merged_concept_ids(memory_dir)
    curated = _tasks_curated_by_other_runs(memory_dir, run)
    predicates = {
        "lessons.jsonl": lesson_keep_reason,
        "meta_notes.jsonl": note_keep_reason,
        "cases.jsonl": case_keep_reason,
        "research_claims.jsonl":
            lambda row, ident: claim_keep_reason(row, ident, curated_tasks=curated),
        "concept_capsules.jsonl":
            lambda row, ident: capsule_keep_reason(row, ident, merged_concepts=merged),
    }
    return [(filename, label, predicates[filename]) for filename, label in CASCADED_TIERS]


def _tier_from_verdicts(verdicts: Iterable[tuple[dict, str]], run: "RunIdentity") -> dict:
    """The tier counts, from verdicts a caller has ALREADY computed.

    Split out because the purge needs the same per-row answer twice — once to count what it will
    delete, once to select the survivors it rewrites — and it was evaluating the predicate a second
    time over the whole store to get the second one, inside the interprocess lock every concurrent
    run's finalize is waiting on.

    `name_matched` counts the rows this tier attributed by directory NAME while a `run_uid` was
    available. It is the disclosure the class docstring promises: without it a purge that fell back
    to bare-name matching still reported `identity: "run_uid"`, so the one case where a shared store
    can lose another checkout's rows was the case the receipt described as exactly keyed.
    """
    deletable, kept, named, reasons = 0, 0, 0, {}
    for row, reason in verdicts:
        if not reason:
            deletable += 1
            if run.name_matched(row):
                named += 1
        elif reason != NOT_THIS_RUN:
            kept += 1
            reasons[reason] = reasons.get(reason, 0) + 1
    return {"deletable": deletable, "kept": kept, "name_matched": named,
            "reasons": [{"reason": r, "rows": n} for r, n in sorted(reasons.items())]}


def _survey_tier(rows: Iterable[dict], keep_reason: Callable, run: "RunIdentity") -> dict:
    return _tier_from_verdicts(((row, keep_reason(row, run)) for row in rows), run)


def _identity_label(run: "RunIdentity", name_matched: int) -> str:
    """What the receipt's `identity` field says this purge was really keyed on.

    Three values, not two. `mixed` is the one that was missing: a uid-keyed caller that nonetheless
    matched legacy rows by directory name reported `run_uid`, which is the label an operator reads
    as "this could not have touched another run"."""
    if run.legacy_only:
        return "run_id"
    return "mixed" if name_matched else "run_uid"


def attributable_memory(memory_dir: str | Path | None, run_id: str, run_uid: str = "") -> dict:
    """Read-only survey: what a cascade WOULD delete, and what it would keep and why.

    Read without the store locks on purpose. This is a preview shown before a confirmation, not a
    decision anything is fenced on — taking every mutable store's interprocess lock to draw a
    dialog would let one hung reader block every concurrent run's finalize. The purge re-reads
    under the lock and re-applies the same predicates, so the preview being a moment stale can only
    change the NUMBER the operator saw, never what the purge is allowed to touch.
    """
    run = RunIdentity(run_id, run_uid)
    base = Path(memory_dir) if memory_dir else None
    empty = {"schema": MEMORY_CASCADE_SCHEMA, "run_id": run.run_id, "run_uid": run.run_uid,
             "identity": _identity_label(run, 0), "available": False,
             "deletable": 0, "kept": 0, "name_matched": 0, "stores": [], "unreadable": [],
             "preserved": [{"store": s, "reason": r} for s, r in PRESERVED_TIERS]}
    if not run.run_id or base is None or not base.is_dir():
        return empty
    stores, unreadable, total_deletable, total_kept, total_named = [], [], 0, 0, 0
    for filename, label, keep_reason in _tier_rules(base, run):
        path = base / filename
        if not path.exists():
            continue
        try:
            rows = _rows(path)
        except StoreUnreadable as exc:
            # SAY SO. A store we could not read is not a store with nothing of ours in it, and the
            # difference decides whether the operator's "N rows" number means anything.
            unreadable.append({"store": label, "file": filename, "error": str(exc)[:200]})
            continue
        tier = _survey_tier(rows, keep_reason, run)
        total_deletable += tier["deletable"]
        total_kept += tier["kept"]
        total_named += tier["name_matched"]
        stores.append({"store": label, "file": filename, **tier})
    return {**empty, "available": True, "deletable": total_deletable, "kept": total_kept,
            "name_matched": total_named, "identity": _identity_label(run, total_named),
            "stores": stores, "unreadable": unreadable}


def _reelect_active_cases(rows: list[dict]) -> list[dict]:
    """Re-run the champion election over what survives, per (task_id, direction) group.

    `active` marks the best contribution in a group. Dropping a run's row can drop the group's only
    active member, and a task whose case bank has no champion is retrieved as if the task had never
    been solved — a silent regression for every run that still exists.

    REVIEW (mega-review 2026-08-13): this election runs over EVERY (task_id, direction) group of
    survivors, not only the groups this purge removed a row from — so a group that already had no
    active member before the cascade (e.g. left that way by an earlier partial rewrite) gets a
    member newly promoted, i.e. other runs' rows are semantically changed beyond the "this run
    alone" rule, and the rewritten row is not counted anywhere in the receipt. Protective
    direction, but it should be scoped to groups that actually lost a member here (and the
    promotion counted in the receipt).
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
        # No measured survivor still elects one — `JsonlCaseLibrary._add_locked` takes
        # `candidates[-1]` in exactly this case, because `valid_case_record` admits `metric=None`.
        # Leaving the group with nobody active makes it invisible to `search()`/`all()`, which filter
        # `active is not False` — i.e. the task reads as never solved, which is the precise outcome
        # this whole function exists to prevent.
        winner = (min(measured, key=lambda c: c["metric"]) if direction == "min"
                  else max(measured, key=lambda c: c["metric"])) if measured else group[-1]
        winner["active"] = True
        changed.append(winner)
    return changed


def unreadable_identity_receipt(run_id: str, reason: str) -> dict:
    """The cascade receipt for "this run's identity could not be established", built HERE.

    Same envelope as `purge_attributable_memory`'s, because the UI reads one shape for both
    (`memoryCascadeModel.cascadeOutcome` / `bulkOutcomeNotice`). The router used to hand-build all
    ten keys, which meant every field added to the real receipt — `memory_dir` was added precisely
    because a client could not retry without it — had to be remembered in a second place, and the
    failure path is the one where a missing field silently offers a retry that cannot succeed."""
    return {"schema": MEMORY_CASCADE_SCHEMA, "run_id": _text(run_id), "run_uid": "",
            "memory_dir": "", "identity": "unknown", "ok": False,
            "deleted": 0, "kept": 0, "stores": [],
            "failures": [{"store": "identity", "error": str(reason)}]}


def purge_attributable_memory(memory_dir: str | Path | None, run_id: str,
                              run_uid: str = "") -> dict:
    """Delete exactly the rows `attributable_memory` reports as deletable. Idempotent.

    Each store is rewritten under its own interprocess lock — the one its own writers take — with
    `replace_jsonl_rows_atomic_preserving_quarantine`, so every line this run does not solely own is
    retained BYTE FOR BYTE, including rows the reader could not parse. A cascade is not a licence to
    launder damage out of a shared store.

    Per-store failures are reported, not raised: the run itself is already gone by the time this
    runs, and a locked store is a reason to try again later, not a reason to report the deletion as
    failed. An UNREADABLE store is one of those failures — it used to read as "nothing of ours here"
    and produce a clean success having done nothing.
    """
    run = RunIdentity(run_id, run_uid)
    base = Path(memory_dir) if memory_dir else None
    # WHICH store this ran against, echoed for the same reason `run_uid` is. A retry arrives after the
    # run directory is gone and can only pass back what the receipt told it, and `memory_dir` is a
    # per-RUN `Settings` field — so echoing `run_uid` while omitting this is what let a faithful
    # client fall back to the SERVER's current global store, find no row carrying that uid, and get a
    # clean `ok: true, deleted: 0` while every lesson, case, note, claim and capsule the run wrote sat
    # untouched in the store it was actually launched with.
    result = {"schema": MEMORY_CASCADE_SCHEMA, "run_id": run.run_id, "run_uid": run.run_uid,
              "memory_dir": str(base) if base is not None else "",
              "identity": _identity_label(run, 0), "ok": True,
              "deleted": 0, "kept": 0, "name_matched": 0, "stores": [], "failures": []}
    if not run.run_id or base is None or not base.is_dir():
        result["ok"] = False
        result["failures"].append({"store": "memory_dir", "error": "no cross-run memory directory"})
        return result

    from looplab.events.eventstore import _interprocess_lock

    for filename, label, keep_reason in _tier_rules(base, run):
        path = base / filename
        if not path.exists():
            continue
        try:
            with _interprocess_lock(Path(f"{path}.lock"), required=True):
                rows = _rows(path)
                # ONE predicate pass. `keep_reason` reaches into `evidence_refs` and re-derives a
                # claim identity per row, and this runs while holding the store's interprocess lock.
                verdicts = [(row, keep_reason(row, run)) for row in rows]
                tier = _tier_from_verdicts(verdicts, run)
                if not tier["deletable"]:
                    result["kept"] += tier["kept"]
                    continue
                survivors = [row for row, reason in verdicts if reason]
                rewritten = (_reelect_active_cases(survivors)
                             if filename == "cases.jsonl" else [])
                # Two things go: the rows this run solely owns, and — for cases — the stale copies of
                # rows whose `active` we just re-elected, which are appended back in their new form.
                superseded = {id(row) for row in rewritten}
                kept_identity = {_row_identity(row) for row in survivors
                                 if id(row) not in superseded}
                replace_jsonl_rows_atomic_preserving_quarantine(
                    path, rewritten,
                    replace_if=lambda row: _row_identity(row) not in kept_identity,
                    loads=json.loads, dumps=json.dumps)
            result["deleted"] += tier["deletable"]
            result["kept"] += tier["kept"]
            result["name_matched"] += tier["name_matched"]
            result["identity"] = _identity_label(run, result["name_matched"])
            result["stores"].append({"store": label, "file": filename,
                                     "deleted": tier["deletable"], "kept": tier["kept"],
                                     "name_matched": tier["name_matched"]})
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
    "purge_attributable_memory", "merged_concept_ids", "run_memory_identity",
    "unreadable_identity_receipt",
    "known_memory_dirs", "memory_dir_is_known",
    "lesson_keep_reason", "note_keep_reason", "case_keep_reason",
    "claim_keep_reason", "capsule_keep_reason",
]
