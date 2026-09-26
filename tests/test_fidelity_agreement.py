"""`looplab fidelity-agreement`: does the cheap evaluation level rank like the full one? (doc 68 68.5)

Driven over logs the REAL fold builds from REAL events: the search's number on each node, the confirm
phase's full-profile mean beside it (`node_confirmed`), and the instrument's reading of the two.
"""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from looplab.cli import app
from looplab.core.models import Idea, durable_idea_payload
from looplab.events.eventstore import EventStore
from looplab.events.fidelity_agreement import fidelity_rank_agreement, spearman_rho
from looplab.events.replay import fold


def _run(rd, nodes, *, direction="max"):
    """`nodes` is [(id, cheap metric, full mean or None, eval_profile)]."""
    rd.mkdir(parents=True)
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": rd.name, "task_id": "t", "goal": "g",
                                 "direction": direction})
    for node_id, cheap, full, profile in nodes:
        idea = Idea(operator="draft", params={"x": float(node_id)}, rationale=f"n{node_id}",
                    eval_profile=profile)
        store.append("node_created", {"node_id": node_id, "parent_ids": [], "operator": "draft",
                                      "idea": durable_idea_payload(idea), "code": "pass\n"})
        store.append("node_evaluated", {"node_id": node_id, "generation": 0, "metric": cheap,
                                        "violations": []})
        if full is not None:
            store.append("node_confirmed", {"node_id": node_id, "generation": 0, "mean": full,
                                            "std": 0.01, "seeds": 3})
    return store


def test_the_orderings_are_compared_pair_by_pair_and_ties_are_set_apart(tmp_path):
    store = _run(tmp_path / "r", [(0, 0.50, 0.60, None), (1, 0.70, 0.65, None),
                                  (2, 0.80, 0.55, "smoke"), (3, 0.90, 0.60, None),
                                  (4, 0.95, None, None)])
    reading = fidelity_rank_agreement(fold(store.read_all()))
    assert [row["node"] for row in reading["nodes"]] == [0, 1, 2, 3], "node 4 has no full level"
    # Pairs (cheap order vs full order): 0<1 & 0.60<0.65 agree; 0<2 & 0.60>0.55 disagree;
    # 0<3 & 0.60=0.60 tie; 1<2 & 0.65>0.55 disagree; 1<3 & 0.65>0.60 disagree; 2<3 & 0.55<0.60 agree.
    assert (reading["pairs"], reading["concordant"], reading["discordant"], reading["ties"]) == (
        5, 2, 3, 1)
    assert reading["agreement"] == pytest.approx(0.4)
    assert reading["spearman"] == pytest.approx(spearman_rho([0.5, 0.7, 0.8, 0.9],
                                                             [0.6, 0.65, 0.55, 0.6]))


def test_a_node_searched_at_full_measures_noise_and_is_counted_apart(tmp_path):
    store = _run(tmp_path / "r", [(0, 0.5, 0.6, "full"), (1, 0.7, 0.8, None)])
    reading = fidelity_rank_agreement(fold(store.read_all()))
    assert reading["same_level"] == 1 and [r["node"] for r in reading["nodes"]] == [1]
    assert reading["pairs"] == 0 and reading["agreement"] is None and reading["spearman"] is None


def test_only_nodes_whose_number_counts_are_read(tmp_path):
    store = _run(tmp_path / "r", [(0, 0.5, 0.6, None), (1, 0.7, 0.8, None), (2, 0.9, 0.1, None)])
    before = fidelity_rank_agreement(fold(store.read_all()))
    assert [r["node"] for r in before["nodes"]] == [0, 1, 2] and before["discordant"] == 2
    store.append("node_tombstoned", {"node_ids": [2]})
    state = fold(store.read_all())
    assert state.nodes[2].tombstoned
    after = fidelity_rank_agreement(state)
    assert [r["node"] for r in after["nodes"]] == [0, 1] and after["agreement"] == 1.0


@pytest.mark.parametrize("left,right,expected", [
    ([1, 2, 3], [10, 20, 30], 1.0), ([1, 2, 3], [30, 20, 10], -1.0),
    ([1, 2], [1, 2], None), ([1, 1, 1], [1, 2, 3], None),
    ([1, 2, 2, 3], [1, 2, 3, 4], pytest.approx(0.9486832980505138)),
])
def test_spearman_over_average_ranks(left, right, expected):
    assert spearman_rho(left, right) == expected


def test_the_command_pools_pairs_across_runs_and_says_when_there_are_none(tmp_path):
    root = tmp_path / "runs"
    _run(root / "a", [(0, 0.5, 0.6, None), (1, 0.7, 0.8, None)])
    _run(root / "b", [(0, 0.5, 0.9, None), (1, 0.7, 0.1, None), (2, 0.9, 0.95, None)])
    out = CliRunner().invoke(app, ["fidelity-agreement", str(root)])
    assert out.exit_code == 0, out.output
    assert "2 run(s); 2 with an ordered cheap/full pair; 5 node(s) carry both levels" in out.output
    # a: 1 agreeing pair; b: (0,1) disagree, (0,2) agree, (1,2) agree -> 3 of 4 pooled.
    assert "pooled: 3 of 4 ordered pair(s) agree (75.0 %)" in out.output
    report = json.loads(CliRunner().invoke(app, ["fidelity-agreement", str(root), "--json"]).output)
    assert report["pairs"] == 4 and report["concordant"] == 3
    assert [row["run"] for row in report["per_run"]] == ["a", "b"]
    lonely = tmp_path / "lonely"
    _run(lonely / "c", [(0, 0.5, None, None)])
    out = CliRunner().invoke(app, ["fidelity-agreement", str(lonely)])
    assert out.exit_code == 0 and "NO ORDERED PAIR" in out.output
    out = CliRunner().invoke(app, ["fidelity-agreement", str(tmp_path / "nothing")])
    assert out.exit_code == 2 and "no runs found" in out.output
