"""The eval canary for external-agent runs (doc 78 §5; audit incident G1): on by default whenever
the task declares `eval.canary`, and a `smoke` mode that passes by SURVIVING its cap, for scripts
that cannot run a tiny slice — so a wrong CLI flag fails in seconds, not after data preparation."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from looplab.adapters.repo_task import CanarySpec
from looplab.engine.eval_canary import canary_spec, smoke_canary_passed
from looplab.engine.evaluate import EvaluateMixin


def test_canary_spec_carries_the_mode_and_refuses_an_unknown_one():
    assert "mode" not in canary_spec({"canary": {"timeout": 60}})   # slice: unchanged shape
    smoke = canary_spec({"canary": {"timeout": 60, "mode": "smoke"}})
    assert smoke["mode"] == "smoke" and smoke["env"]["LOOPLAB_CANARY"] == "1"
    assert canary_spec({"canary": {"mode": "other"}}) is None
    assert CanarySpec(mode="smoke", timeout=90).mode == "smoke"
    with pytest.raises(ValueError):
        CanarySpec(mode="bogus")


def test_smoke_pass_rule_is_survival_or_a_clean_exit():
    alive = SimpleNamespace(exit_code=-15, timed_out=True, metric=None)
    clean = SimpleNamespace(exit_code=0, timed_out=False, metric=None)
    crashed = SimpleNamespace(exit_code=2, timed_out=False, metric=None)  # argparse: unknown args
    assert smoke_canary_passed(alive, interrupted=False)     # the canary clock or a stage's own cap
    assert smoke_canary_passed(clean, interrupted=False)
    assert not smoke_canary_passed(crashed, interrupted=False)
    assert not smoke_canary_passed(alive, interrupted=True)
    assert not smoke_canary_passed(None, interrupted=False)


@pytest.mark.parametrize("external,setting,declared,due", [
    (True, False, True, True),      # external: on by default when the task declares a canary
    (True, False, False, False),    # …but nothing to run without a declaration
    (False, False, True, False),    # internal: still opt-in
    (False, True, True, True),
])
def test_canary_due_for_external_runs_by_default(external, setting, declared, due):
    spec = {"command": ["python", "x.py"], **({"canary": {"timeout": 60}} if declared else {})}
    host = SimpleNamespace(_eval_canary=setting, external_harness=external, _eval_spec=spec,
                           store=SimpleNamespace(read_all=lambda: []))
    a = SimpleNamespace(node_id=0, generation=0, node=SimpleNamespace(files={}, code="", deleted=[], attempt=0))
    assert EvaluateMixin._eval_canary_due(host, a) is due
