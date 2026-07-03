"""D1 holdout-gated promotion (Phase 1): label-partition holdout for host-graded tasks,
holdout-preferred champion selection, generalization gap, disjoint confirm seeds, and the
merge_mode="auto" capability resolution."""
from __future__ import annotations

import anyio
import pytest

from looplab.core.models import Event, Idea, RunState
from looplab.engine.orchestrator import Engine, _holdout_indices
from looplab.events.replay import fold


def _ev(seq, type_, **data):
    return Event(seq=seq, ts=float(seq), type=type_, data=data)


# --------------------------------------------------------------------------- #
# _holdout_indices: deterministic, non-degenerate partition
# --------------------------------------------------------------------------- #

def test_holdout_indices_deterministic_and_bounded():
    a = _holdout_indices(100, 0.25)
    b = _holdout_indices(100, 0.25)
    assert a == b                       # pure function of (n, fraction)
    assert 0 < len(a) < 100             # neither side degenerate
    assert 10 <= len(a) <= 45           # roughly the requested fraction


def test_holdout_indices_small_n_never_degenerate():
    for n in range(2, 12):
        idx = _holdout_indices(n, 0.25)
        assert 0 < len(idx) < n, f"degenerate partition for n={n}"


def test_holdout_indices_zero_fraction_empty():
    assert _holdout_indices(50, 0.0) == frozenset()


# --------------------------------------------------------------------------- #
# fold: holdout_evaluated + holdout_select champion override + gap
# --------------------------------------------------------------------------- #

def _base_events(holdout_select=True):
    return [
        _ev(0, "run_started", run_id="r", task_id="t", direction="max",
            holdout_select=holdout_select),
        _ev(1, "node_created", node_id=0, parent_ids=[], operator="draft",
            idea={"operator": "draft", "params": {}}),
        _ev(2, "node_created", node_id=1, parent_ids=[], operator="draft",
            idea={"operator": "draft", "params": {}}),
        # node 0 looks BETTER on the search metric; node 1 wins the holdout
        _ev(3, "node_evaluated", node_id=0, metric=0.95, eval_seconds=0.1),
        _ev(4, "node_evaluated", node_id=1, metric=0.90, eval_seconds=0.1),
    ]


def test_holdout_select_overrides_val_leader():
    events = _base_events() + [
        _ev(5, "holdout_evaluated", node_id=0, metric=0.70, gap=0.25),
        _ev(6, "holdout_evaluated", node_id=1, metric=0.88, gap=0.02),
    ]
    st = fold(events)
    assert st.holdout_select is True
    assert st.best_node_id == 1                       # unseen signal picks the champion
    assert st.nodes[0].holdout_metric == 0.70
    assert st.nodes[0].generalization_gap == pytest.approx(0.25)
    assert st.nodes[1].generalization_gap == pytest.approx(0.02)
    assert st.holdout_evaluated_ids == [0, 1]


def test_holdout_audit_only_when_select_off():
    events = _base_events(holdout_select=False) + [
        _ev(5, "holdout_evaluated", node_id=0, metric=0.70, gap=0.25),
        _ev(6, "holdout_evaluated", node_id=1, metric=0.88, gap=0.02),
    ]
    st = fold(events)
    assert st.best_node_id == 0                       # legacy pick stands
    assert st.nodes[1].holdout_metric == 0.88         # gap still folded (audit)


def test_old_logs_without_field_fold_legacy():
    events = [
        _ev(0, "run_started", run_id="r", task_id="t", direction="max"),
        _ev(1, "node_created", node_id=0, parent_ids=[], operator="draft",
            idea={"operator": "draft", "params": {}}),
        _ev(2, "node_evaluated", node_id=0, metric=0.5, eval_seconds=0.1),
    ]
    st = fold(events)
    assert st.holdout_select is False
    assert st.best_node_id == 0


def test_holdout_null_metric_gates_but_never_wins():
    events = _base_events() + [
        _ev(5, "holdout_evaluated", node_id=0, metric=None),
    ]
    st = fold(events)
    assert st.holdout_evaluated_ids == [0]            # attempt recorded (gate closes)
    assert st.nodes[0].holdout_metric is None
    assert st.best_node_id == 0                       # no holdout pool -> search pick stands


def test_gap_derived_from_confirmed_mean_without_holdout():
    events = [
        _ev(0, "run_started", run_id="r", task_id="t", direction="min"),
        _ev(1, "node_created", node_id=0, parent_ids=[], operator="draft",
            idea={"operator": "draft", "params": {}}),
        _ev(2, "node_evaluated", node_id=0, metric=1.0, eval_seconds=0.1),
        _ev(3, "node_confirmed", node_id=0, mean=1.3, std=0.1, seeds=3),
    ]
    st = fold(events)
    # min direction: search metric 1.0 looked better than robust 1.3 -> positive gap
    assert st.nodes[0].generalization_gap == pytest.approx(0.3)


# --------------------------------------------------------------------------- #
# Engine end-to-end: host-graded task, search partition scoring + holdout phase
# --------------------------------------------------------------------------- #

class _HostGradedTask:
    """Minimal host-graded task: candidates 'predict' a constant list; labels are known."""
    id = "holdout-task"
    goal = "maximize accuracy"
    direction = "max"

    def __init__(self, n=40):
        self.n = n

    def model_dump(self, mode="json"):
        return {"id": self.id, "goal": self.goal, "direction": self.direction}

    def host_grader(self):
        return {"predictions": "predictions.json", "scorer": "accuracy",
                "labels": [i % 2 for i in range(self.n)]}


class _PredsResearcher:
    def propose(self, state, parent):
        return Idea(operator="draft", params={"x": 1.0}, rationale="predict alternating")


