"""`read_run` bounds the run's goal and names the call that prints it whole.

2026-10-06, the operator's chat about node #37: a goal of several thousand characters filled the
window the head shares with the experiment listing, so each read returned the goal and a cut-off
list, and the model spent its turn re-reading ("the run-level listing is truncated by the goal").
"""
from __future__ import annotations

from looplab.events.eventstore import EventStore
from looplab.tools.machine_runs_tools import READ_RUN_GOAL_CHARS, MachineRunsTools


def _run(root, goal):
    rd = root / "demo"
    rd.mkdir(parents=True)
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "demo", "task_id": "t", "goal": goal, "direction": "max"})
    for nid in range(3):
        store.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft", "params": {}, "rationale": "r"},
                                      "code": f"print({nid})"})
        store.append("node_evaluated", {"node_id": nid, "generation": 0, "metric": 0.1 * nid,
                                        "violations": []})


def test_a_long_goal_is_bounded_and_the_listing_survives(tmp_path):
    goal = "maximize InCart_FilteredUnseenRecall@20 " + "constraint " * 2000
    _run(tmp_path, goal)
    tools = MachineRunsTools(tmp_path)
    out = tools.execute("read_run", {"run_id": "demo"})
    assert f"first {READ_RUN_GOAL_CHARS} of {len(goal.strip())} chars" in out or \
        f"first {READ_RUN_GOAL_CHARS} of {len(goal)} chars" in out
    assert "full_goal=true" in out, "the bound names the call that continues past it"
    assert "#2" in out, "the experiment listing is no longer crowded out"
    whole = tools.execute("read_run", {"run_id": "demo", "full_goal": True})
    assert whole.count("constraint") == 2000


def test_a_short_goal_prints_as_before(tmp_path):
    _run(tmp_path, "minimize (x-3)^2")
    out = MachineRunsTools(tmp_path).execute("read_run", {"run_id": "demo"})
    assert "goal: minimize (x-3)^2 · direction=max" in out and "chars;" not in out
