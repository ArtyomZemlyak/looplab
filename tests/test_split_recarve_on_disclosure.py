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
    disclosed = [e.data for e in first.store.read_all() if e.type == "holdout_evaluated"]
    assert disclosed and all("partition_disclosed" not in row for row in disclosed), (
        "the partition re-score IS the disclosure; its row carries no key (doc 68 68.3d)")

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


def test_lifting_the_finish_of_a_pinned_run_that_still_owes_work_is_not_refused(tmp_path):
    """The eval budget finalized the run with a reset node pending: a drain lifts the finish. On an
    older log the lift re-carves the split, so it is refused; on a pinned run it moves no rows."""
    from looplab.engine.run_boundary import classify_prior_run, drain_only_refusal

    for pinned in (True, False):
        root = tmp_path / f"owed-{pinned}"
        root.mkdir()
        store = EventStore(root / "events.jsonl")
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
        store.append("node_reset", {"node_id": 1, "from_stage": "eval", "generation": 0})
        store.append("run_finished", {"reason": "eval_budget"})
        events = store.read_all()
        state = fold(events)
        kind = classify_prior_run(state, events)
        assert kind == "finished" and state.nodes[1].status.value == "pending", kind
        refused = drain_only_refusal(state, kind, events)
        if pinned:
            assert refused is None, refused
        else:
            assert refused is not None and "re-carves the split" in refused[1], refused


_INJECT_CODE = ("import json\n"
                "preds = [i % 2 for i in range(40)]\n"
                "json.dump(preds, open('predictions.json', 'w'))\n"
                "print(json.dumps({'metric': 0.0}))\n")


def test_the_live_rebuild_carves_the_split_salt_once_the_epochs_part(tmp_path, monkeypatch):
    """The loop head re-carves the rows LIVE when a rotation lands in the same process (critic
    2026-09-26, third pass, driven with real engines): a stop without a disclosure, a plain reopen
    (search epoch 1, split salt 0), a disclosure at the resumed engine's finish, then an operator
    inject landing right after it — a rotation that moves the search epoch to 2 and the split salt
    to 1. The rows the engine scores on from then on are the SPLIT salt's; its search-epoch mutant
    survived every other test in this file."""
    rd = tmp_path / "run"
    first = _engine(rd, max_nodes=1, holdout_top_k=1)
    monkeypatch.setattr(first, "_holdout_pending", lambda state: False)
    assert anyio.run(first.run).finished
    EventStore(rd / "events.jsonl").append("resume", {})
    second = _engine(rd, max_nodes=3, holdout_top_k=1)
    real_phase = second._holdout_phase
    injected = []

    async def phase_then_inject(state):
        await real_phase(state)
        if not injected:
            injected.append(True)
            second.store.append("inject_node", {
                "idea": {"operator": "manual", "params": {"x": 0.5}, "rationale": "late hunch"},
                "parent_id": None, "code": _INJECT_CODE})
            second.store.append("budget_extend", {"add_nodes": 1})

    monkeypatch.setattr(second, "_holdout_phase", phase_then_inject)

    async def bounded():
        with anyio.fail_after(120):
            return await second.run()

    final = anyio.run(bounded)
    assert injected and final.search_epoch == 2 and final.split_salt == 1, (
        final.search_epoch, final.split_salt)
    assert set(second._holdout_idx) == set(second._build_holdout_idx(0.25, final.split_salt))
    assert set(second._holdout_idx) != set(second._build_holdout_idx(0.25, final.search_epoch))


# --------------------------------------------------------------------- doc 68 68.3d
# A holdout that scored NONE of the engine's hidden partition — the MLE-bench private grade (the
# competition's TEST answers) and the operator's withheld scorer (a split the engine does not hold) —
# burns nothing: a reopen after it re-carves no split and re-measures no incumbent. Such a row says
# so (`partition_disclosed: false`); a row without the key folds as it always did.

_PRIVATE = {"protocol": "private_grade", "partition_disclosed": False}
_SCORER = {"protocol": "holdout_scorer", "partition_disclosed": False}


def _disclosed(tmp_path, row, *, pinned=True, host=True, then=("run_finished", "resume"),
               before=(), fraction=0.25):
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "hg", "task_id": "t", "goal": "g", "direction": "max",
                                 "holdout_fraction": fraction,
                                 **({"split_salt": "disclosure"} if pinned else {})})
    if host:
        store.append("host_grading", {"predictions": "predictions.json", "scorer": "accuracy"})
    for nid, metric in ((0, 0.4), (1, 0.5)):
        store.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft", "params": {}, "rationale": "r"},
                                      "code": f"print({nid})"})
        store.append("node_evaluated", {"node_id": nid, "generation": 0, "metric": metric,
                                        "violations": []})
    for kind, data in before:
        store.append(kind, data)
    store.append("holdout_evaluated", {"node_id": 1, "generation": 0, "metric": 0.6, "gap": -0.1,
                                       "n_holdout": 3, "search_epoch": 0, **row})
    for kind in then:
        store.append(kind, {"reason": "done"} if kind == "run_finished" else {})
    return store


def _requeued(state):
    return {nid: (n.status.value, n.attempt) for nid, n in state.nodes.items()}


