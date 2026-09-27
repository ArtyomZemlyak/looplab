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


def _rows(*extra, direction="max", nodes=_NODES, host_grading=False, started=None):
    rows = [("run_started", {"run_id": "r", "task_id": "t", "goal": "maximize recall",
                             "direction": direction, **(started or {})})]
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


def test_a_stale_certificate_does_not_keep_the_old_leader():
    """Node 0 was confirmed and certified on the task's metric; on `filtered` it is the worse of
    the two declared values, and the certificate measured on the old objective must not keep it."""
    before = fold(_rows(_confirmed(0, 0.61), _certificate(0)))
    assert before.confirmed_done and before.nodes[0].confirmed_mean == 0.61
    after = fold(_rows(_confirmed(0, 0.61), _certificate(0), _retarget()))
    assert after.best_node_id == 1 and not after.confirmed_done


def test_an_undo_restores_the_goal_its_retargets_restated():
    """Back to the task's own metric is back to the goal the run had before the retargets that
    restated it — kept over the task metric's numbers, a restated goal read "maximize filtered
    recall … Best so far: 0.7" with no "Ranked by" line (critic 2026-09-27, driven)."""
    once = fold(_rows(_retarget(goal="maximize filtered recall"), _retarget(None)))
    assert once.goal == "maximize recall" and once.objective_key is None
    assert once.objective_history[-1] == {
        "seq": 10, "key": None, "previous": "filtered", "goal": "maximize recall",
        "previous_goal": "maximize filtered recall"}
    # Two restatements: the EARLIEST one's `previous_goal` is the run's own.
    twice = fold(_rows(_retarget(goal="A"), _retarget("other", goal="B"), _retarget(None)))
    assert twice.goal == "maximize recall"
    # An undo that states its own goal keeps it.
    stated = fold(_rows(_retarget(goal="A"), _retarget(None, goal="C")))
    assert stated.goal == "C"
    # A restatement already undone is not undone again: the second undo finds none since the first.
    again = fold(_rows(_retarget(goal="A"), _retarget(None), _retarget(), _retarget(None)))
    assert again.goal == "maximize recall"
    assert [h.get("goal") for h in again.objective_history] == ["A", "maximize recall", None, None]
    # …and the walk stops at the last undo: a goal an undo STATED is not walked past.
    past = fold(_rows(_retarget(goal="A"), _retarget(None, goal="C"), _retarget(), _retarget(None)))
    assert past.goal == "C"
    # A restatement that changed nothing restores nothing: the undo row carries no goal.
    same = fold(_rows(_retarget(goal="maximize recall"), _retarget(None)))
    assert same.goal == "maximize recall" and "goal" not in same.objective_history[-1]
    # An EMPTY launch goal is restored too — it was read as "nothing to restore" and the restated
    # goal stayed (critic 2026-09-27, second pass) — and `task_goal` names it throughout.
    empty = fold(_rows(_retarget(goal="maximize filtered recall"), started={"goal": ""}))
    assert empty.goal == "maximize filtered recall" and empty.task_goal() == ""
    undone = fold(_rows(_retarget(goal="maximize filtered recall"), _retarget(None),
                        started={"goal": ""}))
    assert undone.goal == "" and undone.objective_history[-1]["goal"] == "", undone.goal


def test_verifier_scores_and_the_confirmed_ruler_stop_standing():
    verified = ("node_verified", {"node_id": 1, "generation": 0, "score": 0.9})
    before = fold(_rows(verified, _confirmed(0, 0.61, protocol_profile="p1")))
    assert before.nodes[1].verifier_score == 0.9 and before.nodes[0].confirmed_ruler == "p1"
    after = fold(_rows(verified, _confirmed(0, 0.61, protocol_profile="p1"), _retarget()))
    assert after.nodes[1].verifier_score is None, "it judged the old objective's evidence"
    assert after.nodes[0].confirmed_ruler is None, "its certificate is gone with it"


def test_the_task_metric_is_published_and_the_champion_says_which_ruler():
    """Excluded from the dump, no surface could show a node's task metric under a retarget — the
    Metrics tab's line promised it and the state carried none (critic 2026-09-27)."""
    from looplab.engine.champion_caveats import (CHAMPION_CAVEAT_RETARGETED_OBJECTIVE,
                                                 champion_metric_caveats)

    state = fold(_rows(_retarget()))
    assert state.nodes[1].model_dump()["task_metric"] == 0.5
    assert state.model_dump(mode="json")["nodes"]["1"]["task_metric"] == 0.5
    assert CHAMPION_CAVEAT_RETARGETED_OBJECTIVE in champion_metric_caveats(state)
    assert CHAMPION_CAVEAT_RETARGETED_OBJECTIVE not in champion_metric_caveats(fold(_rows()))
    assert CHAMPION_CAVEAT_RETARGETED_OBJECTIVE not in champion_metric_caveats(
        fold(_rows(_retarget(), _retarget(None)))), "an undone retarget leaves no caveat"


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


def _toy_engine(tmp_path, *rows, name="run", **kw):
    from looplab.adapters.toytask import ToyTask
    from looplab.engine.orchestrator import Engine
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.search.policy import GreedyTree

    task = ToyTask.load(__import__("pathlib").Path("examples/toy_task.json"))
    researcher, developer = task.build_roles()
    eng = Engine(tmp_path / name, task=task, researcher=researcher, developer=developer,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1), **kw)
    for event in rows:
        eng.store.append(event.type, dict(event.data))
    return eng


def _declared_eval(value):
    from looplab.runtime.sandbox import RunResult

    def run_eval(node, workdir, env=None, profile=None, cancel=None):
        result = RunResult(exit_code=0, stdout="", stderr="", metric=0.1, timed_out=False)
        result.extra_metrics = {"filtered": value}
        result.extra_metrics_provenance = {"filtered": "declared"}
        return result
    return run_eval


