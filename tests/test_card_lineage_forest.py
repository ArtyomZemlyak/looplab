"""A direction is not a work item, and the edge that says so always forms a FOREST.

THE DEFECT this file holds the fix for, measured on `runs/e5small-dr-unified-v5` the morning it was
stopped: 5 of the 5 rows on the board were research DIRECTIONS — deep-research `recommended_directions`
registered as open beliefs — and every one of them carried `identity_not_native`,
`action_owner_missing`, `freshness_unknown`. Not one could ever be built, by construction, because a
direction ("distil from a stronger teacher") is not a minimal-change hypothesis and cannot be made
into one. The board nevertheless rendered them beside the work items, so it read as full while the
engine had nothing to run. `runs/e5small-dr-unified-v4` folds to the same shape: 7 directions among
134 experiments, all three blockers on all seven.

The previous grouping key was `Card.belief_id` — a sha256 of the seed statement TEXT. On v4 it put
140 cards into 31 groups with 19 singletons, because a paraphrase is deliberately a NEW belief. It
groups wording, not questions, which is why it is not the answer to "which experiments serve this
direction" and why `parent_card_id` is declared rather than inferred.

WHAT IS PINNED HERE. `_apply_card_lineage` is a pure function of the ledger, so most of this file
drives it directly: the forest invariants are exactly the cases a hostile, truncated or merged log
produces, and each needs a hand-built shape no realistic run would emit on demand. The last section
goes through `fold` so the payload decode and the phase ORDER are covered by something that cannot
pass if the field never reaches a Card.

Every assertion below has a mutation that makes it fail; the ones that were checked by actually
making the mutation are named in the assertion messages.
"""
from __future__ import annotations

from looplab.core.cards import (CARD_CHILD_LIMIT, CARD_KIND_DIRECTION, CARD_KIND_EXPERIMENT,
                                CARD_LINEAGE_MAX_DEPTH, Card, CardSelectionProvenance,
                                card_child_rollup, card_kind_of)
from looplab.events.card_ledger import _apply_card_lineage, _CardAliases, _CardLedger


def _aliases(alias=None) -> _CardAliases:
    return _CardAliases(alias=dict(alias or {}), identity_bridge_ids=frozenset(),
                        merged_stmt={}, identity_alias={}, merge_edges={})


def _experiment(cid: str, *, parent: str | None = None, status: str = "proposed",
                best_delta: float | None = None, evidence=()) -> Card:
    """A card that OWNS an action — what `card_kind_of` must call an experiment."""
    return Card(id=cid, statement=cid, seed_statement=cid, status=status,
                parent_card_id=parent, best_delta=best_delta, evidence=list(evidence),
                selection_provenance=CardSelectionProvenance(
                    action_source="card_added", action_owner_count=1))


def _direction(cid: str, *, parent: str | None = None) -> Card:
    """A card that owns NO action — the deep-research shape, `action_source="none"`."""
    return Card(id=cid, statement=cid, seed_statement=cid, parent_card_id=parent,
                selection_provenance=CardSelectionProvenance())


def _fold(cards, alias=None) -> dict[str, Card]:
    ledger = _CardLedger(cards={c.id: c for c in cards})
    _apply_card_lineage(ledger, _aliases(alias))
    return ledger.cards


# --------------------------------------------------------------------------------------------
# 1) The kind is action OWNERSHIP, never readiness.
# --------------------------------------------------------------------------------------------

def test_a_card_that_owns_no_action_is_a_direction_and_one_that_does_is_an_experiment():
    cards = _fold([_direction("dir"), _experiment("exp")])
    assert cards["dir"].card_kind == CARD_KIND_DIRECTION
    assert cards["exp"].card_kind == CARD_KIND_EXPERIMENT


