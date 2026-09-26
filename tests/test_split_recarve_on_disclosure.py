"""The host split is re-carved only when a disclosed holdout is consumed (doc 68 68.3c).

The search is scored on a split of the host's labels, salted so a run reopened after its holdout
was DISCLOSED scores its new candidates on rows nobody has seen. The salt was the search epoch, and
the search epoch advances on EVERY reopen of a finished run — a plain `resume`, a `node_reset` —
because confirmation and approval end with a candidate set. So each reopen re-carved the rows under
every incumbent: a node evaluated after it was ranked against leaders measured on other rows
(review 2026-09-26: 0.5111 against 0.4444, where the leader's code scored 0.4889 on the new rows).

`RunState.split_salt` is now the number of disclosures consumed, on a run whose `run_started` pinned
the rule (`split_salt: "disclosure"`, written only when a split can exist); an older log keeps the
search-epoch salt it was scored under.
"""
from __future__ import annotations

import anyio
import pytest

from looplab.engine.orchestrator import Engine
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from test_holdout import _HostGradedTask, _PredsDeveloper, _PredsResearcher


def _log(tmp_path, *, pinned: bool, disclosed: bool):
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "hg", "task_id": "t", "goal": "g", "direction": "max",
                                 "holdout_fraction": 0.25,
                                 **({"split_salt": "disclosure"} if pinned else {})})
    store.append("host_grading", {"predictions": "predictions.json", "scorer": "accuracy"})
    for nid, metric in ((0, 0.4), (1, 0.5)):
        store.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft", "params": {}, "rationale": "r"},
                                      "code": f"print({nid})"})
        store.append("node_evaluated", {"node_id": nid, "generation": 0, "metric": metric,
                                        "violations": []})
    if disclosed:
        store.append("holdout_evaluated", {"node_id": 1, "generation": 0, "metric": 0.6,
                                           "search_epoch": 0})
    store.append("run_finished", {"reason": "done"})
    store.append("resume", {})
    return fold(store.read_all())


@pytest.mark.parametrize("pinned,disclosed,salt", [
    (True, False, 0),     # a plain reopen: new search epoch, same rows
    (True, True, 1),      # a disclosure consumed: the rows are hidden again
    (False, False, 1),    # an older log: the search-epoch salt it was scored under
    (False, True, 1),
])
def test_the_split_salt_counts_disclosures_on_a_pinned_run(tmp_path, pinned, disclosed, salt):
    state = _log(tmp_path, pinned=pinned, disclosed=disclosed)
    assert state.search_epoch == 1, "every reopen of a finished run is a new search epoch"
    assert state.split_salt_disclosure is pinned and state.split_salt == salt
    assert "split_epoch" not in state.model_dump(), "re-entry authority, never a dump field"


def _engine(rd, *, max_nodes, **over):
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.search.policy import GreedyTree

    return Engine(rd, task=_HostGradedTask(), researcher=_PredsResearcher(),
                  developer=_PredsDeveloper(), sandbox=SubprocessSandbox(),
                  policy=GreedyTree(n_seeds=1, max_nodes=max_nodes), n_seeds=1,
                  max_nodes=max_nodes, timeout=30.0, holdout_fraction=0.25, **over)


def _reopened(rd, max_nodes):
    EventStore(rd / "events.jsonl").append("resume", {})
    return _engine(rd, max_nodes=max_nodes, holdout_top_k=1)


def test_a_plain_reopen_scores_the_new_candidate_on_the_incumbents_rows(tmp_path, monkeypatch):
    """Driven through two real engines: the run finishes with nothing disclosed — a finish the
    holdout phase did not reach, as a stop's is — is reopened, and the resumed engine carves the rows
    the first one scored on. (A natural finish always discloses: `holdout_top_k` is at least 1, and a
    reopen after it re-carves and re-queues every incumbent, which mixes nothing.)"""
    rd = tmp_path / "run"
    first = _engine(rd, max_nodes=1, holdout_top_k=1)
    monkeypatch.setattr(first, "_holdout_pending", lambda state: False)
    rows = set(first._holdout_idx)
    finished = anyio.run(first.run)
    assert rows and finished.finished and not finished.holdout_evaluated_ids
    started = next(e for e in first.store.read_all() if e.type == "run_started")
    assert started.data["split_salt"] == "disclosure"

    resumed = _reopened(rd, max_nodes=2)
    state = anyio.run(resumed.run)
    assert state.search_epoch >= 1 and state.split_salt == 0
    assert set(resumed._holdout_idx) == rows, "a plain reopen re-carved the incumbents' rows"
    assert len(state.evaluated_nodes()) == 2


def test_a_reopen_after_a_disclosure_hides_the_disclosed_rows_again(tmp_path):
    rd = tmp_path / "run"
    first = _engine(rd, max_nodes=1, holdout_top_k=1)
    rows = set(first._holdout_idx)
    finished = anyio.run(first.run)
    assert finished.holdout_evaluated_ids, "a natural finish discloses the holdout"

    resumed = _reopened(rd, max_nodes=2)
    state = anyio.run(resumed.run)
    assert state.split_salt >= 1
    assert set(resumed._holdout_idx) != rows, "the disclosed rows were scored on again"


def test_a_run_that_cannot_carve_a_split_pins_nothing(tmp_path):
    """The key rides `run_started` only where a split exists — so a toy run's payload, and the
    calibration receipts that pin its key set, are byte-identical."""
    from tests.factories import make_engine

    eng = make_engine(tmp_path / "toy", max_nodes=1)
    anyio.run(eng.run)
    started = next(e for e in eng.store.read_all() if e.type == "run_started")
    assert "split_salt" not in started.data
    no_fraction = _engine(tmp_path / "zero", max_nodes=1, holdout_top_k=1)
    no_fraction._holdout_fraction = 0.0
    anyio.run(no_fraction.run)
    started = next(e for e in no_fraction.store.read_all() if e.type == "run_started")
    assert "split_salt" not in started.data


def test_a_drain_across_a_plain_reopen_is_refused_only_on_an_older_log(tmp_path):
    """`resume --drain-only` refused a reset of a finished host-graded run because the reset's
    reopen re-carved the split (doc 68 68.3a/b). On a pinned run it re-carves nothing, so there is
    nothing to refuse; an older log keeps the refusal it needs."""
    from looplab.engine.run_boundary import classify_prior_run, drain_only_refusal

    for pinned in (True, False):
        store = EventStore(tmp_path / f"pinned-{pinned}" / "events.jsonl")
        (tmp_path / f"pinned-{pinned}").mkdir(exist_ok=True)
        store.append("run_started", {"run_id": "hg", "task_id": "t", "goal": "g",
                                     "direction": "max", "holdout_fraction": 0.25,
                                     **({"split_salt": "disclosure"} if pinned else {})})
        store.append("host_grading", {"predictions": "predictions.json", "scorer": "accuracy"})
        for nid, metric in ((0, 0.4), (1, 0.5)):
            store.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                          "idea": {"operator": "draft", "params": {},
                                                   "rationale": "r"}, "code": f"print({nid})"})
            store.append("node_evaluated", {"node_id": nid, "generation": 0, "metric": metric,
                                            "violations": []})
        store.append("run_finished", {"reason": "done"})
        store.append("node_reset", {"node_id": 1, "from_stage": "eval", "generation": 0})
        events = store.read_all()
        state = fold(events)
        refused = drain_only_refusal(state, classify_prior_run(state, events), events)
        if pinned:
            assert state.search_epoch == 1 and state.split_salt == 0
            assert refused is None, refused
        else:
            assert refused is not None and "re-carved (epoch 1)" in refused[1], refused