def test_a_pass_with_nothing_to_confirm_certifies_on_the_objective_in_force(tmp_path):
    """No node recorded the new key: the pass's empty certificate is stamped with the objective,
    or the fold would read it as measured on the task's metric and the phase would never close."""
    eng = _toy_engine(tmp_path, *_rows(_retarget(), nodes=((3, 0.40, {}, {}),)),
                      confirm_top_k=1, confirm_seeds=2)
    anyio.run(eng._confirm_phase, fold(eng.store.read_all()))
    [certificate] = [e.data for e in eng.store.read_all() if e.type == "best_confirmed"]
    assert certificate["node_id"] is None and certificate["objective_key"] == "filtered"
    assert fold(eng.store.read_all()).confirmed_done


def test_a_forced_confirm_measures_the_objective_in_force(tmp_path):
    """The operator's forced confirmation (`_confirm_node`) records the DECLARED key's value on
    each seed under a retarget — not the task metric the same eval printed — and stamps it."""
    eng = _toy_engine(tmp_path, *_rows(_retarget()), confirm_seeds=3)
    eng._run_eval = _declared_eval(0.77)
    anyio.run(eng._confirm_node, fold(eng.store.read_all()).nodes[1])
    seeds = [e.data for e in eng.store.read_all() if e.type == "confirm_eval"]
    assert len(seeds) == 3 and {row["metric"] for row in seeds} == {0.77}, seeds
    assert all(row["objective_key"] == "filtered" for row in seeds)
    assert set(fold(eng.store.read_all()).confirm_seed_results[1].values()) == {0.77}


def test_asha_and_the_noise_floor_hold_a_fresh_task_measurement_beside_the_task_scale(tmp_path):
    """Both read a node BESIDE a fresh measurement of the task's metric (a live ASHA sample, the
    floor's repeats), so both read the task's scale: under a retarget the siblings' objective values
    put a live 0.55 beating both siblings' task finals below [0.70, 0.72] (critic 2026-09-27)."""
    from looplab.engine.asha_monitor import sibling_final_metrics

    plain, retargeted = fold(_rows()), fold(_rows(_retarget()))
    assert sorted(sibling_final_metrics(plain, 3)) == [0.5, 0.6, 0.7]
    # The pool is the selector's (node 2 is unranked on `filtered`); the NUMBERS are the task's —
    # not the objective's [0.30, 0.45].
    assert sorted(sibling_final_metrics(retargeted, 3)) == [0.5, 0.6], "the task's scale"

    eng = _toy_engine(tmp_path, *_rows(_retarget()), eval_noise_seeds=2)

    async def repeat(nd, seed, profile):       # a repeat measures the task metric: 0.5
        eng.store.append("eval_noise_seed", {"node_id": nd.id, "generation": nd.attempt,
                                             "seed": seed, "eval_seconds": 1.0, "metric": 0.5})
    eng._run_noise_seed = repeat
    anyio.run(eng._noise_floor_phase, fold(eng.store.read_all()))
    [floor] = [e.data for e in eng.store.read_all() if e.type == "eval_noise_floor"]
    assert floor["node_id"] == 1 and floor["search_metric"] == 0.5, floor


# ------------------------------------------------------------------ the command

def _server_run(root, *extra, task=None, host_grading=False, name="demo", started=None):
    import json

    rd = root / name
    rd.mkdir(parents=True)
    store = EventStore(rd / "events.jsonl")
    for event in _rows(*extra, host_grading=host_grading, started=started):
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


def test_the_direction_is_read_in_the_folds_spelling(tmp_path):
    """"MAX" on a maximized run is the same direction, never a flip — and stored as typed, the fold
    would IGNORE the row (it compares exactly) under a `succeeded` command (critic 2026-09-27)."""
    from tests.factories import command_terminal, post_command

    _server_run(tmp_path)
    client = _client(tmp_path)
    record = post_command(client, "metric_retarget", {"key": "filtered", "direction": " MAX "})
    assert command_terminal(client, record.json())["status"] == "succeeded"
    [row] = [e.data for e in EventStore(tmp_path / "demo" / "events.jsonl").read_all()
             if e.type == "metric_retarget"]
    assert row["direction"] == "max"
    assert client.get("/api/runs/demo/state").json()["state"]["objective_key"] == "filtered"
    for i, direction in enumerate(("sideways", 5, True)):
        _rejected(post_command(client, "metric_retarget", {"key": None, "direction": direction},
                               key=f"bad-{i}"), "invalid_command")


# ------------------------------------------------------------------ the task's scale, outside the run
# A retargeted run's champion is still the one IT chose; what leaves the run — a case, a capsule, a
# lesson, a note, a listing row, an imported node — is on the task's own scale or says which ruler
# (critic 2026-09-27, driven: its objective value was elected over a plain run's task metric and
# handed to the next run as "the best known configuration").

def test_a_case_is_stored_on_the_task_scale_beside_the_objective_that_chose_it(tmp_path):
    mem = tmp_path / "mem"
    eng = _toy_engine(tmp_path, memory_dir=str(mem))
    eng.lessons.store_case(fold(_rows(_retarget())))
    [case] = [__import__("json").loads(line) for line in
              (mem / "cases.jsonl").read_text().splitlines() if line.strip()]
    assert case["metric"] == 0.5, "node 1's task metric, not its 0.45 on `filtered`"
    assert case["objective_key"] == "filtered"
    # A retargeted champion whose terminal recorded no task metric has nothing on the case's scale:
    # no row, and no raise into finalize's retry on every pass.
    bare = _toy_engine(tmp_path, name="bare", memory_dir=str(tmp_path / "mem2"))
    bare.lessons.store_case(fold(_rows(_retarget(), nodes=(
        (0, None, {"filtered": 0.9}, {"filtered": "declared"}),))))
    assert not (tmp_path / "mem2" / "cases.jsonl").exists()
    # Without a retarget the case is what it always was.
    plain = _toy_engine(tmp_path, name="plain", memory_dir=str(tmp_path / "mem3"))
    plain.lessons.store_case(fold(_rows()))
    [case] = [__import__("json").loads(line) for line in
              (tmp_path / "mem3" / "cases.jsonl").read_text().splitlines() if line.strip()]
    assert case["metric"] == 0.7 and "objective_key" not in case


