"""`metric_retarget` (doc 68 68.2): the operator makes a DECLARED extra metric the objective.

The incident behind it: a run's target (UnseenRecall@20) rewarded pushing the user's history out of
the top 20, the operator switched to FilteredUnseenRecall@20 — the leader changed — and did it by
editing `node_evaluated.metric` and `run_started.goal` in `events.jsonl` by hand, with backups. Now
it is an operator event the fold applies: every node is re-ranked from the extra metrics it already
recorded, everything measured on the old objective (confirmation, verifier scores, the completion
certificate) stops standing, and the history of objectives stays in the state.
"""
from __future__ import annotations

import anyio
import pytest

from looplab.core.models import Event, objective_value, row_objective
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold

# Two nodes whose task metric and declared extra metric DISAGREE on the leader (direction max):
# node 0 leads on the task's metric, node 1 on `filtered`. Node 2 recorded `filtered` only on the
# candidate's own stdout (`auto`), node 3 never recorded it.
_NODES = (
    (0, 0.60, {"filtered": 0.30}, {"filtered": "declared"}),
    (1, 0.50, {"filtered": 0.45}, {"filtered": "declared"}),
    (2, 0.70, {"filtered": 0.99}, {"filtered": "auto"}),
    (3, 0.40, {}, {}),
)


def _rows(*extra, direction="max", nodes=_NODES, host_grading=False):
    rows = [("run_started", {"run_id": "r", "task_id": "t", "goal": "maximize recall",
                             "direction": direction})]
    if host_grading:
        rows.append(("host_grading", {"predictions": "predictions.json", "scorer": "accuracy"}))
    for nid, metric, extras, channels in nodes:
        rows.append(("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft", "params": {}, "rationale": "r"},
                                      "code": f"print({nid})"}))
        rows.append(("node_evaluated", {"node_id": nid, "generation": 0, "metric": metric,
                                        "violations": [], "extra_metrics": extras,
                                        "extra_metrics_provenance": channels}))
    rows.extend(extra)
    return [Event(seq=i, ts=float(i), type=t, data=d) for i, (t, d) in enumerate(rows)]


def _retarget(key="filtered", **extra):
    return ("metric_retarget", {"key": key, **extra})


# ------------------------------------------------------------------ the fold

def test_a_retarget_re_ranks_every_node_on_the_declared_key_and_can_be_undone():
    before = fold(_rows())
    assert before.best_node_id == 2 and before.objective_key is None
    after = fold(_rows(_retarget(goal="maximize filtered recall")))
    assert after.objective_key == "filtered"
    assert {n.id: n.metric for n in after.nodes.values()} == {0: 0.30, 1: 0.45, 2: None, 3: None}
    assert after.best_node_id == 1, "the leader on the new objective"
    assert {n.id: n.task_metric for n in after.nodes.values()} == {0: 0.6, 1: 0.5, 2: 0.7, 3: 0.4}
    assert after.goal == "maximize filtered recall"
    history, = after.objective_history
    assert history == {"seq": 9, "key": "filtered", "previous": None,
                       "goal": "maximize filtered recall", "previous_goal": "maximize recall"}
    undone = fold(_rows(_retarget(), _retarget(None)))
    assert undone.objective_key is None and undone.best_node_id == 2
    assert {n.id: n.metric for n in undone.nodes.values()} == {0: 0.6, 1: 0.5, 2: 0.7, 3: 0.4}
    assert [h["key"] for h in undone.objective_history] == ["filtered", None]
    assert undone.goal == "maximize recall", "a retarget without a goal restates nothing"


def test_only_the_operators_declared_channel_can_become_the_objective():
    """`auto` is the candidate's own stdout, ungated: node 2 printed 0.99 for itself."""
    assert objective_value({"k": 0.5}, {"k": "declared"}, "k") == 0.5
    for channels in ({"k": "auto"}, {"k": "engine"}, {"k": "unknown"}, {}, None):
        assert objective_value({"k": 0.5}, channels, "k") is None
    for value in (True, float("nan"), float("inf"), "0.5", None):
        assert objective_value({"k": value}, {"k": "declared"}, "k") is None
    assert objective_value({"k": 0.5}, {"k": "declared"}, "") is None