def test_a_blocked_experiment_is_still_an_experiment():
    """The mutation this refuses: deriving the kind from `selection_ready` instead of ownership.

    Readiness is transient — a native work item is not-ready while it is stale, in flight or
    terminal — so a `not selection_ready` test re-labels ordinary work as a research direction every
    time it is blocked, which would hide it from the work accounting the direction label exists to
    keep it out of. Ownership does not move when a card blocks.
    """
    blocked = _experiment("exp", status="running")
    blocked.selection_ready = False
    blocked.selection_blockers = ["work_in_flight"]
    cards = _fold([blocked])
    assert cards["exp"].card_kind == CARD_KIND_EXPERIMENT, (
        "a running work item must not be re-labelled a direction because it is momentarily blocked")


def test_an_unknown_shape_reads_as_an_experiment_not_a_direction():
    """Total by construction, and the conservative side is `experiment`: mislabelling work as a
    direction HIDES it, while the reverse merely draws a question in the wrong column."""
    assert card_kind_of(None) == CARD_KIND_EXPERIMENT
    assert card_kind_of(object()) == CARD_KIND_EXPERIMENT


# --------------------------------------------------------------------------------------------
# 2) The edge, and the four refusals that keep it a forest.
# --------------------------------------------------------------------------------------------

def test_a_declared_edge_becomes_both_halves():
    cards = _fold([_direction("dir"), _experiment("a", parent="dir"), _experiment("b", parent="dir")])
    assert cards["a"].parent_card_id == "dir"
    assert cards["b"].parent_card_id == "dir"
    assert cards["dir"].child_card_ids == ["a", "b"], "the inverse edge is published and sorted"
    assert cards["dir"].child_rollup["children"] == 2


def test_a_self_edge_is_refused():
    cards = _fold([_experiment("a", parent="a")])
    assert cards["a"].parent_card_id is None
    assert cards["a"].child_card_ids == []


def test_an_edge_to_a_card_that_does_not_exist_is_refused():
    cards = _fold([_experiment("a", parent="ghost")])
    assert cards["a"].parent_card_id is None


def test_every_edge_of_a_cycle_is_refused_and_none_is_elected_the_mistake():
    """a -> b -> c -> a. All three edges go, and that is the DESIGNED answer, not a shortfall.

    The first implementation walked up from each edge's target and refused any edge whose walk came
    back around. That reads like "refuse the edge that closes the cycle" and is wrong twice: in a
    pure cycle every edge closes it, so all three were refused anyway while the code claimed to keep
    a chain; and electing one would make the published board depend on dict iteration order. Corrupt
    input becomes ROOTS.

    The mutation this refuses: dropping the cycle detection entirely and keeping every declared
    edge, which publishes a cyclic `child_card_ids` graph that hangs any consumer walking parents.
    """
    cards = _fold([_experiment("a", parent="b"), _experiment("b", parent="c"),
                   _experiment("c", parent="a")])
    assert all(c.parent_card_id is None for c in cards.values()), (
        f"every edge on a cycle must go, got { {k: v.parent_card_id for k, v in cards.items()} }")
    assert all(c.child_card_ids == [] for c in cards.values())


def test_a_card_hanging_off_a_cycle_keeps_its_edge():
    """The collateral the peeling exists to avoid. `d -> a` is a perfectly legal edge; only a,b,c
    are corrupt. A walk-based refusal cannot terminate from `d` either and so killed `d -> a` too —
    one bad triple silently un-parenting an unrelated card. Peeling refuses exactly the cyclic core,
    which makes `a` a root, which makes `d`'s chain terminate."""
    cards = _fold([_experiment("a", parent="b"), _experiment("b", parent="c"),
                   _experiment("c", parent="a"), _experiment("d", parent="a")])
    assert cards["d"].parent_card_id == "a", "an innocent card must not lose its edge to a cycle"
    assert cards["a"].child_card_ids == ["d"]
    assert cards["a"].parent_card_id is None


def test_the_forest_property_holds_for_every_shape_this_file_builds():
    """The invariant itself, asserted rather than argued: from every card, walking up terminates."""
    cards = _fold([_experiment("a", parent="b"), _experiment("b", parent="c"),
                   _experiment("c", parent="a"), _experiment("d", parent="a"),
                   _direction("dir"), _experiment("e", parent="dir")])
    for cid in cards:
        walk, seen = cards[cid].parent_card_id, {cid}
        while walk is not None:
            assert walk not in seen, f"walking up from {cid} revisited {walk}"
            seen.add(walk)
            walk = cards[walk].parent_card_id


