"""An operator-owned GATE reader may never be agent-authored `adapter` code — in every slot.

`cross_check` has refused this since it shipped, through `validate_cross_check`, whose docstring
says it is "one predicate used by both EvalSpec validation and the runtime guard". The rule reached
exactly that one slot. `EvalSpec.readers()` exists precisely so that a rule about readers is
"applied to all four slots or to none", and this was the rule applied to one.

WHAT THE MISSING SLOTS COST. `run_command_eval` re-checks `metrics`/`constraints` for `adapter` and
RAISES on it — from inside the eval worker, AFTER the evaluation has already run. That raise is a
plain `ValueError`, so it is not the `GpuPinUnenforceable` the dispatcher terminalizes, and the
dispatcher's own comment describes what that costs: the raise "cancel[s] every in-flight sibling
eval in the batch and re-crash[es] deterministically on every resume". Probed before the fix,
`EvalSpec(metrics={"m": {"kind": "adapter"}})` constructed cleanly, and NO test covered the raise.

THE PRIMARY `metric` IS DELIBERATELY EXEMPT and that is not an oversight: an adapter there is the
whole `eval_trust_mode="ratify_freeze"` design — the operator freezes the repo's own scorer and
ratifies it, and `cross_check` exists to corroborate exactly that frozen adapter. The line is not
"which slot" but "who the reader answers to": a GATE decides whether a measured node counts, so a
candidate that authored one has a route around the scorer freeze.
"""
from __future__ import annotations

import pytest

from looplab.adapters.repo_task import _GATE_READER_SLOTS, EvalSpec

_CMD = ["python", "-c", "pass"]
_ADAPTER = {"kind": "adapter", "path": "LOOPLAB_adapter.py"}


def _spec(**kw) -> EvalSpec:
    return EvalSpec(command=list(_CMD), **kw)


@pytest.mark.parametrize("kw", [
    {"metrics": {"m": dict(_ADAPTER)}},
    {"constraints": [{"name": "c", "max": 1.0, **_ADAPTER}]},
    {"cross_check": dict(_ADAPTER)},
], ids=["metrics", "constraints", "cross_check"])
def test_every_gate_slot_refuses_an_adapter_at_submit(kw):
    """MUTATION: drop the `_GATE_READER_SLOTS` check -> `metrics` and `constraints` construct, and
    the refusal moves to a raise inside the eval worker that takes the whole run with it."""
    with pytest.raises(ValueError):
        _spec(**kw)


def test_the_primary_metric_may_still_be_a_frozen_adapter():
    """The regression this change could most easily cause, and the shipped onboarding path.

    MUTATION: apply the rule to every slot in `readers()` -> this raises, `ratify_freeze` is
    unusable, and `tests/test_repo_onboarding.py` goes red.
    """
    spec = _spec(metric=dict(_ADAPTER))

    assert spec.metric["kind"] == "adapter"
    assert "metric" not in _GATE_READER_SLOTS, (
        "the primary metric is not a gate — it is the thing the gates are measured against")


def test_the_refusal_names_the_field_the_operator_wrote():
    """`readers()` yields the operator's own spelling so a refusal is actionable. A message naming
    'a reader' would send them looking through four slots."""
    with pytest.raises(ValueError, match=r"eval\.constraints\[0\]"):
        _spec(constraints=[{"name": "c", "max": 1.0, **_ADAPTER}])
    with pytest.raises(ValueError, match=r"eval\.metrics\['m'\]"):
        _spec(metrics={"m": dict(_ADAPTER)})