_KEPT = {0: ("evaluated", 0), 1: ("evaluated", 0)}
_REQUEUED = {0: ("pending", 1), 1: ("pending", 1)}


@pytest.mark.parametrize("why,row,pinned,host,fraction,salt,nodes", [
    ("the partition re-score burned the split (68.3c)", {}, True, True, 0.25, 1, _REQUEUED),
    ("the private grade burned nothing", _PRIVATE, True, True, 0.25, 0, _KEPT),
    ("an older private-grade row folds as it always did", {"protocol": "private_grade"},
     True, True, 0.25, 1, _REQUEUED),
    ("a search-epoch salt moves the rows anyway (a log older than 68.3c)", _PRIVATE,
     False, True, 0.25, 1, _REQUEUED),
    ("a host grade with no split: no rows move", _PRIVATE, False, True, 0.0, 1, _KEPT),
    ("a withheld scorer on a run with no host split", _SCORER, False, False, 0.25, 1, _KEPT),
    ("an older withheld-scorer row re-measured everything, as it always did",
     {"protocol": "holdout_scorer"}, False, False, 0.25, 1, _REQUEUED),
])
def test_a_reopen_re_measures_only_what_a_disclosure_burned(tmp_path, why, row, pinned, host,
                                                             fraction, salt, nodes):
    state = fold(_disclosed(tmp_path, row, pinned=pinned, host=host,
                            fraction=fraction).read_all())
    assert state.search_epoch == 1 and state.holdout_evaluated_ids == [], why
    assert state.nodes[1].holdout_metric is None, "the disclosed number belongs to its epoch"
    assert state.split_salt == salt, why
    assert _requeued(state) == nodes, why
    assert "holdout_partition_disclosed" not in state.model_dump(), "fold-internal, never dumped"


def test_a_reset_or_a_new_candidate_after_a_private_grade_re_measures_no_incumbent(tmp_path):
    for i, (kind, data) in enumerate((
            ("node_reset", {"node_id": 0, "from_stage": "eval", "generation": 0}),
            ("node_created", {"node_id": 2, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "rationale": "r"},
                              "code": "print(2)"}))):
        for row, kept in ((_PRIVATE, True), ({}, False)):
            root = tmp_path / f"{kind}-{i}-{kept}"
            root.mkdir()
            store = _disclosed(root, row, then=("run_finished",) if kind == "node_reset" else ())
            store.append(kind, data)
            state = fold(store.read_all())
            assert state.holdout_evaluated_ids == [] and state.search_epoch == 1, (kind, kept)
            assert (state.nodes[1].status.value, state.nodes[1].attempt) == (
                ("evaluated", 0) if kept else ("pending", 1)), (kind, kept)
            assert state.split_salt == (0 if kept else 1), (kind, kept)


def test_a_drain_after_a_private_grade_is_not_refused_for_the_disclosure(tmp_path):
    """`resume --drain-only` refused ANY disclosed run, because lifting it re-queued every evaluated
    node; after a private grade nothing is re-queued, so the owed node is drained. The shape: node
    0 was reset BEFORE the finish's holdout phase (a reset after it consumes the disclosure itself),
    and the eval budget finished the run with it pending."""
    from looplab.engine.run_boundary import classify_prior_run, drain_only_refusal

    reset = (("node_reset", {"node_id": 0, "from_stage": "eval", "generation": 0}),)
    for row, refused in ((_PRIVATE, False), ({}, True)):
        root = tmp_path / f"drain-{refused}"
        root.mkdir()
        store = _disclosed(root, row, then=("run_finished",), before=reset)
        events = store.read_all()
        state = fold(events)
        kind = classify_prior_run(state, events)
        assert kind == "finished" and state.holdout_evaluated_ids == [1], kind
        assert state.nodes[0].status.value == "pending"
        answer = drain_only_refusal(state, kind, events)
        if refused:
            assert answer is not None and "a holdout was disclosed" in answer[1], answer
        else:
            assert answer is None or "a holdout was disclosed" not in answer[1], answer


def test_a_burn_is_consumed_with_its_epoch(tmp_path):
    """The partition re-score burns the split and the reopen consumes it; a private grade in the
    NEXT epoch burns nothing of its own, and does not inherit the consumed burn."""
    store = _disclosed(tmp_path, {})                       # burned, finished, reopened
    for nid, metric in ((0, 0.45), (1, 0.55)):             # the requeued leaders re-measured
        store.append("node_evaluated", {"node_id": nid, "generation": 1, "metric": metric,
                                        "violations": []})
    store.append("holdout_evaluated", {"node_id": 1, "generation": 1, "metric": 0.6, "gap": -0.1,
                                       "n_holdout": 3, "search_epoch": 1, **_PRIVATE})
    disclosed = fold(store.read_all())
    assert disclosed.holdout_evaluated_ids == [1] and disclosed.split_salt == 1
    store.append("run_finished", {"reason": "done"})
    store.append("resume", {})
    state = fold(store.read_all())
    assert state.search_epoch == 2 and state.split_salt == 1, "one burn, one re-carve"
    assert _requeued(state) == {0: ("evaluated", 1), 1: ("evaluated", 1)}