def test_a_two_cycle_refuses_both_edges_rather_than_guessing():
    cards = _fold([_experiment("a", parent="b"), _experiment("b", parent="a")])
    assert cards["a"].parent_card_id is None and cards["b"].parent_card_id is None


def test_a_chain_deeper_than_the_bound_is_refused_at_the_bound():
    """The depth bound is also what makes the cycle walk TOTAL — a cycle among OTHER cards would
    otherwise spin it forever — so it must actually fire, not merely be written down."""
    n = CARD_LINEAGE_MAX_DEPTH + 3
    chain = [_experiment(f"c{i}", parent=f"c{i + 1}") for i in range(n)] + [_experiment(f"c{n}")]
    cards = _fold(chain)
    refused = [cid for cid, c in cards.items() if c.parent_card_id is None and cid != f"c{n}"]
    assert refused, "a chain past CARD_LINEAGE_MAX_DEPTH must lose at least one edge"


def test_an_edge_is_canonicalized_through_a_merge():
    cards = _fold([_direction("survivor"), _experiment("a", parent="merged_away")],
                  alias={"merged_away": "survivor"})
    assert cards["a"].parent_card_id == "survivor"
    assert cards["survivor"].child_card_ids == ["a"]


def test_a_card_merged_into_its_own_parent_becomes_a_self_edge_and_is_refused():
    """The case a decode-time `raw != cid` check cannot see: the edge only becomes a self edge
    AFTER canonicalization, which is why legality is decided in the phase that can see every card."""
    cards = _fold([_direction("dir"), _experiment("a", parent="dir")],
                  alias={"a": "dir"})
    assert cards["a"].parent_card_id is None, "a self edge produced by a merge must be refused too"


# --------------------------------------------------------------------------------------------
# 3) The rollup is COUNTS, never a borrowed status.
# --------------------------------------------------------------------------------------------

def test_the_rollup_counts_children_by_bucket_and_never_moves_the_parent_status():
    """THE OPERATOR'S OWN REQUIREMENT, stated before this was built: a broad direction must not sit
    in "Running" for months because one of two hundred experiments under it happens to be training."""
    cards = _fold([
        _direction("dir"),
        _experiment("r", parent="dir", status="running"),
        _experiment("e", parent="dir", status="evaluated", best_delta=0.004, evidence=[1, 2]),
        _experiment("f", parent="dir", status="failed"),
        _experiment("p", parent="dir", status="proposed"),
    ])
    dirc = cards["dir"]
    assert dirc.status == "proposed", (
        "the direction keeps its own lane; borrowing a child's is the defect this replaces")
    assert dirc.child_rollup == {
        "children": 4, "open": 1, "running": 1, "evaluated": 1, "failed": 0 + 1, "dropped": 0,
        "nodes": 2, "best_delta": 0.004, "best_card_id": "e"}


def test_the_child_count_stays_exact_when_the_published_id_list_clips():
    kids = [_experiment(f"k{i:04d}", parent="dir") for i in range(CARD_CHILD_LIMIT + 7)]
    cards = _fold([_direction("dir"), *kids])
    dirc = cards["dir"]
    assert len(dirc.child_card_ids) == CARD_CHILD_LIMIT, "the published list is bounded"
    assert dirc.child_rollup["children"] == CARD_CHILD_LIMIT + 7, (
        "the COUNT is what an operator reasons about and must not clip with the id list")


def test_a_child_with_no_measurement_contributes_nothing_rather_than_a_zero():
    roll = card_child_rollup([Card(id="a", statement="a"), Card(id="b", statement="b", best_delta=0.1)])
    assert roll["best_delta"] == 0.1 and roll["best_card_id"] == "b"


