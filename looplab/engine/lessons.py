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

from pathlib import Path
from typing import TYPE_CHECKING

import orjson

from looplab.core.models import RunState
from looplab.engine.lessons_distill import LessonDistillMixin
# The role constants moved to lessons_priors.py with the prior renderer that filters on them;
# re-imported so `from looplab.engine.lessons import LESSON_ROLE_*` (tests, cross-run tooling)
# keeps resolving.
from looplab.engine.lessons_priors import (  # noqa: F401
    LESSON_ROLE_DEVELOPER, LESSON_ROLE_RESEARCHER, LessonPriorsMixin)
from looplab.engine.lessons_reconcile import LessonReconcileMixin
from looplab.engine.memory import JsonlCaseLibrary
from looplab.events.eventstore import read_jsonl_lenient, write_jsonl_atomic
from looplab.events.replay import fold
from looplab.events.types import EV_LESSONS_DISTILLED, EV_LESSONS_REFRESHED

if TYPE_CHECKING:  # engine type hint only — no runtime import of the orchestrator
    from looplab.engine.orchestrator import Engine


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
        # CODEX AGENT: Winner parameter names are outcome-derived, so the alleged task identity changes
        # when another node wins or a run is extended. Scope fingerprints must come from the immutable
        # task/comparison contract; result features belong on attempts/effects.
        pnames = list((best.idea.params or {}).keys()) if best is not None and best.idea else []
        return task_fingerprint(getattr(self._e.task, "kind", ""), final.direction,
                                final.goal or getattr(self._e.task, "goal", ""),
                                metric=str(getattr(self._e.task, "metric", "") or ""),
                                param_names=pnames,
                                universal=bool(getattr(self._e, "_fingerprint_universal", False)))

    def append_lessons(self, lessons: list, *, hygiene: bool = True) -> None:
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
        base.mkdir(parents=True, exist_ok=True)
        path = base / "lessons.jsonl"
        payload = "".join(orjson.dumps(lz).decode() + "\n" for lz in lessons)
        with _interprocess_lock(Path(str(path) + ".lock")):
            with open(path, "a", encoding="utf-8") as f:
                f.write(payload)
            if hygiene:
                # D2 hygiene: consolidate the store after appending — merge duplicate claims into
                # an evidence_count, retire contradicted verdicts (newest wins), THEN bound size. The
                # Researcher client + embedder enable the hybrid+agent paraphrase-merge pass (run end
                # only — hygiene=False mid-run skips it, so the shared file isn't rewritten every node).
                # prompts/parser travel WITH the client so a merge_system.md override and the run's
                # configured structured-output parser reach the merge's adjudication call (I18/ADR-8).
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
        last = max((int(d.get("at_node") or 0) for d in state.lessons_distilled), default=0)
        if not self._e._cadence_due(n, last, self._e.lessons_every):
            return state
        fp = self._e._task_fingerprint(state, state.best())
        lessons, pairs = self._e._comparative_lessons(state, fp, exclude=self._e._spent_pairs(state))
        # Event BEFORE the store write, and always — even with 0 lessons — so the at_node gate
        # advances and the loop doesn't retry this node-count. Event-first ordering: if the
        # process dies between the two writes, a resume sees the gate advanced and skips — the
        # store misses one batch (best-effort memory) instead of re-invoking the LLM and
        # appending the same lessons twice. The statements ride in the event for audit.
        self._e.store.append(EV_LESSONS_DISTILLED, {
            "at_node": n, "trigger": "cadence", "count": len(lessons),
            "pairs": [[pr["a"], pr["b"]] for pr in pairs],
            "lessons": [{"statement": lz["statement"], "outcome": lz["outcome"],
                         "evidence": lz.get("evidence")} for lz in lessons]})
        # Hygiene deferred to run end: the read path dedups/quarantines already, and a full-file
        # rewrite of the shared store every few nodes would race other runs' appends for nothing.
        self._e._append_lessons(lessons, hygiene=False)
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
        last = max((int(d.get("at_node") or 0) for d in state.lessons_refreshed), default=0)
        if not self._e._cadence_due(n, last, self._e.lessons_refresh_every):
            return state
        stamp = self._e._lessons_store_stamp()
        if stamp == self.seen_stamp:
            self._e.store.append(EV_LESSONS_REFRESHED, {"at_node": n, "skipped": "unchanged"})
            return fold(self._e.store.read_all())
        self.seen_stamp = stamp
        before = (self.prior_note_text, self.dev_prior_note_text)
        rid = state.run_id or None
        # ONE scan for BOTH role priors (see load_reflection_priors_both). `changed` must reflect
        # EITHER prior moving — a concurrent run distilling only developer-tagged code-fix lessons
        # updates just dev_prior_note_text, and the refresh audit signal must not report that as
        # unchanged. `chars` sums both priors so the size delta is likewise visible for either role.
        self.prior_note_text, self.dev_prior_note_text = \
            self._e._load_reflection_priors_both(exclude_run_id=rid)
        self._e.store.append(EV_LESSONS_REFRESHED, {
            "at_node": n, "chars": len(self.prior_note_text) + len(self.dev_prior_note_text),
            "changed": (self.prior_note_text, self.dev_prior_note_text) != before})
        return fold(self._e.store.read_all())

    def reflect_client(self):
        """The LLM client to use for run-end distillation — the Researcher's (unwrapping any
        surrogate/fallback), else the Developer's. None when no LLM client is wired (toy backends)."""
        r = getattr(self._e, "researcher", None)
        for obj in (r, getattr(r, "inner", None), getattr(r, "fallback", None),
                    getattr(self._e, "developer", None)):
            c = getattr(obj, "client", None)
            if c is not None and hasattr(c, "complete_text"):
                return c
        return None

    @staticmethod
    def consolidate_lessons_file(path: Path, client=None, embed=None,
                                 parser: str = "tool_call", prompts=None) -> None:
        """D2: rewrite lessons.jsonl through `consolidate_lessons` — duplicate claims merge into
        an evidence_count and a contradicted verdict is retired (the newest observation wins). When a
        `client` is wired, a hybrid-retrieval + agent pass ALSO merges paraphrase-level duplicates the
        exact key misses (`parser`/`prompts` configure that pass's structured-output parser and
        merge_system override). Atomic rewrite; best-effort (a hygiene failure must never fail the run)."""
        try:
            from looplab.engine.memory import consolidate_lessons
            rows = read_jsonl_lenient(path)
            merged = consolidate_lessons(rows, client=client, embed=embed,
                                         parser=parser, prompts=prompts)
            if len(merged) < len(rows):
                write_jsonl_atomic(path, merged)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def compact_lessons(path: Path, max_lines: int = 4000, keep: int = 2000) -> None:
        """Bound the shared lessons store: it is re-read and scored at every run start, and grows by
        a few lines per finished run forever. Past `max_lines`, keep the most recent `keep` (recency
        also wins ties at retrieval, so the dropped prefix is the least useful part)."""
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) > max_lines:
                from looplab.core.atomicio import atomic_write_text
                atomic_write_text(path, "\n".join(lines[-keep:]) + "\n")
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
        lib.add({
            "task_id": final.task_id,
            "goal": final.goal,
            "direction": final.direction,
            "params": best.idea.params,
            "metric": best.robust_metric,
            "rationale": best.idea.rationale,
        })

    def store_concept_capsule(self, final: RunState) -> None:
        """PART IV cross-run Step 2 (§21.20): persist this run's CONCEPT capsule to the shared
        `memory_dir` so a later SIMILAR run can surface "this was tried before -> outcome". Best-effort
        and self-contained: reuses the shipped per-run `node_concepts` tags (no new tagger) and the
        universal-aware `task_fingerprint`; per-concept outcome = the best robust_metric among nodes
        carrying that concept. Never raises — cross-run memory must never fail a run."""
        try:
            if not self._e.memory_dir:
                return
            node_concepts = getattr(final, "node_concepts", None) or {}
            if not node_concepts:
                return                          # nothing tagged this run -> no capsule (no-op)
            from looplab.engine.memory import ConceptCapsuleStore, build_concept_capsule
            direction = final.direction or "min"

            def _better(a, b) -> bool:          # is metric a strictly better than b for this direction?
                return a < b if direction == "min" else a > b

            concepts, outcomes = set(), {}
            for nd in final.nodes.values():
                m = getattr(nd, "robust_metric", None)
                # CODEX AGENT: These are raw per-run labels and ignore `final.concept_consolidation`.
                # Exact string matching therefore misses aliases and merges homonyms; persist a canonical
                # concept UID + taxonomy release while retaining the raw label for audit.
                for c in (node_concepts.get(nd.id) or node_concepts.get(str(nd.id)) or []):
                    ck = str(c)
                    concepts.add(ck)
                    # CODEX AGENT: Assigning the whole node score to every co-tagged concept and selecting
                    # the best score is not a concept outcome. It is confounded/cherry-picked and currently
                    # includes infeasible, tombstoned, failed, aborted, or untrusted evidence; store
                    # node-qualified measurements, eligibility, comparator, and uncertainty instead.
                    if m is not None and (outcomes.get(ck) is None or _better(m, outcomes[ck])):
                        outcomes[ck] = m
            if not concepts:
                return
            best = final.best()
            capsule = build_concept_capsule(
                run_id=final.run_id or final.task_id, direction=direction, concepts=concepts,
                fingerprint=self.task_fingerprint(final, best),
                best_metric=(best.robust_metric if best is not None else None),
                concept_outcomes=outcomes)
            ConceptCapsuleStore(Path(self._e.memory_dir) / "concept_capsules.jsonl").add(capsule)
        except Exception:  # noqa: BLE001 — cross-run capsule write is best-effort, never fails a run
            # CODEX AGENT: Swallowing the write failure here makes `finalize_run` believe this step
            # succeeded, commit `finalization_finished`, and never retry the missing capsule. Return a
            # success signal or re-raise a typed persistence error to the outer retry handshake.
            return
