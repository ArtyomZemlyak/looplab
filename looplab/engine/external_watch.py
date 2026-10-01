"""Live evaluation observations owned by an external coding agent."""
from __future__ import annotations

import hashlib

import anyio

from looplab.engine.asha_monitor import (_ASHA_GRACE_TICKS, asha_underperforming,
                                         latest_intermediate_sample, sibling_final_metrics,
                                         sibling_metrics_at_resource)
from looplab.engine.train_monitor import (LOG_ROLE_TRAINING, _NON_TRAINING_ROLES,
                                          append_watchdog_row,
                                          read_stage_trajectory, read_training_tail_raw,
                                          resolve_stage_log, trajectory_vetoes_kill,
                                          training_authority_spent)
from looplab.engine.shared import engine_fold as fold
from looplab.events.types import EV_ASHA_RANK, EV_TRAIN_MONITOR_ALERT


async def observe_external_eval(engine, a, cancel, phase: str, *, final_pass: bool = False) -> None:
    """Publish changed, attributed live evidence and wait for a deliberate answer.

    No agent process or internal model is launched. An already finished evaluator
    must still account for each opened question before its terminal is committed.
    """
    # the external agent's checkpoints: an engine reach into the harness taken only in an
    # external_harness run (`tests/test_package_layering.py` DEFERRED, engine -> harness)
    from looplab.harness.checkpoints import CheckpointSubjectGone, answer_for, ask
    from looplab.engine.evaluate import _watch_limiter

    last_digest = None
    watched_stage = None
    watching = False
    under_streak = 0
    cadence = engine._monitor_cadence() if phase == "train_monitor" else engine._asha_cadence()
    metric = (engine._eval_spec or {}).get("metric") or {}
    if final_pass and phase in getattr(a, "_external_observed_phases", set()):
        return  # a live tick already gave the agent this phase's observation
    while True:
        if not final_pass:
            await anyio.sleep(cadence)
        if cancel.is_set():
            return

        def observe():
            nonlocal under_streak
            resolved = resolve_stage_log(a.workdir, a._log_plan)
            if resolved is None or resolved.role in _NON_TRAINING_ROLES:
                return None
            tail = read_training_tail_raw(a.workdir, snapshot=a._log_snapshot,
                                          plan=a._log_plan)
            if not tail:
                return None
            stage = str(getattr(resolved, "stage", "") or getattr(resolved, "name", ""))
            if phase == "train_monitor":
                eligible = (resolved.role == LOG_ROLE_TRAINING
                            and not training_authority_spent(a.workdir, a._log_plan))
                if eligible and watching:
                    # The same measured-curve veto as the built-in watchdog can
                    # only remove early-stop authority, never create it.
                    eligible = not trajectory_vetoes_kill(read_stage_trajectory(
                        resolved.path, snapshot=a._log_snapshot))
                return stage, tail, eligible, "", {"log_role": resolved.role}
            sample = latest_intermediate_sample(tail, a.workdir, metric)
            if sample is None:
                under_streak = 0
                return None
            state = fold(engine.store.read_all())
            endpoints = sibling_final_metrics(state, a.node_id)
            comparable = sibling_metrics_at_resource(state, a.node_id, metric, sample)
            if len(endpoints) < engine._asha_live_min_siblings:
                under_streak = 0
                return None
            under = (asha_underperforming(sample.value, comparable, state.direction,
                                          quantile=engine._asha_live_quantile)
                     if len(comparable) >= engine._asha_live_min_siblings else None)
            under_streak = under_streak + 1 if under is True else 0
            detail = (f"Intermediate objective={sample.value}; resource={sample.resource_key}:"
                      f"{sample.resource}; completed endpoints={endpoints[:32]}; "
                      f"same-resource peers={comparable[:32]}; "
                      f"same-resource underperforming={under}; "
                      f"consecutive underperforming ticks={under_streak}; "
                      f"minimum before stop={_ASHA_GRACE_TICKS + 1}.")
            return stage, tail, under_streak > _ASHA_GRACE_TICKS, detail, {
                "intermediate": round(sample.value, 6),
                "population": len(endpoints), "comparable_population": len(comparable),
                "direction": str(state.direction), "quantile": engine._asha_live_quantile,
                "endpoint_underperforming": bool(asha_underperforming(
                    sample.value, endpoints, state.direction,
                    quantile=engine._asha_live_quantile)),
                "resource_underperforming": under,
                "underperforming": bool(under) or bool(asha_underperforming(
                    sample.value, endpoints, state.direction,
                    quantile=engine._asha_live_quantile)),
                "kill_comparable": under is not None,
                **({"resource_key": sample.resource_key, "resource": sample.resource}
                   if sample.resource_key is not None and sample.resource is not None else {}),
            }

        try:
            observed = await anyio.to_thread.run_sync(observe, limiter=_watch_limiter())
            if observed is None or cancel.is_set():
                if final_pass:
                    return
                continue
            stage, tail, eligible, details, facts = observed
            digest = hashlib.sha256(tail.encode("utf-8", "replace")).hexdigest()
            if (stage, digest) == last_digest and not watching:
                continue
            if stage != watched_stage:
                watching = False
                watched_stage = stage
            last_digest = stage, digest
            redactor = getattr(engine, "_redact", None)
            text = redactor(tail) if callable(redactor) else tail
            # The evaluator can finish while ask() is in the worker thread. Keep the durable
            # question and its in-process tracking together: cancellation between them would
            # otherwise commit a node whose newly opened question was never answered.
            with anyio.CancelScope(shield=True):
                question = await anyio.to_thread.run_sync(
                    lambda: ask(engine.run_dir, a.node_id, a.generation, phase,
                                stage=stage, observation=f"{details}\n{text}"[-6000:],
                                kill_enabled=(eligible and watching and bool(
                                    engine._train_monitor_kill if phase == "train_monitor"
                                    else engine._asha_live_kill))), limiter=_watch_limiter())
                observed_phases = getattr(a, "_external_observed_phases", set())
                observed_phases.add(phase)
                a._external_observed_phases = observed_phases
                a._live_questions.append(question["checkpoint_id"])
            while not cancel.is_set():
                answer = await anyio.to_thread.run_sync(
                    lambda: answer_for(engine.run_dir, question["checkpoint_id"]),
                    limiter=_watch_limiter())
                if answer is not None:
                    watching = answer["verdict"] == "watch"
                    # Existing attention/audit readers consume these diagnostic
                    # events. The decision ledger remains the authoritative answer.
                    try:
                        if phase == "train_monitor":
                            await append_watchdog_row(EV_TRAIN_MONITOR_ALERT, {
                                "node_id": a.node_id, "generation": a.generation,
                                "status": ("broken" if answer["verdict"] == "abort" else
                                           "watch" if watching else "healthy"),
                                "reason": answer["reason"][:300],
                                "confidence": 0.0, "log_role": facts["log_role"],
                                "stage": stage,
                                "kill": answer["verdict"] == "abort",
                                "source": "external_agent",
                                "checkpoint_id": question["checkpoint_id"]},
                                engine, shield=answer["verdict"] == "abort")
                        else:
                            await append_watchdog_row(EV_ASHA_RANK, {
                                "node_id": a.node_id, "generation": a.generation,
                                **facts,
                                "source": "external_agent",
                                "checkpoint_id": question["checkpoint_id"]},
                                engine, shield=answer["verdict"] == "abort")
                    except Exception:  # noqa: BLE001 — diagnostic failure cannot lose the answered stop
                        pass
                    if answer["verdict"] == "abort":
                        a.kill_signal.update(kill=True, reason=answer["reason"][:400],
                                             terminal_reason=("monitor_broken" if phase == "train_monitor"
                                                              else "asha_underperforming"))
                        cancel.set()
                        return
                    break
                await anyio.sleep(0.3)
            if final_pass:
                return
        except CheckpointSubjectGone:
            # The fold PROVES the lifecycle this watcher observes is no longer pending (a reset or
            # a terminal moved it): no question about it can be asked, now or on a later tick, and
            # the event that moved it owns the node. Not a hiccup to retry, and not a failed final
            # observation to raise into an attempt that no longer exists (critic 2026-09-29). The
            # intervention is handed to the settle as the intervention watcher would have: a final
            # pass runs after that watcher stopped, and an unseen reset settled as an ordinary
            # stale-generation terminal with no superseded record.
            seen = getattr(engine, "_eval_intervention_seen", None)
            if callable(seen) and not a._seen.get("kind"):
                card_id = getattr(getattr(getattr(a, "node", None), "idea", None), "card_id", None)
                kind = seen(a.node_id, a.generation, getattr(a, "start_seq", -1), card_id)
                if kind:
                    a._seen["kind"] = kind
            return
        except Exception:  # noqa: BLE001 — a tick hiccup cannot disable later observation
            # A transient storage/log read cannot turn an enabled review into an
            # implicit 'continue'. Keep observing; an opened question remains due.
            if final_pass:
                raise  # the final gate must not silently skip a failed observation
            continue


async def settle_external_observations(engine, a) -> None:
    """A completed evaluator cannot silently outrun an unanswered live tick."""
    # the external agent's checkpoints: an engine reach into the harness taken only in an
    # external_harness run (`tests/test_package_layering.py` DEFERRED, engine -> harness)
    from looplab.harness.checkpoints import answer_for
    from looplab.engine.evaluate import _watch_limiter

    if a._seen.get("kind"):
        return  # explicit operator abort/reset owns the lifecycle
    for checkpoint_id in a._live_questions:
        while True:
            try:
                answer = await anyio.to_thread.run_sync(
                    lambda: answer_for(engine.run_dir, checkpoint_id), limiter=_watch_limiter())
            except Exception:  # noqa: BLE001 — an unavailable ledger is not approval
                answer = None
            if answer is not None:
                break
            intervened = await anyio.to_thread.run_sync(
                lambda: engine._eval_intervention_seen(
                    a.node_id, a.generation, a.start_seq,
                    getattr(a.node.idea, "card_id", None)), limiter=_watch_limiter())
            if intervened:
                return
            await anyio.sleep(0.3)