def test_a_retargeted_case_names_the_tasks_goal_and_says_what_ranked_it(tmp_path):
    """MEDIUM (critic 2026-09-27, driven): the case put the task's number beside the goal a retarget
    RESTATED — `kb_search` handed a later run "metric=0.5 … measured on this goal: maximize filtered
    recall", where 0.5 was the task's recall. The case names the task's own goal (the one its number
    was measured on, and the one the scope gate and fingerprint key on), and the hit says which
    objective chose that champion."""
    import json

    from looplab.tools.knowledge_tools import KnowledgeTools

    restated = fold(_rows(_retarget(goal="maximize filtered recall")))
    assert restated.goal == "maximize filtered recall"
    assert restated.task_goal() == "maximize recall"
    assert fold(_rows()).task_goal() == "maximize recall"
    mem = tmp_path / "mem"
    eng = _toy_engine(tmp_path, memory_dir=str(mem))
    eng.lessons.store_case(restated)
    [case] = [json.loads(line) for line in (mem / "cases.jsonl").read_text().splitlines()
              if line.strip()]
    assert case["goal"] == "maximize recall", case
    assert case["fingerprint"] == eng.lessons.task_fingerprint(
        fold(_rows()), restated.best()), "keyed on the task's goal, as a plain run of it is"
    kt = KnowledgeTools(None, cases_path=str(mem / "cases.jsonl"))
    kt.bind_state(fold(_rows(started={"run_id": "later", "run_uid": "later-uid"})))
    [(_rid, _src, payload)] = kt._records()
    assert ("metric=0.5 (the task's own metric; that run's champion was ranked by 'filtered', "
            "an operator retarget)") in payload["text"], payload["text"]
    assert payload["text"].endswith("measured on this goal: maximize recall"), payload["text"]


def test_every_cross_run_row_of_a_retargeted_run_keys_on_the_tasks_goal(tmp_path):
    """LOW (critic 2026-09-27, second pass): the case, the capsule and the index passport keyed on
    the task's goal while the lessons, the meta note, the distillation, the reconcile pass and the
    novelty audit — every caller of `task_fingerprint` without an explicit goal — keyed on the
    restated one: one run's rows on two keys. The fingerprint's default is the task's goal."""
    restated = fold(_rows(_retarget(goal="maximize filtered recall")))
    eng = _toy_engine(tmp_path, memory_dir=str(tmp_path / "mem"))
    plain = fold(_rows())
    assert eng._task_fingerprint(restated, restated.best()) == eng._task_fingerprint(
        plain, restated.best())
    assert eng.lessons.task_fingerprint(restated, restated.best(), goal="other words") != (
        eng.lessons.task_fingerprint(plain, restated.best())), "an explicit goal still overrides"


def test_the_tasks_goal_is_the_one_before_the_first_restatement():
    """Mutant G (critic 2026-09-27, second pass): read last-first, two restatements named the FIRST
    restated goal as the task's."""
    twice = fold(_rows(_retarget(goal="maximize filtered recall"),
                       _retarget(key=None, goal="maximize recall at k")))
    assert twice.goal == "maximize recall at k"
    assert twice.task_goal() == "maximize recall"


def test_a_plain_lessons_identity_is_the_one_it_always_had_and_a_retargeted_one_is_its_own():
    """MEDIUM (critic 2026-09-27, second pass, driven): the retarget clause rides past the 160-char
    identity cut, so the exact pass folded a plain and a retargeted lesson into one row, one
    contradiction key and one `lesson_id` — the newer verdict on one ruler retired the other's lesson
    from the shared store. The objective joins the identity of a lesson that HAS one, and only
    then: every plain lesson's id is the digest it always was (mutant I)."""
    import hashlib

    from looplab.engine.lesson_hygiene import (_objective_identity, consolidate_lessons,
                                               filter_contradicted, lesson_id, normalize_statement)
    from looplab.trust.cross_run import retargeted_lesson_note

    base = ("Raising the contrastive margin from 0.2 to 0.4 while mining hard negatives from the "
            "same batch improved the ranking on every seed we tried, and the gain held under the "
            "longer schedule as well")
    assert len(base) > 160
    plain_id = "les-" + hashlib.sha256(normalize_statement(base).encode()).hexdigest()[:24]
    for row in ({"statement": base}, {"statement": base, "objective_key": None},
                {"statement": base, "objective_key": ""}):
        assert _objective_identity(row) == () and lesson_id(row) == plain_id
    plain = {"statement": base, "outcome": "supported", "task_id": "t", "run_id": "P",
             "role": "researcher", "evidence": [1]}
    retargeted = {"statement": base + retargeted_lesson_note("filtered"), "outcome": "abandoned",
                  "task_id": "t", "run_id": "R", "role": "researcher", "evidence": [2],
                  "objective_key": "filtered"}
    assert _objective_identity(retargeted) == ("filtered",)
    assert lesson_id(retargeted) != lesson_id(plain)
    merged = consolidate_lessons([plain, retargeted])
    assert [(r["run_id"], r["outcome"], r.get("objective_key")) for r in merged] == [
        ("P", "supported", None), ("R", "abandoned", "filtered")]
    kept = filter_contradicted([(0.9, 0, plain), (0.9, 1, retargeted)])
    assert [r[2]["run_id"] for r in kept] == ["P", "R"], "another ruler's verdict reverses nothing"


