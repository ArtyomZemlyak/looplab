"""`looplab fidelity-agreement`: does the cheap evaluation level rank like the full one? (doc 68 68.5)

Driven over logs the REAL fold builds from REAL events: the search's number on each node with the
ruler its terminal recorded, the confirm phase's mean beside it (`node_confirmed`) with the ruler its
seeds ran on, and the instrument's reading of the two. The confirm side's recording is driven on a
real engine run at the end.

The level is decided by the RECORDED ruler, never by `idea.eval_profile` (critic 2026-09-26,
driven): the label-based first cut reported seed noise as a cheap/full disagreement whenever the
Strategist's fidelity, not the node, chose the profile — and on every task with no profiles.
"""
from __future__ import annotations

import json

import anyio
import pytest
from typer.testing import CliRunner

from looplab.cli import app
from looplab.core.models import Idea, durable_idea_payload
from looplab.events.eventstore import EventStore
from looplab.events.fidelity_agreement import CAVEAT, fidelity_rank_agreement, spearman_rho
from looplab.events.replay import fold

# Two protocol `profile` facets, as `engine/comparability.py::protocol_record` digests them.
SMOKE, FULL, OTHER = "a" * 16, "b" * 16, "c" * 16


def _provenance(ruler: str) -> dict:
    return {"comparability": {"version": 1, "authority": "declared", "keys": {"declared": "d" * 16},
                              "protocol": {"profile": ruler}}}


def _run(rd, nodes, *, direction="max"):
    """`nodes` is [(id, cheap metric, full mean or None, search ruler, confirm ruler, label)]; a
    ruler of None writes no record on that side, `label` is the node's `eval_profile`."""
    rd.mkdir(parents=True)
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": rd.name, "task_id": "t", "goal": "g",
                                 "direction": direction})
    for node_id, cheap, full, search_ruler, confirm_ruler, label in nodes:
        idea = Idea(operator="draft", params={"x": float(node_id)}, rationale=f"n{node_id}",
                    eval_profile=label)
        store.append("node_created", {"node_id": node_id, "parent_ids": [], "operator": "draft",
                                      "idea": durable_idea_payload(idea), "code": "pass\n"})
        store.append("node_evaluated", {
            "node_id": node_id, "generation": 0, "metric": cheap, "violations": [],
            **({"metric_provenance": _provenance(search_ruler)} if search_ruler else {})})
        if full is not None:
            store.append("node_confirmed", {
                "node_id": node_id, "generation": 0, "mean": full, "std": 0.01, "seeds": 3,
                **({"protocol_profile": confirm_ruler} if confirm_ruler else {})})
    return store


def _both(node_id, cheap, full, label=None):
    """One node measured on the cheap ruler by the search and on the full one by confirm."""
    return (node_id, cheap, full, SMOKE, FULL, label)


def test_the_orderings_are_compared_pair_by_pair_and_ties_are_set_apart(tmp_path):
    store = _run(tmp_path / "r", [_both(0, 0.50, 0.60), _both(1, 0.70, 0.65),
                                  _both(2, 0.80, 0.55), _both(3, 0.90, 0.60),
                                  (4, 0.95, None, SMOKE, None, None)])
    reading = fidelity_rank_agreement(fold(store.read_all()))
    assert [row["node"] for row in reading["nodes"]] == [0, 1, 2, 3], "node 4 has no full level"
    # Pairs (cheap order vs full order): 0<1 & 0.60<0.65 agree; 0<2 & 0.60>0.55 disagree;
    # 0<3 & 0.60=0.60 tie; 1<2 & 0.65>0.55 disagree; 1<3 & 0.65>0.60 disagree; 2<3 & 0.55<0.60 agree.
    assert (reading["pairs"], reading["concordant"], reading["discordant"], reading["ties"]) == (
        5, 2, 3, 1)
    assert reading["agreement"] == pytest.approx(0.4)
    assert reading["spearman"] == pytest.approx(spearman_rho([0.5, 0.7, 0.8, 0.9],
                                                             [0.6, 0.65, 0.55, 0.6]))


