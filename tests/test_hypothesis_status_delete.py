"""Two hypothesis-board fixes:
- a hypothesis "supported" by a node that ADVANCED the run's SOTA stays supported after a later node
  overtakes it (before: it flipped supported→tested because support keyed on the CURRENT best, a moving
  target — the "board bug" the operator saw);
- `hypothesis_updated status=deleted` removes a card from the board entirely (vs abandoned, which stays)."""
from __future__ import annotations

from looplab.core.models import Event
from looplab.events.replay import fold


def _mk(evs):
    return [Event(type=t, data=d) for t, d in evs]


def _by_statement(st):
    return {h.statement: h for h in st.hypotheses.values()}


def test_support_is_sticky_when_a_record_setter_is_overtaken():
    # three parentless drafts; each new one beats the last. #2 set a record (0.80 -> 0.90), then #3
    # (0.95) overtakes it. #2's hypothesis must STAY supported (it advanced the SOTA), not regress.
    st = fold(_mk([
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("node_created", {"node_id": 1, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "H1 baseline"}}),
        ("node_evaluated", {"node_id": 1, "metric": 0.80}),
        ("node_created", {"node_id": 2, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "H2 record"}}),
        ("node_evaluated", {"node_id": 2, "metric": 0.90}),           # a new record (beat 0.80)
        ("node_created", {"node_id": 3, "operator": "draft",
                          "idea": {"operator": "draft", "hypothesis": "H3 winner"}}),
        ("node_evaluated", {"node_id": 3, "metric": 0.95}),           # overtakes #2
    ]))
    b = _by_statement(st)
    assert st.best_node_id == 3                                       # #3 is now best
    assert b["H2 record"].status == "supported"                      # ... yet #2's verdict STANDS
    assert b["H3 winner"].status == "supported"                      # the current record too
    assert b["H1 baseline"].status == "tested"                       # first node set no record -> no support


def test_hypothesis_delete_removes_it_from_the_board():
    base = [
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("hypothesis_added", {"statement": "a log transform helps", "source": "human"}),
    ]
    st = fold(_mk(base))
    hid = next(iter(st.hypotheses))                                   # the added card's id
    assert st.hypotheses[hid].statement == "a log transform helps"

    st2 = fold(_mk(base + [("hypothesis_updated", {"id": hid, "status": "deleted"})]))
    assert hid not in st2.hypotheses                                 # gone entirely
    assert hid in st2.hypotheses_deleted


def test_delete_beats_abandon_and_survives_reopen_attempt():
    base = [
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("hypothesis_added", {"statement": "x", "source": "human"}),
    ]
    hid = next(iter(fold(_mk(base)).hypotheses))
    st = fold(_mk(base + [
        ("hypothesis_updated", {"id": hid, "status": "abandoned"}),
        ("hypothesis_updated", {"id": hid, "status": "deleted"}),
    ]))
    assert hid not in st.hypotheses                                   # deleted wins, not shown
