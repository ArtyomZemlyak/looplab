"""A repo task with no `eval` and onboard off must be REJECTED at validate_task (submit time / propose_run
/ /api/start), not crash a spawned engine after "Start" leaving a phantom run whose events 404. Mirrors
the engine's own startup invariant, pulled up so the assistant re-proposes instead."""
from __future__ import annotations

import pytest

from looplab.adapters.tasks import validate_task


def _repo(**kw):
    return {"kind": "repo", "direction": "max", "editable_path": "/tmp", "goal": "g", **kw}


def test_repo_without_eval_or_onboard_is_rejected():
    with pytest.raises(Exception) as e:
        validate_task(_repo())
    assert "eval" in str(e.value) and "onboard" in str(e.value)


def test_repo_with_eval_is_accepted():
    validate_task(_repo(eval={"command": ["python", "t.py"], "metric": {"kind": "stdout_json", "key": "m"}}))


def test_repo_with_onboard_is_accepted():
    validate_task(_repo(onboard=True))


def test_non_repo_kinds_unaffected():
    validate_task({"kind": "quadratic", "goal": "min x^2", "direction": "min"})