def test_every_gate_slot_is_a_real_reader_slot():
    """The registry must name slots `readers()` actually yields, or the rule silently covers nothing.

    MUTATION: rename a slot in `_GATE_READER_SLOTS` (say `metric_s`) -> the rule stops applying and
    nothing else notices.
    """
    spec = _spec(metric={"kind": "stdout_regex", "pattern": r"m: ([0-9.]+)"},
                 metrics={"m": {"kind": "stdout_regex", "pattern": r"m: ([0-9.]+)"}},
                 constraints=[{"name": "c", "max": 1.0, "kind": "stdout_regex",
                               "pattern": r"c: ([0-9.]+)"}],
                 cross_check={"kind": "stdout_regex", "pattern": r"m: ([0-9.]+)"})
    slots = {slot for _label, slot, _reader in spec.readers()}

    assert _GATE_READER_SLOTS <= slots, (
        f"{sorted(_GATE_READER_SLOTS - slots)} is registered as a gate slot but `readers()` never "
        "yields it, so the refusal can never fire there")
    assert slots - _GATE_READER_SLOTS == {"metric"}, (
        "every reader slot is either a gate or the primary metric; a new slot needs a decision")


def test_a_run_already_on_disk_stays_resumable():
    """`_grandfathered` is the escape hatch every submit-time reader rule shares: `resume` and
    `finalize` re-validate the run's own `task.snapshot.json`, and a run that already exists must
    stay resumable even when its recorded spec is one we would now refuse."""
    from looplab.adapters.repo_task import eval_reader_path_errors

    spec = EvalSpec.model_construct(command=list(_CMD), metrics={"m": dict(_ADAPTER)},
                                    constraints=[], cross_check=None, metric=None)
    errors = eval_reader_path_errors(spec)

    assert any("adapter" in e for e in errors), (
        "the diagnosis must survive as text for the resume path to report it as a warning")


# --- the runtime backstop -----------------------------------------------------------------------
# Unreachable from any submit surface now, but a `task.snapshot.json` recorded before the refusal
# existed still reaches it (`_grandfathered` keeps such a run resumable), and
# `run_command_eval(metrics=...)` is public.

def test_the_runtime_backstop_fails_the_node_and_never_the_run(tmp_path):
    """It used to `raise`, from inside the eval worker, after the evaluation had already run.

    A plain `ValueError` is not the `GpuPinUnenforceable` the dispatcher terminalizes, so per that
    handler's own comment the raise escaped "with no node terminal", cancelled "every in-flight
    sibling eval in the batch" and re-crashed "deterministically on every resume".

    MUTATION: restore the `raise` -> this test's `pytest.raises`-free call blows up, which is
    exactly what the engine's eval worker did.
    """
    from looplab.runtime.command_eval import run_command_eval

    res = run_command_eval(
        ["python", "-c", "print('metric: 0.5')"], str(tmp_path), 60,
        metric={"kind": "stdout_regex", "pattern": r"metric: ([0-9.]+)"},
        metrics={"m": dict(_ADAPTER)})

    assert res.metric is None, "a spec whose gate is untrustworthy must not yield a metric"
    assert "adapter" in res.stderr, "the node's terminal must carry why it failed"
    assert res.exit_code == 0, "the child really did run and exit 0 — this is a spec refusal"


@pytest.mark.parametrize("kw", [
    {"metrics": {"m": dict(_ADAPTER)}},
    {"constraints": [dict(_ADAPTER, name="c", max=1.0)]},
], ids=["metrics", "constraints"])
def test_each_gate_slot_is_covered_by_the_backstop(tmp_path, kw):
    from looplab.runtime.command_eval import run_command_eval

    res = run_command_eval(["python", "-c", "print('metric: 0.5')"], str(tmp_path), 60,
                           metric={"kind": "stdout_regex", "pattern": r"metric: ([0-9.]+)"}, **kw)

    assert res.metric is None and "adapter" in res.stderr


def test_a_clean_spec_is_untouched_by_the_backstop(tmp_path):
    """The regression guard: the check must not cost a legitimate eval its metric."""
    from looplab.runtime.command_eval import run_command_eval

    res = run_command_eval(
        ["python", "-c", "print('metric: 0.5')"], str(tmp_path), 60,
        metric={"kind": "stdout_regex", "pattern": r"metric: ([0-9.]+)"},
        metrics={"m": {"kind": "stdout_regex", "pattern": r"metric: ([0-9.]+)"}})

    assert res.metric == pytest.approx(0.5)
