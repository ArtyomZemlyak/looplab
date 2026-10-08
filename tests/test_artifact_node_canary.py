"""An ARTIFACT node can pass its eval canary (review 2026-10-08, driven).

An artifact node (doc 73 §1.4) succeeds on a CLEAN pipeline with no metric. With `Settings.eval_canary`
on, the canary asked it for a number anyway: every artifact canary failed, bought a paid triage and a
repair, and the node ended `unclassified` — it could never pass. The canary now takes the full eval's
artifact rule (`eval_canary.canary_passed(artifact=True)`): a clean exit with no metric passes, and a
crash on the slice is still caught cheaply, which is what the canary is for.
"""
from __future__ import annotations

from looplab.engine.eval_canary import canary_passed
from looplab.events.replay import fold
from looplab.runtime.sandbox import RunResult

from test_eval_canary import _Dev, _engine, _evaluate, _of


def test_the_artifact_pass_rule_is_the_full_eval_s_artifact_rule():
    clean = RunResult(exit_code=0, stdout="prepared", stderr="", metric=None, timed_out=False)
    printed = RunResult(exit_code=0, stdout="METRIC: 1", stderr="", metric=1.0, timed_out=False)
    crashed = RunResult(exit_code=1, stdout="", stderr="KeyError", metric=None, timed_out=False)
    clocked = RunResult(exit_code=0, stdout="", stderr="", metric=None, timed_out=True)
    assert canary_passed(clean, artifact=True)
    assert canary_passed(printed, artifact=True), "a printed number is ignored, as in the full eval"
    assert printed.metric == 1.0, "the rule reads a copy: the caller's result is untouched"
    assert not canary_passed(crashed, artifact=True)
    assert not canary_passed(clocked, artifact=True)
    assert not canary_passed(clean, artifact=True, expired=True)
    assert not canary_passed(None, artifact=True)
    # The ranked node's rule is unchanged: a clean run with no number is still a failed canary.
    assert not canary_passed(clean)


def _artifact_node(eng, code: str) -> None:
    eng.store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "inject", "node_kind": "artifact",
        "idea": {"operator": "inject", "params": {}, "rationale": "prepare"},
        "code": "print('unused')\n", "files": {"run.py": code}})


def _program(ledger, *, canary_crashes: bool = False) -> str:
    return ("import os\n"
            "c = os.environ.get('LOOPLAB_CANARY') == '1'\n"
            f"open({str(ledger)!r}, 'a').write(('canary' if c else 'full') + '\\n')\n"
            + ("if c:\n    raise KeyError('shard_index')\n" if canary_crashes else "")
            + "open('shard.bin', 'w').write('x')\n"
            "print('prepared 3 shards')\n")


def test_an_artifact_node_passes_its_canary_and_is_evaluated(tmp_path):
    ledger = tmp_path / "ledger.txt"
    dev = _Dev(_program(ledger))
    eng = _engine(tmp_path / "run", dev, repairs=1)
    _artifact_node(eng, dev.first)
    evs = _evaluate(eng)
    node = fold(evs).nodes[0]
    assert ledger.read_text().split() == ["canary", "full"]
    assert [f.data["passed"] for f in _of(evs, "eval_canary_finished")] == [True]
    assert node.status.value == "evaluated", (node.status, node.error_reason)
    assert node.metric is None and not dev.errors, "no triage, no repair, never ranked"


def test_an_artifact_canary_that_crashes_still_fails_before_the_full_run(tmp_path):
    """The canary's purpose survives: a crash on the slice is caught before the full pipeline."""
    ledger = tmp_path / "ledger.txt"
    dev = _Dev(_program(ledger, canary_crashes=True), fixes=[_program(ledger)])
    eng = _engine(tmp_path / "run", dev, repairs=1)
    _artifact_node(eng, dev.first)
    evs = _evaluate(eng)
    node = fold(evs).nodes[0]
    assert [f.data["passed"] for f in _of(evs, "eval_canary_finished")] == [False, True]
    assert ledger.read_text().split() == ["canary", "canary", "full"]
    assert len(dev.errors) == 1 and node.status.value == "evaluated"
