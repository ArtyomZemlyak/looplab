"""The artifact CONSUMER FENCE (doc 73 §1.4, `engine/artifact_fence.py`): a node that `uses` an
artifact reads exactly the artifact it was admitted with, or does not run.

Before it, an operator `node_reset` of a producer while consumers were queued handed them a
half-written directory (or none — the variable silently dropped the path), and a consumer measured on
the old dataset kept its number with nothing saying which data it was OF.
"""
from __future__ import annotations

import anyio

from factories import make_engine
from looplab.core.models import BENIGN_TERMINAL_REASONS, ENGINE_TERMINAL_REASONS
from looplab.engine.artifact_fence import (READY, UNAVAILABLE, WAITING, defer_waiting_consumers,
                                           stale_uses, uses_receipt, uses_unavailable,
                                           uses_verdicts, uses_waiting)
from looplab.engine.champion_caveats import (CHAMPION_CAVEAT_STALE_ARTIFACT, CHAMPION_CAVEATS,
                                             champion_metric_caveats)
from looplab.events.replay import fold
from looplab.runtime.command_eval import RunResult

_CLEAN = RunResult(exit_code=0, stdout="prepared", metric=None, timed_out=False, stderr="")
_SCORED = RunResult(exit_code=0, stdout='{"metric": 0.5}', metric=0.5, timed_out=False, stderr="")


def _created(engine, nid, **extra):
    engine.store.append("node_created", {
        "node_id": nid, "parent_ids": [], "operator": "inject",
        "idea": {"operator": "inject", "params": {}, "rationale": "r"},
        "code": "print('x')", **extra})


def _evaluate(engine, nid, result):
    calls = []

    def fake_run_eval(node, workdir, env=None, profile=None, cancel=None, start_stage=None):
        calls.append(env or {})
        return result

    engine._run_eval = fake_run_eval
    anyio.run(engine._evaluate, nid, anyio.CapacityLimiter(1), None)
    return calls


def _produced(tmp_path):
    engine = make_engine(tmp_path / "run")
    engine._inline_repair = False
    _created(engine, 0, node_kind="artifact")
    _evaluate(engine, 0, _CLEAN)
    _created(engine, 1, uses=[0])
    return engine


# ------------------------------------------------------------------------------- the pure rules
def test_the_verdicts_follow_the_producers_lifecycle(tmp_path):
    engine = _produced(tmp_path)
    st = fold(engine.store.read_all())
    assert uses_verdicts(st, st.nodes[1]) == [(0, READY, "")]
    assert uses_verdicts(st, st.nodes[0]) == [], "a node that uses nothing has no verdicts"
    engine.store.append("node_reset", {"node_id": 0, "from_stage": "eval"})
    st = fold(engine.store.read_all())
    assert uses_verdicts(st, st.nodes[1])[0][1] == WAITING and uses_waiting(st, st.nodes[1])
    st.nodes[0].tombstoned = True
    assert uses_verdicts(st, st.nodes[1])[0][1] == UNAVAILABLE
    assert "deleted" in uses_unavailable(st, st.nodes[1]) and not uses_waiting(st, st.nodes[1])


def test_an_unavailable_producer_beside_a_waiting_one_closes_rather_than_waits(tmp_path):
    engine = _produced(tmp_path)
    _created(engine, 2, node_kind="artifact")          # pending: being produced
    engine.store.append("node_created", {
        "node_id": 3, "parent_ids": [], "operator": "inject", "uses": [2, 9],
        "idea": {"operator": "inject", "params": {}, "rationale": "r"}, "code": "x"})
    st = fold(engine.store.read_all())
    assert not uses_waiting(st, st.nodes[3]), "#9 will never come, so waiting for #2 is pointless"
    assert "#9 does not exist" in uses_unavailable(st, st.nodes[3])


def test_defer_drops_only_waiting_consumers_evaluate_actions(tmp_path):
    engine = _produced(tmp_path)
    engine.store.append("node_reset", {"node_id": 0, "from_stage": "eval"})
    st = fold(engine.store.read_all())
    actions = [{"kind": "evaluate", "node_id": 0}, {"kind": "evaluate", "node_id": 1},
               {"kind": "draft"}]
    kept, deferred = defer_waiting_consumers(st, actions)
    assert kept == [actions[0], actions[2]] and deferred == 1


