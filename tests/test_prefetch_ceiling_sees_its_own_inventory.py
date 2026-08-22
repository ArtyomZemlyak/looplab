"""The prefetch ceiling must be able to see the thing it buys.

`speculation.py::_speculation_depth_used` is the ceiling on unconsumed prefetch. Its contract counts
"outstanding requests plus committed/unevaluated speculative NODES" — but the lane it gates produces
a staged CARD and deliberately owns no Node slot ("Author their concrete Ideas and durable Cards
now, but deliberately leave every Node slot unowned; the next fresh fold must select them").

So the ceiling could not see its own purchases. Measured live on `runs/e5small-dr-unified-v4`:
`_speculation_depth_used()` returned **1** against a pinned ceiling of **2** while **88** cards sat
unbuilt — `1 < 2` on every turn, without end. Nothing drained them either: while any node is
pending, `card_next_actions` returns `forced_card_actions` (an `evaluate` naming the running node)
and never reaches card selection, so a staged card could not become the Node the counter was
waiting for. Minting stayed legal precisely because consuming was impossible.
"""
from looplab.core.models import Card, Idea, Node, RunState
from looplab.engine.speculation import SpeculationMixin
from looplab.search.card_selection import unconsumed_card_inventory


def _card(cid: str, **over) -> Card:
    base = dict(id=cid, statement="extend n_epochs 15->30 on node #3's recipe",
                seed_statement="extend n_epochs 15->30 on node #3's recipe",
                operator="improve", status="proposed", verdict="open")
    base.update(over)
    return Card.model_validate(base)


def _state(cards=(), nodes=()) -> RunState:
    st = RunState(run_id="r", goal="g", direction="max")
    for c in cards:
        st.cards[c.id] = c
    for n in nodes:
        st.nodes[n.id] = n
    return st


def _pending_node(nid: int, card_id: str | None = None) -> Node:
    idea = {"name": "n", "rationale": "r", "params": {}, "operator": "improve"}
    if card_id is not None:
        idea["card_id"] = card_id
    return Node.model_validate({"id": nid, "operator": "improve", "idea": idea})


# --------------------------------------------------------------------------- the counter itself

def test_a_staged_card_is_inventory():
    """The property the fix exists for: what the lane stages, the ceiling can count."""
    assert unconsumed_card_inventory(_state()) == 0
    assert unconsumed_card_inventory(_state([_card("c1"), _card("c2")])) == 2


def test_terminal_cards_are_not_inventory():
    """A card the board has finished with is not work anyone is waiting to consume — otherwise one
    operator drop would suppress prefetch for the rest of the run."""
    st = _state([
        _card("open"),
        _card("dropped", status="dropped", dropped_reason="operator dropped"),
        _card("merged", merged_into="open"),
        _card("settled", verdict="supported"),
        _card("running", status="running"),
    ])
    assert unconsumed_card_inventory(st) == 1


def test_the_exclusion_prevents_double_counting():
    """The caller already counts outstanding requests and the cards pending Nodes own. Counting them
    again here would make the ceiling twice as strict as its own contract states."""
    st = _state([_card("c1"), _card("c2")])
    assert unconsumed_card_inventory(st, exclude={"c1"}) == 1
    assert unconsumed_card_inventory(st, exclude={"c1", "c2"}) == 0


# --------------------------------------------------------------------------- the ceiling in use

def test_the_depth_counts_staged_cards_not_only_nodes():
    """The live shape, in miniature: three staged cards and no speculative Node at all.

    Before the fix this state answered 0 — the board full and the ceiling blind. It must answer 3.
    """
    st = _state([_card("c1"), _card("c2"), _card("c3")])
    assert SpeculationMixin._prefetch_supply_used(st) == 3


def test_the_consuming_gate_is_NOT_charged_for_the_card_it_consumes():
    """The narrowing this fix needed, learned the hard way.

    A first version added the inventory to `_speculation_depth_used`, which BOTH gates share. Six
    tests went red on `_request_card_build() is False`: charging the consuming gate for the very
    Card it is about to build makes consumption impossible, which is the opposite of the goal. The
    shared counter therefore still counts only outstanding work, and the inventory term lives in
    `_prefetch_supply_used`, which only the raw-propose branch consults."""
    st = _state([_card("c1"), _card("c2"), _card("c3")])
    assert SpeculationMixin._speculation_depth_used(st) == 0, (
        "the consuming gate must not see board inventory")
    assert SpeculationMixin._prefetch_supply_used(st) == 3, (
        "the producing gate must")


def test_a_card_a_node_already_owns_is_not_inventory():
    """The overlap the exclusion exists for, and the answer is ZERO — which is not what I first
    asserted, and the test is what said so.

    A Card a pending Node owns is spoken for, so it is not inventory. And an ORDINARY pending node
    is not prefetch either: `_speculative_pending_nodes` counts only nodes carrying the durable
    speculative marker. So this state holds no unconsumed PREFETCH at all, and charging the
    prefetch ceiling for ordinary work would throttle a lane that never bought anything."""
    st = _state([_card("c1")], [_pending_node(7, card_id="c1")])
    assert SpeculationMixin._prefetch_supply_used(st) == 0
    # ...and the card really is excluded rather than merely uncounted by accident:
    assert unconsumed_card_inventory(st) == 1
    assert unconsumed_card_inventory(st, exclude={"c1"}) == 0


def test_the_gate_refuses_once_inventory_covers_the_ceiling():
    """The arithmetic the spree turned on. With the run's own pinned ceiling of 2, a board holding
    three unbuilt cards must stop the lane; before the fix it compared 0 < 2 and bought another."""
    ceiling = 2
    st = _state([_card("c1"), _card("c2"), _card("c3")])
    assert not (SpeculationMixin._prefetch_supply_used(st) < ceiling)
    # ...and an empty board still prefetches, which is the feature working as intended.
    assert SpeculationMixin._prefetch_supply_used(_state()) < ceiling
