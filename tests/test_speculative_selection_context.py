"""The speculative-selection SESSION is declared once, and its asymmetry is deliberate (doc 25 SE-14).

Five call layers used to re-declare and forward the same five keywords. That is not merely verbose:
one field (`consumed_inflight`) had already been added to only THREE of them, and the difference —
which is load-bearing — existed only as an absence in a parameter list nobody reads side by side.

`SpeculativeSelectionContext` makes the session one object, so:

  * adding a session field is one edit, not five;
  * the asymmetry becomes a VISIBLE unset field at a caller instead of a missing keyword.

Both halves are pinned here. The second one matters more than the first: the tidiest-looking edit a
future reader can make to this code is "election and freshness should obviously use the same
population", and that edit is wrong.
"""
from __future__ import annotations

import dataclasses
import inspect

import pytest

from looplab.core.models import (
    Card,
    CardIdentityProvenance,
    CardSelectionProvenance,
    Idea,
    Node,
    NodeStatus,
    RunState,
)
from looplab.search.card_selection import (
    NO_SPECULATIVE_CONTEXT,
    SpeculativeSelectionContext,
    speculative_card_actions,
    speculative_card_is_fresh,
    speculative_card_selection_set,
    speculative_raw_actions,
)

_DIGEST = "card-action:v1:" + "5" * 64

# The five fields one producer/consumer session holds constant across every query it makes.
SESSION_FIELDS = ("scoring", "excluded_card_ids", "ignored_pending_node_ids",
                  "resource_envelope", "consumed_inflight")
ENTRY_POINTS = (speculative_card_selection_set, speculative_card_actions,
                speculative_raw_actions, speculative_card_is_fresh)


# --------------------------------------------------------------- the session is declared once

def test_no_entry_point_re_declares_a_session_field():
    """The whole point: adding a sixth session field must be ONE edit, in the dataclass."""
    for fn in ENTRY_POINTS:
        params = set(inspect.signature(fn).parameters)
        leaked = params.intersection(SESSION_FIELDS)
        assert leaked == set(), (
            f"{fn.__name__} re-declares {sorted(leaked)}; that is what SE-14 removed")
        assert "context" in params, f"{fn.__name__} lost its session parameter"


def test_the_context_declares_exactly_the_session_and_nothing_per_call():
    """The per-call SUBJECT stays an ordinary argument on purpose — it is the ONE thing that
    distinguishes the entry points from each other, so burying it in the session would hide it."""
    fields = tuple(f.name for f in dataclasses.fields(SpeculativeSelectionContext))
    assert fields == SESSION_FIELDS
    for subject in ("card_id", "node_id", "include_owned_card_id", "include_owned_node_id"):
        assert subject not in fields, f"{subject} is per-call and must not live in the session"

    # ...and each entry point still names the subject it is about.
    assert {"include_owned_card_id", "include_owned_node_id"}.issubset(
        inspect.signature(speculative_card_selection_set).parameters)
    assert {"card_id", "node_id"}.issubset(
        inspect.signature(speculative_card_is_fresh).parameters)
    # The raw lane has no subject at all; it is a counterfactual over the whole population.
    assert not {"card_id", "node_id", "include_owned_card_id", "include_owned_node_id"} & set(
        inspect.signature(speculative_raw_actions).parameters)


def test_the_shared_empty_session_cannot_be_mutated_by_one_caller():
    """`NO_SPECULATIVE_CONTEXT` is a module-level default shared by every default-arg call site. If
    it were writable, one caller stamping a field on it would silently re-aim every other lane."""
    assert NO_SPECULATIVE_CONTEXT == SpeculativeSelectionContext()
    assert all(getattr(NO_SPECULATIVE_CONTEXT, name) in (None, (), frozenset())
               for name in SESSION_FIELDS)
    with pytest.raises(dataclasses.FrozenInstanceError):
        NO_SPECULATIVE_CONTEXT.excluded_card_ids = {"anything"}


# --------------------------------------------------- the asymmetry election/freshness must keep

def _node(node_id: int, *, parents: tuple[int, ...] = (), operator: str = "draft",
          status: NodeStatus = NodeStatus.evaluated, metric: float | None = 0.5,
          card_id: str | None = None) -> Node:
    return Node(
        id=node_id,
        parent_ids=list(parents),
        operator=operator,
        idea=Idea(operator=operator, card_id=card_id, hypothesis=f"h {card_id or node_id}"),
        status=status,
        metric=metric,
    )


def _card(card_id: str) -> Card:
    return Card(
        id=card_id,
        statement=f"proposal {card_id}",
        seed_statement=f"proposal {card_id}",
        source="engine",
        status="proposed",
        verdict="open",
        identity=CardIdentityProvenance(
            kind="native", source="card_added_receipt", durable=True,
            receipt_valid=True, action_digest=_DIGEST,
        ),
        selection_provenance=CardSelectionProvenance(
            action_source="card_added", action_owner_count=1, action_complete=True,
            freshness="current", owner_state="none",
        ),
        selection_blockers=[],
        selection_ready=True,
        operator="improve",
        parent_id=0,
        parent_ids=[0],
        parent_generations={"0": 0},
        scored_against=0,
        scored_against_generation=0,
        scored_against_empty=False,
    )