def test_the_level_is_the_ruler_that_ran_never_the_label(tmp_path):
    """The critic's case: every node left `eval_profile` null, the Strategist ran them at `full`,
    and confirm at `full` — one ruler at both, so seed noise. And a node LABELLED `full` whose search
    number was recorded on the cheap ruler is a real cheap/full pair."""
    noise = _run(tmp_path / "noise", [(i, 0.5 + i / 10, 0.9 - i / 10, FULL, FULL, None)
                                      for i in range(4)])
    reading = fidelity_rank_agreement(fold(noise.read_all()))
    assert reading["same_level"] == 4 and reading["nodes"] == [] and reading["pairs"] == 0
    assert reading["agreement"] is None and reading["spearman"] is None
    labelled = _run(tmp_path / "labelled", [_both(0, 0.5, 0.6, "full"), _both(1, 0.7, 0.8, "full")])
    reading = fidelity_rank_agreement(fold(labelled.read_all()))
    assert reading["same_level"] == 0 and reading["pairs"] == 1 and reading["agreement"] == 1.0
    assert [row["profile"] for row in reading["nodes"]] == ["full", "full"]


@pytest.mark.parametrize("search_ruler,confirm_ruler", [(None, FULL), (SMOKE, None), (None, None)])
def test_a_side_with_no_recorded_ruler_is_unknown(tmp_path, search_ruler, confirm_ruler):
    store = _run(tmp_path / "r", [(0, 0.5, 0.6, search_ruler, confirm_ruler, None),
                                  _both(1, 0.7, 0.8), _both(2, 0.9, 0.7)])
    reading = fidelity_rank_agreement(fold(store.read_all()))
    assert reading["unknown"] == 1 and [row["node"] for row in reading["nodes"]] == [1, 2]
    assert reading["pairs"] == 1 and reading["discordant"] == 1


def test_numbers_on_two_cheap_rulers_are_never_one_ordering(tmp_path):
    """Nodes 0,1 were searched on one cheap ruler and 2,3 on another: each pair lies inside one
    (search ruler, confirm ruler) group, and one rho over both groups would be none."""
    store = _run(tmp_path / "r", [_both(0, 0.5, 0.6), _both(1, 0.7, 0.8),
                                  (2, 0.1, 0.9, OTHER, FULL, None), (3, 0.2, 0.95, OTHER, FULL, None)])
    reading = fidelity_rank_agreement(fold(store.read_all()))
    assert reading["groups"] == 2 and reading["pairs"] == 2 and reading["concordant"] == 2
    assert reading["spearman"] is None


def test_only_nodes_whose_number_counts_are_read(tmp_path):
    store = _run(tmp_path / "r", [_both(0, 0.5, 0.6), _both(1, 0.7, 0.8), _both(2, 0.9, 0.1)])
    before = fidelity_rank_agreement(fold(store.read_all()))
    assert [r["node"] for r in before["nodes"]] == [0, 1, 2] and before["discordant"] == 2
    store.append("node_tombstoned", {"node_ids": [2]})
    state = fold(store.read_all())
    assert state.nodes[2].tombstoned
    after = fidelity_rank_agreement(state)
    assert [r["node"] for r in after["nodes"]] == [0, 1] and after["agreement"] == 1.0


@pytest.mark.parametrize("exclusion", ["aborted", "flagged", "infeasible"])
def test_an_excluded_node_is_not_read(tmp_path, exclusion):
    """`core/fitness.py::counts_toward_best`, the one rule: an operator abort, a trust flag and an
    infeasible result each keep a node's number out (critic 2026-09-26: removing any one check from
    the first cut's re-spelling of that rule survived its tests)."""
    state = fold(_run(tmp_path / "r", [_both(0, 0.5, 0.6), _both(1, 0.7, 0.8),
                                       _both(2, 0.9, 0.1)]).read_all())
    if exclusion == "aborted":
        state.aborted_nodes = {2}
    elif exclusion == "flagged":
        state.breed_excluded = {2}
    else:
        state.nodes[2].feasible = False
    reading = fidelity_rank_agreement(state)
    assert [r["node"] for r in reading["nodes"]] == [0, 1] and reading["agreement"] == 1.0


