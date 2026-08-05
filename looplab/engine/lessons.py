"""Cross-run memory / lessons / reflection for the engine (extracted from orchestrator.py):
the E4 meta-note prior, M2/M3 fingerprint-keyed lessons (incl. negatives), M6 comparative
(credit-assigned pair) lessons with their mid-run distill/refresh cadences, M4 auto-distilled
skills, D2 store hygiene (consolidate/compact), and the I19 case library write.

`LessonMemory` wraps the engine instance (`self._e`) rather than owning copies of its state:
the method bodies are verbatim moves from the Engine, reading the engine's knobs/store/task
through `self._e` and calling sibling cluster methods through the Engine's thin delegators
(so a test monkeypatching e.g. `engine._reflect_client` still intercepts every internal call).
Only the purely lessons-owned mutable state (`seen_stamp`, `prior_note_text`) lives here; the
Engine exposes them back under the original attribute names via properties.

Decomposed the same way the Engine was (see engine/novelty.py's mixin convention): the prior
loading (lessons_priors.py), LLM distillation (lessons_distill.py) and comparative/reconcile
(lessons_reconcile.py) clusters are MIXINS on `LessonMemory` — verbatim method moves, `self`
there IS the LessonMemory, zero call-site churn. This module keeps the constructor/owned state,
the store append + the maybe_* cadence wrappers, and the static file maintenance; the Engine's
CLASS-attribute refs (`LessonMemory.spent_pairs` / `consolidate_lessons_file` /
`compact_lessons`) keep resolving through mixin inheritance.

Layering: this module must not import the orchestrator (TYPE_CHECKING only) and never imports
serve — it touches only engine.memory, events, core and stdlib/orjson."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING

import orjson

from looplab.core.atomicio import append_jsonl_bytes_locked
from looplab.core.models import (
    NODE_CONCEPT_PROVENANCE_CLASSIFIER,
    RunState,
    latest_lesson_node_count,
    valid_concept_id,
)
from looplab.engine.concept_registry import normalize_key
from looplab.engine.lessons_distill import LessonDistillMixin
# The role constants moved to lessons_priors.py with the prior renderer that filters on them;
# re-imported so `from looplab.engine.lessons import LESSON_ROLE_*` (tests, cross-run tooling)
# keeps resolving.
from looplab.engine.lessons_priors import (  # noqa: F401
    LESSON_ROLE_DEVELOPER, LESSON_ROLE_RESEARCHER, LessonPriorsMixin)
from looplab.engine.lessons_reconcile import LessonReconcileMixin
from looplab.engine.memory import JsonlCaseLibrary
from looplab.events.replay import fold
from looplab.events.types import (
    EV_LESSONS_DISTILLED, EV_LESSONS_REFRESHED, EV_LESSONS_STORE_UNAVAILABLE)
from looplab.core.jsonutil import valid_digest_ref

if TYPE_CHECKING:  # engine type hint only — no runtime import of the orchestrator
    from looplab.engine.orchestrator import Engine


_CURATION_CLAIM_DIR = ".curation_invocations"
_CURATION_CLAIM_MAX_BYTES = 16 * 1024
_FINALIZE_STEWARD_PARSER = "tool_call_once"
# Soft cap on `.curation_invocations/`. `_interprocess_lock` opens (creates) a `<name>.lock` per paid
# decision and never unlinks it, and the concept/claim curation keys carry the EVOLVING portfolio digest,
# so the scratch dir would otherwise accrete a lock file per finalize forever. Past this cap we best-effort
# prune the oldest ORPHAN lock files (no matching `.json` recovery claim). Claim `.json` markers are durable
# crash-recovery state and are never pruned here.
_CURATION_SCRATCH_MAX_ENTRIES = 512
# Never prune a lock younger than a finalize's worst-case wall-clock, so a GC pass can never unlink a lock
# an in-flight decision on another process still holds (the paid LLM call runs inside the lock).
_CURATION_SCRATCH_MIN_AGE_S = 6 * 3600
_CURATION_THREAD_LOCKS: dict[str, tuple[threading.Lock, int]] = {}
_CURATION_THREAD_LOCKS_GUARD = threading.Lock()


@contextmanager
def _curation_thread_lock(key: str):
    """Serialize one semantic curation claim locally without retaining an unbounded lock registry."""
    with _CURATION_THREAD_LOCKS_GUARD:
        lock, users = _CURATION_THREAD_LOCKS.get(key, (threading.Lock(), 0))
        _CURATION_THREAD_LOCKS[key] = (lock, users + 1)
    try:
        with lock:
            yield
    finally:
        with _CURATION_THREAD_LOCKS_GUARD:
            current = _CURATION_THREAD_LOCKS.get(key)
            if current is not None and current[0] is lock:
                if current[1] <= 1:
                    _CURATION_THREAD_LOCKS.pop(key, None)
                else:
                    _CURATION_THREAD_LOCKS[key] = (lock, current[1] - 1)



@dataclass(frozen=True)
class _StewardPlan:
    """What one finalize steward needs after its snapshot is read, before the decision lock.

    `propose(client)` returns `(outcome, proposals)` — the outcome name is the steward's own, since
    "empty" means different things to a curation (`curation_is_empty`) and to a facet set (falsy).
    `fast_paths` are evaluated INSIDE the lock in order; each returns `(outcome, proposals)` to
    settle, or None to continue.
    """

    input_schema: str
    input_digest: str
    curation_key: str
    has_input: bool
    propose: Callable[[object], tuple[str, dict]]
    fast_paths: tuple = ()


class LessonMemory(LessonPriorsMixin, LessonDistillMixin, LessonReconcileMixin):
    """The engine's cross-run memory / lessons / reflection cluster. See the module docstring
    for the `self._e` (engine handle) convention and the mixin decomposition."""

    def __init__(self, engine: "Engine") -> None:
        self._e = engine
        self.seen_stamp = None   # (size, mtime_ns) of the store at the last read
        self.prior_note_text = ""   # E4: cross-run RESEARCHER prior (R&D lessons), loaded at run start
        self.dev_prior_note_text = ""   # §role-split: cross-run DEVELOPER prior (code-fix lessons)
        # Reconcile gate: a hash of {node_id -> outcome-signature} at the last reconcile scan. Recomputed
        # each cadence pass (cheap, no I/O); when it CHANGES (a node reached / left / flipped a terminal —
        # in particular a node_reset re-eval that altered a metric or status), we re-read the lesson file
        # and re-derive any of THIS run's lessons whose evidence sig moved. None on start → first pass
        # always scans (verifies the store against the folded state after a restart/resume).
        self._reconcile_sig_hash = None

    def empty_state_for_fp(self) -> RunState:
        """Minimal RunState carrying just what `_task_fingerprint` reads at run START (before any
        node), so the prior loader can fingerprint the current task the same way the writer will."""
        return RunState(task_id=self._e.task.id, goal=getattr(self._e.task, "goal", ""),
                        direction=getattr(self._e.task, "direction", "min"))

    def task_fingerprint(self, final: RunState, best=None) -> list[str]:
        """M2: content fingerprint of this task so cross-run transfer reaches SIMILAR tasks, not only
        the exact same task_id. Built from kind/direction/metric/goal keywords + the winner's params."""
        from looplab.engine.memory import task_fingerprint
        # NOTE: the winner's param NAMES are outcome-derived, so this fingerprint shifts when a new
        # node wins / the run extends — i.e. it is a fuzzy RETRIEVAL key, not an immutable scope identity.
        # This is the SHIPPED convention (store_case / lesson priors key the same way); the capsule reuses
        # it for consistency. The immutable task/ComparisonContract identity is the CR0 TODO (§21.20.13) —
        # deliberately NOT changed here, since it would re-key every existing lesson/case store.
        pnames = list((best.idea.params or {}).keys()) if best is not None and best.idea else []
        return task_fingerprint(getattr(self._e.task, "kind", ""), final.direction,
                                final.goal or getattr(self._e.task, "goal", ""),
                                metric=str(getattr(self._e.task, "metric", "") or ""),
                                param_names=pnames,
                                universal=bool(getattr(self._e, "_fingerprint_universal", False)))

    def append_lessons(self, lessons: list, *, hygiene: bool = True, state: RunState = None) -> None:
        """Append lessons to the SHARED cross-run store. Used by run-end reflection AND the M6
        mid-run distillation, so a lesson distilled mid-flight is visible to a concurrent run's
        refresh immediately. Concurrency: the whole append (and the optional hygiene rewrite) runs
        under the same best-effort interprocess lock the event store uses — the D2 consolidate/
        compact pass is a full-file read-modify-write, and without the lock a concurrent run's
        O_APPEND between our read and our rewrite would be silently clobbered (losing exactly the
        cross-run lesson the live share exists to propagate). `hygiene=False` (the mid-run path)
        skips consolidate/compact entirely: the read path already dedups and quarantines, so
        hygiene can wait for run end instead of rewriting a shared file every few nodes."""
        if not (lessons and self._e.memory_dir):
            return
        from looplab.events.eventstore import _interprocess_lock
        base = Path(self._e.memory_dir)
        path = base / "lessons.jsonl"
        # The concept SHELF's durable tag, stamped at the ONE funnel both producers reach so a lesson
        # distilled mid-run is tagged identically to one distilled at run end. ADDITIVE and
        # reader-defaulted (invariant 5): the claim-source validator gates on statement/outcome/stance
        # and ignores unknown keys, so an old reader loads a tagged lesson unchanged.
        # Per-lesson, from that lesson's OWN evidence nodes rather than the run's whole set — the
        # difference between "this finding is about `loss/contrastive`" and "the run that produced it
        # touched `loss/contrastive`". A lesson whose evidence is untagged records nothing and falls
        # back to run-level inheritance at READ time, where it is labelled as the weaker claim.
        if state is not None:
            from looplab.engine.concept_shelf import state_concepts
            for lz in lessons:
                if not isinstance(lz, dict) or lz.get("concepts"):
                    continue
                evidence = lz.get("evidence")
                concepts = state_concepts(
                    state, evidence if isinstance(evidence, (list, tuple)) else None)
                if concepts:
                    lz["concepts"] = concepts
        payload = b"\n".join(orjson.dumps(lz) for lz in lessons)
        # BEST-EFFORT, the write twin of `maybe_refresh_lessons`'s read guard. The SHARED store lives
        # on a DIFFERENT filesystem from the run dir — a read-only / full / quota'd network mount
        # raises OSError here while the run's own events.jsonl append moments earlier succeeded — and
        # unguarded that propagated out of `maybe_distill_lessons` through `_run_cadences` into the
        # run() spine and FAILED the run, contradicting this subsystem's own "the store misses one
        # batch" stance. The EV_LESSONS_DISTILLED gate has already advanced, so every LATER distill
        # cadence re-crashed the same way. Disclosed, not swallowed: cross-run propagation is what is
        # lost, and the lessons themselves are already in this run's own event log.
        # OSError ONLY, deliberately: `required=True` makes an unavailable interprocess lock raise
        # EventStoreLockError, and THAT strictness is a safety contract, not a bug — "no lock, no
        # unlocked mutation of a file every concurrent run appends to" (see
        # tests/test_claim_source_health.py::test_lesson_append_refuses_to_mutate_without_required_lock).
        # Degrading there would be indistinguishable from silently dropping the lock requirement.
        try:
            base.mkdir(parents=True, exist_ok=True)
            with _interprocess_lock(Path(str(path) + ".lock"), required=True):
                append_jsonl_bytes_locked(path, payload)
        except OSError as e:  # noqa: BLE001 - advisory cross-run memory cannot fail the run
            self._e.store.append(EV_LESSONS_STORE_UNAVAILABLE, {
                "mode": "write", "count": len(lessons), "error": str(e)[:300]})
            return
        if hygiene:
            # D2 hygiene: consolidate the store after appending — merge duplicate claims into
            # an evidence_count, retire contradicted verdicts (newest wins), THEN bound size. The
            # Researcher client + embedder enable the hybrid+agent paraphrase-merge pass (run end
            # only — hygiene=False mid-run skips it, so the shared file isn't rewritten every node).
            # prompts/parser travel WITH the client so a merge_system.md override and the run's
            # configured structured-output parser reach the merge's adjudication call (I18/ADR-8).
            # OUTSIDE the append lock, and each hygiene pass takes the lock itself: the paraphrase
            # merge is a PROVIDER call, and running it under the shared store's lock froze every
            # concurrent run's lesson writes for as long as the model took. Our append above is
            # already committed and durable, so a hygiene pass that finds the file moved and declines
            # to rewrite loses nothing.
            prompts, parser = self._merge_prompt_opts()
            self._e._consolidate_lessons_file(path, self._e._reflect_client(), self._e._embedder,
                                              parser=parser, prompts=prompts)
            self._e._compact_lessons(path)

    def maybe_distill_lessons(self, state: RunState) -> RunState:
        """M6 write side (doc 13 §7 items 2+5): every `lessons_every` NEW nodes, distill
        comparative lessons and append them to the SHARED cross-run store IMMEDIATELY — a
        concurrent run's refresh (read side below) can pick them up mid-flight, the AgentRxiv
        live-share pattern. The `lessons_distilled` event is the replay-safe gate (at_node +
        the pair ids already spent); fires only at a creation decision point (no pending evals),
        mirroring deep-research. No-op when the cadence is 0 or reflection memory is off."""
        if (self._e.lessons_every <= 0 or not self._e._comparative_lessons_on
                or not (self._e._reflection_priors and self._e.memory_dir)):
            return state
        if state.pending_nodes():
            return state
        n = len(state.nodes)
        last = latest_lesson_node_count(state.lessons_distilled)
        if not self._e._cadence_due(n, last, self._e.lessons_every):
            return state
        fp = self._e._task_fingerprint(state, state.best())
        lessons, pairs = self._e._comparative_lessons(state, fp, exclude=self._e._spent_pairs(state))
        # Event BEFORE the store write, and always — even with 0 lessons — so the at_node gate
        # advances and the loop doesn't retry this node-count. Event-first ordering: if the
        # process dies between the two writes, a resume sees the gate advanced and skips — the
        # store misses one batch (best-effort memory) instead of re-invoking the LLM and
        # appending the same lessons twice. The statements ride in the event for audit.
        from looplab.core.advisory_payloads import research_lesson_receipt
        self._e.store.append(EV_LESSONS_DISTILLED, {
            "at_node": n, "trigger": "cadence", "count": len(lessons),
            "pairs": [[pr["a"], pr["b"]] for pr in pairs],
            "lessons": [research_lesson_receipt(lz, state) for lz in lessons]})
        # Hygiene deferred to run end: the read path dedups/quarantines already, and a full-file
        # rewrite of the shared store every few nodes would race other runs' appends for nothing.
        # The append itself is best-effort — `append_lessons` guards the OSError an unwritable shared
        # store raises and discloses it as `lessons_store_unavailable` — so this call cannot fail the
        # run, matching the "the store misses one batch" stance two comments up.
        self._e._append_lessons(lessons, hygiene=False, state=state)
        return fold(self._e.store.read_all())

    def lessons_store_stamp(self):
        """(size, mtime_ns) of the shared lessons store, or None — the cheap change detector the
        refresh gate uses to skip a full re-read/re-score when no run has written since."""
        if not self._e.memory_dir:
            return None
        try:
            st = (Path(self._e.memory_dir) / "lessons.jsonl").stat()
            return (st.st_size, st.st_mtime_ns)
        except OSError:
            return None

    def maybe_refresh_lessons(self, state: RunState) -> RunState:
        """M6 read side (doc 13 §7 item 5): every `lessons_refresh_every` NEW nodes, re-read the
        SHARED cross-run store and rebuild the proposal prior — so lessons a CONCURRENT run
        distilled after this run started reach this run's next proposals (pre-M6, the store was
        read at run start only). No LLM call; this run's own lessons are excluded (they're already
        in the digest). The `lessons_refreshed` event is the replay-safe cadence gate. When the
        store file is UNCHANGED since the last look (stat stamp), the rebuild — a full re-read +
        re-score (+ harmonic re-embed) of every lesson — is skipped; the gate still advances.
        No-op when the cadence is 0 or reflection memory is off."""
        if self._e.lessons_refresh_every <= 0 or not (self._e._reflection_priors and self._e.memory_dir):
            return state
        n = len(state.nodes)
        last = latest_lesson_node_count(state.lessons_refreshed)
        if not self._e._cadence_due(n, last, self._e.lessons_refresh_every):
            return state
        stamp = self._e._lessons_store_stamp()
        if stamp == self.seen_stamp:
            self._e.store.append(EV_LESSONS_REFRESHED, {"at_node": n, "skipped": "unchanged"})
            return fold(self._e.store.read_all())
        before = (self.prior_note_text, self.dev_prior_note_text)
        rid = state.run_id or None
        # BEST-EFFORT, like every sibling advisory path here — priors are a hint, never a
        # correctness input. `_load_reflection_priors_both` reads the shared store through
        # `read_jsonl_lenient`, which RAISES OSError on an unreadable lessons.jsonl/meta_notes.jsonl
        # (permissions, a transient FS fault). Unguarded that propagated through `_run_cadences`
        # into the run() spine and errored the run — and because the EV_LESSONS_REFRESHED gate only
        # advances on success, a persistently unreadable shared store crash-looped the run at the
        # same cadence on every resume.
        # ONE scan for BOTH role priors (see load_reflection_priors_both). `changed` must reflect
        # EITHER prior moving — a concurrent run distilling only developer-tagged code-fix lessons
        # updates just dev_prior_note_text, and the refresh audit signal must not report that as
        # unchanged. `chars` sums both priors so the size delta is likewise visible for either role.
        try:
            self.prior_note_text, self.dev_prior_note_text = \
                self._e._load_reflection_priors_both(exclude_run_id=rid)
        except (OSError, ValueError) as e:  # noqa: BLE001 - an advisory refresh cannot fail the run
            # The stamp is NOT advanced: the next cadence retries the same (still-changed) store
            # instead of treating an unread store as read. The skip is disclosed, not silent.
            self._e.store.append(EV_LESSONS_REFRESHED, {
                "at_node": n, "skipped": "unreadable", "error": str(e)[:300]})
            return fold(self._e.store.read_all())
        self.seen_stamp = stamp        # committed only once the load actually succeeded
        self._e.store.append(EV_LESSONS_REFRESHED, {
            "at_node": n, "chars": len(self.prior_note_text) + len(self.dev_prior_note_text),
            "changed": (self.prior_note_text, self.dev_prior_note_text) != before})
        return fold(self._e.store.read_all())

    def reflect_client(self):
        """The LLM client to use for run-end distillation — the Researcher's (unwrapping any
        surrogate/fallback), else the Developer's. None when no LLM client is wired (toy backends)."""
        from looplab.agents.roles import resolve_role_client

        return resolve_role_client(getattr(self._e, "researcher", None),
                                   getattr(self._e, "developer", None))

    @staticmethod
    def lessons_file_token(path: Path):
        """Content identity of the shared store, or None when it cannot be read.

        This is the compare-and-swap token an UNLOCKED hygiene pass swaps against before it rewrites
        the file. A content hash rather than (size, mtime_ns): unlike `lessons_store_stamp`, which
        only has to notice growth to invalidate a cache, this token guards a DESTRUCTIVE whole-file
        replace, and a concurrent consolidation can land on the same size inside one mtime tick. The
        store is line-capped by `compact_lessons`, so hashing it costs nothing worth saving.
        """
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None                 # unreadable -> no proof of "unchanged" -> no rewrite

    @staticmethod
    def consolidate_lessons_file(path: Path, client=None, embed=None,
                                 parser: str = "tool_call", prompts=None) -> None:
        """D2: rewrite lessons.jsonl through `consolidate_lessons` — duplicate claims merge into
        an evidence_count and a contradicted verdict is retired (the newest observation wins). When a
        `client` is wired, a hybrid-retrieval + agent pass ALSO merges paraphrase-level duplicates the
        exact key misses (`parser`/`prompts` configure that pass's structured-output parser and
        merge_system override). Atomic rewrite; best-effort (a hygiene failure must never fail the run).

        LOCKING — this method takes `lessons.jsonl.lock` ITSELF, so callers must NOT hold it. The
        paid paraphrase pass runs with the lock RELEASED: that lock gates a file every concurrent run
        appends to, so holding it across a provider call let one slow (or hung) model block every
        other run's lesson writes, and the governed readers queued behind them, for as long as the
        provider took. The shape is: snapshot under the lock, pay for the merge unlocked, then
        re-acquire and compare-and-swap. If any writer touched the store while we were waiting on the
        model, the rewrite is DROPPED — applying a stale snapshot would erase their append, which is
        exactly the loss the lock exists to prevent. Hygiene is best-effort and cadence-driven, so a
        skipped round costs nothing: the next one merges the combined file."""
        try:
            from looplab.engine.claims import (_load_claim_source_path,
                                               _valid_claim_source_row)
            from looplab.engine.memory import consolidate_lessons
            from looplab.events.eventstore import (_interprocess_lock,
                                                   replace_jsonl_rows_atomic_preserving_quarantine)
            lock_path = Path(str(path) + ".lock")
            with _interprocess_lock(lock_path, required=True):
                before = LessonMemory.lessons_file_token(path)
                rows = _load_claim_source_path(path, research=False)
            merged = consolidate_lessons(rows, client=client, embed=embed,   # unlocked: may be paid
                                         parser=parser, prompts=prompts)
            if len(merged) >= len(rows):
                return
            with _interprocess_lock(lock_path, required=True):
                if before is None or LessonMemory.lessons_file_token(path) != before:
                    return              # a concurrent writer moved the store under the merge
                # hygiene owns only understood lesson rows. Raw malformed/future records stay
                # byte-preserved quarantine until an explicit repair/migration acknowledges them.
                replace_jsonl_rows_atomic_preserving_quarantine(
                    path, merged,
                    replace_if=lambda row: _valid_claim_source_row(row, research=False),
                )
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def compact_lessons(path: Path, max_lines: int = 4000, keep: int = 2000) -> None:
        """Bound the shared lessons store: it is re-read and scored at every run start, and grows by
        a few lines per finished run forever. Past `max_lines`, keep the most recent `keep` (recency
        also wins ties at retrieval, so the dropped prefix is the least useful part).

        Takes `lessons.jsonl.lock` itself (callers must not hold it) — this is a read-modify-write of
        a file other runs append to, and it used to inherit the lock from a caller that held it
        across the paid consolidation pass. There is no unlocked window to swap against here: no
        provider call sits between the read and the write."""
        try:
            from looplab.engine.claims import (_load_claim_source_path,
                                               _valid_claim_source_row)
            from looplab.events.eventstore import (_interprocess_lock,
                                                   replace_jsonl_rows_atomic_preserving_quarantine)
            with _interprocess_lock(Path(str(path) + ".lock"), required=True):
                rows = _load_claim_source_path(path, research=False)
                if len(rows) > max_lines:
                    # Retention applies to interpreted lessons, never to quarantine bytes. A damaged/
                    # future row may exceed the soft file cap but cannot be silently laundered by
                    # unrelated hygiene.
                    replace_jsonl_rows_atomic_preserving_quarantine(
                        path, rows[-keep:],
                        replace_if=lambda row: _valid_claim_source_row(row, research=False),
                    )
        except Exception:  # noqa: BLE001 — compaction is best-effort; never fail the run for it
            pass

    def store_case(self, final: RunState) -> None:
        """Cross-run memory (I19): persist the best result as a retrievable case."""
        if not self._e.memory_dir:
            return
        best = final.best()
        if best is None:
            return
        lib = JsonlCaseLibrary(Path(self._e.memory_dir) / "cases.jsonl")
        from looplab.engine.concept_shelf import state_concepts
        case = {
            "task_id": final.task_id,
            "goal": final.goal,
            "direction": final.direction,
            "params": best.idea.params,
            "metric": best.robust_metric,
            "rationale": best.idea.rationale,
            # Both fields are ADDITIVE and reader-defaulted (invariant 5): `valid_case_record` gates on
            # `v`/`record_kind`/task_id/metric/params and ignores anything else, so an OLD reader loads a
            # new case unchanged and a NEW reader treats an old case as untagged. No migration.
            # `run_id` is what makes a case joinable AT ALL — a case is the one memory tier whose
            # historical rows carry no run reference, so run-level inheritance could never reach them.
            "run_id": final.run_id or "",
            # The WINNER's concepts, not the run's: a case IS the winning configuration, so recording
            # everything the run touched would over-claim exactly the way `state_concepts` refuses to.
            "concepts": state_concepts(final, [best.id]),
        }
        # An empty value is dropped rather than persisted: absence is the wire shape the shelf reads as
        # "not tagged", and `""`/`[]` would pin the row as durably-untagged and block the run fallback.
        lib.add({key: value for key, value in case.items()
                 if value or key not in ("run_id", "concepts")})

    def store_concept_capsule(self, final: RunState) -> None:
        """PART IV cross-run Step 2 (§21.20): persist this run's CONCEPT capsule to the shared
        `memory_dir` so a later SIMILAR run can surface "this was tried before -> outcome". Best-effort
        and self-contained: reuses the shipped per-run `node_concepts` tags (no new tagger) and the
        universal-aware `task_fingerprint`; per-concept outcome = the best robust_metric among nodes
        carrying that concept. Never raises — cross-run memory must never fail a run."""
        if not self._e.memory_dir:
            return
        try:
            node_concepts = getattr(final, "node_concepts", None) or {}
            from looplab.engine.memory import build_concept_capsule
            from looplab.events.replay import promotion_eligible_nodes
            direction = final.direction
            if direction not in ("min", "max"):
                return

            # NOTE: per-run concept labels carry no `concept_consolidation` / UID / taxonomy version
            # yet (the CR1a concept_uid resolver is the §21.20.13 TODO), so a later reader matches by
            # string — which is exactly why the ONE spelling below has to be the CANONICAL one.
            # Attempt coverage retains every tagged node, while a durable numeric outcome may come
            # only from the same feasible, live, unflagged pool used for promotion.
            concepts, outcomes = set(), {}
            provenance = getattr(final, "node_concept_provenance", None) or {}
            materialization_receipts = (
                getattr(final, "node_concept_materialization_receipts", None) or {})
            aborted = set(getattr(final, "aborted_nodes", None) or [])
            evidence_nodes_total = evidence_nodes_incomplete = 0
            classifier_observed = False
            eligible_ids = {node.id for node in promotion_eligible_nodes(final)}
            for nd in final.nodes.values():
                classified = provenance.get(nd.id) == NODE_CONCEPT_PROVENANCE_CLASSIFIER
                # `classifier_observed` counts a node whose classifier RAN, including one later
                # aborted or deleted — it answers "did this run ever classify?", which is what
                # separates "no evidence" from "evidence unknown" below. The evidence loop then
                # skips those lifecycles, so the two counts differ ON PURPOSE (pinned by
                # tests/test_cross_run_concepts.py's tombstone/abort/classifier_empty cases).
                classifier_observed = classifier_observed or classified
                if not classified or nd.id in aborted or getattr(nd, "tombstoned", False):
                    continue
                evidence_nodes_total += 1
                evidence_nodes_incomplete += int(nd.id in materialization_receipts)
                m = getattr(nd, "robust_metric", None)
                # the valid retained subset of a partial classifier result remains positive
                # evidence, but the producer-level denominator below permanently forbids absence/frequency
                # inference. Authored/heuristic labels and deleted/aborted attempts never cross this wall.
                record = m is not None and nd.id in eligible_ids
                # ONE spelling for add/get/set, and it must be the READER's spelling. The classifier's
                # RAW casing is deliberately preserved by `bounded_raw_concept_values`, so a single
                # technique arrives as `Data/Hard-Negative-Mining` on one node and
                # `data/hard-negative-mining` on another; keying the outcome map by the raw string made
                # those two different concepts, so the best-of comparison never ran between them and
                # the capsule published the SAME concept as both a winner and a loser — which also fed
                # a phantom value into `_concept_profit_signs`' run median, shifting unrelated
                # concepts' signs. The spelling is `concept_registry.normalize_key` (guarded by
                # `valid_concept_id`), which is exactly what `canonicalize_concepts` applies on the
                # READ side (novelty/portfolio/cards); `core/concepts.py::normalize_concept_id` would
                # NOT do — it maps spaces to `-`, so `A B` would be written `a-b` and never intersect
                # the reader's `a b`. Governance (aliases/splits) stays out: the capsule records what
                # this run observed, and readers apply the CURRENT rules when they read it.
                # Dropping (not coercing) an unusable id is the same rule the rest of the stack
                # follows — `str()` would launder `7`/`None` into the perfectly valid ids `'7'`/`'None'`
                # and persist them as cross-run evidence.
                node_dropped_a_tag = False
                for c in node_concepts.get(nd.id) or []:
                    key = normalize_key(c) if valid_concept_id(c) else None
                    if not key:
                        node_dropped_a_tag = True
                        continue
                    concepts.add(key)
                    if record:
                        prev = outcomes.get(key)
                        # `RunState.is_better` is THE comparator (core/fitness.py) — one spelling of
                        # "better" across the fold, the policies and this capsule.
                        if prev is None or final.is_better(m, prev):
                            outcomes[key] = m
                # `evidence_nodes_incomplete` counts NODES, not tags: `build_concept_capsule` rejects
                # the whole capsule when it exceeds `evidence_nodes_total`, and a per-tag increment on
                # a node with two unusable tags would do exactly that — the BUILD `except` below then
                # swallows the ValueError and NO capsule is written at all.
                if node_dropped_a_tag and nd.id not in materialization_receipts:
                    evidence_nodes_incomplete += 1
            run_id = final.run_id or final.task_id
            # `evidence_nodes_total == 0` is the term that matters: without it a run whose only
            # classifier node was later aborted took the write path with classifier_observed=True and
            # published `nodes_total=0, evidence_complete=True, concepts_complete=True` — the exact
            # zero the comment below forbids, asserting complete knowledge of no concepts for a run
            # that classified one. The other two conjuncts are implied by `not classifier_observed`
            # (nothing can be counted without a classified node) and stay for readability.
            requires_existing_capsule = (
                not concepts and evidence_nodes_incomplete == 0
                and (not classifier_observed or evidence_nodes_total == 0))
            best = final.best()
            capsule = build_concept_capsule(
                run_id=run_id, task_id=final.task_id, direction=direction,
                concepts=concepts, fingerprint=self.task_fingerprint(final, best),
                best_metric=(best.robust_metric if best is not None else None),
                concept_outcomes=outcomes,
                concept_evidence_nodes_total=evidence_nodes_total,
                concept_evidence_nodes_incomplete=evidence_nodes_incomplete,
                concept_evidence_observed=classifier_observed)
        except Exception:  # noqa: BLE001 — BUILD is best-effort: a projection hiccup must never fail a run
            return
        # The WRITE is NOT swallowed (mirrors store_case): a real persistence failure must reach finalize's
        # retry handshake (which sets complete=False and retries on the next re-entry) rather than being
        # silently lost while `finalization_finished` is committed.
        from looplab.engine.memory import ConceptCapsuleStore
        capsule_store = ConceptCapsuleStore(
            Path(self._e.memory_dir) / "concept_capsules.jsonl")
        if requires_existing_capsule:
            # A brand-new run with no live classified evidence contributes NO capsule — an absent row
            # says "unknown", which is the truth, whereas a written one would claim knowledge of zero.
            # If this run ALREADY published, though, leaving that row active would resurrect stale
            # concepts after an abort/tombstone/reset erased their provenance, so it is superseded.
            # That supersede row is deliberately `observed=true` with empty collections — the
            # SAME-RUN TOMBSTONE `build_concept_capsule` documents (engine/memory.py): this run really
            # does carry no concepts now, and readers must retire the old ones rather than keep them.
            if not any(c.get("run_id") == run_id for c in capsule_store.all()):
                return
        # `add` returns False WITHOUT raising when the row fails validation (an empty run_id, a
        # concepts/outcomes key-set mismatch). Discarding it dropped the capsule forever while
        # finalize still marked the step done — the silent loss the comment above says must not
        # happen. Raise so it reaches finalize's retry handshake like any other write failure.
        if not capsule_store.add(capsule):
            raise RuntimeError(f"concept capsule for run {run_id!r} was rejected by the store")

    def _already_curated(self, log_name: str, curation_key: str) -> bool:
        """Whether semantic work has a terminal outcome; unavailable clients do not consume the key."""
        from looplab.engine.governance_health import read_curation_rows

        p = Path(self._e.memory_dir) / log_name
        if not p.exists():
            return False
        return any(
            r.get("v") == 2 and not isinstance(r.get("v"), bool)
            and r.get("action") is None
            and str(r.get("curation_key") or "") == curation_key
            and str(r.get("outcome") or "") != "unavailable"
            for r in read_curation_rows(p)
        )

    @staticmethod
    def _curation_finish_seq(final: RunState) -> int | None:
        finish_seq = getattr(final, "last_finish_seq", None)
        return (finish_seq if isinstance(finish_seq, int) and not isinstance(finish_seq, bool)
                and finish_seq >= 0 else None)

    @classmethod
    def _curation_source_key(cls, final: RunState) -> str:
        """Derived by `governance_health`, which is also what VALIDATES it on every ledger read.

        The two used to compute it independently. They cannot be allowed to disagree: the validator
        recomputes this key and rejects any row that does not match, so a drift would retroactively
        invalidate every receipt already on disk (doc 25 EM-04).
        """
        from looplab.engine.governance_health import curation_source_key

        return curation_source_key(
            run_id=str(final.run_id or ""), task_id=str(final.task_id or ""),
            finish_seq=cls._curation_finish_seq(final))

    @staticmethod
    def _portfolio_curation_key(kind: str, input_digest: str) -> str:
        if kind not in {"concept", "claim"} or len(input_digest) != 64:
            raise ValueError("invalid portfolio curation identity")
        # paid portfolio work is identified by the exact frozen model input, never by
        # whichever run happened to trigger finalize.  This is both cross-run dedup and the TOCTOU fence.
        return f"{kind}:v2:{input_digest}"

    @staticmethod
    def _facets_curation_key(task_id: str) -> str:
        """Derived by `governance_health` — same reason as `_curation_source_key`."""
        from looplab.engine.governance_health import facets_curation_key

        return facets_curation_key(task_id)

    @classmethod
    def _diagnostic_curation_key(cls, kind: str, final: RunState) -> str:
        return f"{kind}:diagnostic:v2:{cls._curation_source_key(final).rsplit(':', 1)[-1]}"

    def _curation_provenance(self, *, input_digest: str, input_schema: str,
                             client) -> dict:
        from looplab.core.redact import redact_persisted_text

        model = getattr(client, "model", None) if client is not None else None
        if not model:
            model = getattr(getattr(self._e, "settings", None), "llm_model", None)
        model = redact_persisted_text(
            model or "unknown", max_chars=200, entropy=True, single_line=True)
        return {
            "input_digest": input_digest,
            "input_schema": input_schema,
            "model": model or "unknown",
            "parser": _FINALIZE_STEWARD_PARSER,
        }

    def _curation_claim_path(self, log_name: str, curation_key: str) -> Path:
        digest = hashlib.sha256(f"{log_name}\0{curation_key}".encode("utf-8")).hexdigest()
        return Path(self._e.memory_dir) / _CURATION_CLAIM_DIR / f"{digest}.json"

    def _legacy_curation_claim_path(self, log_name: str, final: RunState) -> Path | None:
        """The v1 run-keyed claim path, checked only for an exact non-empty run id."""
        rid = str(final.run_id or "")
        if not rid:
            return None
        digest = hashlib.sha256(f"{log_name}\0{rid}".encode("utf-8")).hexdigest()
        return Path(self._e.memory_dir) / _CURATION_CLAIM_DIR / f"{digest}.json"

    def _legacy_curation_terminal(self, log_name: str, final: RunState) -> bool:
        """Bridge known v1 outcomes without reviving the old polymorphic run/task identity."""
        from looplab.engine.governance_health import read_curation_rows

        rid, tid = str(final.run_id or ""), str(final.task_id or "")
        path = Path(self._e.memory_dir) / log_name
        if not rid or not path.exists():
            return False
        return any(
            not row.get("curation_key")
            and str(row.get("run_id") or "") == rid
            and str(row.get("task_id") or "") == tid
            and str(row.get("outcome") or "") != "unavailable"
            for row in read_curation_rows(path)
        )

    def _write_curation_claim(self, path: Path, log_name: str, kind: str,
                              final: RunState, curation_key: str,
                              provenance: dict, incomplete: dict) -> None:
        """Create and strictly sync the one-way claim that gates a paid finalize invocation."""
        from looplab.core.atomicio import strict_fsync, strict_fsync_parent
        from looplab.engine.governance_health import CURATION_ID_MAX_CHARS

        auto_requested = incomplete.get("auto_requested")
        if not isinstance(auto_requested, bool):
            raise ValueError("paid curation claim requires boolean auto_requested")
        run_id, task_id = str(final.run_id or ""), str(final.task_id or "")
        if (len(run_id) > CURATION_ID_MAX_CHARS or any(ord(ch) < 32 for ch in run_id)
                or len(task_id) > CURATION_ID_MAX_CHARS
                or any(ord(ch) < 32 for ch in task_id)):
            # The terminal ledger uses the same bounds. Validate before the irreversible provider
            # boundary so a claim can never be durable while every matching terminal is unwritable.
            raise ValueError("invalid paid curation source identity")
        claim_dir = path.parent
        created_dir = not claim_dir.exists()
        claim_dir.mkdir(parents=True, exist_ok=True)
        if created_dir:
            strict_fsync_parent(claim_dir)
        payload = {
            "v": 2,
            "action": "finalize-steward-begun",
            "kind": kind,
            "log": log_name,
            "curation_key": curation_key,
            "source_key": self._curation_source_key(final),
            "run_id": run_id,
            "task_id": task_id,
            "finish_seq": self._curation_finish_seq(final),
            "auto": False,
            "auto_requested": auto_requested,
            **provenance,
        }
        encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")) + "\n").encode("utf-8")
        # Exclusive create is a second line of defence behind the semantic invocation lock. Any
        # extant file, including a torn claim from a failed sync, is conservatively non-replayable.
        with path.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            strict_fsync(handle.fileno())
        strict_fsync_parent(path)

    def _read_curation_claim(self, path: Path, log_name: str, kind: str,
                             curation_key: str) -> tuple[RunState, dict, bool]:
        """Read an existing v2 paid claim without borrowing identity from the retrying run."""
        from looplab.engine.governance_health import CURATION_ID_MAX_CHARS

        def _unique_object(pairs):
            obj = {}
            for key, value in pairs:
                if key in obj:
                    raise ValueError("duplicate curation claim field")
                obj[key] = value
            return obj

        def _reject_constant(_value):
            raise ValueError("non-finite curation claim value")

        with path.open("rb") as handle:
            raw = handle.read(_CURATION_CLAIM_MAX_BYTES + 1)
        if not raw or len(raw) > _CURATION_CLAIM_MAX_BYTES:
            raise ValueError("invalid curation claim size")
        if raw.count(b"\n") != 1 or not raw.endswith(b"\n"):
            raise ValueError("curation claim must be one complete record")
        try:
            claim = json.loads(
                raw.decode("utf-8"), object_pairs_hook=_unique_object,
                parse_constant=_reject_constant)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid curation claim encoding") from exc
        expected_fields = {
            "v", "action", "kind", "log", "curation_key", "source_key", "run_id",
            "task_id", "finish_seq", "auto", "auto_requested", "input_digest",
            "input_schema", "model", "parser",
        }
        if not isinstance(claim, dict) or set(claim) != expected_fields:
            raise ValueError("invalid curation claim fields")
        if claim.get("v") != 2 or isinstance(claim.get("v"), bool):
            raise ValueError("unsupported curation claim version")
        if claim.get("action") != "finalize-steward-begun":
            raise ValueError("invalid curation claim action")
        if claim.get("kind") != kind or claim.get("log") != log_name:
            raise ValueError("foreign curation claim scope")
        if claim.get("curation_key") != curation_key:
            raise ValueError("foreign curation claim identity")
        if claim.get("auto") is not False or not isinstance(claim.get("auto_requested"), bool):
            raise ValueError("invalid curation claim invocation mode")

        bounded_strings = {
            "run_id": CURATION_ID_MAX_CHARS,
            "task_id": CURATION_ID_MAX_CHARS,
            "source_key": 80,
            "curation_key": 100,
            "input_digest": 64,
            "input_schema": 200,
            "model": 200,
            "parser": 100,
        }
        for field, maximum in bounded_strings.items():
            value = claim.get(field)
            if (not isinstance(value, str) or not value or len(value) > maximum
                    or any(ord(ch) < 32 for ch in value)):
                # Run ids may be empty in historical state, but a durable claim still binds the exact
                # empty value. Handle those two identity fields separately below.
                if field not in {"run_id", "task_id"} or value != "":
                    raise ValueError(f"invalid curation claim {field}")
        finish_seq = claim.get("finish_seq")
        if (finish_seq is not None
                and (isinstance(finish_seq, bool) or not isinstance(finish_seq, int)
                     or finish_seq < 0)):
            raise ValueError("invalid curation claim finish_seq")
        # The shared predicate also CLOSES a gap here (doc 25 EV-04): this copy tested `len` and
        # membership without an `isinstance`, so a 64-element list of hex characters satisfied both
        # and was accepted as a digest. The binding stays — `_portfolio_curation_key` below rebuilds
        # this claim's identity from it.
        digest = claim["input_digest"]
        if not valid_digest_ref(digest):
            raise ValueError("invalid curation claim input_digest")
        from looplab.core.redact import redact_persisted_text
        for field, maximum in (("input_schema", 200), ("model", 200), ("parser", 100)):
            value = claim[field]
            if redact_persisted_text(
                    value, max_chars=maximum, entropy=True, single_line=True) != value:
                raise ValueError(f"unsafe curation claim {field}")
        if kind in {"concept", "claim"}:
            if self._portfolio_curation_key(kind, digest) != curation_key:
                raise ValueError("curation claim digest does not match its identity")
        elif kind == "facets":
            if self._facets_curation_key(claim["task_id"]) != curation_key:
                raise ValueError("facets claim task does not match its identity")
        else:
            raise ValueError("invalid curation claim kind")

        claim_final = RunState(
            run_id=claim["run_id"], task_id=claim["task_id"],
            last_finish_seq=finish_seq if finish_seq is not None else -1)
        if self._curation_source_key(claim_final) != claim["source_key"]:
            raise ValueError("curation claim source identity mismatch")
        provenance = {
            field: claim[field] for field in ("input_digest", "input_schema", "model", "parser")
        }
        return claim_final, provenance, claim["auto_requested"]

    @contextmanager
    def _curation_decision_lock(self, log_name: str, final: RunState, curation_key: str):
        """Serialize every terminal decision for one semantic key, including no-call fast paths."""
        from looplab.core.atomicio import strict_fsync_parent
        from looplab.events.eventstore import _interprocess_lock

        claim_path = self._curation_claim_path(log_name, curation_key)
        legacy_path = self._legacy_curation_claim_path(log_name, final)
        created_dir = not claim_path.parent.exists()
        claim_path.parent.mkdir(parents=True, exist_ok=True)
        if created_dir:
            strict_fsync_parent(claim_path.parent)
        self._prune_curation_scratch(claim_path.parent)
        key = str(claim_path.absolute())
        with _curation_thread_lock(key):
            # The legacy (v1, run-keyed) claim is NEVER written by this v2 path — it is only READ
            # (`_curation_attempt_already_resolved_locked`). Its interprocess lock therefore only matters
            # when a legacy claim actually exists on disk (a v1-era writer left one). Acquiring it
            # unconditionally would open (create) a `<run_id>.json.lock` — and since the legacy path is
            # keyed by the unique run_id and `_interprocess_lock` never unlinks, that accreted one orphan
            # lock per run in `.curation_invocations/` forever. Serialize against it only when there is a
            # legacy claim to serialize against; the v2 claim lock below always fences the paid decision.
            legacy_guard = (
                _interprocess_lock(Path(str(legacy_path) + ".lock"), required=True)
                if legacy_path is not None and legacy_path.exists() else nullcontext())
            with legacy_guard:
                with _interprocess_lock(Path(str(claim_path) + ".lock"), required=True):
                    yield

    def _prune_curation_scratch(self, scratch: Path) -> None:
        """Best-effort bound on `.curation_invocations/`. Once the dir grows past the soft cap, unlink the
        OLDEST orphan `<digest>.json.lock` files — locks with no matching `<digest>.json` recovery claim,
        i.e. pure interprocess-mutex scratch left behind by empty/unavailable/evolving-digest decisions.
        Skips any lock younger than a finalize's worst-case wall-clock so an in-flight paid decision's lock
        is never pulled out from under it, and never touches the durable `.json` claim markers. Never
        raises — a hiccup in scratch GC must not perturb finalize."""
        try:
            entries = list(scratch.iterdir())
        except OSError:
            return
        if len(entries) <= _CURATION_SCRATCH_MAX_ENTRIES:
            return
        claims = {p.name for p in entries if p.name.endswith(".json")}
        now = time.time()
        prunable: list[tuple[float, Path]] = []
        for p in entries:
            if not p.name.endswith(".json.lock"):
                continue
            if p.name[:-len(".lock")] in claims:
                continue  # keep a lock paired with a live recovery claim
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if now - mtime < _CURATION_SCRATCH_MIN_AGE_S:
                continue  # a lock this fresh may be held by an in-flight decision on another process
            prunable.append((mtime, p))
        prunable.sort()  # oldest first
        for _mtime, p in prunable[: len(entries) - _CURATION_SCRATCH_MAX_ENTRIES]:
            try:
                p.unlink()
            except OSError:
                pass

    @contextmanager
    def _paid_curation_attempt_locked(self, log_name: str, kind: str, final: RunState,
                                      curation_key: str, provenance: dict, incomplete: dict):
        """Paid-attempt protocol; the caller must hold ``_curation_decision_lock``."""
        claim_path = self._curation_claim_path(log_name, curation_key)
        if self._curation_attempt_already_resolved_locked(
                log_name, kind, final, curation_key, incomplete):
            yield False
            return
        self._write_curation_claim(
            claim_path, log_name, kind, final, curation_key, provenance, incomplete)
        yield True

    def _recover_curation_claim_locked(self, log_name: str, kind: str, curation_key: str,
                                       incomplete: dict) -> bool:
        """Close one existing ambiguous paid claim; the semantic decision lock must be held."""
        claim_path = self._curation_claim_path(log_name, curation_key)
        if not claim_path.exists():
            return False
        # recovery metadata comes exclusively from the durable paid claim. A retrying
        # run/model may observe the same semantic key, but it never impersonates the lost attempt.
        claim_final, claim_provenance, claim_auto_requested = self._read_curation_claim(
            claim_path, log_name, kind, curation_key)
        recovered_incomplete = {
            **incomplete,
            "auto": False,
            "auto_requested": claim_auto_requested,
        }
        self._append_curation_once(
            log_name, claim_final, curation_key, claim_provenance, recovered_incomplete,
            require_durable=True)
        return True

    def _curation_attempt_already_resolved_locked(
            self, log_name: str, kind: str, final: RunState,
            curation_key: str, incomplete: dict) -> bool:
        """Resolve/suppress old work before any new v2 terminal; decision lock must be held."""
        if self._already_curated(log_name, curation_key):
            return True
        if self._legacy_curation_terminal(log_name, final):
            return True
        legacy_path = self._legacy_curation_claim_path(log_name, final)
        if legacy_path is not None and legacy_path.exists():
            # A v1 provider may have accepted the call, but its receipt did not bind an exact
            # model-visible snapshot. Suppress only this exact run and never invent a v2 terminal.
            return True
        return self._recover_curation_claim_locked(log_name, kind, curation_key, incomplete)

    @contextmanager
    def _paid_curation_attempt(self, log_name: str, kind: str, final: RunState,
                               curation_key: str, provenance: dict, incomplete: dict):
        """Yield once only after a durable claim; resolve a prior ambiguous claim without replay."""
        with self._curation_decision_lock(log_name, final, curation_key):
            with self._paid_curation_attempt_locked(
                    log_name, kind, final, curation_key, provenance, incomplete) as invoke:
                yield invoke

    def _append_curation_once(self, log_name: str, final: RunState, curation_key: str,
                              provenance: dict, rec: dict, *,
                              require_durable: bool = False) -> bool:
        """Append one semantic steward outcome; unavailable audits remain non-blocking."""
        from looplab.engine.concept_registry import _append_governance
        from looplab.engine.governance_health import read_curation_rows

        class _AlreadyLogged(RuntimeError):
            pass

        path = Path(self._e.memory_dir) / log_name
        path.parent.mkdir(parents=True, exist_ok=True)
        source_key = self._curation_source_key(final)
        outcome = str(rec.get("outcome") or "")
        locked_rows: list[dict] = []

        def _read_locked(current: Path) -> list[dict]:
            # paid history is policy. Capture the complete validated ledger under the
            # physical append lock so dedup and the next revision are derived from the same snapshot.
            rows = read_curation_rows(current)
            locked_rows[:] = rows
            return rows

        def _validate_locked() -> None:
            for row in locked_rows:
                if str(row.get("curation_key") or "") != curation_key:
                    continue
                prior_outcome = str(row.get("outcome") or "")
                if outcome == "unavailable":
                    # a late no-client observer is an audit only. Once another process
                    # commits a terminal result it may never append after or supersede that result.
                    if prior_outcome != "unavailable":
                        raise _AlreadyLogged
                    if prior_outcome == "unavailable" and row.get("source_key") == source_key:
                        raise _AlreadyLogged
                elif prior_outcome != "unavailable":
                    raise _AlreadyLogged

        payload = {
            "v": 2,
            "curation_key": curation_key,
            "source_key": source_key,
            "run_id": str(final.run_id or ""),
            "task_id": str(final.task_id or ""),
            "finish_seq": self._curation_finish_seq(final),
            **provenance,
            **rec,
        }
        try:
            _append_governance(
                path, payload, validate=_validate_locked, read_rows=_read_locked,
                require_durable=require_durable)
            return True
        except _AlreadyLogged:
            return False

    # --- one finalize-steward driver, three configurations (doc 25 EM-02) -----------------------
    #
    # The concept, claim and task-facet stewards share a ~90-line at-most-once protocol: the
    # cross-run gate, the semantic decision lock, the already-resolved check, the fast paths that
    # must not race a paid attempt, the paid attempt itself, and TWO error terminals (one inside the
    # lock, one diagnostic outside it). It was copy-pasted three times, so every protocol fix — a
    # lock-ordering change, a new terminal, a receipt field — had to be applied three times IN STEP
    # or the three ledgers would disagree about what happened during the same finalize.
    #
    # What actually differs between them is data: which log, which snapshot, which propose call, and
    # the empty shape of that steward's proposals. `fast_paths` carries the one STRUCTURAL
    # difference — facets are once-per-TASK, so an already-governed task must short-circuit inside
    # the lock, before any provider call.

    def _run_finalize_steward(self, final: RunState, *, log_name: str, kind: str,
                              unavailable_schema: str, empty_proposals, plan,
                              diagnostic_proposals=None) -> str:
        """Drive one finalize steward through the shared at-most-once protocol.

        `empty_proposals()` returns a FRESH empty proposals dict (never a shared mutable default).
        `plan(final)` runs inside the outer try and returns either a short-circuit outcome string or
        a `_StewardPlan`. `diagnostic_proposals()` defaults to `empty_proposals` and exists for the
        facets steward, whose diagnostic row carries the task id even when planning failed.
        """
        if not (self._e.memory_dir and getattr(self._e, "_cross_run_curation", False)):
            return "disabled"
        auto_requested = bool(getattr(self._e, "_cross_run_curation_auto", False))
        diagnostic_key = self._diagnostic_curation_key(kind, final)
        diagnostic_provenance = self._curation_provenance(
            input_digest="", input_schema=unavailable_schema, client=None)

        def row(outcome, proposals, **extra):
            return {"outcome": outcome, "auto": False, "auto_requested": auto_requested,
                    "proposals": proposals, "receipt": None, **extra}

        try:
            planned = plan(final)
            if isinstance(planned, str):          # a pre-lock short-circuit (facets with no task id)
                return planned
            curation_key = planned.curation_key
            incomplete = row("prior_attempt_incomplete_not_replayed", empty_proposals(),
                             ambiguity="provider_outcome_unknown")

            def settle(outcome, proposals, provenance, *, durable=False):
                appended = self._append_curation_once(
                    log_name, final, curation_key, provenance, row(outcome, proposals),
                    **({"require_durable": True} if durable else {}))
                return outcome if appended else "already-resolved"

            # The semantic decision lock covers every fast path AND the paid attempt. Otherwise a
            # stale empty/unavailable observer can commit while another process is paying, then
            # suppress that provider's terminal result at append time.
            with self._curation_decision_lock(log_name, final, curation_key):
                if self._curation_attempt_already_resolved_locked(
                        log_name, kind, final, curation_key, incomplete):
                    return "already-resolved"
                unpaid = self._curation_provenance(
                    input_digest=planned.input_digest, input_schema=planned.input_schema,
                    client=None)
                for fast_path in planned.fast_paths:
                    decided = fast_path()
                    if decided is not None:
                        return settle(decided[0], decided[1], unpaid)
                if not planned.has_input:
                    return settle("empty", empty_proposals(), unpaid)
                client = self.reflect_client()
                provenance = self._curation_provenance(
                    input_digest=planned.input_digest, input_schema=planned.input_schema,
                    client=client)
                if client is None:
                    return settle("unavailable", empty_proposals(), provenance)
                # Finalize is an untrusted-agent proposal boundary. Even the legacy `auto` flag
                # cannot mutate taxonomy before a durable receipt; only an explicit operator command
                # may apply a proposal.
                with self._paid_curation_attempt_locked(
                        log_name, kind, final, curation_key, provenance, incomplete) as invoke:
                    if not invoke:
                        return "already-resolved"
                    try:
                        outcome, proposals = planned.propose(client)
                        return settle(outcome, proposals, provenance, durable=True)
                    except Exception as exc:  # noqa: BLE001 - close while decision lock is held
                        self._append_curation_once(
                            log_name, final, curation_key, provenance,
                            row("error", empty_proposals(), error_type=type(exc).__name__),
                            require_durable=True)
                        return "error"
        except Exception as exc:  # noqa: BLE001 — agentic curation must never fail a run
            try:
                self._append_curation_once(
                    log_name, final, diagnostic_key, diagnostic_provenance,
                    row("error", (diagnostic_proposals or empty_proposals)(),
                        error_type=type(exc).__name__),
                    require_durable=True)
            except Exception:  # noqa: BLE001 — logging stays best-effort relative to finalization
                pass
            return "error"

    def store_concept_curation(self, final: RunState) -> str:
        """PART IV §22.4 — the AGENTIC taxonomy steward at finalize: when `cross_run_curation` is on and an
        LLM client is available (`reflect_client`), let the LLM review the freshly-updated portfolio concept
        graph and PROPOSE a curation (merge/split/purge). Every outcome, including an empty proposal or an
        unavailable client, is durably LOGGED to `concept_curation_log.jsonl` for operator ratification.
        Finalize never applies an agent proposal: mutation requires an explicit operator CLI/API action.
        Portfolio-scoped and fully decoupled from the run's terminal state — best-effort, never raises."""
        def plan(_final):
            from looplab.engine.concept_steward import (
                CONCEPT_CURATION_INPUT_SCHEMA,
                concept_curation_has_input,
                concept_curation_snapshot,
                curation_is_empty,
                propose_concept_curation,
            )

            overview, input_digest = concept_curation_snapshot(self._e.memory_dir)

            def propose(client):
                proposals = propose_concept_curation(
                    overview, client, parser=_FINALIZE_STEWARD_PARSER, raise_on_failure=True)
                return ("empty" if curation_is_empty(proposals) else "proposed"), proposals

            return _StewardPlan(
                input_schema=CONCEPT_CURATION_INPUT_SCHEMA,
                input_digest=input_digest,
                curation_key=self._portfolio_curation_key("concept", input_digest),
                has_input=bool(concept_curation_has_input(overview)),
                propose=propose,
            )

        return self._run_finalize_steward(
            final, log_name="concept_curation_log.jsonl", kind="concept",
            unavailable_schema="finalize-concept-curation/input-unavailable",
            empty_proposals=lambda: {"merges": [], "splits": [], "purges": []},
            plan=plan)

    def store_claim_curation(self, final: RunState) -> str:
        """PART IV §22.4 — the AGENTIC CLAIM steward at finalize (companion to `store_concept_curation`):
        the LLM reviews the evidence-grounded claim assessments and PROPOSES operator decisions
        (ratify/reject/pin). All outcomes are locked/durably logged to `claim_curation_log.jsonl`; finalize
        never applies them. Same gate/decoupling/best-effort contract as the concept steward."""
        def plan(_final):
            from looplab.engine.claim_steward import (
                CLAIM_CURATION_INPUT_SCHEMA,
                claim_curation_has_input,
                claim_curation_snapshot,
                curation_is_empty,
                propose_claim_curation,
            )

            claims, input_digest = claim_curation_snapshot(self._e.memory_dir, structured=True)

            def propose(client):
                proposals = propose_claim_curation(
                    claims, client, parser=_FINALIZE_STEWARD_PARSER, raise_on_failure=True)
                return ("empty" if curation_is_empty(proposals) else "proposed"), proposals

            return _StewardPlan(
                input_schema=CLAIM_CURATION_INPUT_SCHEMA,
                input_digest=input_digest,
                curation_key=self._portfolio_curation_key("claim", input_digest),
                has_input=bool(claim_curation_has_input(claims)),
                propose=propose,
            )

        return self._run_finalize_steward(
            final, log_name="claim_curation_log.jsonl", kind="claim",
            unavailable_schema="finalize-claim-curation/input-unavailable",
            empty_proposals=lambda: {"decisions": []},
            plan=plan)

    def store_task_facets(self, final: RunState) -> str:
        """PART IV §21.20.2 — propose task facets and queue them for operator ratification.

        Facets can widen retrieval scope, so agent output is never silently promoted into policy at finalize.
        Outcomes are written once/task to `task_facets_curation_log.jsonl`, including empty/unavailable ones.
        """
        def plan(final_state):
            tid = str(getattr(final_state, "task_id", "") or "")
            if not tid:
                return "empty"
            from looplab.engine.task_facets import (
                TASK_FACETS_INPUT_SCHEMA,
                load_task_facets,
                propose_task_facets,
                task_facets_goal_is_empty,
                task_facets_input_digest,
            )

            goal = str(getattr(final_state, "goal", "") or "")
            kind = str(getattr(getattr(self._e, "task", None), "kind", "") or "")

            def already_governed():
                # Facets are once/TASK, so a task the operator already governs must settle before any
                # provider call — and inside the lock, so it cannot race a paid attempt.
                current = load_task_facets(self._e.memory_dir).get(tid)
                return None if current is None else (
                    "already-governed", {"task_id": tid, "facets": current})

            def empty_goal():
                return ("empty", {"task_id": tid, "facets": {}}) if task_facets_goal_is_empty(
                    goal, kind) else None

            def propose(client):
                facets = propose_task_facets(
                    goal, kind, client, parser=_FINALIZE_STEWARD_PARSER, raise_on_failure=True)
                return ("proposed" if facets else "empty"), {"task_id": tid, "facets": facets}

            return _StewardPlan(
                input_schema=TASK_FACETS_INPUT_SCHEMA,
                input_digest=task_facets_input_digest(goal, kind),
                # Facets are once/task, so differently worded runs share this decision lock.
                curation_key=self._facets_curation_key(tid),
                has_input=True,        # the goal check above is the facets steward's "no input"
                propose=propose,
                fast_paths=(already_governed, empty_goal),
            )

        return self._run_finalize_steward(
            final, log_name="task_facets_curation_log.jsonl", kind="facets",
            unavailable_schema="finalize-task-facets/input-unavailable",
            empty_proposals=lambda: {"task_id": str(getattr(final, "task_id", "") or ""),
                                     "facets": {}},
            plan=plan)

    def store_research_claims(self, final: RunState) -> None:
        """PART IV/§21.20 — persist this run's D8 deep-research claims (from the memo ledger) to the
        cross-run `research_claims.jsonl`, so evidence-backed research findings survive their run and can
        support/contest lesson verdicts. Best-effort BUILD; the WRITE reaches finalize's retry handshake."""
        if not self._e.memory_dir:
            return
        try:
            claims = []
            claims_total = 0
            claims_receipt_known = True
            evidence_complete = True
            raw_research = getattr(final, "research", None)
            if raw_research is None:
                return
            # a malformed outer memo collection is one UNKNOWN producer slot, not an
            # iterable of trusted memos (a dict/string used to be walked key/character by character).
            memos = raw_research if type(raw_research) in (list, tuple) else (None,)
            # An explicitly observed empty research collection is a complete zero-row D8 snapshot. It must
            # reach the upsert writer so a finalize retry can clear stale understood rows for the same run.
            # By contrast, `None` above and a non-empty collection of pre-D8 memos with no `claims` field do
            # not assert anything about the D8 source and deliberately leave an existing store untouched.
            d8_source_observed = not memos
            for memo in memos:
                if type(memo) is not dict:
                    d8_source_observed = True
                    claims.append(None)
                    claims_total += 1
                    claims_receipt_known = False
                    continue
                # Old pre-D8 memos legitimately have no `claims` key. Once the field is present, however,
                # only the declared list/tuple shape can prove its cardinality. Any scalar/container mismatch
                # contributes one opaque omitted slot so finalize cannot silently publish a complete receipt.
                if "claims" not in memo:
                    continue
                d8_source_observed = True
                raw_claims = memo.get("claims")
                if type(raw_claims) not in (list, tuple):
                    claims.append(None)
                    claims_total += 1
                    claims_receipt_known = False
                    continue
                from looplab.core.advisory_payloads import research_claims_receipt
                memo_receipt = research_claims_receipt(memo)
                if memo_receipt is None:
                    claims_total += len(raw_claims)
                    claims_receipt_known = False
                else:
                    claims_total += memo_receipt["total"]
                verification = memo.get("verification")
                verification = verification if type(verification) is dict else {}
                verdicts = verification.get("verdicts")
                verdicts = verdicts if type(verdicts) in (list, tuple) else ()
                method_value = verification.get("method")
                method = method_value[:80] if isinstance(method_value, str) else ""
                for i, c in enumerate(raw_claims):
                    # Preserve one slot per producer item. The D8 writer deliberately counts opaque None
                    # markers but never indexes/persists their contents, so an all-invalid memo still emits a
                    # durable incomplete-source sentinel and a malformed prefix cannot shrink the receipt.
                    if type(c) is not dict:
                        claims.append(None)
                        continue
                    try:
                        statement = c.get("statement")
                        if not isinstance(statement, str) or not statement.strip():
                            claims.append(None)
                            continue
                        # `verify_memo` promises an index-aligned verdict list. Fail closed if a malformed
                        # event breaks that alignment or names a different statement: the citation remains
                        # drillable, but it is never upgraded into positive support.
                        v = verdicts[i] if i < len(verdicts) and type(verdicts[i]) is dict else {}
                        verified_statement = v.get("statement")
                        same = (isinstance(verified_statement, str)
                                and verified_statement.strip() == statement.strip())
                        verdict_value = v.get("verdict")
                        verdict = (verdict_value if same and isinstance(verdict_value, str)
                                   else "unverified")
                        note_value = v.get("note")
                        note = (note_value[:400] if same and isinstance(note_value, str)
                                else "verification alignment mismatch")
                        verified_evidence = None
                        if verdict == "supported":
                            from looplab.trust.memo_verify import finalize_verified_evidence
                            verified_evidence, stale_reason = finalize_verified_evidence(c, v, final)
                            if verified_evidence is None:
                                # an old bare node id, a capped evidence set, or a lifecycle
                                # changed after verification is audit history, never durable positive support.
                                verdict = "unverified"
                                note = stale_reason
                                evidence_complete = False
                        # Forward only the fields the D8 writer understands. Unknown model output remains
                        # untrusted run-local data and cannot hitch a ride into durable cross-run memory.
                        prepared = {
                            "statement": statement,
                            "node_ids": (verified_evidence["node_ids"]
                                         if verified_evidence is not None else c.get("node_ids")),
                            "urls": (verified_evidence["urls"]
                                     if verified_evidence is not None else c.get("urls")),
                            "verification": {"verdict": verdict, "method": method, "note": note},
                        }
                        if verified_evidence is not None:
                            prepared.update(verified_evidence)
                        for key in ("metric_name", "metric_key", "objective_metric", "metric", "fingerprint"):
                            if key in c:
                                prepared[key] = c.get(key)
                        claims.append(prepared)
                    except Exception:  # noqa: BLE001 - one hostile legacy item is one omitted source slot
                        claims.append(None)
            if not d8_source_observed:
                return
        except Exception:  # noqa: BLE001 — extraction is best-effort, never fails a run
            return
        from looplab.engine.claims import record_research_claims
        record_research_claims(self._e.memory_dir, run_id=final.run_id or final.task_id,
                               task_id=final.task_id, claims=claims,
                               direction=final.direction, claims_total=claims_total,
                               claims_receipt_known=claims_receipt_known,
                               evidence_complete=evidence_complete)
