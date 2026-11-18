"""Conditional proposal-tape replay; never a generated or causal off-policy measurement."""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys

import anyio
import orjson
import pytest
from typer.testing import CliRunner

from looplab.cli import app
from looplab.events.eventstore import EventStore
from looplab.search import policy_replay as replay


def start(store, direction="max"):
    store.append("run_started", {"run_id": "recorded", "run_uid": "a" * 32,
        "goal": "recorded objective", "direction": direction})


def node(store, nid, parents, metric, *, operator=None, terminal="node_evaluated", **extra):
    operator = operator or ("improve" if parents else "draft")
    uses = extra.pop("uses", [])
    store.append("node_created", {"node_id": nid, "parent_ids": parents, "operator": operator,
        "idea": {"operator": operator, "params": {}, "uses": uses}, "uses": uses, **extra})
    if terminal:
        store.append(terminal, {"node_id": nid, "metric": metric,
            **({"reason": "evaluation_error"} if terminal == "node_failed" else {})})


@pytest.fixture
def tree(tmp_path):
    """The logged order is greedy. Evolutionary reaches its same merge earlier on this tape.

    This says nothing about what its LLM would generate in that different history.
    """
    store = EventStore(tmp_path / "events.jsonl")
    start(store)
    for nid, parents, metric in [(0, [], 1.), (1, [], .9), (2, [0], .1),
                                  (3, [0], .2), (4, [0], .3)]:
        node(store, nid, parents, metric)
    node(store, 5, [0, 1], 2., operator="merge")
    return store


def ids(result):
    return [row["node_id"] for row in result["steps"]]


def test_actual_named_policies_same_recorded_target_different_conditional_count(tree):
    events = tree.read_all()
    greedy = replay.replay_policy(events, policy="greedy", n_seeds=2)
    evolutionary = replay.replay_policy(events, policy="evolutionary", n_seeds=2)
    assert ids(greedy) == [0, 1, 2, 3, 4, 5]
    assert ids(evolutionary) == [0, 1, 5]
    for result, count in [(greedy, 6), (evolutionary, 3)]:
        assert result["state"] == "recorded_target_reached"
        assert result["recorded_champion"]["node_id"] == 5
        assert result["recorded_attempts_to_champion"] == 6
        assert result["replayed_attempts_to_champion"] == count
        assert result["coverage"] == 1. and result["unknown_decisions"] == 0
        assert "No causal" in result["meaning"]
        assert "physical eval calls" in result["counting_unit"]


def test_mcts_does_not_renormalize_onto_only_logged_parent(tree):
    result = replay.replay_policy(tree.read_all(), policy="mcts", n_seeds=2)
    assert ids(result) == [0, 1, 2]
    assert result["stop_reason"] == "policy_choice_outside_recorded_tape"
    assert result["coverage"] == .75 and result["unknown_decisions"] == 1
    assert result["replayed_attempts_to_champion"] is None and not result["target_reached"]


def test_merge_parent_order_is_not_an_interchangeable_candidate(tree):
    events = [e.model_copy(deep=True) for e in tree.read_all()]
    events[11].data["parent_ids"] = [1, 0]
    result = replay.replay_policy(events, policy="evolutionary", n_seeds=2)
    assert ids(result) == [0, 1] and result["coverage"] == 2 / 3
    assert result["stop_reason"] == "policy_choice_outside_recorded_tape"


def test_no_unchosen_future_metric_or_advice_reaches_policy(tree, monkeypatch):
    tree.append("hint", {"node_id": 0, "text": "future global advice", "kind": "operator_direction"})
    real_factory, seen = replay.make_policy, []

    class Observe:
        def __init__(self, policy):
            self.policy = policy

        def next_actions(self, state):
            seen.append({nid: n.metric for nid, n in state.nodes.items()})
            assert state.operator_direction_ledger == {} and not state.pending_hints
            assert 5 not in state.nodes  # Champion not seen until its real outcome is chosen.
            return self.policy.next_actions(state)

    monkeypatch.setattr(replay, "make_policy", lambda *a, **kw: Observe(real_factory(*a, **kw)))
    result = replay.replay_policy(tree.read_all(), policy="evolutionary", n_seeds=2)
    assert result["target_reached"]
    assert seen == [{}, {0: 1.}, {0: 1., 1: .9}]


def test_earliest_exact_match_not_hindsight_best(tmp_path):
    store = EventStore(tmp_path / "events.jsonl")
    start(store)
    node(store, 0, [], 1.)
    node(store, 1, [0], .1)  # Earlier, losing proposal for exactly the same action.
    node(store, 2, [0], 2.)
    result = replay.replay_policy(store.read_all(), n_seeds=1)
    assert ids(result) == [0, 1, 2]
    assert result["steps"][1]["metric"] == .1 and result["target_reached"]


