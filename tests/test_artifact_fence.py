"""What sits BESIDE the artifact pin (doc 73 §1.4, `engine/artifact_fence.py`).

The pin (`Node.uses_attempts`, `tests/test_artifact_use_pins.py`) refuses a consumer whose artifact
lifecycle is gone. Two things it cannot say: a consumer pinned to a lifecycle the producer is STILL
producing can still be served — it WAITS instead of being refused — and a number measured on a
dataset that was re-produced SINCE should say so (`metric_provenance.uses` + the champion caveat
`stale_artifact`).
"""
from __future__ import annotations

import anyio

from factories import make_engine
from looplab.core.models import BENIGN_TERMINAL_REASONS, ENGINE_TERMINAL_REASONS
from looplab.engine.artifact_fence import (defer_waiting_consumers, stale_uses, uses_receipt,
                                           uses_waiting)
from looplab.engine.champion_caveats import (CHAMPION_CAVEAT_STALE_ARTIFACT, CHAMPION_CAVEATS,
                                             champion_metric_caveats)
from looplab.events.replay import fold
from looplab.runtime.command_eval import RunResult

_CLEAN = RunResult(exit_code=0, stdout="prepared", metric=None, timed_out=False, stderr="")
_SCORED = RunResult(exit_code=0, stdout='{"metric": 0.5}', metric=0.5, timed_out=False, stderr="")


def _created(engine, nid, *, idea=None, **extra):
    engine.store.append("node_created", {
        "node_id": nid, "parent_ids": [], "operator": "inject",
        "idea": {"operator": "inject", "params": {}, "rationale": "r", **(idea or {})},
        "code": "print('x')", **extra})


def _evaluate(engine, nid, result):
    calls = []

    def fake_run_eval(node, workdir, env=None, profile=None, cancel=None, start_stage=None):
        calls.append(env or {})
        return result

    engine._run_eval = fake_run_eval
    anyio.run(engine._evaluate, nid, anyio.CapacityLimiter(1), None)
    return calls


def _engine(tmp_path):
    engine = make_engine(tmp_path / "run")
    engine._inline_repair = False
    return engine


def test_a_researcher_proposed_use_is_pinned_at_creation(tmp_path):
    engine = _engine(tmp_path)
    _created(engine, 0, node_kind="artifact")
    _created(engine, 1, idea={"uses": [0]})
    node = fold(engine.store.read_all()).nodes[1]
    assert node.uses == [0] and node.uses_attempts == {"0": 0}


def test_a_consumer_pinned_to_a_lifecycle_still_being_produced_waits_then_runs(tmp_path):
    engine = _engine(tmp_path)
    _created(engine, 0, node_kind="artifact")          # proposed back to back: still pending
    _created(engine, 1, idea={"uses": [0]})
    st = fold(engine.store.read_all())
    assert uses_waiting(st, st.nodes[1])
    assert _evaluate(engine, 1, _SCORED) == [], "nothing ran"
    events = engine.store.read_all()
    assert not [e for e in events if e.type in ("node_evaluated", "node_failed")
                and e.data.get("node_id") == 1], "no terminal: the lifecycle can still be produced"
    _evaluate(engine, 0, _CLEAN)
    st = fold(engine.store.read_all())
    assert not uses_waiting(st, st.nodes[1])
    assert _evaluate(engine, 1, _SCORED), "the producer settled: the consumer runs"
    node = fold(engine.store.read_all()).nodes[1]
    assert node.status.value == "evaluated"
    assert node.metric_provenance["uses"]["0"]["generation"] == 0


def test_a_reset_producer_is_not_waited_for(tmp_path):
    """A lifecycle generation only grows: the pinned one can never come back, so the pin refuses."""
    engine = _engine(tmp_path)
    _created(engine, 0, node_kind="artifact")
    _evaluate(engine, 0, _CLEAN)
    _created(engine, 1, uses=[0], uses_attempts={"0": 0})
    engine.store.append("node_reset", {"node_id": 0, "from_stage": "eval"})
    st = fold(engine.store.read_all())
    assert not uses_waiting(st, st.nodes[1])
    assert _evaluate(engine, 1, _SCORED) == []
    assert fold(engine.store.read_all()).nodes[1].error_reason == "artifact_unavailable"


def test_defer_drops_only_waiting_consumers_evaluate_actions(tmp_path):
    engine = _engine(tmp_path)
    _created(engine, 0, node_kind="artifact")
    _created(engine, 1, idea={"uses": [0]})
    st = fold(engine.store.read_all())
    actions = [{"kind": "evaluate", "node_id": 0}, {"kind": "evaluate", "node_id": 1},
               {"kind": "draft"}]
    kept, deferred = defer_waiting_consumers(st, actions)
    assert kept == [actions[0], actions[2]] and deferred == 1


def test_the_receipt_goes_stale_when_the_dataset_is_reproduced(tmp_path):
    engine = _engine(tmp_path)
    _created(engine, 0, node_kind="artifact")
    _evaluate(engine, 0, _CLEAN)
    _created(engine, 1, uses=[0], uses_attempts={"0": 0})
    assert _evaluate(engine, 1, _SCORED)
    st = fold(engine.store.read_all())
    receipt = st.nodes[1].metric_provenance["uses"]
    assert receipt == uses_receipt(st, st.nodes[1]) and len(receipt["0"]["code"]) == 16
    assert st.best_node_id == 1 and stale_uses(st, st.nodes[1]) == []
    assert CHAMPION_CAVEAT_STALE_ARTIFACT not in champion_metric_caveats(st)
    engine.store.append("node_reset", {"node_id": 0, "from_stage": "eval"})
    _evaluate(engine, 0, _CLEAN)                        # re-produced: generation 1
    st = fold(engine.store.read_all())
    assert stale_uses(st, st.nodes[1]) == [0]
    assert CHAMPION_CAVEAT_STALE_ARTIFACT in champion_metric_caveats(st)


def test_a_node_that_uses_nothing_carries_no_receipt(tmp_path):
    engine = _engine(tmp_path)
    _created(engine, 0)
    _evaluate(engine, 0, _SCORED)
    st = fold(engine.store.read_all())
    assert "uses" not in (st.nodes[0].metric_provenance or {})
    assert stale_uses(st, st.nodes[0]) == []


def test_the_vocabulary():
    assert "artifact_unavailable" in ENGINE_TERMINAL_REASONS
    assert "artifact_unavailable" not in BENIGN_TERMINAL_REASONS, "the owner alert must show it"
    assert CHAMPION_CAVEAT_STALE_ARTIFACT in CHAMPION_CAVEATS