@pytest.mark.parametrize("why,rows", [
    ("a direction flip", _rows(_retarget(direction="min"))),
    ("a malformed key", _rows(("metric_retarget", {"key": ["filtered"]}))),
    ("a blank key", _rows(("metric_retarget", {"key": "   "}))),
    ("a host-graded run", _rows(_retarget(), host_grading=True)),
    ("a holdout already scored", _rows(
        ("holdout_evaluated", {"node_id": 0, "generation": 0, "metric": 0.5}), _retarget())),
])
def test_what_the_fold_ignores_it_ignores_whole(why, rows):
    state = fold(rows)
    assert state.objective_key is None and state.objective_history == [], why
    assert state.nodes[0].metric == 0.6, why


def test_a_duplicate_row_is_one_retarget():
    state = fold(_rows(_retarget(), _retarget()))
    assert len(state.objective_history) == 1


def test_a_terminal_folded_after_the_retarget_ranks_on_the_key():
    late = [("node_created", {"node_id": 4, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "rationale": "r"},
                              "code": "print(4)"}),
            ("node_evaluated", {"node_id": 4, "generation": 0, "metric": 0.1, "violations": [],
                                "extra_metrics": {"filtered": 0.9},
                                "extra_metrics_provenance": {"filtered": "declared"}})]
    state = fold(_rows(_retarget(), *late))
    assert state.nodes[4].metric == 0.9 and state.best_node_id == 4


def test_a_reset_forgets_the_task_metric_and_a_backfill_ranks_what_it_recovers():
    reset = fold(_rows(_retarget(), ("node_reset", {"node_id": 1, "from_stage": "eval",
                                                     "generation": 0})))
    assert reset.nodes[1].metric is None and reset.nodes[1].task_metric is None
    backfilled = fold(_rows(_retarget(), ("score_metrics_backfilled", {
        "node_id": 3, "generation": 0, "extra_metrics": {"filtered": 0.5},
        "extra_metrics_provenance": {"filtered": "declared"}})))
    assert backfilled.nodes[3].metric == 0.5, "a recovered declared value ranks like a live one"
    assert backfilled.nodes[3].task_metric == 0.4


def _confirmed(nid, mean, **stamp):
    return ("node_confirmed", {"node_id": nid, "generation": 0, "mean": mean, "std": 0.01,
                               "seeds": 2, **stamp})


def _certificate(nid, **stamp):
    return ("best_confirmed", {"node_id": nid, "significant": True, "search_epoch": 0,
                               "generations": {"0": 0, "1": 0, "2": 0, "3": 0}, **stamp})


def test_what_was_measured_on_the_old_objective_stops_standing():
    before = fold(_rows(_confirmed(0, 0.61), _certificate(0),
                        ("confirm_eval", {"node_id": 0, "generation": 0, "seed": 1,
                                          "eval_seconds": 1.0, "metric": 0.61})))
    assert before.confirmed_done and before.nodes[0].confirmed_mean == 0.61
    assert before.confirm_seed_results == {0: {1: 0.61}}
    after = fold(_rows(_confirmed(0, 0.61), _certificate(0),
                       ("confirm_eval", {"node_id": 0, "generation": 0, "seed": 1,
                                         "eval_seconds": 1.0, "metric": 0.61}),
                       _retarget()))
    assert not after.confirmed_done, "the confirm phase must run again, on the new key"
    assert after.nodes[0].confirmed_mean is None and after.confirm_seed_results == {}
    # A pass that began before the retarget writes rows stamped with the OLD objective (or none):
    late_old = fold(_rows(_retarget(), _confirmed(0, 0.61), _certificate(0),
                          ("confirm_eval", {"node_id": 0, "generation": 0, "seed": 1,
                                            "eval_seconds": 1.0, "metric": 0.61})))
    assert late_old.nodes[0].confirmed_mean is None and not late_old.confirmed_done
    assert late_old.confirm_seed_results == {}
    # …and one measured on the new objective counts.
    stamped = {"objective_key": "filtered"}
    current = fold(_rows(_retarget(), _confirmed(1, 0.44, **stamped), _certificate(1, **stamped),
                         ("confirm_eval", {"node_id": 1, "generation": 0, "seed": 1,
                                           "eval_seconds": 1.0, "metric": 0.44, **stamped})))
    assert current.nodes[1].confirmed_mean == 0.44 and current.confirmed_done
    assert current.confirm_seed_results == {1: {1: 0.44}}
    assert row_objective("filtered") == "filtered"
    assert [row_objective(v) for v in (None, "", 7, ["filtered"])] == [None] * 4