@pytest.mark.parametrize("direction,values", [("min", (1., 2., .1)), ("max", (1., .1, 2.))])
def test_direction_and_original_failed_outcomes_are_kept(tmp_path, direction, values):
    store = EventStore(tmp_path / "events.jsonl")
    start(store, direction)
    node(store, 0, [], values[0])
    node(store, 1, [0], None, terminal="node_failed")
    node(store, 2, [0], values[1])
    node(store, 3, [0], values[2])
    result = replay.replay_policy(store.read_all(), n_seeds=1)
    assert ids(result) == [0, 1, 2, 3] and result["target_reached"]
    assert result["steps"][1]["status"] == "failed"
    assert result["steps"][1]["metric"] is None  # Not an imputed zero.


@pytest.mark.parametrize("variant,reason", [
    ("pending", "recorded_candidate_has_no_terminal"),
    ("reset", "reset_lifecycle_not_supported"),
    ("protocol", "candidate_protocol_differs_from_target"),
    ("uses", "recorded_choice_requires_unobserved_dependencies"),
    ("tombstone", "retired_or_aborted_candidate_not_supported"),
    ("trust_after", "post_terminal_trust_evidence_not_supported"),
])
def test_first_unsupported_match_stops_even_with_later_supported_outcome(tmp_path, variant, reason):
    store = EventStore(tmp_path / "events.jsonl")
    start(store)
    node(store, 0, [], 1.)
    kwargs = {"protocol_version": 1} if variant == "protocol" else {}
    if variant == "uses":
        kwargs["uses"] = [99]
    node(store, 1, [0], .1, terminal=None if variant == "pending" else "node_evaluated", **kwargs)
    if variant == "reset":
        store.append("node_reset", {"node_id": 1, "from_stage": "eval"})
    if variant == "tombstone":
        store.append("node_tombstoned", {"node_ids": [1]})
    if variant == "trust_after":
        store.append("reward_hack_suspected", {"node_id": 1, "signal": "hardcoded_metric"})
    node(store, 2, [0], 2.)
    result = replay.replay_policy(store.read_all(), n_seeds=1)
    assert ids(result) == [0]
    assert result["stop_reason"] == reason
    assert result["coverage"] == .5 and result["replayed_attempts_to_champion"] is None
    if variant == "reset":
        assert result["recorded_attempts_to_champion"] is None


@pytest.mark.parametrize("event_type,reason", [
    ("metric_retarget", "objective_changed_not_supported"),
    ("node_confirmed", "post_terminal_selection_schedule_not_supported"),
    ("holdout_evaluated", "post_terminal_selection_schedule_not_supported"),
    ("trust_gate_changed", "post_terminal_selection_schedule_not_supported"),
    ("verifier_group_scored", "post_terminal_selection_schedule_not_supported"),
    ("approval_granted", "post_terminal_selection_schedule_not_supported"),
])
def test_no_fabricated_review_confirmation_or_objective_schedule(tree, event_type, reason):
    tree.append(event_type, {"node_id": 0})
    result = replay.replay_policy(tree.read_all(), n_seeds=2)
    assert result["state"] == "unavailable" and result["stop_reason"] == reason
    assert result["steps"] == [] and result["coverage"] is None


def test_budget_censoring_is_not_target_count(tree):
    result = replay.replay_policy(tree.read_all(), n_seeds=2, max_attempts=3)
    assert result["stop_reason"] == "attempt_budget_exhausted"
    assert result["coverage"] == 1. and not result["target_reached"]
    assert result["replayed_attempts_to_champion"] is None


@pytest.mark.parametrize("kwargs", [{"policy": "asha"}, {"n_seeds": True},
    {"n_seeds": 0}, {"max_attempts": 1001}, {"max_attempts": False}])
def test_unsupported_parameters_refused(tree, kwargs):
    with pytest.raises(ValueError):
        replay.replay_policy(tree.read_all(), **kwargs)


def test_programmatic_sequence_and_version_fence(tree):
    events = tree.read_all()
    with pytest.raises(ValueError, match="dense"):
        replay.replay_policy(events[1:])
    events[2].v = 2
    with pytest.raises(ValueError, match="version"):
        replay.replay_policy(events)


def test_duplicate_start_and_node_recreation_are_not_guessed(tree):
    events = [e.model_copy(deep=True) for e in tree.read_all()]
    events[3].type = "run_started"
    with pytest.raises(ValueError, match="exactly one"):
        replay.replay_policy(events)
    tree.append("node_created", tree.read_all()[1].data)
    result = replay.replay_policy(tree.read_all(), n_seeds=2)
    assert result["stop_reason"] == "recreated_lifecycle_not_supported"
    assert result["coverage"] == 0. and result["steps"] == []


def test_feasibility_is_reconstructed_from_original_terminal(tmp_path):
    store = EventStore(tmp_path / "events.jsonl")
    start(store)
    node(store, 0, [], 100.)
    node(store, 1, [], 1.)
    node(store, 2, [1], 2.)
    events = store.read_all()
    events[2].data["violations"] = ["constraint failed"]
    result = replay.replay_policy(events, n_seeds=2)
    assert result["target_reached"] and ids(result) == [0, 1, 2]
    assert result["steps"][0]["feasible"] is False
    assert result["recorded_champion"]["node_id"] == 2


