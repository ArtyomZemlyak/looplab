"""The single-command eval path arms the deterministic divergence watchdog, under a switch.

A declared stage has always had it (`command_eval.py` passes `health_check=True`). The SINGLE
command — which on a RepoTask usually IS the training, and which `train_monitor.eval_log_plan` grants
`LOG_ROLE_TRAINING` for exactly that reason — was the only eval path with no deterministic early stop
at all. The stated blocker was a SCORER inside such a run legitimately printing `loss: nan`, and
nobody had counted it.

MEASURED 2026-08-30 by replaying the SHIPPED `_StageHealthMonitor` over every preserved log on this
box: it fires on 0 of 110 `score.log` scoring phases (30 MB) and 0 of 1 `eval.log`, while firing on
2 of 133 `train.log` — and those two are the TRUE positives this rung exists for
(`e5small-dr-unified-v2` node 7, `rubertlite-dense-retrieval` node 15). The false-positive population
on this deployment is EMPTY.

IT STAYS A SWITCH because that population is a property of the deployment's SCORERS, not of the
engine, and it takes a LEGACY snapshot row because a resumed run must not gain kill authority it
never consented to — the same rule `train_monitor_kill` follows.

Every assertion has an input that makes it FAIL; the mutations are named.
"""
from __future__ import annotations

from looplab.core.config import LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings
from looplab.runtime.sandbox import _StageHealthMonitor


def test_the_shipped_monitor_still_refuses_an_isolated_non_finite_record():
    """The property that makes ON defensible: a warm-up overflow does not accumulate.

    Mutation: make the streak cumulative (drop the `_FINITE` reset) and a healthy AMP run that logs
    a handful of `grad_norm: inf` steps over a multi-hour eval is killed."""
    m = _StageHealthMonitor(threshold=5)
    for _ in range(4):
        assert not m.feed("grad_norm: inf\n")
    assert not m.feed("loss: 0.42\n"), "a finite record must clear the streak"
    for _ in range(4):
        assert not m.feed("grad_norm: inf\n")


def test_five_CONSECUTIVE_non_finite_records_do_fire():
    """The true positive. Mutation: raise the threshold past the corpus's real divergences and the
    two `train.log` files this rung exists for stop firing."""
    m = _StageHealthMonitor(threshold=5)
    fired = any(m.feed("loss: nan\n") for _ in range(5))
    assert fired


def test_a_SCORER_line_is_not_a_divergence_record():
    """The measured false-positive shape, pinned as a property rather than as a corpus count: a
    scoring phase reports a METRIC, not a loss, so it cannot trip the pattern at all.

    Mutation: widen `_PAT` to any key and `RECALL@100: nan` from a scorer starts killing runs."""
    m = _StageHealthMonitor(threshold=2)
    assert not m.feed("RECALL@100: nan\n")
    assert not m.feed("ndcg: inf\n")


def test_the_switch_defaults_ON_and_a_RESUME_keeps_the_old_behaviour():
    """Mutation: drop the LEGACY row and a run resumed from a pre-2026-08-30 snapshot silently gains
    a kill it never consented to — the exact rule that table exists for."""
    assert Settings().single_command_divergence_watch is True
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["single_command_divergence_watch"] is False


def test_a_bare_exec_carrier_arms_NOTHING():
    """`_EvalExec.divergence_watch` defaults False so a construction that never heard of the setting
    cannot acquire kill authority. Mutation: default it True and every direct caller of
    `run_command_eval` gains the watchdog without asking."""
    from looplab.runtime.command_eval import _EvalExec
    import inspect

    sig = inspect.signature(_EvalExec)
    assert sig.parameters["divergence_watch"].default is False

    from looplab.runtime import command_eval
    assert inspect.signature(command_eval.run_command_eval).parameters[
        "divergence_watch"].default is False


def test_the_single_command_path_passes_the_flag_through():
    """AST-free and text-free: read the actual call. Mutation: hard-code `health_check=True` (or
    False) in `_run_single` and the switch stops meaning anything."""
    import inspect

    from looplab.runtime import command_eval

    body = inspect.getsource(command_eval._run_single)
    assert "health_check=bool(ex.divergence_watch)" in body, (
        "the single-command path must arm the watchdog FROM THE SWITCH; a literal here makes the "
        "Settings field and its LEGACY row decorative")