class _PredsDeveloper:
    """Writes predictions matching labels on EVEN indices only (half right everywhere)."""

    def implement(self, idea):
        return ("import json\n"
                "preds = [i % 2 for i in range(40)]\n"
                "json.dump(preds, open('predictions.json', 'w'))\n"
                "print(json.dumps({'metric': 0.0}))\n")


@pytest.mark.parametrize("holdout_select", [True, False])
def test_engine_holdout_phase_emits_events(tmp_path, holdout_select):
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.search.policy import GreedyTree

    task = _HostGradedTask()
    eng = Engine(
        tmp_path / "run", task=task,
        researcher=_PredsResearcher(), developer=_PredsDeveloper(),
        sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=2),
        n_seeds=1, max_nodes=2, timeout=30.0,
        holdout_fraction=0.25, holdout_select=holdout_select, holdout_top_k=2,
    )
    assert eng._holdout_idx                       # partition was reserved
    final = anyio.run(eng.run)
    assert final.finished
    # every evaluated node got a search-partition metric (perfect predictions -> 1.0)
    for n in final.evaluated_nodes():
        assert n.metric == pytest.approx(1.0)
    # the top-k got holdout events; perfect predictions -> holdout metric 1.0, gap 0
    assert final.holdout_evaluated_ids
    hn = [n for n in final.nodes.values() if n.holdout_metric is not None]
    assert hn and all(n.holdout_metric == pytest.approx(1.0) for n in hn)
    # replay reproduces the same state (replay-safe)
    st2 = fold(eng.store.read_all())
    assert st2.best_node_id == final.best_node_id
    assert st2.holdout_evaluated_ids == final.holdout_evaluated_ids


def test_engine_search_never_sees_holdout_rows(tmp_path):
    """A candidate that is perfect on the search partition but wrong on every holdout row must
    score 1.0 during search and be exposed by the holdout re-score."""
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.search.policy import GreedyTree

    task = _HostGradedTask()
    eng = Engine(
        tmp_path / "run", task=task,
        researcher=_PredsResearcher(), developer=_PredsDeveloper(),
        sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
        n_seeds=1, max_nodes=1, timeout=30.0,
        holdout_fraction=0.25, holdout_select=True, holdout_top_k=1,
    )
    hold = eng._holdout_idx
    labels = [i % 2 for i in range(40)]
    # overwrite the developer with one that flips predictions ON THE HOLDOUT ROWS only
    flipped = [(1 - labels[i]) if i in hold else labels[i] for i in range(40)]
    class _Cheater:
        def implement(self, idea):
            return ("import json\n"
                    f"json.dump({flipped!r}, open('predictions.json', 'w'))\n")
    eng.developer = _Cheater()
    final = anyio.run(eng.run)
    n = final.nodes[0]
    assert n.metric == pytest.approx(1.0)             # search signal: perfect
    assert n.holdout_metric == pytest.approx(0.0)     # unseen signal: exposed
    assert n.generalization_gap == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# Confirm seeds disjoint from search seed (1.3)
# --------------------------------------------------------------------------- #

def test_confirm_seeds_start_at_base(tmp_path):
    from looplab.runtime.sandbox import RunResult, SubprocessSandbox
    from looplab.search.policy import GreedyTree

    class _T:
        id = "t"; goal = "min"; direction = "min"
        def model_dump(self, mode="json"):
            return {"id": "t"}

    eng = Engine(tmp_path / "run", task=_T(), researcher=_PredsResearcher(),
                 developer=_PredsDeveloper(), sandbox=SubprocessSandbox(),
                 policy=GreedyTree(n_seeds=1, max_nodes=1),
                 confirm_top_k=1, confirm_seeds=3, confirm_seed_base=1)
    ran: list[int] = []

    def fake_run_eval(node, workdir, env=None, profile=None, cancel=None):
        ran.append(int((env or {}).get("LOOPLAB_EVAL_SEED", -1)))
        return RunResult(exit_code=0, stdout="", stderr="", metric=1.0, timed_out=False)

    eng._run_eval = fake_run_eval
    st = RunState(direction="min")
    st.nodes[0] = __import__("looplab.core.models", fromlist=["Node"]).Node(
        id=0, operator="draft", idea=Idea(operator="draft", params={}), metric=1.0,
        status="evaluated")
    anyio.run(eng._confirm_phase, st)
    assert ran == [1, 2, 3]                    # disjoint from the search's implicit seed 0


# --------------------------------------------------------------------------- #
# merge_mode="auto" capability resolution (1.2)
# --------------------------------------------------------------------------- #

def test_merge_auto_resolves_by_capability(tmp_path):
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.search.policy import GreedyTree

    class _T:
        id = "t"; goal = "g"; direction = "min"
        def model_dump(self, mode="json"):
            return {"id": "t"}

    def mk(developer):
        return Engine(tmp_path / f"run-{id(developer)}", task=_T(),
                      researcher=_PredsResearcher(), developer=developer,
                      sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                      merge_mode="auto")

    toy = mk(_PredsDeveloper())
    assert toy._merge_mode == "mean"           # templated developer -> mean

    class _CodeGen(_PredsDeveloper):
        is_code_generating = True

    llm = mk(_CodeGen())
    assert llm._merge_mode == "ensemble"       # code-generating developer -> ensemble


def test_settings_defaults_and_validation():
    from looplab.core.config import Settings
    s = Settings()
    assert s.merge_mode == "auto"
    assert s.holdout_fraction == pytest.approx(0.25)
    assert s.holdout_select is True
    assert s.confirm_seed_base == 1
    with pytest.raises(ValueError):
        Settings(merge_mode="bogus")