def test_canonical_atomic_batch_expands_without_creating_an_eventstore(tree):
    tree.append_many([("diagnostic_one", {}), ("diagnostic_two", {})])
    events, source = replay.read_source(tree.path)
    assert len(events) == 15 and source["logical_events"] == 15
    assert len(tree.path.read_bytes().splitlines()) == 14
    assert replay.replay_policy(events, n_seeds=2)["target_reached"]


def test_source_and_cli_are_exactly_read_only_including_cold_process(tree):
    path = tree.path
    before = {p.name: p.read_bytes() for p in path.parent.iterdir() if p.is_file()}
    events, meta = replay.read_source(path)
    assert meta["sha256"] == hashlib.sha256(before["events.jsonl"]).hexdigest()
    assert len(events) == meta["logical_events"] == 13
    result = CliRunner().invoke(app, ["policy-replay", str(path.parent), "--policy", "evolutionary", "--n-seeds", "2"])
    assert result.exit_code == 0, result.output
    assert orjson.loads(result.stdout)["replayed_attempts_to_champion"] == 3
    cold = subprocess.run([sys.executable, "-c", "from looplab.cli import app; app()", "policy-replay", str(path.parent),
        "--policy", "evolutionary", "--n-seeds", "2"], capture_output=True, timeout=40, check=False)
    assert cold.returncode == 0, cold.stderr.decode()
    assert orjson.loads(cold.stdout)["source"] == meta
    assert before == {p.name: p.read_bytes() for p in path.parent.iterdir() if p.is_file()}


@pytest.mark.parametrize("damage", ["torn", "gap", "version", "junk", "batch_tamper"])
def test_source_damage_refused_without_repair(tree, damage):
    path = tree.path
    if damage == "batch_tamper":
        tree.append_many([("node_created", {"node_id": 6}), ("node_failed", {"node_id": 6})])
        raw = path.read_bytes().replace(b'"count":2', b'"count":3')
    else:
        raw = path.read_bytes()
        raw = {"torn": raw[:-1], "gap": raw.replace(b'"seq":1,', b'"seq":21,', 1),
               "version": raw.replace(b'"v":1,', b'"v":2,', 1), "junk": raw + b'{}\n'}[damage]
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        replay.read_source(path)
    result = CliRunner().invoke(app, ["policy-replay", str(path.parent)])
    assert result.exit_code == 2 and "cannot replay policy" in result.output
    assert path.read_bytes() == raw


def test_changed_during_descriptor_capture_is_unavailable(tree, monkeypatch):
    real, calls = os.fstat, []

    def changed(fd):
        result = real(fd)
        if calls:
            tree.path.write_bytes(tree.path.read_bytes() + b"\n")
            result = real(fd)
        calls.append(fd)
        return result

    from looplab.events import source_capture
    monkeypatch.setattr(source_capture.os, "fstat", changed)
    with pytest.raises(ValueError, match="changed"):
        replay.read_source(tree.path)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO only")
def test_nonregular_source_refused_before_reading_or_waiting_for_a_writer(tmp_path):
    fifo = tmp_path / "events.jsonl"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular"):
        replay.read_source(fifo)


def test_no_scalar_champion_is_explicit_unknown(tmp_path):
    store = EventStore(tmp_path / "events.jsonl")
    start(store)
    node(store, 0, [], None, terminal="node_failed")
    result = replay.replay_policy(store.read_all())
    assert result["stop_reason"] == "no_recorded_scalar_champion"
    assert result["recorded_champion"] is None and result["coverage"] is None


def test_actual_toy_engine_source_is_supported_without_running_anything_in_replay(tmp_path, monkeypatch):
    from pathlib import Path
    from looplab.adapters.toytask import ToyTask
    from looplab.engine.orchestrator import Engine
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.search.policy import make_policy
    task = ToyTask.load(Path(__file__).parents[1] / "examples" / "toy_task.json")
    researcher, developer = task.build_roles()
    engine = Engine(tmp_path, task=task, researcher=researcher, developer=developer,
        sandbox=SubprocessSandbox(), policy=make_policy("greedy", n_seeds=2, max_nodes=8))
    original = anyio.run(engine.run)
    assert original.finished and original.best() is not None
    events, source = replay.read_source(tmp_path / "events.jsonl")
    original_bytes = (tmp_path / "events.jsonl").read_bytes()

    def forbid(*args, **kwargs):
        raise AssertionError("offline policy replay must not start engine or subprocess")

    monkeypatch.setattr(Engine, "run", forbid)
    monkeypatch.setattr(subprocess, "Popen", forbid)
    result = replay.replay_policy(events, n_seeds=2, max_attempts=8)
    assert result["target_reached"], result
    assert result["recorded_champion"]["node_id"] == original.best().id
    assert result["source_node_count"] == 8 and source["logical_events"] > 16
    assert result["parameters"]["effective_policy_attributes"]["operator_bandit"] is False
    assert len(result["implementation_sha256"]["policy.py"]) == 64
    assert (tmp_path / "events.jsonl").read_bytes() == original_bytes