def test_a_holdout_row_under_a_retarget_is_another_scales_number():
    state = fold(_rows(_retarget(), ("holdout_evaluated", {
        "node_id": 1, "generation": 0, "metric": 0.9, "eval_seconds": 2.0})))
    assert state.holdout_evaluated_ids == [] and state.nodes[1].holdout_metric is None
    assert state.eval_seconds_by_kind.get("holdout") == 2.0, "its compute is still charged"


def test_the_brief_names_the_key_only_while_a_retarget_is_in_force():
    from looplab.agents.state_brief import _state_brief

    plain = _state_brief(fold(_rows()), None)
    assert "Ranked by" not in plain
    retargeted = _state_brief(fold(_rows(_retarget(goal="maximize filtered recall"))), None)
    assert retargeted.startswith("Goal: maximize filtered recall\n")
    assert "Ranked by: filtered — the operator made this declared metric the objective" in retargeted


# ------------------------------------------------------------------ the engine

def test_the_holdout_phase_is_not_pending_under_a_retarget(tmp_path):
    from looplab.engine.holdout import HoldoutGrader

    class _Engine:
        _holdout_idx = [1, 2]
        _host_grader = None
        _eval_spec = {"holdout_scorer": {"command": ["python", "withheld.py"]}}

        def _holdout_topk(self, state):
            return [1]

    grader = HoldoutGrader(_Engine())
    assert grader.holdout_pending(fold(_rows())) is True
    assert grader.holdout_pending(fold(_rows(_retarget()))) is False


def test_the_confirm_phase_measures_and_stamps_the_objective_in_force(tmp_path):
    """Driven through the engine's own confirm phase: under a retarget each seed records the
    DECLARED key's value — not the task metric the same eval printed — and the three rows carry the
    objective they were measured on, so the fold counts them."""
    from looplab.adapters.toytask import ToyTask
    from looplab.engine.orchestrator import Engine
    from looplab.runtime.sandbox import RunResult, SubprocessSandbox
    from looplab.search.policy import GreedyTree

    task = ToyTask.load(__import__("pathlib").Path("examples/toy_task.json"))
    researcher, developer = task.build_roles()
    eng = Engine(tmp_path / "run", task=task, researcher=researcher, developer=developer,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                 confirm_top_k=1, confirm_seeds=2)
    for event in _rows(_retarget()):
        eng.store.append(event.type, dict(event.data))

    def fake_run_eval(node, workdir, env=None, profile=None, cancel=None):
        result = RunResult(exit_code=0, stdout="", stderr="", metric=0.1, timed_out=False)
        result.extra_metrics = {"filtered": 0.8 + node.id / 100}
        result.extra_metrics_provenance = {"filtered": "declared"}
        return result

    eng._run_eval = fake_run_eval
    anyio.run(eng._confirm_phase, fold(eng.store.read_all()))
    rows = [e for e in eng.store.read_all()
            if e.type in ("confirm_eval", "node_confirmed", "best_confirmed")]
    assert {e.type for e in rows} == {"confirm_eval", "node_confirmed", "best_confirmed"}, rows
    assert all(e.data.get("objective_key") == "filtered" for e in rows), rows
    assert {e.data["metric"] for e in rows if e.type == "confirm_eval"} == {0.81}
    state = fold(eng.store.read_all())
    assert state.nodes[1].confirmed_mean == pytest.approx(0.81) and state.confirmed_done


def test_a_retarget_mid_pass_retires_the_pass(tmp_path):
    """The objective moves while a seed runs: the pass stops before it certifies anything, so the
    next pass measures on the new key (as a reset retires a pass)."""
    from looplab.adapters.toytask import ToyTask
    from looplab.engine.orchestrator import Engine
    from looplab.runtime.sandbox import RunResult, SubprocessSandbox
    from looplab.search.policy import GreedyTree

    task = ToyTask.load(__import__("pathlib").Path("examples/toy_task.json"))
    researcher, developer = task.build_roles()
    eng = Engine(tmp_path / "run", task=task, researcher=researcher, developer=developer,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                 confirm_top_k=1, confirm_seeds=2)
    for event in _rows():
        eng.store.append(event.type, dict(event.data))

    def retarget_while_running(node, workdir, env=None, profile=None, cancel=None):
        if not any(e.type == "metric_retarget" for e in eng.store.read_all()):
            eng.store.append("metric_retarget", {"key": "filtered"})
        return RunResult(exit_code=0, stdout="", stderr="", metric=0.6, timed_out=False)

    eng._run_eval = retarget_while_running
    anyio.run(eng._confirm_phase, fold(eng.store.read_all()))
    types = [e.type for e in eng.store.read_all()]
    assert "best_confirmed" not in types and "node_confirmed" not in types, types
    assert not fold(eng.store.read_all()).confirmed_done


