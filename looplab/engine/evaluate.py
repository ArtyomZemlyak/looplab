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

import time
from collections.abc import Mapping
from typing import Optional

import anyio

from looplab.core.models import (NodeStatus, developer_artifact_footprint,
                                 normalize_extra_metrics)
from looplab.engine.options import _UNSET
from looplab.engine.train_monitor import snapshot_training_logs
from looplab.engine.triage import _MAX_DEP_ROUNDS, _failure_reason, _normalize_error_sig
from looplab.events.replay import fold
from looplab.runtime.sandbox import GpuPinUnenforceable
from looplab.events.types import (EV_CARD_DROPPED, EV_DEPS_INSTALLED, EV_NODE_ABORT,
                                  EV_NODE_EVALUATED, EV_NODE_FAILED, EV_NODE_REPAIRED,
                                  EV_NODE_RESET, EV_PROXY_SCORED, EV_REWARD_HACK_SUSPECTED,
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


class EvaluateMixin:
    """The engine's eval-task cluster. See the module docstring for the mixin convention
    (`self` is the Engine)."""

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
            generation = node.attempt       # immutable identity of THIS worker's node lifecycle
            start_seq = events_at_start[-1].seq if events_at_start else -1
            sp.set("operator", node.operator)
            # The dispatcher owns this reservation for the complete node lifecycle. Keeping the same
            # devices across every inline repair/retry prevents a repaired process from jumping onto a
            # sibling's GPU; the dispatcher releases it exactly once in its worker `finally`.
            # CODEX AGENT: the dispatcher registered this reservation under the ADMISSION-time
            # generation, but `generation` here is node.attempt from this worker's fresher fold. An
            # eval-stage node_reset landing in that window (attempt+1, still pending, rerun_from None)
            # passes the prestart guard above, the lookup returns None, and _resource_eval_env(None)
            # yields an UNPINNED env — this eval then sees every GPU while pinned siblings run and the
            # dispatcher still holds this node's devices under the stale key. When dispatcher-admitted
            # with _eval_parallel > 1, a miss here should fail closed / re-admit, not degrade to
            # whole-box visibility.
            _resource_reservation = self._eval_resource_reservation(node_id, generation)
            try:
                eval_env = self._resource_eval_env(
                    _resource_reservation, inherit_host=True)
            except GpuPinUnenforceable as exc:
                # CODEX AGENT: an explicit positive declaration on a zero-device inventory is a
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
            _reuse = bool(node.rerun_stage and workdir.exists() and not _superseded_marker.exists())
            if not _reuse:
                self._materialize(node, workdir)    # seed tree -> node edits -> task assets
                # A stage-scoped re-run whose workdir was GONE has nothing to reuse — the re-seed just
                # wiped any artifacts. Skipping earlier stages now would run the restarted stage against
                # MISSING inputs, so drop the start_stage and re-run the FULL pipeline instead.
                if node.rerun_stage:
                    node.rerun_stage = None
            # Hybrid crash repair: each attempt runs the eval (with the mid-eval abort watcher) and,
            # if it CRASHES, the agent triages it and may repair the code IN PLACE and re-run — all
            # within this one node (no new tree node, no max_nodes spent). At most
            # `inline_repair_attempts` repairs; then the node fails normally and stays eligible for the
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
            stuck_sig = None                 # anti-stuck: consecutive identical-error signatures
            stuck_n = 0
            # Multi-stage reuse across repair attempts: `next_start` is the stage to run FROM on the next
            # eval — _UNSET on the first eval (derives node.rerun_stage), then set by the safe-reuse
            # predicate after each repair (a stage name = reuse the completed earlier stages, e.g. skip
            # re-train when only the score script was fixed; None = a full re-run). `full_retrains` counts
            # the EXPENSIVE full re-runs a repair forced, bounded by inline_repair_retrain_cap.
            next_start = _UNSET
            full_retrains = 0
            while True:
                _t0 = time.time()
                # CODEX AGENT: repair/retry attempts reuse the workdir and sandbox stage logs append.
                # When either watchdog is enabled, snapshot every existing log before this attempt
                # starts so it cannot rank/classify prior-attempt bytes. Keep the monitor-off path free
                # of extra filesystem work (`off == today`).
                _eval_spec = getattr(self, "_eval_spec", None)
                _watching_logs = (
                    (getattr(self, "_train_monitor", False) and bool(_eval_spec))
                    or (getattr(self, "_asha_live", False) and isinstance(_eval_spec, dict)))
                _log_snapshot = snapshot_training_logs(workdir) if _watching_logs else None
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
                            intervention = await anyio.to_thread.run_sync(_intervention_seen)
                            if intervention is not None:
                                superseded = intervention == "reset"
                                operator_card_dropped = intervention == "card_drop"
                                aborted = intervention in {"abort", "card_drop"}
                                cancel.set()
                                return
                    _tg.start_soon(_watch)
                    # Training-log monitor (off by default): a sibling task that tails this eval's live
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
                                       _mon_ctx, kill_signal, _log_snapshot)
                    # ASHA live-curve rank watchdog (off by default): a sibling task that reads the live
                    # log's latest INTERMEDIATE metric and ranks it against finished siblings; advisory
                    # unless asha_live_kill. Same command-eval gate (needs a live log + the metric spec).
                    if getattr(self, "_asha_live", False) and isinstance(getattr(self, "_eval_spec", None), dict):
                        _mspec = self._eval_spec.get("metric") or {}
                        _tg.start_soon(self._monitor_asha, node_id, generation, workdir, cancel,
                                       _mspec, state.direction, kill_signal, _log_snapshot)
                    # The lifecycle reservation selected by the dispatcher stays unchanged through this
                    # retry. CUDA_VISIBLE_DEVICES contains physical ids (logical→physical remap), while
                    # an unspecified serial eval keeps eval_env=None and sees the whole box as before.
                    try:
                        res = await anyio.to_thread.run_sync(
                            self._run_eval, node, str(workdir), eval_env, None, cancel, next_start
                        )
                    except GpuPinUnenforceable as exc:
                        # CODEX AGENT: the shield/return path has no focused async regression test that
                        # forces GpuPinUnenforceable while watcher siblings are live, then proves exactly
                        # one terminal, reservation release, and no unbound-result fallthrough. This is
                        # the cancellation topology the prior broad suite failed to exercise.
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
                _sig = _normalize_error_sig(err)
                stuck_n = (stuck_n + 1) if _sig and _sig == stuck_sig else 1
                stuck_sig = _sig
                # Eval-budget stop: the inline-repair loop re-runs FULL evals with no budget check
                # between attempts — the loop-top / per-eval guards only see `total_eval_seconds` from
                # TERMINAL events, and no terminal is emitted mid-repair, so an LLM whose repairs vary
                # the stderr (never tripping anti-stuck) can overshoot the eval budget by multiples
                # inside ONE node. Abandon once this node's cumulative eval time would cross the ceiling.
                if max_es is not None and state.total_eval_seconds + total_eval >= max_es:
                    triage_outcome = ("abandon", "eval budget exhausted during inline repair")
                    break
                # Inline-repair gate: feature on, repairable reason, a Developer that can repair, and
                # something to repair (whole-file code, multi-file edits, or a repo). The attempt CAP is
                # skipped when unlimited (_inline_repair_attempts == 0); the anti-stuck guard bounds it.
                if (not self._inline_repair
                        or reason not in self._inline_repair_reasons
                        or (self._inline_repair_attempts and attempt >= self._inline_repair_attempts)
                        or stuck_n >= self._inline_repair_stuck_repeat
                        or not callable(getattr(self.developer, "repair", None))
                        or not (node.code or node.files or self._repo_spec)):
                    if stuck_n >= self._inline_repair_stuck_repeat and self._inline_repair:
                        triage_outcome = ("abandon", f"same error repeated {stuck_n}x — stuck, abandoning")
                    break
                triage = self._triage_crash(state, node, err, attempt + 1, reason=reason)
                action = triage.get("action", "repair")
                if action == "abandon":
                    triage_outcome = ("abandon", triage.get("rationale", ""))
                    break
                if action == "reject_idea":   # the idea itself is wrong -> mark the lineage; steer to a new idea
                    reason = "idea_rejected"
                    triage_outcome = ("reject_idea", triage.get("rationale", ""))
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
                async with self._write_lock:
                    repair_payload = {
                        "node_id": node_id, "generation": generation,
                        "attempt": attempt, "code": new_code,
                        "files": repaired_files,
                        "deleted": repaired_deleted,
                        "error_in": err, "triage_action": "repair",
                        "rationale": str(triage.get("rationale", ""))[:300]}
                    if repaired_footprint is not None:
                        repair_payload.update({
                            "idea_footprint": repaired_footprint,
                            "footprint_finalized": True,
                        })
                    self.store.append(EV_NODE_REPAIRED, repair_payload)
                node = fold(self.store.read_all()).nodes[node_id]   # node.code now == repaired code
                if node.attempt != generation:
                    await _record_superseded()
                    return                   # reset raced the repair; never adopt its newer lifecycle
                self._write_node_files(node, workdir)               # re-materialize before re-eval
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
                    self.store.append(
                        EV_NODE_EVALUATED,
                        {"node_id": node_id, "generation": generation,
                         "metric": res.metric,
                         "stdout_tail": self._redact(res.stdout[-500:]), "eval_seconds": total_eval,
                         "extra_metrics": normalize_extra_metrics(res.extra_metrics),   # #5 multi-objective
                         "violations": res.violations or [],
                         # Intra-node sweep: the whole grid's per-trial results, carried on the ONE
                         # node_evaluated event (the sweep is a single atomic eval — eval_seconds is
                         # the whole-sweep wall-clock; per-trial seconds are audit-only). [] normally.
                         "trials": res.trials or []},
                    )
                    # B5 reward-hacking detector + I3 code-leakage scan emit the shared Trust-panel event.
                    # CODEX AGENT: emission does not rewrite the metric, but the folded trust_gate policy
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
                    if self._code_leakage_detect and scan_src:
                        from looplab.trust.leakage import code_leakage_scan
                        for f in code_leakage_scan(scan_src)["flags"]:
                            sigs.append({"signal": "data_leakage:" + f["signal"],
                                         "detail": f"line {f['line']}: {f['code']}"})
                    if self._critic_check and scan_src:
                        from looplab.trust.critic import critique
                        # Host-graded tasks (MLE-bench &c.) score a submission file out-of-process,
                        # so the critic's in-code `metric` checks don't apply — hand it the expected
                        # submission filename so it checks the right output contract instead.
                        sub_file = self._graded_output_name()
                        for c in critique(node.idea, scan_src, submission_file=sub_file):
                            sigs.append({"signal": "critic:" + c["issue"], "detail": c["detail"]})
                    if sigs:
                        # P1-7 versioned TrustEvidence: bind the evidence to a schema version + a digest
                        # of the exact scanned surface (provenance — which bytes produced these signals),
                        # so a stored flag isn't a bare {node_id, signals}. Additive; the fold reads the
                        # new fields with defaults, so old logs are unaffected.
                        import hashlib
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
