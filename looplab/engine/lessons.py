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

from looplab.core.atomicio import append_jsonl_bytes_locked
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
        # NOTE (CODEX): the winner's param NAMES are outcome-derived, so this fingerprint shifts when a new
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
        payload = b"\n".join(orjson.dumps(lz) for lz in lessons)
        with _interprocess_lock(Path(str(path) + ".lock")):
            append_jsonl_bytes_locked(path, payload)
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
                         "claim_stance": lz.get("claim_stance"),
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
        if not self._e.memory_dir:
            return
        try:
            node_concepts = getattr(final, "node_concepts", None) or {}
            if not node_concepts:
                return                          # nothing tagged this run -> no capsule (no-op)
            from looplab.engine.memory import build_concept_capsule
            from looplab.events.replay import promotion_eligible_nodes
            direction = final.direction or "min"

            def _better(a, b) -> bool:          # is metric a strictly better than b for this direction?
                return a < b if direction == "min" else a > b

            # NOTE (CODEX): raw per-run concept LABELS — no `concept_consolidation` canonicalization / UID /
            # taxonomy version yet (the CR1a concept_uid resolver is the §21.20.13 TODO), so a later reader
            # matches by exact string. Attempt coverage retains every tagged node, while a durable numeric
            # outcome may come only from the same feasible, live, unflagged pool used for promotion.
            concepts, outcomes = set(), {}
            eligible_ids = {node.id for node in promotion_eligible_nodes(final)}
            for nd in final.nodes.values():
                m = getattr(nd, "robust_metric", None)
                for c in (node_concepts.get(nd.id) or node_concepts.get(str(nd.id)) or []):
                    concepts.add(str(c))
                    if (nd.id in eligible_ids and m is not None
                            and (outcomes.get(c) is None or _better(m, outcomes[c]))):
                        outcomes[str(c)] = m
            if not concepts:
                return
            best = final.best()
            capsule = build_concept_capsule(
                run_id=final.run_id or final.task_id, task_id=final.task_id, direction=direction,
                concepts=concepts, fingerprint=self.task_fingerprint(final, best),
                best_metric=(best.robust_metric if best is not None else None),
                concept_outcomes=outcomes)
        except Exception:  # noqa: BLE001 — BUILD is best-effort: a projection hiccup must never fail a run
            return
        # The WRITE is NOT swallowed (mirrors store_case): a real persistence failure must reach finalize's
        # retry handshake (which sets complete=False and retries on the next re-entry) rather than being
        # silently lost while `finalization_finished` is committed (CODEX).
        from looplab.engine.memory import ConceptCapsuleStore
        ConceptCapsuleStore(Path(self._e.memory_dir) / "concept_capsules.jsonl").add(capsule)

    def _already_curated(self, log_name: str, final: RunState) -> bool:
        """True when this run already produced a steward log entry — so a finalize RE-ENTRY (crash+resume)
        does NOT re-run the LLM and append a duplicate proposal batch to the operator queue (mega-review)."""
        import json
        from looplab.events.eventstore import read_jsonl_lenient
        p = Path(self._e.memory_dir) / log_name
        if not p.exists():
            return False
        rid = str(final.run_id or final.task_id)
        return any(str(r.get("run_id") or "") == rid
                   for r in read_jsonl_lenient(p, loads=json.loads, dicts_only=True))

    def _append_curation_once(self, log_name: str, final: RunState, rec: dict) -> bool:
        """Append one finalize steward outcome exactly once per run under the governance lock."""
        from looplab.engine.concept_registry import _append_governance
        from looplab.events.eventstore import read_jsonl_lenient
        import json

        class _AlreadyLogged(RuntimeError):
            pass

        path = Path(self._e.memory_dir) / log_name
        path.parent.mkdir(parents=True, exist_ok=True)
        rid = str(final.run_id or final.task_id)

        def _validate_locked() -> None:
            if path.exists() and any(str(r.get("run_id") or "") == rid
                                     for r in read_jsonl_lenient(path, loads=json.loads, dicts_only=True)):
                raise _AlreadyLogged

        payload = {"v": 1, "run_id": rid, "task_id": str(final.task_id or ""), **rec}
        try:
            _append_governance(path, payload, validate=_validate_locked)
            return True
        except _AlreadyLogged:
            return False

    def store_concept_curation(self, final: RunState) -> None:
        """PART IV §22.4 — the AGENTIC taxonomy steward at finalize: when `cross_run_curation` is on and an
        LLM client is available (`reflect_client`), let the LLM review the freshly-updated portfolio concept
        graph and PROPOSE a curation (merge/split/purge). Every outcome, including an empty proposal or an
        unavailable client, is durably LOGGED to `concept_curation_log.jsonl` for operator ratification.
        Finalize never applies an agent proposal: mutation requires an explicit operator CLI/API action.
        Portfolio-scoped and fully decoupled from the run's terminal state — best-effort, never raises."""
        if not (self._e.memory_dir and getattr(self._e, "_cross_run_curation", False)):
            return
        auto_requested = bool(getattr(self._e, "_cross_run_curation_auto", False))
        try:
            if self._already_curated("concept_curation_log.jsonl", final):
                return                          # idempotent on a finalize re-entry: don't re-run the LLM
            client = self.reflect_client()
            if client is None:
                self._append_curation_once("concept_curation_log.jsonl", final, {
                    "outcome": "unavailable", "auto": False, "auto_requested": auto_requested,
                    "proposals": {"merges": [], "splits": [], "purges": []}, "receipt": None})
                return
            from looplab.engine.concept_steward import curation_is_empty, steward_concepts
            # Finalize is an untrusted-agent proposal boundary. Even the legacy `auto` flag cannot mutate
            # taxonomy before a durable receipt; only an explicit operator command may apply.
            out = steward_concepts(self._e.memory_dir, client, apply=False, by="steward")
            proposals = out["proposals"]
            self._append_curation_once("concept_curation_log.jsonl", final, {
                "outcome": "empty" if curation_is_empty(proposals) else "proposed",
                "auto": False, "auto_requested": auto_requested, "proposals": proposals, "receipt": None})
        except Exception as exc:  # noqa: BLE001 — agentic curation must never fail a run
            try:
                self._append_curation_once("concept_curation_log.jsonl", final, {
                    "outcome": "error", "error_type": type(exc).__name__, "auto": False,
                    "auto_requested": auto_requested,
                    "proposals": {"merges": [], "splits": [], "purges": []}, "receipt": None})
            except Exception:  # noqa: BLE001 — logging remains best-effort relative to run finalization
                pass

    def store_claim_curation(self, final: RunState) -> None:
        """PART IV §22.4 — the AGENTIC CLAIM steward at finalize (companion to `store_concept_curation`):
        the LLM reviews the evidence-grounded claim assessments and PROPOSES operator decisions
        (ratify/reject/pin). All outcomes are locked/durably logged to `claim_curation_log.jsonl`; finalize
        never applies them. Same gate/decoupling/best-effort contract as the concept steward."""
        if not (self._e.memory_dir and getattr(self._e, "_cross_run_curation", False)):
            return
        auto_requested = bool(getattr(self._e, "_cross_run_curation_auto", False))
        try:
            if self._already_curated("claim_curation_log.jsonl", final):
                return                          # idempotent on a finalize re-entry: don't re-run the LLM
            client = self.reflect_client()
            if client is None:
                self._append_curation_once("claim_curation_log.jsonl", final, {
                    "outcome": "unavailable", "auto": False, "auto_requested": auto_requested,
                    "proposals": {"decisions": []}, "receipt": None})
                return
            from looplab.engine.claim_steward import curation_is_empty, steward_claims
            out = steward_claims(self._e.memory_dir, client, apply=False, by="steward")
            proposals = out["proposals"]
            self._append_curation_once("claim_curation_log.jsonl", final, {
                "outcome": "empty" if curation_is_empty(proposals) else "proposed",
                "auto": False, "auto_requested": auto_requested, "proposals": proposals, "receipt": None})
        except Exception as exc:  # noqa: BLE001 — agentic curation must never fail a run
            try:
                self._append_curation_once("claim_curation_log.jsonl", final, {
                    "outcome": "error", "error_type": type(exc).__name__, "auto": False,
                    "auto_requested": auto_requested, "proposals": {"decisions": []}, "receipt": None})
            except Exception:  # noqa: BLE001
                pass

    def store_task_facets(self, final: RunState) -> None:
        """PART IV §21.20.2 — propose task facets and queue them for operator ratification.

        Facets can widen retrieval scope, so agent output is never silently promoted into policy at finalize.
        Outcomes are written once/run to `task_facets_curation_log.jsonl`, including empty/unavailable ones.
        """
        if not (self._e.memory_dir and getattr(self._e, "_cross_run_curation", False)):
            return
        auto_requested = bool(getattr(self._e, "_cross_run_curation_auto", False))
        try:
            if self._already_curated("task_facets_curation_log.jsonl", final):
                return
            tid = str(getattr(final, "task_id", "") or "")
            if not tid:
                return
            from looplab.engine.task_facets import load_task_facets, propose_task_facets
            current = load_task_facets(self._e.memory_dir).get(tid)
            if current is not None:
                self._append_curation_once("task_facets_curation_log.jsonl", final, {
                    "outcome": "already-governed", "auto": False, "auto_requested": auto_requested,
                    "proposals": {"task_id": tid, "facets": current}, "receipt": None})
                return
            client = self.reflect_client()
            if client is None:
                self._append_curation_once("task_facets_curation_log.jsonl", final, {
                    "outcome": "unavailable", "auto": False, "auto_requested": auto_requested,
                    "proposals": {"task_id": tid, "facets": {}}, "receipt": None})
                return
            kind = str(getattr(getattr(self._e, "task", None), "kind", "") or "")
            facets = propose_task_facets(str(getattr(final, "goal", "") or ""), kind, client)
            self._append_curation_once("task_facets_curation_log.jsonl", final, {
                "outcome": "proposed" if facets else "empty", "auto": False,
                "auto_requested": auto_requested,
                "proposals": {"task_id": tid, "facets": facets}, "receipt": None})
        except Exception as exc:  # noqa: BLE001 — agentic faceting must never fail a run
            try:
                self._append_curation_once("task_facets_curation_log.jsonl", final, {
                    "outcome": "error", "error_type": type(exc).__name__, "auto": False,
                    "auto_requested": auto_requested,
                    "proposals": {"task_id": str(final.task_id or ""), "facets": {}}, "receipt": None})
            except Exception:  # noqa: BLE001
                pass

    def store_research_claims(self, final: RunState) -> None:
        """PART IV/§21.20 — persist this run's D8 deep-research claims (from the memo ledger) to the
        cross-run `research_claims.jsonl`, so evidence-backed research findings survive their run and can
        support/contest lesson verdicts. Best-effort BUILD; the WRITE reaches finalize's retry handshake."""
        if not self._e.memory_dir:
            return
        try:
            claims = []
            for memo in (getattr(final, "research", None) or []):
                if not isinstance(memo, dict):
                    continue
                raw_claims = memo.get("claims") or []
                verification = memo.get("verification") if isinstance(memo.get("verification"), dict) else {}
                verdicts = verification.get("verdicts") if isinstance(verification.get("verdicts"), list) else []
                method = str(verification.get("method") or "")[:80]
                for i, c in enumerate(raw_claims):
                    if not isinstance(c, dict):
                        continue
                    # `verify_memo` promises an index-aligned verdict list.  Fail closed if a malformed event
                    # breaks that alignment or names a different statement: the citation remains drillable,
                    # but it is never upgraded into positive support.
                    v = verdicts[i] if i < len(verdicts) and isinstance(verdicts[i], dict) else {}
                    same = str(v.get("statement") or "").strip() == str(c.get("statement") or "").strip()
                    verdict = str(v.get("verdict") or "unverified") if same else "unverified"
                    claims.append({**c, "verification": {
                        "verdict": verdict, "method": method,
                        "note": str(v.get("note") or "")[:400] if same else "verification alignment mismatch",
                    }})
            if not claims:
                return
        except Exception:  # noqa: BLE001 — extraction is best-effort, never fails a run
            return
        from looplab.engine.claims import record_research_claims
        record_research_claims(self._e.memory_dir, run_id=final.run_id or final.task_id,
                               task_id=final.task_id, claims=claims,
                               direction=final.direction)