# ------------------------------------------------------------------ the command

def _server_run(root, *extra, task=None, host_grading=False):
    import json

    rd = root / "demo"
    rd.mkdir(parents=True)
    store = EventStore(rd / "events.jsonl")
    for event in _rows(*extra, host_grading=host_grading):
        store.append(event.type, dict(event.data))
    if task is not None:
        (rd / "task.snapshot.json").write_text(json.dumps(task))
    return rd


def _client(root):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from looplab.serve.server import make_app

    return TestClient(make_app(root))


def test_the_command_re_ranks_a_stopped_run_without_an_engine(tmp_path):
    from tests.factories import command_terminal, post_command

    _server_run(tmp_path)
    client = _client(tmp_path)
    record = post_command(client, "metric_retarget",
                          {"key": " filtered ", "goal": "maximize filtered recall"})
    assert record.status_code in (200, 202), record.text
    done = command_terminal(client, record.json())
    assert done["status"] == "succeeded", done
    state = client.get("/api/runs/demo/state").json()["state"]
    assert state["objective_key"] == "filtered" and state["best_node_id"] == 1
    assert state["goal"] == "maximize filtered recall"
    appended = [e for e in EventStore(tmp_path / "demo" / "events.jsonl").read_all()
                if e.type == "metric_retarget"]
    assert [{k: v for k, v in e.data.items() if k != "_command_id"} for e in appended] == [
        {"key": "filtered", "goal": "maximize filtered recall"}], "stripped, and nothing else"


def _rejected(answer, code):
    """An intake refusal is a durable REJECTED record naming its code, nothing appended."""
    assert answer.status_code == 200, answer.text
    record = answer.json()
    assert record["status"] == "rejected" and record["error"]["code"] == code, record
    return record


@pytest.mark.parametrize("data,code", [
    ({"key": "nowhere"}, "retarget_key_not_declared"),
    ({"key": "filtered", "direction": "min"}, "retarget_direction_flip"),
    ({"key": None}, "retarget_unchanged"),
    ({}, "invalid_command"),
    ({"key": "filtered", "extra": 1}, "invalid_command"),
    ({"key": "  "}, "invalid_command"),
])
def test_the_command_refuses_what_the_fold_would_not_apply(tmp_path, data, code):
    from tests.factories import post_command

    _server_run(tmp_path)
    record = _rejected(post_command(_client(tmp_path), "metric_retarget", data), code)
    if code == "retarget_key_not_declared":
        assert record["error"]["message"].endswith("declared: 'filtered'"), record
    assert not [e for e in EventStore(tmp_path / "demo" / "events.jsonl").read_all()
                if e.type == "metric_retarget"]


def test_a_key_the_declaration_orients_against_the_run_is_refused(tmp_path):
    from tests.factories import post_command

    oriented = [("node_created", {"node_id": 5, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {}, "rationale": "r"},
                                  "code": "print(5)"}),
                ("node_evaluated", {"node_id": 5, "generation": 0, "metric": 0.3,
                                    "violations": [], "extra_metrics": {"latency": 12.0},
                                    "extra_metrics_provenance": {"latency": "declared"},
                                    "extra_metrics_direction": {"latency": "min"}})]
    _server_run(tmp_path, *oriented)
    _rejected(post_command(_client(tmp_path), "metric_retarget", {"key": "latency"}),
              "retarget_direction_flip")
    state = fold(_rows(*oriented, _retarget("latency")))
    assert state.nodes[5].metric is None, "the fold never ranks it the run's way either"


@pytest.mark.parametrize("holdout", ["host_graded", "scored", "withheld_scorer"])
def test_a_run_with_a_holdout_cannot_be_retargeted(tmp_path, holdout):
    from tests.factories import post_command

    extra = ([("holdout_evaluated", {"node_id": 0, "generation": 0, "metric": 0.5})]
             if holdout == "scored" else [])
    task = ({"kind": "repo", "id": "t", "goal": "g", "direction": "max",
             "editable_path": str(tmp_path), "eval": {
                 "command": ["python", "score.py"],
                 "holdout_scorer": {"command": ["python", "/withheld/score.py"]}}}
            if holdout == "withheld_scorer" else None)
    _server_run(tmp_path, *extra, task=task, host_grading=holdout == "host_graded")
    _rejected(post_command(_client(tmp_path), "metric_retarget", {"key": "filtered"}),
              "retarget_with_holdout")
