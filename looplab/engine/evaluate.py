"""The eval task (`_evaluate` — the engine's single largest method: materialize -> eval ->
trust scans -> inline repair loop -> ONE terminal event) — extracted from orchestrator.py as a
MIXIN: `class Engine(EvaluateMixin, …)` inherits it unchanged, so there is ZERO call-site churn
and `self` here IS the engine. The body is a verbatim move and reads engine attributes freely
(~30 of them: `_write_lock`, `proxy_scorer`, `_inline_repair*`, `sandbox`, trust knobs, …); its
helpers (`_materialize`/`_run_eval`/`_triage_crash`/`_repair`/`_safe_reuse_start`/
`_audit_workdir_writes`/…) resolve through `self` — onto the sibling mixins or the Engine
class itself (`_materialize`/`_write_node_files` stay in orchestrator.py).

`fold` is imported from its canonical home here (the orchestrator's module-global `fold` seam —
monkeypatched by two tests — does not reach `_evaluate`: those patches gate node CREATION).
Invariant #2 lives in this file: exactly ONE terminal event per node, emitted at the end of the
attempt loop. Trust scans (reward-hack / code-leakage / critic) stay lazy, method-local imports."""
from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from typing import Optional

import anyio
import orjson

from looplab.core.models import (NodeStatus, developer_artifact_footprint, is_developer_error,
                                 normalize_extra_metrics)
from looplab.core.node_evidence import begin_metrics_attempt
from looplab.engine.asha_monitor import extract_resource_curve
from looplab.engine.options import _UNSET
from looplab.engine.train_monitor import eval_log_plan, snapshot_training_logs

# Watchdog/monitor ticks get their OWN thread pool, separate from anyio's shared 40-token default.
# Every `to_thread.run_sync` in the engine draws on that default, and an in-flight eval holds a token
# for its whole (often multi-hour) duration — so at high `eval_parallel` the evals pin the pool and
# every liveness poll (operator abort/reset detection, the train and ASHA kill signals) queues behind
# them, going blind exactly when a kill matters. These ticks are short reads; a small pool is enough,
# and is what keeps them immediately schedulable. Process-wide and lazily built so importing this
# module never touches the event loop.
_WATCH_THREADS = 8
_WATCH_LIMITER: "anyio.CapacityLimiter | None" = None


def _watch_limiter() -> "anyio.CapacityLimiter":
    global _WATCH_LIMITER
    if _WATCH_LIMITER is None:
        _WATCH_LIMITER = anyio.CapacityLimiter(_WATCH_THREADS)
    return _WATCH_LIMITER
from looplab.engine.triage import (_MAX_DEP_ROUNDS, _environment_failure, _failure_reason,
                                   _normalize_error_sig)
from looplab.events.replay import fold
from looplab.runtime.sandbox import GpuPinUnenforceable
from looplab.events.types import (EV_CARD_DROPPED, EV_DEPS_INSTALLED, EV_NODE_ABORT,
                                  EV_NODE_EVAL_STARTED,
                                  EV_NODE_EVALUATED, EV_NODE_FAILED, EV_NODE_REPAIRED,
                                  EV_NODE_RESET, EV_PAUSE, EV_PROXY_SCORED,
                                  EV_REWARD_HACK_SUSPECTED,
                                  EV_SPEC_DRIFT, EV_STAGE_FINISHED)


def _card_identity_spellings(state, raw_card_id) -> frozenset[str]:
    """Return the unambiguous spellings of one folded Card identity.

    Nodes intentionally retain their immutable proposal-time ``card_id`` while replay collapses
    merged Cards onto the canonical row.  Active controls therefore have to compare identities,
    not just the two raw strings.  A spelling owned by more than one row is excluded, and an
    ambiguous node subject resolves to nothing: cancellation must fail closed.
    """
    cards = getattr(state, "cards", None)
    if not isinstance(raw_card_id, str) or not raw_card_id or not isinstance(cards, Mapping):
        return frozenset()

    owners: dict[str, set[str]] = {}
    for canonical, card in cards.items():
        if not isinstance(canonical, str) or not canonical:
            continue
        spellings = {canonical}
        aliases = getattr(card, "aliases", None)
        if isinstance(aliases, list):
            spellings.update(alias for alias in aliases if isinstance(alias, str) and alias)
        for spelling in spellings:
            owners.setdefault(spelling, set()).add(canonical)

    subject_owners = owners.get(raw_card_id, set())
    if len(subject_owners) != 1:
        return frozenset()
    subject = next(iter(subject_owners))
    return frozenset(
        spelling for spelling, spelling_owners in owners.items()
        if spelling_owners == {subject}
    )


def _workdir_manifest_digest(node) -> str:
    """Digest of the node source manifest a workdir was materialized from.

    Module-level on purpose: `_evaluate` takes a lazy `import hashlib` further down, which would make
    `hashlib` an unbound function-local for any closure defined above it.
    """
    return hashlib.sha256(orjson.dumps(
        {"attempt": node.attempt, "code": node.code,
         "files": node.files or {}, "deleted": sorted(node.deleted or [])},
        option=orjson.OPT_SORT_KEYS)).hexdigest()


class SpeculativeEvaluationInvariantError(AssertionError):
    """A speculative build reached an evaluation without a confirmed selection. See below."""