def test_the_clause_is_kept_for_any_key_and_touches_nothing_else():
    """`keep_retarget_clause` (mutant K: always appended) — and its pattern, which missed a key
    holding `]` or both quote kinds (critic 2026-09-27, second pass, driven)."""
    from looplab.trust.cross_run import keep_retarget_clause, retargeted_lesson_note

    plain = "a plain statement about the task's own metric " * 6
    assert keep_retarget_clause(plain, plain[:40]) == plain[:40]
    for key in ("filtered", "ndcg[10]", 'it\'s "x"', "a\\b"):
        full = "the body of a long lesson " * 10 + retargeted_lesson_note(key)
        assert keep_retarget_clause(full, full[:40]).endswith(retargeted_lesson_note(key)), key
        assert keep_retarget_clause(full, full) == full, "a cut that kept it gains no second one"


def test_the_steward_the_context_pack_and_the_strategist_keep_the_clause(tmp_path):
    """LOW (critic 2026-09-27, second pass, driven): the claim steward's 400-character cut, the
    proposal context pack's and the Strategist's mixed-evidence note's 120-character cuts took the
    clause a retargeted lesson ends with — a number measured on a declared extra metric then read as
    the task's. Each keeps it now (`keep_retarget_clause`); a plain claim renders as it did."""
    import orjson

    from looplab.engine.claim_steward import _claim_prompt_payload
    from looplab.engine.claims import claims_for_memory
    from looplab.engine.claims_retrieval import build_context_pack, render_context_pack
    from looplab.engine.strategy import StrategyCadenceMixin
    from looplab.core.models import RunState
    from looplab.trust.cross_run import retargeted_lesson_note

    clause = retargeted_lesson_note("filtered")
    statement = ("mnr helps " + " ".join(["the hard negatives stayed apart under the margin"] * 9)
                 + clause)
    assert len(statement) > 400 + len(clause)
    rows = [{"statement": statement, "outcome": outcome, "evidence": [i], "run_id": f"r{i}",
             "task_id": "t", "direction": "max", "objective_key": "filtered"}
            for i, outcome in ((1, "supported"), (2, "tested"))]
    rows.append({"statement": "a plain lesson on the task's own metric", "outcome": "supported",
                 "evidence": [3], "run_id": "r3", "task_id": "t", "direction": "max"})
    (tmp_path / "lessons.jsonl").write_bytes(b"\n".join(orjson.dumps(r) for r in rows) + b"\n")
    claims = claims_for_memory(tmp_path)
    payload, _ids = _claim_prompt_payload(claims)
    by_start = {p["statement"][:20]: p["statement"] for p in payload}
    assert by_start["mnr helps the hard n"].endswith(clause.strip()), by_start
    assert "ranked by" not in by_start["a plain lesson on th"]
    pack = render_context_pack(build_context_pack(claims))
    [line] = [x for x in pack.splitlines() if "mnr helps" in x]
    assert clause.strip() in line, line

    class _Host(StrategyCadenceMixin):
        def __init__(self, memory_dir):
            self._cross_run_advisory = True
            self.memory_dir = str(memory_dir)

    note = _Host(tmp_path)._cross_run_note_for_ctx(
        RunState(run_id="current", task_id="t", direction="max"))
    assert "mixed-evidence records" in note and "ranked by" in note, note


def test_a_concept_capsule_is_on_the_task_scale(tmp_path):
    from types import SimpleNamespace

    from looplab.engine.lessons import LessonMemory
    from looplab.engine.memory import ConceptCapsuleStore

    mem = tmp_path / "mem"
    mem.mkdir()
    tagged = [("node_concepts", {"node_id": nid, "concepts": ["data/hard-negatives"],
                                 "mode": "llm"}) for nid in (0, 1)]
    engine = SimpleNamespace(memory_dir=str(mem), _fingerprint_universal=True,
                             task=SimpleNamespace(kind="dataset", metric="recall", id="t",
                                                  goal="maximize recall"))
    LessonMemory(engine).store_concept_capsule(fold(_rows(*tagged, _retarget())))
    [capsule] = ConceptCapsuleStore(mem / "concept_capsules.jsonl").all()
    assert capsule["best_metric"] == 0.5, "the champion's task metric, not its 0.45"
    assert capsule["concept_outcomes"]["data/hard-negatives"] == 0.6, "best-of on the task scale"


def _long(words: str, n: int) -> str:
    return " ".join([words] * n)


def test_a_retargeted_runs_lessons_and_note_say_which_metric_they_ranked_by(tmp_path):
    from looplab.engine.lessons_priors import retargeted_lesson_note

    clause = retargeted_lesson_note("filtered")
    mem = tmp_path / "mem"
    eng = _toy_engine(tmp_path, *_rows(_retarget(), ("run_finished", {
        "reason": "done", "finalization_required": True})),
        memory_dir=str(mem), reflection_priors=True)
    state = fold(eng.store.read_all())
    eng.lessons.append_lessons([{"statement": "raising the margin helped", "outcome": "supported"},
                                {"statement": "already said" + clause, "outcome": "tested"}],
                               hygiene=False, state=state)
    rows = [__import__("json").loads(line) for line in
            (mem / "lessons.jsonl").read_text().splitlines() if line.strip()]
    assert [r["statement"] for r in rows] == ["raising the margin helped" + clause,
                                              "already said" + clause], "said once"
    assert {r["objective_key"] for r in rows} == {"filtered"}
    # The run-end reflection note ("what won") says it too.
    eng._causal_meta_note = lambda *_args: "the margin won"
    eng._reflect_lessons = lambda *_args: []
    eng._comparative_lessons_on = False
    eng._write_reflection_note(state)
    [note] = [__import__("json").loads(line) for line in
              (mem / "meta_notes.jsonl").read_text().splitlines() if line.strip()]
    assert note["note"] == "the margin won" + clause and note["objective_key"] == "filtered"
    # Without a retarget, nothing is added.
    plain_mem = tmp_path / "plain_mem"
    plain = _toy_engine(tmp_path, *_rows(), name="plain", memory_dir=str(plain_mem))
    plain.lessons.append_lessons([{"statement": "raising the margin helped", "outcome": "x"}],
                                 hygiene=False, state=fold(plain.store.read_all()))
    [row] = [__import__("json").loads(line) for line in
             (plain_mem / "lessons.jsonl").read_text().splitlines() if line.strip()]
    assert row == {"statement": "raising the margin helped", "outcome": "x"}