@pytest.mark.parametrize("left,right,expected", [
    ([1, 2, 3], [10, 20, 30], 1.0), ([1, 2, 3], [30, 20, 10], -1.0),
    ([1, 2], [1, 2], None), ([1, 1, 1], [1, 2, 3], None),
    ([1, 2, 2, 3], [1, 2, 3, 4], pytest.approx(0.9486832980505138)),
])
def test_spearman_over_average_ranks(left, right, expected):
    assert spearman_rho(left, right) == expected


def test_the_command_pools_pairs_across_runs_and_says_when_there_are_none(tmp_path):
    root = tmp_path / "runs"
    _run(root / "a", [_both(0, 0.5, 0.6), _both(1, 0.7, 0.8)])
    _run(root / "b", [_both(0, 0.5, 0.9), _both(1, 0.7, 0.1), _both(2, 0.9, 0.95)])
    out = CliRunner().invoke(app, ["fidelity-agreement", str(root)])
    assert out.exit_code == 0, out.output
    assert "2 run(s); 2 with an ordered cheap/full pair; 5 node(s) carry both levels" in out.output
    # a: 1 agreeing pair; b: (0,1) disagree, (0,2) agree, (1,2) agree -> 3 of 4 pooled.
    assert "pooled: 3 of 4 ordered pair(s) agree (75.0 %)" in out.output
    assert CAVEAT in out.output
    report = json.loads(CliRunner().invoke(app, ["fidelity-agreement", str(root), "--json"]).output)
    assert report["pairs"] == 4 and report["concordant"] == 3 and report["caveat"] == CAVEAT
    assert [row["run"] for row in report["per_run"]] == ["a", "b"]
    lonely = tmp_path / "lonely"
    _run(lonely / "c", [(0, 0.5, None, SMOKE, None, None)])
    out = CliRunner().invoke(app, ["fidelity-agreement", str(lonely)])
    assert out.exit_code == 0 and "NO ORDERED PAIR" in out.output
    out = CliRunner().invoke(app, ["fidelity-agreement", str(tmp_path / "nothing")])
    assert out.exit_code == 2 and "no runs found" in out.output


def test_the_limit_counts_only_runs_with_something_to_show(tmp_path):
    """Two node-less runs sort ahead of a confirmed one (fewer pairs, earlier names): the limit is
    applied AFTER they are skipped, and what it hides is said (critic 2026-09-26, driven)."""
    root = tmp_path / "runs"
    _run(root / "a_empty", [(0, 0.5, None, SMOKE, None, None)])
    _run(root / "b_empty", [(0, 0.5, None, SMOKE, None, None)])
    _run(root / "c_one", [_both(0, 0.5, 0.6)])
    _run(root / "d_two", [_both(0, 0.5, 0.6), _both(1, 0.7, 0.8)])
    out = CliRunner().invoke(app, ["fidelity-agreement", str(root), "--limit", "1"])
    assert out.exit_code == 0, out.output
    assert "d_two" in out.output and "c_one" not in out.output
    assert "+1 more run(s) with both levels" in out.output
    out = CliRunner().invoke(app, ["fidelity-agreement", str(root), "--limit", "3"])
    assert "c_one" in out.output and "more run(s)" not in out.output


def test_a_log_damaged_part_way_is_said_to_be(tmp_path):
    root = tmp_path / "runs"
    store = _run(root / "a", [_both(0, 0.5, 0.6), _both(1, 0.7, 0.8)])
    with open(store.path, "ab") as handle:
        handle.write(b'{"seq": 999, "type": "node_evaluated", "data": {broken\n')
        handle.write(b'{"seq": 1000, "type": "run_finished", "ts": 1.0, "data": {}}\n')
    _run(root / "b", [_both(0, 0.5, 0.6), _both(1, 0.7, 0.8)])
    out = CliRunner().invoke(app, ["fidelity-agreement", str(root)])
    assert out.exit_code == 0, out.output
    # The ONE wording (`eventstore.py::integrity_sentence`), and the run is left out: "we cannot
    # show you this run", never its prefix counted as the whole.
    assert "[INCOMPLETE RECORD] a" in out.output and "Skipped." in out.output
    assert "1 run(s); 1 with an ordered cheap/full pair" in out.output


