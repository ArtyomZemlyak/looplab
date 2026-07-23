"""Characterization + purity guard for the extracted verdict helpers (Layer 1a).

`_record_setter_ids` and `_evidence_verdict` were extracted VERBATIM from the (now removed)
`_derive_hypotheses` so `_derive_cards` — the sole board derivation — reuses the identical logic. `tests/test_golden_replay.py`
already pins byte-identity on a REAL run; these tests LOCK the extracted behavior on the tricky paths
the toy golden never reaches — merge/`_canon` alias chain, `abandoned` override, record-setter
stickiness after being overtaken, testing-vs-open — and prove the helpers NEVER mutate the evidence
nodes (the invariant `_derive_cards` will lean on: it computes a card's verdict without stamping onto
Node/Hypothesis/Card)."""
from __future__ import annotations

from looplab.core.models import Event, hypothesis_id
from looplab.events.replay import _evidence_verdict, _record_setter_ids, fold


def _mk(evs):
    return [Event(type=t, data=d) for t, d in evs]


def _base():
    """A run touching every verdict branch: SOTA-setter, overtaken-but-sticky, testing, open, abandoned."""
    return fold(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        # node 1 draft ESTABLISHES the SOTA (0.80); it has no parent so best_delta stays None, but the
        # record-setter flag makes its hypothesis "supported" and it STAYS supported after node 2 overtakes.
        ("node_created", {"node_id": 1, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "draft baseline"}}),
        ("node_evaluated", {"node_id": 1, "metric": 0.80}),
        # node 2 improve BEATS its parent (0.80 -> 0.88) -> supported + a record-setter.
        ("node_created", {"node_id": 2, "operator": "improve", "parent_ids": [1],
                          "idea": {"operator": "improve", "hypothesis": "interactions help"}}),
        ("node_evaluated", {"node_id": 2, "metric": 0.88}),
        # node 3 improve is WORSE than parent (0.85 < 0.88); node 4 (same hypothesis) still pending
        # -> "a deeper model helps" is inconclusive -> testing, with a NEGATIVE best_delta.
        ("node_created", {"node_id": 3, "operator": "improve", "parent_ids": [2],
                          "idea": {"operator": "improve", "hypothesis": "a deeper model helps"}}),
        ("node_evaluated", {"node_id": 3, "metric": 0.85}),
        ("node_created", {"node_id": 4, "operator": "improve", "parent_ids": [2],
                          "idea": {"operator": "improve", "hypothesis": "a deeper model helps"}}),
        # node 5 states a hypothesis whose only evidence FAILED -> evidence exists but none usable -> open.
        ("node_created", {"node_id": 5, "operator": "improve", "parent_ids": [2],
                          "idea": {"operator": "improve", "hypothesis": "regularization only"}}),
        ("node_failed", {"node_id": 5, "reason": "boom"}),
        # explicit added hypothesis then an ABANDONED override (overrides regardless of evidence).
        ("hypothesis_added", {"statement": "external data raises accuracy", "source": "deep_research"}),
        ("hypothesis_updated", {"id": hypothesis_id("external data raises accuracy"),
                                "status": "abandoned"}),
    ]))


def _by_stmt(st):
    return {h.statement: h for h in st.cards.values()}


def test_record_setters_are_the_sota_advancers_only():
    st = _base()
    # 1 (establishes) and 2 (beats) advance the SOTA; 3 (worse) does not; 4 pending / 5 failed are
    # not evaluated-feasible so cannot set a record.
    assert _record_setter_ids(st.nodes, st.direction) == {1, 2}


def test_record_setter_stickiness_survives_being_overtaken():
    # "draft baseline" (node 1) has no parent and best_delta None, yet stays supported because node 1
    # set the run's first SOTA — even though node 2 later overtook it. The regression this guards is the
    # old "is the CURRENT best" bug that flipped it supported->tested the moment something beat it.
    h = _by_stmt(_base())["draft baseline"]
    assert h.verdict == "supported" and h.best_delta is None


def test_supported_carries_positive_best_delta():
    h = _by_stmt(_base())["interactions help"]
    assert h.verdict == "supported" and h.best_delta is not None and h.best_delta > 0


def test_testing_when_evaluated_worse_plus_pending():
    h = _by_stmt(_base())["a deeper model helps"]
    # evidence 3 (evaluated, worse) + 4 (pending) -> inconclusive; best_delta is the (negative) 3-vs-2 gap.
    assert h.verdict == "testing" and h.evidence == [3, 4]
    assert h.best_delta is not None and h.best_delta < 0


def test_open_when_only_evidence_failed():
    h = _by_stmt(_base())["regularization only"]
    assert h.verdict == "open" and h.best_delta is None


def test_abandoned_override_wins_over_evidence():
    h = _by_stmt(_base())["external data raises accuracy"]
    assert h.verdict == "abandoned"


def test_merge_alias_chain_unions_evidence_into_the_canonical():
    # a -> b -> c: fold "interactions help" into "a deeper model helps" into "draft baseline". `_canon`
    # must resolve the chain cycle-safe so ALL evidence lands on the single canonical card.
    a, b, c = "interactions help", "a deeper model helps", "draft baseline"
    st = fold(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", {"node_id": 1, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": c}}),
        ("node_evaluated", {"node_id": 1, "metric": 0.80}),
        ("node_created", {"node_id": 2, "operator": "improve", "parent_ids": [1],
                          "idea": {"operator": "improve", "hypothesis": a}}),
        ("node_evaluated", {"node_id": 2, "metric": 0.88}),
        ("node_created", {"node_id": 3, "operator": "improve", "parent_ids": [2],
                          "idea": {"operator": "improve", "hypothesis": b}}),
        ("node_evaluated", {"node_id": 3, "metric": 0.85}),
        ("hypothesis_merged", {"canonical": hypothesis_id(b), "aliases": [hypothesis_id(a)]}),
        ("hypothesis_merged", {"canonical": hypothesis_id(c), "aliases": [hypothesis_id(b)]}),
    ]))
    # Only the canonical "draft baseline" survives; its evidence is the union {1,2,3}, and it is
    # supported (nodes 1 & 2 advanced the SOTA).
    survivors = _by_stmt(st)
    assert set(survivors) == {c}
    assert survivors[c].evidence == [1, 2, 3]
    assert survivors[c].verdict == "supported"


def test_helpers_never_mutate_the_evidence_nodes():
    # The purity invariant `_derive_cards` relies on: computing a verdict must not touch Node state.
    st = _base()
    before = {nid: n.model_dump(mode="json") for nid, n in st.nodes.items()}
    setters = _record_setter_ids(st.nodes, st.direction)
    for h in st.cards.values():
        _evidence_verdict(h.evidence, st.nodes, st.direction, setters, h.id in st.hypotheses_abandoned)
    after = {nid: n.model_dump(mode="json") for nid, n in st.nodes.items()}
    assert before == after
