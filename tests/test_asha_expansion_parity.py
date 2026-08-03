"""The one ASHA expansion/retirement computation the policy and the Card lane share (doc 25 SE-03).

`ASHAPolicy.next_actions` is the authority on ASHA promotion; `card_selection._asha_lane` must agree
with it or the Card lane promotes a lineage the policy has retired. The `_ASHA_MAX_FAILED_PROMOTIONS`
CONSTANT was already shared — the algorithm around it was copy-pasted, which is the worse half: a
constant that drifts is one grep, a semantics change is not.

And unlike GreedyTree/Card parity, which the 15-case scorer-fidelity matrix re-checks at runtime on
every election, the ASHA lane has NO such guard (`SCORER_FIDELITY_CASE_NAMES` is GreedyTree-only). A
desync here is silent by construction, so the shared function plus a source scan is the guard.
"""
from __future__ import annotations

import pytest

from looplab.core.models import NodeStatus
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.search.policy import _ASHA_MAX_FAILED_PROMOTIONS, asha_expansion


def _state(tmp_path, nodes):
    """`nodes` = list of (node_id, parent_ids, "ok" | "failed" | "pending")."""
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "t", "task_id": "toy", "goal": "g", "direction": "max"})
    for node_id, parents, status in nodes:
        store.append("node_created", {"node_id": node_id, "parent_ids": list(parents),
                                      "operator": "improve",
                                      "idea": {"operator": "improve", "params": {}}})
        if status == "ok":
            store.append("node_evaluated", {"node_id": node_id, "metric": 0.5})
        elif status == "failed":
            store.append("node_failed", {"node_id": node_id, "error": "boom", "reason": "crash"})
    return fold(store.read_all())


def test_a_live_child_marks_a_survivor_expanded(tmp_path):
    state = _state(tmp_path, [(0, [], "ok"), (1, [0], "ok")])
    has_live_child, retired = asha_expansion(state)
    assert has_live_child == {0} and retired == set()


def test_a_pending_child_counts_as_live_not_as_a_failure(tmp_path):
    """"Expanded" is about work already spent on the lineage, so a still-running child holds the
    slot. Counting it as failed would re-promote a survivor whose child is mid-eval."""
    state = _state(tmp_path, [(0, [], "ok"), (1, [0], "pending")])
    has_live_child, retired = asha_expansion(state)
    assert has_live_child == {0} and retired == set()


def test_one_failed_child_leaves_the_survivor_re_promotable(tmp_path):
    """ONE transient crash must not abandon a possibly-best lineage."""
    state = _state(tmp_path, [(0, [], "ok"), (1, [0], "failed")])
    has_live_child, retired = asha_expansion(state)
    assert has_live_child == set() and retired == set()


def test_the_retry_cap_retires_a_deterministically_crashing_lineage(tmp_path):
    """Without the cap, `chosen = lowest-id unexpanded survivor` re-promotes the same crashing
    lineage every iteration, starving its siblings and burning the whole node budget on it."""
    children = [(index + 1, [0], "failed") for index in range(_ASHA_MAX_FAILED_PROMOTIONS)]
    state = _state(tmp_path, [(0, [], "ok"), *children])
    _has_live_child, retired = asha_expansion(state)
    assert retired == {0}


def test_a_live_child_rescues_a_survivor_that_already_hit_the_cap(tmp_path):
    """Retirement is "enough failures AND nothing live" — a lineage that finally produced a working
    child is expanded, not retired, and the two states are not the same thing downstream."""
    children = [(index + 1, [0], "failed") for index in range(_ASHA_MAX_FAILED_PROMOTIONS)]
    state = _state(tmp_path, [(0, [], "ok"), *children,
                              (_ASHA_MAX_FAILED_PROMOTIONS + 1, [0], "ok")])
    has_live_child, retired = asha_expansion(state)
    assert has_live_child == {0} and retired == set()


def test_failures_are_counted_per_parent_not_run_wide(tmp_path):
    """One failure each against two parents must retire neither."""
    state = _state(tmp_path, [(0, [], "ok"), (1, [], "ok"), (2, [0], "failed"), (3, [1], "failed")])
    _has_live_child, retired = asha_expansion(state)
    assert retired == set()


def test_a_merge_child_counts_against_every_parent_it_names(tmp_path):
    """A failed merge is evidence against BOTH lineages that produced it — dropping the multi-parent
    fan-out would leave two survivors each looking one failure short of the cap forever."""
    failures = [(index + 2, [0, 1], "failed") for index in range(_ASHA_MAX_FAILED_PROMOTIONS)]
    state = _state(tmp_path, [(0, [], "ok"), (1, [], "ok"), *failures])
    _has_live_child, retired = asha_expansion(state)
    assert retired == {0, 1}


def test_an_empty_run_is_not_an_error(tmp_path):
    assert asha_expansion(_state(tmp_path, [])) == (set(), set())


# ------------------------------------------------------------------ the parity guard

def test_neither_the_policy_nor_the_card_lane_re_derives_the_computation():
    """The source scan IS the runtime guard here: the scorer-fidelity matrix, which would otherwise
    catch a divergence on every election, covers only GreedyTree cases."""
    from pathlib import Path

    import inspect
    from pathlib import Path

    from looplab.search import policy

    lines, start = inspect.getsourcelines(policy.asha_expansion)
    inside_helper = range(start, start + len(lines))

    search = Path(__file__).resolve().parents[1] / "looplab" / "search"
    offenders = []
    for path in sorted(search.glob("*.py")):
        for index, line in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1):
            if "_ASHA_MAX_FAILED_PROMOTIONS" not in line:
                continue
            if path.name == "policy.py" and (line.startswith("_ASHA_MAX_FAILED_PROMOTIONS")
                                             or index in inside_helper):
                continue                                  # its definition, and the one reader
            offenders.append(f"{path.name}:{index}")
    assert offenders == [], (
        f"the ASHA retry cap is read outside `asha_expansion`: {offenders}. Both the policy and the "
        "Card lane must go through the shared helper — there is no runtime parity gate for ASHA.")


def test_the_scorer_fidelity_matrix_is_still_greedytree_only():
    """Pins the premise of the guard above. If an ASHA case is ever added to the runtime matrix, this
    test is the reminder that the source scan can be relaxed — and until then, that it cannot."""
    from looplab.search.scorer_fidelity import SCORER_FIDELITY_CASE_NAMES

    assert not any("asha" in name.lower() for name in SCORER_FIDELITY_CASE_NAMES)


@pytest.mark.parametrize("module,symbol", [("looplab.search.policy", "ASHAPolicy"),
                                           ("looplab.search.card_selection", "_asha_lane")])
def test_both_authorities_call_the_shared_helper(module, symbol):
    import importlib
    import inspect

    source = inspect.getsource(getattr(importlib.import_module(module), symbol))
    assert "asha_expansion(state)" in source, f"{module}.{symbol} no longer shares the computation"