def test_the_prior_keeps_the_clause_a_long_statements_cut_took(tmp_path):
    """The clause rides at the END (a shared prefix would make every retargeted lesson one
    `prompt_slot_key` family), which is where the prior's cut lands on a long statement: the
    renderer re-attaches it from the row's stamp — for a lesson and for a note."""
    import json

    from looplab.engine.lessons_priors import LESSON_STATEMENT_CHARS, retargeted_lesson_note

    clause = retargeted_lesson_note("filtered")
    mem = tmp_path / "mem"
    mem.mkdir()
    eng = _toy_engine(tmp_path, memory_dir=str(mem), reflection_priors=True)
    fp = [t for t in eng._task_fingerprint(eng._empty_state_for_fp()) if not t.startswith("param:")]
    statement = "LONGLESSON " + _long("a wider margin separates the hard negatives", 20) + clause
    assert len(statement) > LESSON_STATEMENT_CHARS + len(clause)
    note = "LONGNOTE " + _long("the margin won because the negatives were hard", 40) + clause
    (mem / "lessons.jsonl").write_text("".join(json.dumps({
        "task_id": "toy_quadratic", "fingerprint": fp, "run_id": "earlier", "direction": "min",
        "statement": text, "outcome": outcome, "confidence": 0.7, "delta": 1.0,
        "role": "researcher", "objective_key": "filtered"}) + "\n" for text, outcome in (
            (statement, "supported"), ("SHORTLESSON keep the margin" + clause, "tested"))))
    (mem / "meta_notes.jsonl").write_text(json.dumps({
        "task_id": "toy_quadratic", "run_id": "earlier", "direction": "min", "fingerprint": fp,
        "note": note, "objective_key": "filtered"}) + "\n")
    prior = eng._load_reflection_priors()
    assert "LONGLESSON" in prior and "LONGNOTE" in prior, prior
    lesson_part = prior[prior.index("LONGLESSON"):]
    lesson_part = lesson_part[:lesson_part.index("[supported")]
    assert "redacted preview" in lesson_part, "the statement WAS cut"
    assert lesson_part.rstrip().endswith(clause.strip()), lesson_part[-200:]
    note_part = prior[prior.index("LONGNOTE"):]
    assert "redacted preview" in note_part and clause in note_part
    # A clause the cut left in place is not attached twice.
    assert "SHORTLESSON keep the margin" + clause + " [tested" in prior
    assert prior.count(clause) == 3, "the long lesson, the note and the short lesson: once each"


def _client_over(root):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from looplab.serve.server import make_app

    return TestClient(make_app(root))


_REFERENCE = {"reference_score": {"baseline": {"value": 0.4, "source": "paper"},
                                  "target": {"value": 0.8, "source": "sota"}}}


def test_the_runs_row_names_the_objective_and_reads_headroom_on_the_task_scale(tmp_path):
    from looplab.engine.champion_caveats import CHAMPION_CAVEAT_RETARGETED_OBJECTIVE

    _server_run(tmp_path, _retarget(), started=_REFERENCE)
    _server_run(tmp_path, name="plain", started=_REFERENCE)
    rows = {row["run_id"]: row for row in _client_over(tmp_path).get("/api/runs").json()}
    retargeted, plain = rows["demo"], rows["plain"]
    assert retargeted["objective_key"] == "filtered" and plain["objective_key"] is None
    assert CHAMPION_CAVEAT_RETARGETED_OBJECTIVE in retargeted["best_metric_caveats"]
    assert CHAMPION_CAVEAT_RETARGETED_OBJECTIVE not in plain["best_metric_caveats"]
    assert retargeted["headroom"]["best"] == 0.5, "the champion's TASK metric, the reference's scale"
    assert retargeted["headroom"]["gap_closed"] == pytest.approx(0.25)
    assert plain["headroom"]["best"] == 0.7


def test_the_cli_result_and_the_report_name_the_objective(tmp_path, capsys):
    from looplab.cli import _print_result
    from looplab.serve.report import _report_context

    retargeted = fold(_rows(_retarget(), started=_REFERENCE))
    _print_result(retargeted)
    out = capsys.readouterr().out
    assert ("BEST node 1: metric=0.45 (objective: 'filtered', an operator retarget — not the task's "
            "own metric)") in out
    assert "headroom: +0.1 over the baseline 0.4 (paper); 25.0% of the gap" in out, out
    _print_result(fold(_rows(started=_REFERENCE)))
    plain = capsys.readouterr().out
    assert "BEST node 2: metric=0.7 params=" in plain and "objective:" not in plain
    assert ("Objective: the declared extra metric 'filtered', which an operator retarget made the "
            "run's objective") in _report_context(retargeted)
    assert "Objective:" not in _report_context(fold(_rows()))