def test_the_confirm_phase_records_the_ruler_its_mean_was_measured_on(tmp_path, monkeypatch):
    """Driven on a real engine run: each confirm seed records the ruler it RAN under, the digest a
    node's terminal records, and `node_confirmed` the one they agree on — or says they did not."""
    from looplab.engine.comparability import protocol_record
    from tests.factories import make_engine

    full = {"profile": "full", "overrides": ["steps=100"]}
    other = {"profile": "full", "overrides": ["steps=7"]}
    for name, protocols in (("agreed", {1: full, 2: full}), ("mixed", {1: full, 2: other})):
        engine = make_engine(tmp_path / name, n_seeds=3, max_nodes=6, confirm_top_k=2,
                             confirm_seeds=2)
        real = engine._run_eval

        def _run_eval(node, workdir, env=None, profile=None, *args, _real=real,
                      _protocols=protocols, **kwargs):
            res = _real(node, workdir, env, profile, *args, **kwargs)
            if profile == "full":
                res.eval_protocol = dict(_protocols[int((env or {})["LOOPLAB_EVAL_SEED"])])
            return res

        monkeypatch.setattr(engine, "_run_eval", _run_eval)
        anyio.run(engine.run)
        events = engine.store.read_all()
        seeds = [e.data for e in events if e.type == "confirm_eval" and e.data.get("metric")]
        assert {row["protocol_profile"] for row in seeds} == {
            protocol_record(eval_protocol=p)["profile"] for p in protocols.values()}
        confirmed = [e.data for e in events if e.type == "node_confirmed"]
        assert confirmed, name
        state = fold(events)
        for row in confirmed:
            if name == "agreed":
                expected = protocol_record(eval_protocol=full)["profile"]
                assert row["protocol_profile"] == expected and "protocol_mixed" not in row
                assert state.nodes[row["node_id"]].confirmed_ruler == expected
            else:
                assert row["protocol_mixed"] is True and "protocol_profile" not in row
                assert state.nodes[row["node_id"]].confirmed_ruler is None


def test_a_failed_confirm_seed_does_not_mix_the_ruler(tmp_path, monkeypatch):
    """The ruler is agreed over the seeds the mean COUNTS: a seed that failed measured nothing and
    recorded no ruler, and counting it would turn every node with one crashed seed `protocol_mixed`
    (critic 2026-09-26: that mutant survived every test, none of which had a failed seed)."""
    from looplab.engine.comparability import protocol_record
    from tests.factories import make_engine

    full = {"profile": "full", "overrides": ["steps=100"]}
    engine = make_engine(tmp_path / "crash", n_seeds=3, max_nodes=6, confirm_top_k=2,
                         confirm_seeds=2)
    real = engine._run_eval

    def _run_eval(node, workdir, env=None, profile=None, *args, **kwargs):
        res = real(node, workdir, env, profile, *args, **kwargs)
        if profile == "full":
            res.eval_protocol = dict(full)
            if (env or {}).get("LOOPLAB_EVAL_SEED") == "2":
                res.metric, res.exit_code = None, 1            # the second seed crashes
        return res

    monkeypatch.setattr(engine, "_run_eval", _run_eval)
    anyio.run(engine.run)
    confirmed = [e.data for e in engine.store.read_all() if e.type == "node_confirmed"]
    assert confirmed and all(row["seeds"] == 1 for row in confirmed), confirmed
    expected = protocol_record(eval_protocol=full)["profile"]
    assert all(row.get("protocol_profile") == expected and "protocol_mixed" not in row
               for row in confirmed), confirmed