def _owned(card: Card, node_id: int) -> Card:
    return card.model_copy(deep=True, update={
        "status": "running",
        "verdict": "testing",
        "evidence": [node_id],
        "selection_provenance": card.selection_provenance.model_copy(
            update={"owner_state": "in_flight"}),
        "selection_blockers": ["work_in_flight"],
        "selection_ready": False,
    })


class _OneSlotPolicy:
    n_seeds = 0
    debug_depth = 0
    card_select_k = 1

    def next_actions(self, _state):
        return [{"kind": "improve", "parent_id": 0}]

    def card_score(self, _state, card, *, scoring):
        del scoring
        return 0, (2.0 if card.id == "strong" else 1.0,)


def _two_owned_siblings() -> RunState:
    """Two committed speculative siblings competing for one K=1 backlog slot."""
    strong = _node(2, parents=(0,), operator="improve", status=NodeStatus.pending,
                   metric=None, card_id="strong",
                   ).model_copy(update={"speculative": True, "card_build_generation": 7})
    weak = _node(3, parents=(0,), operator="improve", status=NodeStatus.pending,
                 metric=None, card_id="weak",
                 ).model_copy(update={"speculative": True, "card_build_generation": 8})
    return RunState(
        nodes={0: _node(0, metric=0.9), 2: strong, 3: weak},
        best_node_id=0,
        cards={"strong": _owned(_card("strong"), 2), "weak": _owned(_card("weak"), 3)},
        speculative_nodes={
            2: {"card_id": "strong", "generation": 7},
            3: {"card_id": "weak", "generation": 8},
        },
    )


_SHARED = dict(excluded_card_ids={"strong", "weak"}, ignored_pending_node_ids={2, 3})


def test_the_freshness_gate_reads_consumed_inflight_and_changes_its_answer():
    """The consumer runs this AFTER admitting an attempt, so an admitted sibling no longer competes
    for the next slot — the weaker Card becomes fresh precisely because the stronger one is busy."""
    state = _two_owned_siblings()
    subject = dict(card_id="weak", node_id=3)

    assert speculative_card_is_fresh(
        state, _OneSlotPolicy(), 5,
        context=SpeculativeSelectionContext(**_SHARED), **subject,
    ) is False
    assert speculative_card_is_fresh(
        state, _OneSlotPolicy(), 5,
        context=SpeculativeSelectionContext(consumed_inflight={(2, 0)}, **_SHARED), **subject,
    ) is True


@pytest.mark.parametrize("elect", [speculative_card_actions, speculative_raw_actions])
def test_election_is_invariant_to_consumed_inflight_on_purpose(elect):
    """The mirror image, and the reason the field stays unset at the two election call sites:
    election runs BEFORE the consumer admits anything, so from its vantage point those attempts have
    not happened. Honouring `consumed_inflight` here would mask work that is not yet in flight."""
    state = _two_owned_siblings()

    blind = elect(state, _OneSlotPolicy(), 5, context=SpeculativeSelectionContext(**_SHARED))
    told = elect(state, _OneSlotPolicy(), 5,
                 context=SpeculativeSelectionContext(consumed_inflight={(2, 0)}, **_SHARED))
    assert blind == told, (
        f"{elect.__name__} started reading consumed_inflight; election runs before admission")


def test_consumed_inflight_is_structurally_unreachable_without_a_named_subject():
    """The invariance above is a sample; this is the mechanism, and it is what actually holds.

    `_speculative_selection` may read `consumed_inflight` ONLY inside the branch guarded by a named
    owned subject. Election passes no subject, so the field is unreachable for it BY CONSTRUCTION
    rather than by the fixture happening not to notice. Any "let's make the lanes consistent" edit
    has to move a read out from under that guard, and that is what this catches — including edits
    whose effect a behavioural probe over one state would miss."""
    import ast
    import textwrap

    from looplab.search import card_selection

    source = textwrap.dedent(inspect.getsource(card_selection._speculative_selection))
    tree = ast.parse(source)

    guarded: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
        if not {"include_owned_card_id", "include_owned_node_id"}.issubset(names):
            continue
        for stmt in node.body:
            for inner in ast.walk(stmt):
                guarded.add(id(inner))

    reads = [n for n in ast.walk(tree)
             if isinstance(n, ast.Name) and n.id == "consumed_inflight"
             and isinstance(n.ctx, ast.Load)]
    assert reads, "the field is no longer read at all; the freshness gate lost its admission input"
    ungated = [n.lineno for n in reads if id(n) not in guarded]
    assert ungated == [], (
        "consumed_inflight is read outside the named-subject guard at "
        f"{ungated} — election would now see consumer admissions that have not happened yet")


def test_election_and_freshness_do_see_different_populations():
    """Stated as one assertion so the asymmetry cannot be read as an accident of two separate tests:
    hand the SAME session to both lanes and they legitimately disagree about the same Card."""
    state = _two_owned_siblings()
    context = SpeculativeSelectionContext(consumed_inflight={(2, 0)}, **_SHARED)

    elected = speculative_card_selection_set(state, _OneSlotPolicy(), 5, context=context)
    fresh = speculative_card_is_fresh(state, _OneSlotPolicy(), 5, context=context,
                                      card_id="weak", node_id=3)
    # Election sees both siblings excluded and nothing left to elect...
    assert elected == []
    # ...while the freshness gate reopens the subject against a population where the admitted
    # sibling no longer competes, and keeps it alive.
    assert fresh is True