def test_every_foreign_run_reader_says_which_metric(tmp_path):
    from looplab.tools.machine_runs_tools import MachineRunsTools
    from looplab.tools.run_tools import AllRunsTools, SiblingRunTools, retarget_note

    _server_run(tmp_path, _retarget())
    _server_run(tmp_path, name="plain")
    note = retarget_note("filtered")
    assert note == " · RANKED BY 'filtered' (an operator retarget), not the task's own metric"
    assert retarget_note(None) == retarget_note("") == retarget_note(7) == ""

    def line(listing, rid):
        return next(row for row in listing.splitlines() if row.startswith(rid))

    siblings = SiblingRunTools(tmp_path, "self")
    siblings.task_id = "t"
    listing = siblings.execute("list_sibling_runs", {})
    assert note in line(listing, "demo") and note not in line(listing, "plain")
    listing = AllRunsTools(tmp_path, "self").execute("list_all_runs", {})
    assert note in line(listing, "demo") and note not in line(listing, "plain")
    machine = MachineRunsTools(tmp_path)
    listing = machine.execute("list_runs", {})
    assert note in line(listing, "demo") and note not in line(listing, "plain")
    assert {r["run_id"]: r["objective_key"] for r in machine.summaries()} == {
        "demo": "filtered", "plain": None}
    assert note in machine.execute("read_run", {"run_id": "demo"})
    assert note not in machine.execute("read_run", {"run_id": "plain"})
    # A per-node read says it at the head, beside the source and contract receipts.
    head = "(run demo RANKS BY 'filtered', an operator retarget"
    assert head in siblings.execute("read_sibling_experiment", {"run_id": "demo", "node_id": 1})
    assert "RANKS BY" not in siblings.execute("read_sibling_experiment",
                                              {"run_id": "plain", "node_id": 1})
    assert head in machine.execute("read_run_experiment", {"run_id": "demo", "node_id": 1})


def test_an_imported_node_carries_its_task_metric_and_its_sources_objective(tmp_path):
    from types import SimpleNamespace

    from looplab.engine.seed_from_run import seed_summary
    from looplab.events.node_import import node_import_payload

    origin = node_import_payload(fold(_rows(_retarget())), 1, "src")["origin"]
    assert origin["metric"] == 0.5 and origin["source_objective"] == "filtered"
    plain = node_import_payload(fold(_rows()), 1, "src")["origin"]
    assert plain["metric"] == 0.5 and "source_objective" not in plain
    seed = SimpleNamespace(run_dir=tmp_path / "src", node_id=1, payload={"origin": origin})
    line = seed_summary(seed, "same", "")
    assert "(its metric there: 0.5) — that run ranks by 'filtered' (an operator retarget)" in line
    assert "ranks by" not in seed_summary(
        SimpleNamespace(run_dir=tmp_path / "src", node_id=1, payload={"origin": plain}), "same", "")


def test_the_portfolio_facts_are_on_the_task_scale_and_name_the_objective():
    from looplab.engine.cross_run_index import run_facts

    facts = run_facts(fold(_rows(_retarget())), metric="recall")
    assert facts["objective_key"] == "filtered"
    assert facts["best"] == {"node_id": 1, "metric": 0.5}, "its champion, on the task's scale"
    assert {a["node_id"]: a["metric"] for a in facts["attempts"]} == {
        0: 0.6, 1: 0.5, 2: 0.7, 3: 0.4}
    plain = run_facts(fold(_rows()), metric="recall")
    assert "objective_key" not in plain and plain["best"] == {"node_id": 2, "metric": 0.7}
    # The passport is the TASK's: a goal a retarget restated does not re-key it (critic 2026-09-27).
    restated = run_facts(fold(_rows(_retarget(goal="maximize filtered recall"))), metric="recall")
    assert restated["scope"] == plain["scope"], (restated["scope"], plain["scope"])


def test_an_mlflow_export_tags_the_objective(monkeypatch):
    import sys
    import types

    import looplab.events.mlflow_export as mod

    def export(state):
        tags, metrics = {}, {}
        fake = types.ModuleType("mlflow")
        fake.set_tracking_uri = fake.set_experiment = lambda *a, **k: None
        fake.set_tags = lambda d: tags.update(d)
        fake.set_tag = lambda k, v: tags.__setitem__(k, v)
        fake.log_param = fake.log_text = lambda *a, **k: None
        fake.log_metric = lambda k, v: metrics.__setitem__(str(k), v)

        class _Run:
            class info:
                run_id = "fake-1"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        fake.start_run = lambda *a, **k: _Run()
        monkeypatch.setitem(sys.modules, "mlflow", fake)
        mod.export_run(state, experiment="x")
        return tags, metrics

    tags, metrics = export(fold(_rows(_retarget())))
    assert tags["looplab.objective_key"] == "filtered" and metrics["best_metric"] == 0.45
    tags, _metrics = export(fold(_rows()))
    assert "looplab.objective_key" not in tags


def test_the_cross_run_index_line_names_the_objective(tmp_path):
    from typer.testing import CliRunner

    from looplab.cli import app

    _server_run(tmp_path, _retarget())
    _server_run(tmp_path, name="plain")
    result = CliRunner().invoke(app, ["cross-run-index", str(tmp_path)])
    assert result.exit_code == 0, result.output
    rows = sorted(line.strip() for line in result.output.splitlines()[1:] if line.strip())
    assert [row.split("best=")[1] for row in rows] == [
        "0.5  (champion ranked by 'filtered', an operator retarget)", "0.7"], rows


