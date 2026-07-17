"""Explicit, non-derived EV_CONCEPT_EDGE assertions fold commutatively into RunState.concept_edges.

``co_occurs`` is intentionally excluded: it is derived from current node memberships by ConceptFrame,
because a max-only event fold cannot retract a stale count or ghost pair after re-tagging.
"""
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold


def _store(tmp_path, edge_events):
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "toy", "goal": "g", "direction": "max"})
    for d in edge_events:
        s.append("concept_edge", d)
    return s


def test_single_and_batch_edges(tmp_path):
    st = fold(_store(tmp_path, [
        {"src": "loss/dcl", "rel": "is_a", "dst": "loss", "provenance": "asserted", "confidence": 1.0},
        {"edges": [
            {"src": "loss/dcl", "rel": "co_occurs", "dst": "reg/r-drop", "provenance": "evidenced", "confidence": 5},
            {"src": "arch/moe", "rel": "is_a", "dst": "arch", "provenance": "asserted", "confidence": 1.0},
        ]},
    ]).read_all())
    edges = st.concept_edges
    assert edges["loss/dcl\tis_a\tloss"]["dst"] == "loss"
    assert "loss/dcl\tco_occurs\treg/r-drop" not in edges
    assert all(len(k.split("\t")) == 3 for k in edges) and len(edges) == 2


def test_max_confidence_wins_commutatively(tmp_path):
    lo = {"src": "a", "rel": "r", "dst": "b", "provenance": "evidenced", "confidence": 1.0}
    hi = {"src": "a", "rel": "r", "dst": "b", "provenance": "asserted", "confidence": 9.0}
    a = fold(_store(tmp_path, [lo, hi]).read_all()).concept_edges
    b = fold(_store(tmp_path, [hi, lo]).read_all()).concept_edges           # reversed order
    assert a == b                                                          # commutative -> order-tolerant
    assert a["a\tr\tb"]["confidence"] == 9.0                               # higher confidence wins


def test_provenance_rank_breaks_confidence_tie(tmp_path):
    ev = {"src": "a", "rel": "r", "dst": "b", "provenance": "evidenced", "confidence": 2.0}
    asrt = {"src": "a", "rel": "r", "dst": "b", "provenance": "asserted", "confidence": 2.0}
    a = fold(_store(tmp_path, [ev, asrt]).read_all()).concept_edges
    b = fold(_store(tmp_path, [asrt, ev]).read_all()).concept_edges
    assert a == b and a["a\tr\tb"]["provenance"] == "asserted"             # asserted outranks on a tie


def test_malformed_edges_skipped(tmp_path):
    st = fold(_store(tmp_path, [
        {"src": "a", "rel": "", "dst": "b"},        # empty rel -> skip
        {"src": "a", "dst": "b"},                   # no rel key -> not an inline edge -> skip
        {"src": "ok", "rel": "r", "dst": "d"},      # valid (confidence defaults 0.0)
    ]).read_all())
    assert list(st.concept_edges) == ["ok\tr\td"]
    assert st.concept_edges["ok\tr\td"]["confidence"] == 0.0


def test_empty_default(tmp_path):
    assert fold(_store(tmp_path, []).read_all()).concept_edges == {}


def test_nan_and_bool_confidence_stay_commutative(tmp_path):
    # Regression: a NaN confidence used to break invariant-5 order-tolerance (every `>` against a NaN
    # tuple-head is False, so first-arrival stuck), and a bool coerced via isinstance(int) to 1.0 could
    # wrongly win. Both must neutralize to 0.0 so the accumulate stays a commutative max. NaN is reachable
    # because stdlib json round-trips `NaN` literals and confidence is agent-supplied.
    nan = {"src": "a", "rel": "r", "dst": "b", "provenance": "evidenced", "confidence": float("nan")}
    real = {"src": "a", "rel": "r", "dst": "b", "provenance": "evidenced", "confidence": 5.0}
    flag = {"src": "a", "rel": "r", "dst": "b", "provenance": "asserted", "confidence": True}
    forward = fold(_store(tmp_path / "f", [nan, real, flag]).read_all()).concept_edges
    reverse = fold(_store(tmp_path / "r", [flag, real, nan]).read_all()).concept_edges
    assert forward == reverse                                    # order-tolerant despite NaN/bool
    assert forward["a\tr\tb"]["confidence"] == 5.0               # the real edge wins; NaN/True -> 0.0 lose
