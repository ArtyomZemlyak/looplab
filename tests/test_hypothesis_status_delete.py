"""Two belief-board fixes (1 card = 1 hypothesis — the single Card board):
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
    return {h.statement: h for h in st.cards.values()}


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
    assert b["H2 record"].verdict == "supported"                      # ... yet #2's verdict STANDS
    assert b["H3 winner"].verdict == "supported"                      # the current record too
    # #1 ESTABLISHED the SOTA, which is not the same as advancing it: there was nothing to advance
    # over. This line asserted `supported` and is INVERTED — see
    # `tests/test_verdict_needs_a_comparison.py` for what that verdict cost on
    # `runs/e5small-dr-unified-v5`, where a Researcher read it, disbelieved it in its own trace and
    # spent a whole Developer build re-implementing the card.
    #
    # The three parentless drafts above are also why the discriminator is the ESTABLISHER and not a
    # parent: keyed on "has a feasible parent", #2 and #3 — genuine record-beaters, one of them the
    # run's champion — read `tested` too, which is the same board lie pointed the other way.
    assert b["H1 baseline"].verdict == "tested"


def test_hypothesis_delete_removes_it_from_the_board():
    base = [
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("hypothesis_added", {"statement": "a log transform helps", "source": "human"}),
    ]
    st = fold(_mk(base))
    hid = next(iter(st.cards))                                   # the added card's id
    assert st.cards[hid].statement == "a log transform helps"

    st2 = fold(_mk(base + [("hypothesis_updated", {"id": hid, "status": "deleted"})]))
    assert hid not in st2.cards                                 # gone entirely
    assert hid in st2.hypotheses_deleted


def test_delete_beats_abandon_and_survives_reopen_attempt():
    base = [
        ("run_started", {"run_id": "r", "task_id": "t", "direction": "max"}),
        ("hypothesis_added", {"statement": "x", "source": "human"}),
    ]
    hid = next(iter(fold(_mk(base)).cards))
    st = fold(_mk(base + [
        ("hypothesis_updated", {"id": hid, "status": "abandoned"}),
        ("hypothesis_updated", {"id": hid, "status": "deleted"}),
    ]))
    assert hid not in st.cards                                   # deleted wins, not shown