def test_the_terminal_reason_is_registered_and_benign():
    assert "artifact_unavailable" in ENGINE_TERMINAL_REASONS
    assert "artifact_unavailable" in BENIGN_TERMINAL_REASONS
    assert CHAMPION_CAVEAT_STALE_ARTIFACT in CHAMPION_CAVEATS


# ------------------------------------------------------------------------------- driven: the engine
def test_a_consumer_of_a_failed_artifact_closes_at_zero_cost_without_running(tmp_path):
    engine = make_engine(tmp_path / "run")
    engine._inline_repair = False
    _created(engine, 0, node_kind="artifact")
    _evaluate(engine, 0, RunResult(exit_code=1, stdout="", metric=None, timed_out=False,
                                   stderr="Traceback: boom"))
    _created(engine, 1, uses=[0])
    calls = _evaluate(engine, 1, _SCORED)
    assert calls == [], "nothing ran"
    node = fold(engine.store.read_all()).nodes[1]
    assert node.status.value == "failed" and node.error_reason == "artifact_unavailable"
    row = [e for e in engine.store.read_all() if e.type == "node_failed"][-1]
    assert row.data["eval_seconds"] == 0 and "artifact #0 failed" in row.data["error"]


def test_a_consumer_of_a_producer_being_reproduced_waits_without_a_terminal(tmp_path):
    engine = _produced(tmp_path)
    engine.store.append("node_reset", {"node_id": 0, "from_stage": "eval"})
    calls = _evaluate(engine, 1, _SCORED)
    assert calls == []
    events = engine.store.read_all()
    assert not [e for e in events if e.type in ("node_evaluated", "node_failed")
                and e.data.get("node_id") == 1], "no terminal: it runs once the producer settles"
    assert fold(events).nodes[1].status.value == "pending"


def test_the_terminal_carries_the_receipt_and_a_reproduced_producer_makes_it_stale(tmp_path):
    engine = _produced(tmp_path)
    calls = _evaluate(engine, 1, _SCORED)
    assert calls, "a READY producer admits the consumer"
    st = fold(engine.store.read_all())
    receipt = st.nodes[1].metric_provenance["uses"]
    assert receipt == uses_receipt(st, st.nodes[1]) and receipt["0"]["generation"] == 0
    assert len(receipt["0"]["code"]) == 16
    assert st.best_node_id == 1 and stale_uses(st, st.nodes[1]) == []
    assert CHAMPION_CAVEAT_STALE_ARTIFACT not in champion_metric_caveats(st)
    engine.store.append("node_reset", {"node_id": 0, "from_stage": "eval"})
    _evaluate(engine, 0, _CLEAN)                        # re-produced: generation 1
    st = fold(engine.store.read_all())
    assert stale_uses(st, st.nodes[1]) == [0]
    assert CHAMPION_CAVEAT_STALE_ARTIFACT in champion_metric_caveats(st)


def test_a_node_that_uses_nothing_carries_no_receipt(tmp_path):
    engine = make_engine(tmp_path / "run")
    _created(engine, 0)
    _evaluate(engine, 0, _SCORED)
    node = fold(engine.store.read_all()).nodes[0]
    assert "uses" not in (node.metric_provenance or {})
    assert stale_uses(fold(engine.store.read_all()), node) == []


def test_the_workdir_variable_omits_a_producer_whose_stamp_is_not_its_lifecycle(tmp_path):
    engine = _produced(tmp_path)
    st = fold(engine.store.read_all())
    assert engine._uses_workdirs_env(st.nodes[1])
    stamp = tmp_path / "run" / "nodes" / "node_0" / ".looplab-manifest"
    assert stamp.is_file(), "the producer's workdir carries its manifest stamp"
    stamp.write_text("another-lifecycle")
    assert engine._uses_workdirs_env(st.nodes[1]) == {}