class EvaluateMixin:
    """The engine's eval-task cluster. See the module docstring for the mixin convention
    (`self` is the Engine)."""

    def _assert_speculative_selection_confirmed(self, state, node) -> None:
        """INVARIANT: a speculative build must never consume an evaluation before its selection
        is confirmed.

        This is the load-bearing premise of admitting positive ``speculation_depth`` on real
        Dataset/Repo/Command workloads (see the admission block in `engine/orchestrator.py`): a
        prediction that misses is thrown away BEFORE it runs, so a miss costs one Developer call and
        zero GPU seconds, and its node-budget slot is refunded. Every part of that argument collapses
        the moment a speculative node can reach the sandbox on an unconfirmed selection: the miss
        would then be real GPU time on a real training run, which the argument does not cover and
        which MUST NOT be admitted on it. (The harm did not go to zero when the refund landed — a
        measured A/B put `mean_normalized_regret` at 0.0026 rather than 0.0256 — so the cheap
        regression signal stays too: `speculation_budget_observation` in the run's `budget` receipt.)

        THE SAME PREDICATE, both sides of the lifecycle. Confirmation is the durable
        ``card_build_done`` link (`state.speculative_nodes`) binding this exact attempt-zero lifecycle
        to the Card it was built for — the pre-terminal half of the very fact
        `core/models.py::is_unevaluated_speculative_discard` proves post-terminal before it
        refunds a slot (both go through `_durable_speculative_lifecycle`). This method deliberately
        introduces NO third spelling: it calls `SpeculationMixin._speculative_link_matches`, the
        engine's own one, already used by `_run_card_session`'s admission gate.

        The producer appends the link only after the consumer claims the build as the selection it
        actually wanted, and `_run_card_session` re-runs `speculative_card_is_fresh` immediately
        before dispatch — so the link is the durable, replay-visible half of a confirmation the
        session has already re-checked in memory. Asserted HERE because `_evaluate` is the single
        funnel every evaluation passes through — the card session, the ordinary dispatcher, recovery
        and direct library callers all arrive here — so no future path can reach a sandbox around it.

        Spelled as an explicit raise rather than `assert`: `python -O` strips assert statements, and
        an invariant whose whole purpose is to stop unbudgeted GPU spend must not be optimized out.
        """
        if getattr(node, "speculative", False) is not True or node.attempt != 0:
            return
        if not self._speculative_link_matches(state, node):
            raise SpeculativeEvaluationInvariantError(
                f"speculative node {node.id} reached evaluation without a confirmed selection "
                "(no matching card_build_done link); refusing to spend evaluation budget on an "
                "unconfirmed prediction"
            )

    def _record_eval_start_boundary(self, node) -> bool:
        """Append the durable eval-START boundary for one lifecycle, at most once.

        THE ONE SPELLING, called from two places on purpose. The dispatch decision belongs to the
        MAIN task (`_run_card_session`'s admission), and writing it there is what keeps it out of the
        speculative election's compare-and-swap window: `_request_card_build` reads the log, consults
        the Card scorer, then appends with `expected_last_seq`, and a row appended by a WORKER inside
        that window makes the election lose its CAS. Doing that once per eval defeated the prefetch
        request every single turn — a depth-1 treatment run silently became serial (measured: 17
        nodes built / 5 discarded became 12 / 0, with zero producer/consumer overlap left).
        `_evaluate` still calls it, because `_evaluate` is the single funnel every evaluation passes
        through and the boundary must exist before ANY caller reaches a sandbox — recovery and direct
        library callers included. In a card session that call is already satisfied by the folded flag
        and appends nothing.

        Unlocked, like `_record_card_build_attempt`: it is ONE independent per-node row that the fold
        keys by (node, generation) and applies set-only, so it pairs with nothing and its splice
        position cannot change any other event's meaning.

        COST, for whoever re-runs `looplab speculation-gate`: one append is one flush+fsync, which on
        a network/FUSE run dir is not free — measured on this box at ~200 ms against ~1 ms on local
        disk. Against a real evaluation that is noise (a training run is minutes), but it is the same
        order as the calibration harness's ~300 ms toy eval, and there it can flip the
        producer/consumer race the benchmark measures: on a FUSE run dir seed 2 closes prefetches as
        `stale` at commit instead of creating and discarding them, which the clean-protocol validator
        refuses to score. On a local-disk run dir the A/B reproduces its published aggregates exactly
        (`mean_normalized_regret` 0.002590, `mean_hit_rate` 0.751961). Put the calibration run dirs on
        local disk.
        """
        if (getattr(node, "eval_start_boundary", False) is not True
                or getattr(node, "eval_started", False) is True):
            return False
        self.store.append(EV_NODE_EVAL_STARTED, {
            "node_id": node.id, "generation": node.attempt})
        return True

    @property
    def _probe_developer(self):
        """Developer used for ablation *probes* (I7): the raw inner developer, bypassing
        any ValidatingDeveloper's retry/fallback. Probes are a measurement harness, not a
        shipped step — routing them through validation would (a) substitute the LLM
        fallback mid-measurement, corrupting impact numbers, and (b) multiply expensive
        external-agent calls by len(params) per ablation (ADR-7 cost rule)."""
        return getattr(self.developer, "inner", self.developer)

    async def _evaluate(self, node_id: int, limiter: anyio.CapacityLimiter,
                        max_es: Optional[float] = None) -> None:
        async with limiter:
          with self.tracer.span("evaluate", new_trace=True, node_id=node_id) as sp:
            events_at_start = self.store.read_all()
            state = fold(events_at_start)
            node = state.nodes.get(node_id)
            # The dispatcher checks this before and after resource admission, but _evaluate is also a
            # defensive public seam used by recovery/tests. An operator Card drop that predates this
            # worker must close the pending lifecycle at zero cost; the watcher below intentionally
            # considers only post-start events so it can charge genuinely consumed compute.
            prestart_stop = getattr(self, "_skip_if_aborted", None)
            if node is not None and callable(prestart_stop) \
                    and prestart_stop({"node_id": node_id}, state):
                return
            # A batch is selected from an earlier fold. Before this worker actually starts, reset
            # (especially implement/propose), abort, tombstone, pause or finish may have won. Never
            # evaluate blank/not-yet-rebuilt code or terminalize a superseded lifecycle.
            if (node is None or node.status is not NodeStatus.pending or node.tombstoned
                    or node.id in state.aborted_nodes or node.rerun_from is not None
                    or state.paused or state.finished or state.stop_requested):
                return
            # The one gate that keeps a speculative miss provably free: no unconfirmed prediction may
            # cross into the sandbox. See `_assert_speculative_selection_confirmed`.
            self._assert_speculative_selection_confirmed(state, node)
            generation = node.attempt       # immutable identity of THIS worker's node lifecycle
            # The trace is opened before the fold above so pre-start exits remain observable. Once
            # this worker has an exact lifecycle, stamp the root; the span index uses that root receipt
            # to keep reset attempts disjoint while every nested generation/tool stays in this trace.
            sp.set("generation", generation)
            start_seq = events_at_start[-1].seq if events_at_start else -1
            sp.set("operator", node.operator)
            # The dispatcher owns this reservation for the complete node lifecycle. Keeping the same
            # devices across every inline repair/retry prevents a repaired process from jumping onto a
            # sibling's GPU; the dispatcher releases it exactly once in its worker `finally`.
            _resource_reservation = self._eval_resource_reservation(node_id, generation)
            # The dispatcher registered this reservation under its ADMISSION-time generation, but
            # `generation` here is node.attempt from a fresher fold. An eval-stage node_reset landing in
            # that window (attempt+1, still pending, rerun_from None) passes the prestart guard above and
            # misses the current-generation lookup — and `_resource_eval_env(None)` would yield an
            # UNPINNED env that sees every sibling's GPU. If the dispatcher still holds this node's
            # reservation under the superseded key, fail closed (return without a terminal) so the
            # dispatcher re-admits the reset lifecycle under its current generation and re-pins it,
            # instead of degrading to whole-box visibility. The presence of a stale-generation reservation
            # is the authoritative signal that this node was dispatcher-admitted — a never-admitted
            # recovery/test call holds no stale key and is exempt. Gate on that reservation, NOT on the
            # live mutable `_eval_parallel`: a Strategist/operator that lowers eval_parallel to 1 mid-batch
            # (while pinned siblings are still draining) must not flip this into an unpinned launch. A
            # serial re-admit of a whole-pool-unpinned node is harmless (it just re-runs unpinned).
            if (
                _resource_reservation is None
                and self._eval_reservation_under_other_generation(node_id, generation)
            ):
                return
            try:
                eval_env = self._resource_eval_env(
                    _resource_reservation, inherit_host=True)
            except GpuPinUnenforceable as exc:
                # an explicit positive declaration on a zero-device inventory is a
                # fail-closed, zero-compute terminal — never an unpinned launch and never an endless
                # resource wait. The dispatcher's finally still releases/clears the marker exactly once.
                async with self._write_lock:
                    self.store.append(EV_NODE_FAILED, {
                        "node_id": node_id, "generation": generation,
                        "error": str(exc)[:400], "reason": "gpu_unavailable",
                        "eval_seconds": 0.0})
                    self._maybe_crash()
                return
            # A6 proxy/predictive scoring: cheaply predict this candidate's metric from the observed
            # history and skip a full eval for the doomed bottom fraction (cost lever). Deterministic
            # + replay-safe: the skip is recorded as node_failed reason="proxy_skipped" and a
            # proxy_scored audit event. OFF by default (kill_fraction=0 -> never skips).
            if self.proxy_scorer is not None and self.proxy_kill_fraction > 0:
                pred = self.proxy_scorer.score(state, node)
                if pred is not None:
                    skip = self.proxy_scorer.should_skip(state, node, pred)
                    sp.set_many(proxy_score=round(pred, 6), proxy_skipped=skip)
                    async with self._write_lock:
                        self.store.append(EV_PROXY_SCORED,
                                          {"node_id": node_id, "generation": generation,
                                           "score": round(pred, 6), "skipped": skip})
                        if skip:
                            self.store.append(EV_NODE_FAILED, {
                                "node_id": node_id, "generation": generation,
                                "error": "skipped by proxy scorer (predicted in the doomed bottom fraction)",
                                "reason": "proxy_skipped", "eval_seconds": 0.0})
                            self._maybe_crash()
                    if skip:
                        return
            # DURABLE EVAL-START BOUNDARY (events/types.py::EV_NODE_EVAL_STARTED). This is the LAST
            # instant at which "this build never ran" is still true: every line below writes a node
            # workdir and then executes the node's own code. Both zero-compute exits above (an
            # unenforceable GPU pin, a proxy skip) have already terminalized, so the boundary is never
            # stamped on a lifecycle that really did cost nothing.
            #
            # WHY AN EVENT, weighed against the alternatives. The log records evaluation cost only at
            # the TERMINAL (`events/replay.py::_charge_terminal_cost`) and appends `stage_finished`
            # rows inside that terminal's own write-lock block, so a process killed mid-training left
            # a node byte-identical to one that was never dispatched. The only thing that told them
            # apart was the IN-MEMORY `eval_inflight` set, which a resumed process starts empty — so
            # after a crash the refund fired on 40 GPU-minutes of real work. Persisting that set needs
            # a durable write anyway; a sidecar file is not a function of the log (invariant #4/#5);
            # and no existing durable fact is written between dispatch and the sandbox. Refusing every
            # refund whose lifecycle is unprovable is the remaining option and IS what happens for a
            # node without `eval_start_boundary` — this event is what lets a node that CAN prove it
            # keep the refund the calibration numbers depend on.
            #
            # Scoped to the lifecycles that can ever be refunded (`eval_start_boundary`, stamped on a
            # speculative attempt-zero `node_created`), so this is not a new per-eval append on the
            # ordinary hot path: a run without speculation writes byte-identical logs. Normally
            # already satisfied here — the card session's admission wrote it in the MAIN task, which
            # is what keeps it out of the speculative election's CAS window (see
            # `_record_eval_start_boundary`). This call is the funnel guarantee for every OTHER way
            # into a sandbox: recovery, the legacy dispatcher, a direct library caller.
            async with self._write_lock:
                self._record_eval_start_boundary(node)
            workdir = self.run_dir / "nodes" / f"node_{node_id}"
            # Phase 2 stage-scoped re-run: REUSE the existing workdir (earlier stages' artifacts — the
            # checkpoint `train` wrote) instead of re-seeding it, which would wipe them.
            _superseded_marker = workdir / ".looplab-superseded"
            def _mark_superseded_workdir() -> None:
                try:
                    workdir.mkdir(parents=True, exist_ok=True)
                    _superseded_marker.write_text(str(generation), encoding="ascii")
                except OSError:
                    import shutil
                    shutil.rmtree(workdir, ignore_errors=True)
            # Stage reuse is the ONE path that evaluates a workdir it did not just build, so it must
            # prove the bytes on disk are the manifest the folded node claims. `node_repaired` is
            # appended BEFORE the repaired files are written (both under the same attempt), so a
            # process death in that window leaves state claiming the repair while the workdir still
            # holds the pre-repair source. A later stage-scoped reset then sets `rerun_stage` without
            # any superseded marker (only the live process writes one, and it died), and reuse would
            # skip materialization and score the OLD bytes as if they were the repair.
            # The stamp closes that: it is written only after the files are on disk, so a crash in the
            # gap leaves it naming the previous manifest and reuse is refused. Fail-closed by
            # construction — a missing/unreadable/mismatched stamp just forces the full materialize
            # that every other entry path already does, costing artifacts, never correctness.
            _manifest_stamp = workdir / ".looplab-manifest"
            def _stamp_workdir(n) -> None:
                try:
                    _manifest_stamp.write_text(_workdir_manifest_digest(n), encoding="ascii")
                except OSError:
                    pass          # unstamped => the next reuse check fails closed and rematerializes
            def _workdir_matches(n) -> bool:
                try:
                    return _manifest_stamp.read_text(encoding="ascii").strip() == _workdir_manifest_digest(n)
                except (OSError, ValueError):
                    return False
            _reuse = bool(node.rerun_stage and workdir.exists()
                          and not _superseded_marker.exists() and _workdir_matches(node))
            if not _reuse:
                self._materialize(node, workdir)    # seed tree -> node edits -> task assets
                _stamp_workdir(node)                # the workdir now IS this manifest
                # A stage-scoped re-run whose workdir was GONE has nothing to reuse — the re-seed just
                # wiped any artifacts. Skipping earlier stages now would run the restarted stage against
                # MISSING inputs, so drop the start_stage and re-run the FULL pipeline instead.
                if node.rerun_stage:
                    node.rerun_stage = None
            # Bind mutable TensorBoard/metric sidecars to this exact lifecycle before launching any
            # user code.  A reset can reuse the node directory (stage restart), so the serving layer
            # also filters points by this start time instead of relabelling an older curve.
            try:
                begin_metrics_attempt(workdir, generation)
            except Exception:  # noqa: BLE001 - telemetry must never block the evaluation itself
                pass
            # Hybrid crash repair: each attempt runs the eval (with the mid-eval abort watcher) and,
            # if it CRASHES, the agent triages it and may repair the code IN PLACE and re-run — all
            # within this one node (no new tree node, no max_nodes spent). At most
            # `inline_repair_attempts` repairs OF THE EXPERIMENT (the ledger comment below the
            # anti-stuck ledger explains the second, environment-reconciliation ledger the same
            # number bounds); then the node fails normally and stays eligible for the
            # budgeted inter-node debug operator. Exactly ONE terminal event (node_evaluated/node_failed)
            # is emitted at the end so first_terminal budget accounting and resume re-entry are intact;
            # only NON-terminal `node_repaired` events are written mid-loop.
            import threading
            attempt = 0
            dep_rounds = 0                   # env-prep auto-install + re-run rounds (separate from repair attempts)
            total_eval = 0.0                 # summed subprocess wall-clock across all attempts (cost)
            async def _record_superseded() -> None:
                async with self._write_lock:
                    self.store.append(EV_NODE_FAILED, {
                        "node_id": node_id, "generation": generation,
                        "error": "superseded by node reset", "reason": "superseded",
                        "eval_seconds": total_eval})
                _mark_superseded_workdir()
            triage_outcome = None            # ("abandon"|"reject_idea", rationale) for the terminal event
            err = ""
            reason = "crash"
            # Anti-stuck ledger: how many times each normalized error signature has been seen in
            # THIS node's repair loop. Was a consecutive-streak counter (`stuck_sig`/`stuck_n`); see
            # the comment at the update site for why a streak is the wrong shape.
            stuck_seen: dict = {}
            stuck_n = 0
            # APPORTIONED repair budget. `inline_repair_attempts` is what the operator budgets for
            # REPAIRING THE EXPERIMENT; a repair that only reconciles the authored code with the
            # installed libraries (`engine/triage.py::REPAIR_CLASSES`) is a different resource and
            # draws on its own ledger of the same size, so a stale `requirements.txt` cannot consume
            # the research allowance. Measured on `runs/rubert-dr-0805` node 0: with
            # `inline_repair_attempts: 6`, all six went on PL-2.x/transformers/accelerate migrations
            # (the triage rationale said "mechanical" every time) and the first genuine research
            # question — a DDP `find_unused_parameters` modelling decision — arrived with nothing
            # left. `env_repairs` counts the exempted ones; `attempt` keeps counting ALL of them, so
            # the `node_repaired` attempt numbers stay a dense sequence and every existing reader is
            # unaffected.
            env_repairs = 0
            # Best pipeline depth any attempt has reached (stages passed/reused before the failure).
            # Reaching a LATER stage is the other evidence a repair did real work, alongside a
            # never-before-seen error signature — see `_progress` at the gate below.
            best_depth = -1
            # Multi-stage reuse across repair attempts: `next_start` is the stage to run FROM on the next
            # eval — _UNSET on the first eval (derives node.rerun_stage), then set by the safe-reuse
            # predicate after each repair (a stage name = reuse the completed earlier stages, e.g. skip
            # re-train when only the score script was fixed; None = a full re-run). `full_retrains` counts
            # the EXPENSIVE full re-runs a repair forced, bounded by inline_repair_retrain_cap.
            next_start = _UNSET
            full_retrains = 0
            while True:
                _t0 = time.time()
                # repair/retry attempts reuse the workdir and sandbox stage logs append.
                # When either watchdog is enabled, snapshot every existing log before this attempt
                # starts so it cannot rank/classify prior-attempt bytes. Keep the monitor-off path free
                # of extra filesystem work (`off == today`).
                _eval_spec = getattr(self, "_eval_spec", None)
                _watching_logs = (
                    (getattr(self, "_train_monitor", False) and bool(_eval_spec))
                    or (getattr(self, "_asha_live", False) and isinstance(_eval_spec, dict)))
                _log_snapshot = snapshot_training_logs(workdir) if _watching_logs else None
                # Which log each phase of THIS attempt writes. Both watchdogs live across the WHOLE
                # eval — setup, every stage, and the ALWAYS-appended `score` stage — so without the
                # resolved pipeline they can only guess whose bytes they are reading, and the freshest
                # `*.log` is `setup.log` during a pip install and `score.log` after the training has
                # already SUCCEEDED. `_resolved_stages` re-resolves exactly what `_run_eval` will run
                # ([] = the single-command path, whose `eval.log` IS the training log).
                _log_plan = eval_log_plan(self._resolved_stages(node, workdir)) if _watching_logs else None
                # Mid-eval intervention: a watcher polls while the eval runs in a worker thread. An
                # exact node lifecycle mutation or operator drop of THIS node's Card sets the cancel
                # Event, which tree-kills the in-flight subprocess (sandbox._run_argv). The pre-eval
                # skip only catches not-yet-started nodes — this kills a running one.
                cancel = threading.Event()
                aborted = False
                superseded = False
                operator_card_dropped = False
                kill_signal: dict = {}       # filled by the training monitor if it kills a broken run (Phase 3)
                async with anyio.create_task_group() as _tg:
                    def _intervention_seen() -> str | None:
                        intervention = None
                        card_id = getattr(getattr(node, "idea", None), "card_id", None)
                        current_events = self.store.read_all()
                        operator_drop_ids: list[str] = []
                        for e in current_events:
                            if e.seq <= start_seq:
                                continue
                            if e.type == EV_CARD_DROPPED:
                                # Only the explicit operator stop affordance is an active cancel.
                                # Engine/freshness drops deliberately burn to terminal as evidence.
                                drop_id = e.data.get("id")
                                if (isinstance(drop_id, str) and drop_id
                                        and e.data.get("dropped_by") == "operator"):
                                    operator_drop_ids.append(drop_id)
                                continue
                            if e.data.get("node_id") != node_id:
                                continue
                            raw_generation = e.data.get("generation")
                            # Controls name the lifecycle they intend to mutate. Missing stamps are
                            # legacy generation-0 only; a stale gen-0 click must never cancel a gen-1
                            # worker merely because the numeric node id was reused after reset.
                            if raw_generation is None:
                                if generation != 0:
                                    continue
                            else:
                                if isinstance(raw_generation, bool):
                                    continue
                                try:
                                    event_generation = int(raw_generation)
                                except (TypeError, ValueError, OverflowError):
                                    continue
                                if (isinstance(raw_generation, float)
                                        and not raw_generation.is_integer()):
                                    continue
                                if event_generation != generation:
                                    continue
                            if e.type == EV_NODE_RESET:
                                return "reset"
                            if e.type == EV_NODE_ABORT:
                                intervention = "abort"
                        if (intervention is None and operator_drop_ids
                                and isinstance(card_id, str) and card_id):
                            # Fold only once an explicit post-start operator drop exists AND this node
                            # actually carries a Card identity to match — a card-less worker can never
                            # be a drop target (`_card_identity_spellings` returns nothing for a missing
                            # id), so skipping the fold there is behaviour-preserving, not just cheaper.
                            # This follows merge chains added before or during the eval without making
                            # every 300 ms watcher poll replay the complete run. Any corrupt/ambiguous
                            # ownership is deliberately a no-op rather than a kill of the wrong worker.
                            try:
                                active_spellings = _card_identity_spellings(
                                    fold(current_events), card_id)
                            except Exception:  # noqa: BLE001 - active cancellation must fail closed
                                active_spellings = frozenset()
                            if any(drop_id in active_spellings for drop_id in operator_drop_ids):
                                intervention = "card_drop"
                        return intervention
                    async def _watch():
                        nonlocal aborted, operator_card_dropped, superseded
                        while True:
                            await anyio.sleep(0.3)
                            if cancel.is_set():
                                return
                            # Its OWN limiter, never anyio's shared default. Every `run_sync` in the
                            # engine draws on that shared 40-token pool, and each in-flight eval's
                            # `_run_eval` worker holds a token for the eval's whole (often multi-hour)
                            # duration — so at `eval_parallel` near or above 40 (the config allows up
                            # to 1024; 0 = AUTO = GPU count) the evals pin every token and this tick
                            # queues BEHIND them. Operator abort/reset and both watchdog kills then go
                            # blind until an eval finishes on its own, and over-admitted evals sit on
                            # reserved GPUs while queued. A tick is a short poll, so a small dedicated
                            # pool is always immediately available for it.
                            intervention = await anyio.to_thread.run_sync(
                                _intervention_seen, limiter=_watch_limiter())
                            if intervention is not None:
                                superseded = intervention == "reset"
                                operator_card_dropped = intervention == "card_drop"
                                aborted = intervention in {"abort", "card_drop"}
                                cancel.set()
                                return
                    _tg.start_soon(_watch)
                    # Training-log monitor (ON by default in the product Settings since 2026-08-04;
                    # still off in a bare `Engine(...)`/`EngineOptions`): a sibling task that tails this eval's live
                    # training log on a timer while it runs in the worker thread, asks the Developer to
                    # judge its health, and records the verdict (advisory unless kill is enabled).
                    # Cancelled with the eval by `_tg.cancel_scope.cancel()` below. Gated on the
                    # command-eval path (`_eval_spec`): only those write the per-stage `<stage>.log` the
                    # monitor tails — the solution.py path (toy/dataset) has no live log to watch.
                    if getattr(self, "_train_monitor", False) and getattr(self, "_eval_spec", None):
                        _idea = getattr(node, "idea", None)
                        _rationale = (getattr(_idea, "rationale", "") or "")[:400] if _idea else ""
                        _mkey = ((self._eval_spec.get("metric") or {}).get("key", "metric")
                                 if isinstance(self._eval_spec, dict) else "metric")
                        _mon_ctx = f"Optimizing metric {_mkey!r}." + (
                            f" Experiment: {_rationale}" if _rationale else "")
                        _tg.start_soon(self._monitor_training, node_id, generation, workdir, cancel,
                                       _mon_ctx, kill_signal, _log_snapshot, _log_plan)
                    # ASHA live-curve rank watchdog (ON by default in the product Settings since
                    # 2026-08-04; still off in a bare `Engine(...)`): a sibling task that reads the live
                    # log's latest INTERMEDIATE metric and ranks it against finished siblings; advisory
                    # unless asha_live_kill. Same command-eval gate (needs a live log + the metric spec).
                    if getattr(self, "_asha_live", False) and isinstance(getattr(self, "_eval_spec", None), dict):
                        _mspec = self._eval_spec.get("metric") or {}
                        _tg.start_soon(self._monitor_asha, node_id, generation, workdir, cancel,
                                       _mspec, state.direction, kill_signal, _log_snapshot, _log_plan)
                    # The lifecycle reservation selected by the dispatcher stays unchanged through this
                    # retry. CUDA_VISIBLE_DEVICES contains physical ids (logical→physical remap), while
                    # an unspecified serial eval keeps eval_env=None and sees the whole box as before.
                    try:
                        # CODEX AGENT: an evaluator may finish paid/external side effects here, but its
                        # terminal event is appended much later. A process death in that gap makes resume
                        # run the evaluator again. Persist an attempt-scoped outcome/outbox before exposing
                        # success, or require a reconciliable idempotency key at the evaluator boundary.
                        res = await anyio.to_thread.run_sync(
                            self._run_eval, node, str(workdir), eval_env, None, cancel, next_start
                        )
                    except GpuPinUnenforceable as exc:
                        # Fail-closed device pin the Docker daemon/runtime cannot enforce. Terminalize
                        # THIS node instead of letting the raise cancel every in-flight sibling eval in
                        # the batch and re-crash deterministically on every resume; the reservation is
                        # still released by the dispatcher's finally.
                        cancel.set()
                        _tg.cancel_scope.cancel()
                        # Cancelling the task-group scope cancels THIS host task at its next checkpoint,
                        # and the write-lock acquisition below IS such a checkpoint. Without a shield the
                        # pending CancelledError preempts the append: the promised node_failed is skipped,
                        # the task group swallows its own scope's cancellation, and execution falls through
                        # to `ok = (res.metric ...)` with `res` still unbound (UnboundLocalError — NO
                        # terminal written, and a deterministic re-crash on every resume, exactly what this
                        # handler exists to prevent). Shield the terminal so scope cancellation cannot
                        # preempt it; the acquire is bounded (the watcher/monitor siblings only briefly
                        # read or append under the same lock and are already being cancelled).
                        with anyio.CancelScope(shield=True):
                            async with self._write_lock:
                                self.store.append(EV_NODE_FAILED, {
                                    "node_id": node_id, "generation": generation,
                                    "error": str(exc)[:400], "reason": "gpu_unpinnable",
                                    # Add THIS attempt's elapsed (`time.time() - _t0`) — the normal path
                                    # accumulates it only after the task group exits (line 330), which this
                                    # early return skips, so recording the bare accumulator would drop the
                                    # Docker/runtime probe + setup cost from the immutable eval budget.
                                    "eval_seconds": round(total_eval + (time.time() - _t0), 3)})
                                self._maybe_crash()
                        return
                    cancel.set()                  # eval finished on its own …
                    _tg.cancel_scope.cancel()     # … stop the watcher now (no poll-interval latency)
                total_eval = round(total_eval + (time.time() - _t0), 3)   # cumulative eval cost (#2)
                # STALL SALVAGE: a stage the stall-watchdog tree-killed AFTER it had already printed its
                # metric (a completed train+eval that only hung on teardown — a distributed finalize
                # deadlock / wedged CUDA op) still counts: the metric is real, the non-zero exit is only
                # the kill. Self-gating — `res.metric is not None` on a stall means the value WAS emitted
                # before the silence. NOT for a real deadline timeout (that is still mid-training).
                ok = (res.metric is not None and not res.timed_out
                      and (res.exit_code == 0 or getattr(res, "stalled", False)))
                if superseded:
                    # The reset discards this lifecycle's metric/state, not compute already spent. A
                    # stale-generation terminal is fold-budget-only: replay rejects its state fields
                    # but charges eval_seconds once for this immutable generation.
                    await _record_superseded()
                    return                         # the reset owns the next lifecycle generation
                if aborted and not ok:                       # killed mid-eval by the operator (and the
                    async with self._write_lock:             # eval didn't already finish cleanly first)
                        self.store.append(EV_NODE_FAILED, {
                            "node_id": node_id, "generation": generation,
                            "error": (
                                "Card dropped by operator (killed mid-eval)"
                                if operator_card_dropped
                                else "aborted by operator (killed mid-eval)"
                            ),
                            "reason": "card_dropped" if operator_card_dropped else "aborted",
                            "eval_seconds": total_eval})
                        self._maybe_crash()
                    return
                if kill_signal.get("kill") and not ok:       # a live watchdog tree-killed the run early
                    # ONE terminal event; the watchdog names the reason so the fold/failure-reflection
                    # knows WHY: the training monitor leaves it default ('monitor_broken'), the ASHA
                    # watchdog sets terminal_reason='asha_underperforming'. The advisory record
                    # (EV_TRAIN_MONITOR_ALERT / EV_ASHA_RANK) already ran live; replay reconstructs the
                    # node from this terminal and never re-invokes the watchdog.
                    _kreason = str(kill_signal.get("terminal_reason") or "monitor_broken")
                    async with self._write_lock:
                        self.store.append(EV_NODE_FAILED, {
                            "node_id": node_id, "generation": generation,
                            "error": ("live watchdog stopped the run early: "
                                      + str(kill_signal.get("reason", ""))[:400]),
                            "reason": _kreason, "eval_seconds": total_eval})
                        self._maybe_crash()
                    return
                if ok:
                    break
                reason = _failure_reason(res)
                # A clean run (exit 0) with no parseable metric is the most confusing failure for the
                # repair agent — the terse "no_metric" gave it nothing to fix, so the debug node just
                # re-ran and failed again. Tell it EXACTLY what the eval reads (the configured metric
                # key + the one line it must print), so a no-metric node can actually be repaired.
                _ms = (self._eval_spec.get("metric") or {}) if isinstance(self._eval_spec, dict) else {}
                _mk = _ms.get("key", "metric")
                _no_metric_hint = (
                    f" — the command ran cleanly (exit 0) but printed NO parseable metric. The eval reads"
                    f" a stdout JSON line for key {_mk!r}; the entrypoint MUST print exactly one line like"
                    f" print(json.dumps({{{_mk!r}: <float>}})) as its last stdout."
                    if _ms.get("kind", "stdout_json") == "stdout_json"
                    else " — ran cleanly but produced no parseable metric (check the eval's metric reader).")
                err = self._redact(res.stderr[-500:]) or (
                    f"metric drift: {res.drift}" if res.drift is not None else
                    f"exit={res.exit_code} timed_out={res.timed_out} no_metric{_no_metric_hint}"
                )
                # Environment self-prep (deps.py): a crash that is purely a missing KNOWN library is
                # not a bad idea — install it (trusted_local only) and re-run BEFORE the crash-triage
                # agent can reject the idea. This is what lets torch/XGBoost/CatBoost (e.g. a GRU
                # model) run on a fresh box instead of dying as `idea_rejected`. Bounded by
                # _MAX_DEP_ROUNDS + the `_dep_attempted` cache; does NOT consume a repair attempt (env
                # prep is not a code fix), and the unchanged node is simply re-evaluated.
                if (self._auto_install_deps and reason == "crash" and dep_rounds < _MAX_DEP_ROUNDS):
                    installed = await anyio.to_thread.run_sync(self._prepare_env, res.stderr)
                    if installed:
                        dep_rounds += 1
                        async with self._write_lock:
                            self.store.append(EV_DEPS_INSTALLED, {
                                "node_id": node_id, "generation": generation,
                                "packages": installed, "round": dep_rounds})
                        continue   # re-run now that the library is present (no repair attempt spent)
                # Anti-stuck: when the SAME error recurs with no progress, stop (even under unlimited
                # repair) so the agent doesn't loop forever on an unfixable failure.
                # T10: NORMALIZED signature — the same semantic error with different line numbers /
                # sizes / paths counts as "stuck" too (exact-match compare missed those loops).
                #
                # Counted PER SIGNATURE over the whole node, not as a consecutive streak. A streak
                # counter is defeated by ANY interleaving: an oscillating repair (fix A breaks B, fix
                # B breaks A) and a failure that cycles through variants both keep resetting it to 1
                # and it never reaches the threshold. That is precisely the shape of the 3.5 h
                # runaway — 2345 failures, longest identical run 2, threshold 4, guard never fired —
                # and with `inline_repair_attempts = 0` (UNLIMITED, an operator decision) this guard
                # is the ONLY bound on the loop, so it must not be defeatable by re-ordering.
                # Seeing one signature `inline_repair_stuck_repeat` times inside a single node is not
                # progress under any reading, consecutive or not. The normalizer (engine/triage.py)
                # documents which variation it absorbs and which it deliberately keeps distinct, so a
                # node whose error genuinely MOVES as it is fixed still mints fresh signatures and
                # keeps every attempt it is entitled to.
                _sig = _normalize_error_sig(err)
                if _sig:
                    stuck_seen[_sig] = stuck_n = stuck_seen.get(_sig, 0) + 1
                else:
                    stuck_n = 1                  # an empty signature carries no evidence either way
                # Eval-budget stop: the inline-repair loop re-runs FULL evals with no budget check
                # between attempts — the loop-top / per-eval guards only see `total_eval_seconds` from
                # TERMINAL events, and no terminal is emitted mid-repair, so an LLM whose repairs vary
                # the stderr (never tripping anti-stuck) can overshoot the eval budget by multiples
                # inside ONE node. Abandon once this node's cumulative eval time would cross the ceiling.
                # RE-FOLD before comparing. `state` is the fold taken at eval START, so under
                # eval_parallel>1 every terminal a sibling appended since this worker began is
                # invisible to it and the "cumulative ceiling" undercounts run-wide
                # total_eval_seconds — the repair loop kept re-running full evals well past
                # max_eval_seconds whenever siblings burned the remaining budget mid-loop. This is
                # invariant 4 (never carry derived state across loop iterations); one fold is cheap
                # next to the full eval it is guarding.
                if max_es is not None:
                    spent = fold(self.store.read_all()).total_eval_seconds
                    if spent + total_eval >= max_es:
                        triage_outcome = ("abandon", "eval budget exhausted during inline repair")
                        break
                # FORWARD PROGRESS: evidence that the repairs are doing real work rather than
                # circling. Two shapes, both read off state that already exists — a signature this
                # node has never produced before (the `stuck_seen` ledger's first sighting, using
                # the SAME normalizer the anti-stuck guard uses, so there is no second notion of
                # "the same failure"), or a pipeline depth no earlier attempt reached. This is what
                # keeps the environment ledger from becoming a second runaway: in the 2345-repair
                # incident every failure normalized to ONE signature, so it buys exactly one exempt
                # attempt no matter how the agent labels it, and the anti-stuck guard still
                # terminalizes the node.
                _depth = len([s for s in (res.stages or [])
                              if isinstance(s, dict) and s.get("status") in ("ok", "reused")])
                _progress = stuck_n <= 1 or _depth > best_depth
                best_depth = max(best_depth, _depth)
                # Two ledgers, one number: `charged` are the repairs that changed the EXPERIMENT,
                # `env_repairs` the ones that only reconciled it with the installed libraries. Each
                # is bounded by `inline_repair_attempts` (0 = both unlimited — unchanged behaviour,
                # the anti-stuck guard is then the only bound), so the worst case per node is 2N
                # repairs and the environment half additionally has to move the failure forward
                # every single time.
                charged = attempt - env_repairs
                budget_left = (not self._inline_repair_attempts
                               or charged < self._inline_repair_attempts)
                env_left = (not self._inline_repair_attempts
                            or env_repairs < self._inline_repair_attempts)
                # Inline-repair gate: feature on, repairable reason, a Developer that can repair, and
                # something to repair (whole-file code, multi-file edits, or a repo). The attempt CAP is
                # skipped when unlimited (_inline_repair_attempts == 0); the anti-stuck guard bounds it.
                if (not self._inline_repair
                        or reason not in self._inline_repair_reasons
                        or not (budget_left or (env_left and _progress))
                        or stuck_n >= self._inline_repair_stuck_repeat
                        or not callable(getattr(self.developer, "repair", None))
                        or not (node.code or node.files or self._repo_spec)):
                    if stuck_n >= self._inline_repair_stuck_repeat and self._inline_repair:
                        triage_outcome = ("abandon", f"the same error signature has now failed this "
                                                     f"node {stuck_n}x — stuck, abandoning")
                    break
                triage = self._triage_crash(state, node, err, attempt + 1, reason=reason,
                                            charged_attempt=charged + 1)
                action = triage.get("action", "repair")
                if action == "abandon":
                    triage_outcome = ("abandon", triage.get("rationale", ""))
                    break
                if action == "reject_idea":   # the idea itself is wrong -> mark the lineage; steer to a new idea
                    reason = "idea_rejected"
                    triage_outcome = ("reject_idea", triage.get("rationale", ""))
                    break
                # A library the traceback never NAMED. `_prepare_env` above installs only what the
                # crash reports as missing; when a library degrades an absent dependency into a
                # NameError/AttributeError (an `is_x_available()` guard), the agent's diagnosis is
                # the only place the name exists. Considered BEFORE the ledger decision below,
                # because an install is not a code repair: it spends no attempt on either ledger
                # (exactly like the traceback-driven round above), so an exhausted budget must not
                # be what stops the engine from making the node runnable. Bounded by the same
                # `_MAX_DEP_ROUNDS` + once-per-module `_dep_attempted` cache; the fail-closed
                # conditions live with the extraction (runtime/deps.py).
                if self._auto_install_deps and dep_rounds < _MAX_DEP_ROUNDS:
                    installed = await anyio.to_thread.run_sync(
                        self._prepare_env_from_triage, triage, err)
                    if installed:
                        dep_rounds += 1
                        async with self._write_lock:
                            self.store.append(EV_DEPS_INSTALLED, {
                                "node_id": node_id, "generation": generation,
                                "packages": installed, "round": dep_rounds, "source": "triage"})
                        continue   # re-run with the library present (no repair attempt spent)
                # Which ledger this repair spends. An exemption needs THREE independent signals to
                # agree — the agent's structured class, the engine's own reading of the traceback
                # (`_environment_failure`, which never takes the agent's word for it), and forward
                # progress — plus room in the ledger. Any disagreement charges the experiment budget,
                # which is the fail-closed direction: the worst case of a wrong "experiment" call is
                # today's behaviour.
                repair_class = ("environment"
                                if (triage.get("repair_class") == "environment" and _progress
                                    and env_left and _environment_failure(err))
                                else "experiment")
                if repair_class == "experiment" and not budget_left:
                    triage_outcome = ("abandon", f"inline repair has spent its "
                                                 f"{self._inline_repair_attempts} experiment "
                                                 "attempt(s); this failure is not environment "
                                                 "reconciliation, so there is no separate budget "
                                                 "left to draw on")
                    break
                # action == "repair": fix the code in place and re-eval (no new node, no budget spent).
                # Snapshot the PRE-repair file set now (node is still the pre-repair fold) so we can
                # compute the repair's REAL change set below — `developer.last_files` is the node's whole
                # cumulative solution for the repo developer (repair_from preloads every node file), so a
                # raw key set would always intersect the train stage and defeat checkpoint reuse.
                # Deletions get the same NODE-side baseline: post-repair `last_deleted` is cumulative
                # (repair_from seeds it from node.deleted), so only THIS repair's deletion DELTA may
                # veto checkpoint reuse — and like `prev_files`, the baseline must be read off the
                # NODE, not the shared developer: at this instant `developer.last_deleted` belongs to
                # whatever node it built LAST (see the `_repair` docstring), so a sibling's stale
                # deletions would mask a real repair deletion from the fail-closed reuse guard (or
                # veto reuse for a deletion this node never made).
                prev_files = dict(getattr(node, "files", {}) or {})
                prev_deleted = set(getattr(node, "deleted", []) or [])
                with self.tracer.span("inline_repair", node_id=node_id, attempt=attempt + 1):
                    new_code = self._repair(
                        node, self._repair_error_context(reason, err, state=state, node=node), state)
                # A REPAIR THAT DID NOT PRODUCE A REPAIR. The Developer returns the in-band
                # "(developer error: …)" sentinel when its OWN session failed — an unreachable
                # endpoint, a 401, a 402 "out of credits" — so `new_code` is a provider/transport
                # error message, not code. Nothing downstream can tell the difference: the sentinel
                # was committed as the node's code by `node_repaired`, re-materialized into the
                # workdir, and re-evaluated; the eval then failed with a fresh error, so the loop
                # simply asked again. A dead OpenRouter account produced 2343 such "repairs" on ONE
                # node at ~11/min for 3.5 h, each one a full re-eval, every attempt counted as normal
                # and every stuck counter reset — unbounded, because `inline_repair_attempts = 0`
                # means UNLIMITED (an operator decision) and the anti-stuck guard was the only bound.
                #
                # A provider failure is not a code defect, so it must not drive the code-repair loop.
                # No `node_repaired`, no attempt spent, no files written, no stuck counter touched —
                # the loop breaks here and the node terminalizes ONCE below with
                # reason="developer_crash" naming the provider failure. (Deliberately NOT committing
                # the sentinel as node.code also keeps the recovery sweep's `_developer_sentinel`
                # scan, which keys on exactly that, from later re-terminalizing this node.)
                #
                # It is ALSO a RUN-level condition — every other node reaches the same dead endpoint,
                # so continuing just re-spends the node budget on nodes that cannot be built. Mirror
                # the build path's "PAUSE on the FIRST developer_crash" circuit-breaker
                # (`_create_node`) so the operator learns "your endpoint is out of credits" from one
                # pause reason instead of by reading thousands of repair events. The pause is
                # RUN-level (no node_id): the fix is to the provider, not to this node, so it must
                # not be clearable by a node reset — and a run-level pause folds as a monotone latch
                # (`replay.py::_on_pause`), which keeps it order-tolerant against every sibling eval
                # appending concurrently. Appended under `_write_lock` with the same
                # already-halting re-check `confirm_phase.py::_pace_confirm_refusal` uses for its own
                # auto-pause, so a run that is already paused/finished/stopping gets no second one.
                if is_developer_error(new_code):
                    _dev_err = str(new_code)[:400]
                    triage_outcome = ("abandon", "the repair CALL failed at the provider — no "
                                                 "repaired code was produced")
                    reason = "developer_crash"
                    err = (f"{_dev_err}\n[the Developer's own session failed, so this node was never "
                           f"repaired. Its last eval error was: {err[-200:]}]")
                    if not self._run_halt_intent():
                        async with self._write_lock:
                            if not self._run_halt_intent():
                                self.store.append(EV_PAUSE, {
                                    "reason": "auto-paused: the Developer's LLM provider failed "
                                              f"while repairing node {node_id}, so the repair "
                                              f"returned an error instead of code — {_dev_err}. "
                                              "Every other node reaches the same endpoint; fix it "
                                              "(credits, key, base URL, or the endpoint itself) and "
                                              "resume."})
                    break
                # Snapshot the developer's per-call audit state IMMEDIATELY, before any `await`: under
                # max_parallel>1 the developer instance is SHARED across concurrent _evaluate tasks,
                # and `async with self._write_lock` below is a checkpoint — a sibling task's repair()
                # would overwrite `developer.last_files` in the gap, so reading it after the lock would
                # record (and re-materialize) ANOTHER node's edits as this node's. Capture now.
                repaired_files = dict(getattr(self.developer, "last_files", {}) or {})
                repaired_deleted = list(getattr(self.developer, "last_deleted", []) or [])
                repaired_footprint = developer_artifact_footprint(
                    node.idea.footprint, new_code, repaired_files)
                if repaired_footprint is not None:
                    repaired_footprint = (
                        self._clamp_resource_footprint(repaired_footprint)
                        or repaired_footprint)
                    # A repair keeps the dispatcher's lifecycle reservation.  It may refine the
                    # declaration within those already-held devices, but cannot grow onto GPUs owned
                    # by a sibling while the retry loop is live.
                    if ((_resource_reservation or {}).get("cpu_only")
                            and "gpus" in repaired_footprint):
                        repaired_footprint["gpus"] = 0
                    elif ((_resource_reservation or {}).get("pin")
                          and "gpus" in repaired_footprint):
                        repaired_footprint["gpus"] = min(
                            repaired_footprint["gpus"],
                            int(_resource_reservation.get("count", 0) or 0))
                    held_ids = ((_resource_reservation or {}).get("gpu_ids") or [])
                    held_mem = [getattr(self, "_gpu_mem", {}).get(gpu)
                                for gpu in held_ids]
                    held_mem = [value for value in held_mem if type(value) is int]
                    if (held_mem and isinstance(repaired_footprint.get("gpu_mem_mib"), int)):
                        repaired_footprint["gpu_mem_mib"] = min(
                            repaired_footprint["gpu_mem_mib"], min(held_mem))
                attempt += 1
                if repair_class == "environment":
                    env_repairs += 1
                async with self._write_lock:
                    repair_payload = {
                        "node_id": node_id, "generation": generation,
                        "attempt": attempt, "code": new_code,
                        "files": repaired_files,
                        "deleted": repaired_deleted,
                        "error_in": err, "triage_action": "repair",
                        # WHICH ledger paid for this repair (additive; the fold ignores it). Without
                        # it the operator cannot see why a node with `inline_repair_attempts: 6`
                        # made more than six repairs, and the split is not reconstructable from the
                        # log afterwards — the rationale is free text, deliberately not a contract.
                        "repair_class": repair_class,
                        "rationale": str(triage.get("rationale", ""))[:300]}
                    if repaired_footprint is not None:
                        repair_payload.update({
                            "idea_footprint": repaired_footprint,
                            "footprint_finalized": True,
                        })
                    # This commits the repaired code to folded state BEFORE the files below are
                    # materialized, so state briefly claims a repair the workdir does not hold. The
                    # window is closed on the READ side rather than by reordering (either order skews
                    # one way or the other): the workdir carries a manifest stamp written only after
                    # its files land, and stage reuse — the one path that evaluates a workdir it did
                    # not just build — refuses to proceed unless that stamp matches the folded node.
                    # See `_stamp_workdir` / `_workdir_matches` at the top of this method.
                    self.store.append(EV_NODE_REPAIRED, repair_payload)
                node = fold(self.store.read_all()).nodes[node_id]   # node.code now == repaired code
                if node.attempt != generation:
                    await _record_superseded()
                    return                   # reset raced the repair; never adopt its newer lifecycle
                self._write_node_files(node, workdir)               # re-materialize before re-eval
                _stamp_workdir(node)     # only NOW does the workdir match the repaired manifest
                if fold(self.store.read_all()).nodes[node_id].attempt != generation:
                    await _record_superseded()
                    return                   # reset raced the filesystem write; force clean next materialize
                # Choose the NEXT eval's start stage: REUSE the completed earlier stages (the train
                # checkpoint is still on disk — _write_node_files overlays, never wipes) when the repair
                # provably didn't touch them, so a fixed score/eval script doesn't pay to re-train. Else
                # a full re-run — bounded by inline_repair_retrain_cap so a repair that keeps rewriting
                # training code can't burn many full trains (the anti-stuck guard is signature-, not
                # cost-based). The workdir persists across attempts, so a reused checkpoint is valid.
                # The repair's REAL change set = files whose content actually differs from the pre-repair
                # node (last_files is cumulative — see prev_files above), plus THIS repair's deletions.
                changed = {f for f, c in repaired_files.items() if prev_files.get(f) != c}
                # Deletions likewise get the delta, not the cumulative set: a deletion that predates
                # the completed train stage cannot invalidate its checkpoint — the stage already ran
                # (and passed) without that file on disk. Blocking on the cumulative `repaired_deleted`
                # (seeded from node.deleted at repair_from) would permanently disable stage reuse for
                # any node whose implement ever deleted a file; only THIS repair's deletions can
                # invalidate the checkpoint, so only they enter the reuse decision.
                new_deleted = [d for d in repaired_deleted if d not in prev_deleted]
                changed |= set(new_deleted)
                _stages = self._resolved_stages(node, workdir)
                # `deleted` and the eval spec's `cwd` ride along so the predicate can fail closed on
                # its blind spots: a deletion is invisible to the reachability closure (the file was
                # unlinked by _write_node_files above), and a non-default cwd re-bases the stage
                # scripts so the changed-vs-reachable intersection would prove nothing.
                next_start = self._safe_reuse_start(
                    _stages, res.failed_stage, changed, workdir,
                    deleted=new_deleted,
                    cwd=(self._eval_spec or {}).get("cwd") if isinstance(self._eval_spec, dict) else None)
                # Count a full re-train against the cap ONLY when completed EARLIER-stage work is being
                # discarded: a LATER stage failed yet reuse was refused because the repair could
                # have changed an earlier stage. A first-stage failure (nothing to reuse) or a single-
                # command eval is an ordinary retry, bounded by attempts/stuck like any other — NOT the
                # retrain cap (mirrors config.py: "only a repair that changes an EARLIER stage's code
                # forces a full re-train ... counted"). Check BEFORE incrementing so cap=N runs exactly N.
                # First-vs-later is judged from the PRE-repair `res.stages` (one record per stage that
                # ran, in order, the failed stage always LAST) — never from the failed stage's index in
                # the POST-repair `_stages`: a repair that renames/drops the failed stage (or a
                # _resolved_stages exception fallback to []) loses that index (-1) for FIRST- and
                # LATER-stage failures alike. A renamed LATER stage still discards completed
                # earlier-stage work on the forced full re-run, so it keeps consuming the cap (the
                # point of counting the renamed case at all — leaving it uncounted let a
                # stage-renaming repair burn unlimited full trains); a renamed FIRST stage never had
                # earlier work to discard, so it must stay an ordinary retry.
                was_first = len(res.stages or []) <= 1
                if res.failed_stage and not was_first and next_start is None:   # forces a full (expensive) re-train
                    if (self._inline_repair_retrain_cap
                            and full_retrains >= self._inline_repair_retrain_cap):
                        triage_outcome = ("abandon",
                            f"repair keeps changing earlier-stage (training) code — {full_retrains} full "
                            "re-train(s) already spent; abandoning in-node repair to avoid burning compute "
                            "(a budgeted inter-node debug node can still pick it up)")
                        break
                    full_retrains += 1
                # loop -> re-run the eval with the corrected code (reusing earlier stages when safe)
            sp.set_many(eval_seconds=total_eval, exit_code=res.exit_code, timed_out=res.timed_out,
                        metric=res.metric, ok=ok, repair_attempts=attempt)
            if res.violations:
                sp.set("violations", len(res.violations))
            if res.drift is not None:
                sp.set("drift", True)
            # ASHA past-experiment curve (#7): a bounded per-RUNG [[rung, metric], ...] (canonical
            # geometric rungs) mined from the eval's CAPTURED stdout when the task declares a stdout_json
            # `resource_key`, so a future live node — snapping its sample to the same rung — finds a sibling
            # checkpoint across the whole run. Additive/only-when-present → old logs fold byte-identically.
            # Computed OUTSIDE the write-lock: it parses `res.stdout` (run_argv's bounded ~64 KB tail — for a
            # staged eval, the FINAL stage's output) and depends only on the eval result + `_eval_spec`,
            # nothing the lock guards, so doing it under the global append lock needlessly serialized every
            # other writer once per completed node. (Widening the tail to a teed full-curve accumulator is a
            # follow-up; the fold + reader already degrade safely to the tail.)
            _curve = None
            if ok:
                _spec = getattr(self, "_eval_spec", None)
                _curve = extract_resource_curve(
                    res.stdout, _spec.get("metric") if isinstance(_spec, dict) else None)
            async with self._write_lock:
                # Multi-stage pipeline (Phase 1): record each stage's pass/fail BEFORE the terminal so the
                # fold + trace show data_prep ✓ / train ✓ / eval ✗, and a later stage-scoped re-run knows
                # which stages already passed. Empty on the classic single-command eval.
                for _st in (res.stages or []):
                    self.store.append(EV_STAGE_FINISHED,
                                      {"node_id": node_id, **_st, "generation": generation})
                if res.drift is not None:               # Phase 4: uncorroborated metric (audit)
                    self.store.append(EV_SPEC_DRIFT,
                                      {"node_id": node_id, **res.drift, "generation": generation})
                if ok:
                    _eval_payload = {
                        "node_id": node_id, "generation": generation,
                        "metric": res.metric,
                        "stdout_tail": self._redact(res.stdout[-500:]), "eval_seconds": total_eval,
                        "extra_metrics": normalize_extra_metrics(res.extra_metrics),   # #5 multi-objective
                        "violations": res.violations or [],
                        # Intra-node sweep: the whole grid's per-trial results, carried on the ONE
                        # node_evaluated event (the sweep is a single atomic eval — eval_seconds is
                        # the whole-sweep wall-clock; per-trial seconds are audit-only). [] normally.
                        "trials": res.trials or [],
                    }
                    if _curve:                     # computed above, outside the write-lock (see the #7 note)
                        _eval_payload["resource_curve"] = _curve
                    self.store.append(EV_NODE_EVALUATED, _eval_payload)
                    # B5 reward-hacking detector + I3 code-leakage scan emit the shared Trust-panel event.
                    # emission does not rewrite the metric, but the folded trust_gate policy
                    # can exclude high-precision signals from champion/breeding under gate/block.
                    sigs = []
                    # Scan the WHOLE solution surface, not just solution.py — a patch-gated multi-file
                    # agent can hide answer-key access / leakage / the real computation in an in-surface
                    # helper module that solution.py imports. Concatenate node.files so the reward-hack /
                    # leakage / critic scans cover the imported code too (not only the clean entrypoint).
                    scan_src = node.code + "".join(
                        f"\n\n# --- {fn} ---\n{src}" for fn, src in (node.files or {}).items()
                        if str(fn).replace("\\", "/").lower() != "solution.py")
                    if self.reward_hack_detect:
                        from looplab.trust.reward_hack import detect_reward_hacks
                        protected = set(self._repo_spec.get("protected_names", [])) | set(self._assets)
                        # The grader-IMPORT waiver keys on the task genuinely MATERIALIZING
                        # grader.py (an ASSET → calling `grader.score(...)` is the documented
                        # grading contract, e.g. the in-workdir mlebench brief). Pass it explicitly
                        # instead of letting the detector infer it from `protected`: that union also
                        # carries the operator's protect list, and a merely-PROTECTED grader.py
                        # (protect=["grader.py"], no asset) means "hands off", not "import me" —
                        # inference from the union would wrongly waive the import tells for it.
                        sigs += detect_reward_hacks(
                            scan_src, res.metric, state.direction,
                            protected_names=protected, stdout=res.stdout,
                            # Match the asset key NORMALIZED (path separators + case), exactly like
                            # the detector normalizes `protected_names` — the inference this call
                            # replaced got that normalization for free, so 'Grader.py' or a
                            # backslashed key must keep sanctioning the import here too.
                            grader_import_ok=any(str(a).replace("\\", "/").lower() == "grader.py"
                                                 for a in (self._assets or ())))
                        # 4.3: also apply the hardened exploit ruleset grown by `looplab harden`
                        # (hacker-fixer-solver) — each previously-discovered exploit stays guarded.
                        if self._exploit_suite is not None:
                            sigs += self._exploit_suite.scan(scan_src)
                        # 4.4 sandbox instrumentation (RewardHackingAgents recipe): flag RUNTIME
                        # writes to protected/frozen files — behavioral evidence a static scan of the
                        # code can miss (a write via a helper, os.system, a template). Compares the
                        # workdir against the assets/protected set the engine placed there.
                        if self._workdir_audit:
                            sigs += self._audit_workdir_writes(workdir, protected)
                    # Both detectors emit their OWN namespaced signals (doc 25 CT-10). This used to
                    # mint `data_leakage:`/`critic:` here, which put the string `is_hard_signal`
                    # gates on three files away from the detector that knows what it found.
                    if self._code_leakage_detect and scan_src:
                        from looplab.trust.leakage import code_leakage_findings
                        sigs += code_leakage_findings(scan_src)
                    if self._critic_check and scan_src:
                        from looplab.trust.critic import critic_findings
                        # Host-graded tasks (MLE-bench &c.) score a submission file out-of-process,
                        # so the critic's in-code `metric` checks don't apply — hand it the expected
                        # submission filename so it checks the right output contract instead.
                        sigs += critic_findings(node.idea, scan_src,
                                                submission_file=self._graded_output_name())
                    if sigs:
                        # P1-7 versioned TrustEvidence: bind the evidence to a schema version + a digest
                        # of the exact scanned surface (provenance — which bytes produced these signals),
                        # so a stored flag isn't a bare {node_id, signals}. Additive; the fold reads the
                        # new fields with defaults, so old logs are unaffected.
                        # (No local `import hashlib` here: it would make `hashlib` a function-local
                        # name for ALL of _evaluate, so any earlier use in this method would raise
                        # UnboundLocalError — the exact trap _workdir_manifest_digest's docstring
                        # records having to move out of this method to dodge. Module-level import.)
                        self.store.append(EV_REWARD_HACK_SUSPECTED,
                                          {"node_id": node_id, "generation": generation,
                                           "signals": sigs,
                                           "evidence_version": 1,
                                           "code_digest": hashlib.sha256(
                                               scan_src.encode("utf-8", "replace")).hexdigest()[:16]})
                else:
                    # `err`/`reason` were computed in the attempt loop (reason may be "idea_rejected"
                    # if the crash-triage agent judged the idea fundamentally wrong).
                    sp.set("error_reason", reason)
                    data = {"node_id": node_id, "generation": generation,
                            "error": err, "reason": reason, "eval_seconds": total_eval}
                    if res.failed_stage:                # Phase 1: pinpoint which pipeline stage broke
                        data["failed_stage"] = res.failed_stage
                    if triage_outcome is not None:
                        data["triage_action"], data["triage_rationale"] = (
                            triage_outcome[0], str(triage_outcome[1])[:300])
                    self.store.append(EV_NODE_FAILED, data)
                self._maybe_crash()