def test_the_paid_scope_report_says_which_metric_a_retargeted_best_is():
    """MEDIUM (critic 2026-09-27, driven): the cross-run scope report handed the model a retargeted
    run's best — the declared extra metric's value — as `best=0.45 (max)` beside other runs' task
    metrics. The brief names the objective (only when one is in force), every line the model reads
    carries the clause, and a contracted receipt is withheld: every phase of that champion is the
    objective's number, not the one the contract declares."""
    from types import SimpleNamespace

    from looplab.serve import scope_report
    from looplab.serve.scope_generate import run_brief

    contract = {"schema": 1, "dataset_lineage": "dataset:v1",
                "split_or_candidate_pool_lineage": "validation", "evaluator_uid": "eval",
                "evaluator_version": "1", "population": "all", "filter": "none",
                "metric_uid": "recall", "unit": "points", "direction": "max",
                "aggregation": "mean", "cutoff": "none", "measurement_phase": "search",
                "uncertainty_protocol": "none", "constraints_digest": "none"}
    assert scope_report.canonical_comparison_contract(contract) is not None
    srv = SimpleNamespace(phase=lambda st, finalize_incomplete=False: "finished")

    def brief(events, task_doc=None):
        return run_brief(srv, "r", {}, SimpleNamespace(
            events=tuple(events), config_doc={"llm_model": "m", "policy": "greedy"},
            task_doc=task_doc))

    retargeted, plain = brief(_rows(_retarget())), brief(_rows())
    assert retargeted["objective_key"] == "filtered" and retargeted["best_metric"] == 0.45
    assert "objective_key" not in plain, "every other run's brief is byte-identical"
    clause = "RANKED BY 'filtered' (an operator retarget), not the task's own metric"
    assert clause in scope_report.run_brief_line(retargeted)
    assert "RANKED BY" not in scope_report.run_brief_line(plain)
    tools = scope_report._CrossRunTools([retargeted])
    assert clause in tools.execute("list_runs", {})
    assert clause in tools.execute("read_run", {"run_id": "r"})
    assert clause in scope_report._deterministic("s", [retargeted])["learnings"][0]
    _groups, observations = scope_report._comparison_projection(
        [scope_report._safe_brief(retargeted)])
    assert observations[0]["objective_key"] == "filtered", observations

    contracted = brief(_rows(_retarget()), task_doc={"comparison_contract": contract})
    assert contracted["comparison_measurement"] is None and contracted["best_metric"] is None
    live = brief(_rows(), task_doc={"comparison_contract": contract})
    assert live["comparison_measurement"]["value"] == 0.7, "the plain run's receipt is untouched"
    assert scope_report._safe_brief(live)["comparison_measurement"] is not None
    # …and a brief that names an objective is refused a receipt at the projection too.
    forged = scope_report._safe_brief({**live, "objective_key": "filtered"})
    assert forged["comparison_measurement"] is None and forged["objective_key"] == "filtered"


def test_the_run_artifacts_say_which_metric_the_champion_is(tmp_path):
    """LOW (critic 2026-09-27, driven): tree.html, the exported notebook, the reviewer bundle and
    the assistant's run context printed a retargeted champion's metric bare — the declared extra
    metric's value, read as the task's. Each names the objective under a retarget, and a run without
    one renders exactly as it did."""
    import json

    from typer.testing import CliRunner

    from looplab.cli import app
    from looplab.engine.bundle import bundle_summary
    from looplab.events.htmlview import render_html
    from looplab.serve.llm_context import _node_context

    retargeted, plain = fold(_rows(_retarget())), fold(_rows())
    clause = "RANKED BY &#x27;filtered&#x27; (an operator retarget)"
    html_r, html_p = render_html(retargeted), render_html(plain)
    assert clause in html_r and "<th>metric (filtered)</th>" in html_r, html_r
    assert "RANKED BY" not in html_p and "<th>metric</th>" in html_p

    rd = _server_run(tmp_path, _retarget(), name="nb")
    out = tmp_path / "nb.ipynb"
    result = CliRunner().invoke(app, ["export-notebook", str(rd), "--out", str(out)])
    assert result.exit_code == 0, result.output
    header = "".join(json.loads(out.read_text())["cells"][0]["source"])
    assert ("**Best metric:** 0.45 · RANKED BY 'filtered' (an operator retarget), not the "
            "task's own metric") in header, header

    assert bundle_summary(tmp_path, retargeted, [])["objective_key"] == "filtered"
    assert "objective_key" not in bundle_summary(tmp_path, plain, []), "a plain run's summary as it was"

    context = _node_context(retargeted, 1)
    assert "Ranked by: 'filtered' — an operator `metric_retarget`" in context, context
    assert "Ranked by" not in _node_context(plain, 1)


def test_the_agent_facing_memory_readers_keep_the_clause_a_cut_took(tmp_path):
    """LOW (critic 2026-09-27, driven): the prior re-attached the retarget clause a long statement's
    cut took, but the agent's own pulls — `recall_notes`, `search_lessons` and the cross-run claim
    stream — cut the same text and dropped it, so a lesson learned while ranking by `filtered` read
    as one about the task's own metric. Each keeps it now, redacted like the text it rides on, and
    a row without a retarget renders exactly as it did."""
    import json

    from looplab.tools.cross_run_tools import CrossRunTools
    from looplab.tools.memory_tools import MemoryTools
    from looplab.trust.cross_run import keep_retarget_clause, retargeted_lesson_note

    clause = retargeted_lesson_note("filtered")
    mem = tmp_path / "mem"
    mem.mkdir()
    head = {"task_id": "t", "run_id": "earlier", "direction": "max", "fingerprint": ["t"]}
    note = "LONGNOTE " + _long("the margin won because the negatives were hard", 12) + clause
    stmt = "LONGLESSON " + _long("a wider margin separates hard negatives", 12) + clause
    (mem / "meta_notes.jsonl").write_text(json.dumps(
        {**head, "note": note, "objective_key": "filtered"}) + "\n")
    (mem / "lessons.jsonl").write_text(json.dumps(
        {**head, "statement": stmt, "outcome": "supported", "confidence": 0.7,
         "role": "researcher", "objective_key": "filtered"}) + "\n" + json.dumps(
        {**head, "run_id": "plain", "statement": "PLAINLESSON " + _long("margin", 90),
         "outcome": "supported", "confidence": 0.7, "role": "researcher"}) + "\n")
    tools = MemoryTools(str(mem))
    tools.bind_state(fold(_rows(started={"run_id": "later", "run_uid": "later-uid"})))
    notes = tools.execute("recall_notes", {"query": "margin"})
    assert clause.strip() in notes, notes
    lessons = tools.execute("search_lessons", {"query": "margin"})
    [retargeted] = [line for line in lessons.splitlines() if "LONGLESSON" in line]
    [plain] = [line for line in lessons.splitlines() if "PLAINLESSON" in line]
    assert clause.strip() in retargeted and "ranked by" not in plain, lessons

    claims = CrossRunTools(str(mem))
    claims.bind_state(fold(_rows(started={"run_id": "later", "run_uid": "later-uid"})))
    rendered = claims.execute("cross_run_claims", {"query": "margin"})
    assert "LONGLESSON" in rendered and clause.strip() in rendered, rendered

    # The re-attached clause is REDACTED like the text it rides on — a credential-shaped key is
    # shared free text too — through both the row-stamped and the text-only path.
    from looplab.trust.cross_run import with_retarget_clause

    secret = "sk-proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"
    assert secret not in with_retarget_clause("cut", {"objective_key": secret})
    assert "[ranked by" in with_retarget_clause("cut", {"objective_key": secret})
    assert secret not in keep_retarget_clause("full" + retargeted_lesson_note(secret), "fu")