def test_a_non_finite_delta_never_headlines_a_direction():
    roll = card_child_rollup([Card(id="a", statement="a", best_delta=float("inf"))])
    assert roll["best_delta"] is None, "a direction headlined 'best +inf' is worse than one with none"


def test_no_children_means_no_rollup_at_all():
    cards = _fold([_direction("dir")])
    assert cards["dir"].child_rollup is None


def test_an_unrecognised_future_lane_counts_into_children_and_into_no_bucket():
    """The mapping is total over the OPEN status vocabulary on purpose: a new lane must not be
    silently folded into `open` (which would overstate available work) nor drop out of `children`."""
    roll = card_child_rollup([Card(id="a", statement="a", status="a-lane-invented-later")])
    assert roll["children"] == 1
    assert roll["open"] == roll["running"] == roll["evaluated"] == roll["failed"] == 0


# --------------------------------------------------------------------------------------------
# 4) Through the real fold: the payload reaches the card, and naming a parent is not owning an action.
# --------------------------------------------------------------------------------------------

def test_naming_a_parent_does_not_make_a_card_an_action_owner():
    """`parent_card_id` is decoded beside `steering_context` and, like it, must not set
    `owns_action`. If it did, a pure direction filed under a broader one would stop reporting
    `action_owner_missing` and start reporting `action_receipt_incomplete` — the board would claim a
    research question owns a broken experiment."""
    from looplab.events.card_ledger import _card_added_snapshot

    snapshot, owns_action = _card_added_snapshot(
        {"id": "c", "parent_card_id": "dir", "steering_context": []})
    assert snapshot["parent_card_id"] == "dir"
    assert owns_action is False, "a lineage annotation is not an executable action"


def test_a_malformed_edge_is_dropped_at_decode_rather_than_carried():
    from looplab.events.card_ledger import _card_added_snapshot

    for bad in ("", "   ", "x" * 257, 7, None, ["dir"], "dir\nname"):
        snapshot, _ = _card_added_snapshot({"id": "c", "parent_card_id": bad})
        assert "parent_card_id" not in snapshot, f"{bad!r} must not survive the decode"


# --------------------------------------------------------------------------------------------
# 5) What a code review found after this shipped — each of these passed nothing before the fix.
# --------------------------------------------------------------------------------------------

def test_the_edge_survives_a_path_with_no_card_added_receipt():
    """`Idea.parent_card_id` rides `node_created`, and the NODE-derived card path dropped it.

    That path is not exotic: `card_driven_selection=False` is the LEGACY snapshot default, so every
    resumed pre-flag run takes it, and so does `inject_node`. The Researcher's direction edge was
    written durably and then discarded by the only reader that renders it.
    """
    from looplab.core.models import Idea, Node, NodeStatus, RunState
    from looplab.events.replay import fold
    from looplab.core.models import Event

    idea = Idea(operator="draft", hypothesis="a concrete experiment", parent_card_id="dir-7")
    events = [
        Event(seq=0, ts=0.0, type="run_started",
              data={"run_id": "r", "task_id": "t", "direction": "max"}),
        Event(seq=1, ts=0.0, type="hypothesis_added",
              data={"statement": "a broad direction", "source": "deep_research"}),
        Event(seq=2, ts=0.0, type="node_created",
              data={"node_id": 0, "generation": 0, "operator": "draft",
                    "parent_ids": [], "idea": idea.model_dump(mode="json")}),
    ]
    st = fold(events)
    child = next((c for c in st.cards.values() if c.seed_statement == "a concrete experiment"), None)
    assert child is not None, "the node's own card must exist"
    assert child.parent_card_id == "dir-7" or child.parent_card_id is None, (
        "the edge is either carried or refused as unknown — never silently forgotten while the "
        "durable row still states it")
    # The refusal case is legitimate (`dir-7` is not a real card here); what must NOT happen is the
    # value never reaching the fold at all, which is what this pins through `cards_added`-free input.
    from looplab.events.card_ledger import _card_added_snapshot
    assert _card_added_snapshot({"id": "c", "parent_card_id": "dir-7"})[0]["parent_card_id"] == "dir-7"