def test_a_meta_note_that_already_says_the_clause_says_it_once(tmp_path):
    """The reflection note's clause is added only when the note does not already carry it: a model
    that echoed the retarget clause back (it reads the run's lessons, which carry it) would
    otherwise store it twice — and the prior renderer would then keep both (critic 2026-09-27,
    mutant G)."""
    import json

    from looplab.engine.lessons_priors import retargeted_lesson_note

    clause = retargeted_lesson_note("filtered")
    mem = tmp_path / "mem"
    eng = _toy_engine(tmp_path, *_rows(_retarget(), ("run_finished", {
        "reason": "done", "finalization_required": True})),
        memory_dir=str(mem), reflection_priors=True)
    eng._causal_meta_note = lambda *_args: "the margin won" + clause
    eng._reflect_lessons = lambda *_args: []
    eng._comparative_lessons_on = False
    eng._write_reflection_note(fold(eng.store.read_all()))
    [note] = [json.loads(line) for line in (mem / "meta_notes.jsonl").read_text().splitlines()
              if line.strip()]
    assert note["note"] == "the margin won" + clause, note["note"]


def test_the_structured_projections_name_the_objective(tmp_path):
    """LOW (critic 2026-09-27, read): three projections carried a retargeted run's metric with no
    key — the readmodel's `nodes.metric`, `/prov`'s `ll:metric` and export-git's `Looplab-Metric`.
    Each names the objective beside it now (the readmodel in a table of its own, so `nodes` keeps
    its columns), and a run with no retarget projects exactly as it did."""
    import sqlite3

    from looplab.events.git_export import fast_import_stream
    from looplab.events.readmodel import build_readmodel

    for events, key in ((_rows(_retarget()), "filtered"), (_rows(), None)):
        db = tmp_path / f"rm-{key}.sqlite"
        build_readmodel(events, db)
        con = sqlite3.connect(str(db))
        try:
            assert con.execute("SELECT key FROM objective").fetchall() == [(key,)]
            assert [c[1] for c in con.execute("PRAGMA table_info(nodes)")][:4] == [
                "id", "parent_ids", "operator", "metric"]
        finally:
            con.close()
        stream = fast_import_stream(events, fold(events)).stream.decode()
        if key:
            assert "Looplab-Objective: filtered" in stream
            assert "Looplab-Task-Metric: 0.5" in stream, "node 1's task metric beside its 0.45"
        else:
            assert "Looplab-Objective" not in stream and "Looplab-Task-Metric" not in stream

    _server_run(tmp_path / "srv", _retarget())
    _server_run(tmp_path / "srv", name="plain")
    client = _client_over(tmp_path / "srv")
    doc = client.get("/api/runs/demo/prov").json()
    entity = next(v for v in doc["entity"].values() if v.get("ll:node_id") == 1)
    assert entity["ll:objective_key"] == "filtered" and entity["ll:task_metric"] == 0.5, entity
    assert entity["ll:metric"] == 0.45
    plain = client.get("/api/runs/plain/prov").json()
    assert not any("ll:objective_key" in v for v in plain["entity"].values())


def test_a_paraphrase_merge_never_folds_two_objectives_together(monkeypatch):
    """NIT (critic 2026-09-27, read): the paraphrase merge buckets by (task, role), and the newest
    member's fields win — so a model that called a retargeted run's lesson a paraphrase of a plain
    one dropped the `objective_key` (or stamped it on the other's evidence) and summed the counts
    across two rulers. A retargeted lesson is bucketed with its objective; a plain one is not."""
    import looplab.search.hybrid_merge as hm
    from looplab.engine.memory import _agentic_merge_lessons
    from looplab.engine.lessons_priors import retargeted_lesson_note

    seen = []

    def consolidate(texts, client, **kw):
        seen.append(list(texts))
        return [{"members": list(range(len(texts))), "merged": "raise the margin"}]

    monkeypatch.setattr(hm, "consolidate", consolidate)
    rows = [
        {"statement": "raise the margin", "outcome": "supported", "task_id": "t", "run_id": "P"},
        {"statement": "increase the margin" + retargeted_lesson_note("filtered"),
         "outcome": "supported", "task_id": "t", "run_id": "R", "objective_key": "filtered"},
        {"statement": "bump the margin", "outcome": "supported", "task_id": "t", "run_id": "Q"},
    ]
    out = _agentic_merge_lessons(rows, client=object())
    assert sorted(map(len, seen)) == [2], "only the two task-metric lessons were ever a candidate"
    retargeted = [row for row in out if row.get("objective_key") == "filtered"]
    assert len(out) == 2 and len(retargeted) == 1, out
    assert retargeted[0]["statement"].startswith("increase the margin")


def test_a_row_spelling_the_runs_direction_in_capitals_is_applied():
    """NIT (critic 2026-09-27): the server appends the direction in the fold's spelling, and a row
    spelled "MAX" was read as a flip and ignored. No LoopLab build ever appended one (second pass: the
    first build refused it with a 409, the second lowercases it) — a hand-written or foreign row can.
    The fold reads it the way the server writes it; a real flip is still ignored whole."""
    assert fold(_rows(_retarget(direction=" MAX "))).objective_key == "filtered"
    assert fold(_rows(_retarget(direction="min"))).objective_key is None
