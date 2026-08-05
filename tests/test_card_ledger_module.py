"""The Card ledger is a LEAF of `events/` with an ordered, order-tolerant derivation (doc 25 EV-01).

`_derive_cards` used to be an ~850-line function inside `events/replay.py`, together with ~2,200
lines of Card machinery in a 5,800-line fold module. It is now `events/card_ledger.py`, and the
split introduces exactly two things that can silently rot:

* **the import direction.** `replay` imports `card_ledger`; `card_ledger` may not import `replay`.
  It is not a style preference — five names crossed that boundary before the split, and the reason
  `coerce_node_id` / `INHERITABLE_CONCEPT_PROVENANCE` moved down to `core/models.py` is that a
  module-level import back into `replay` is a hard ImportError at startup and a function-local one
  is a hidden cycle nobody notices until something else imports in the other order.
* **the phase ORDER.** Nine numbered phases that used to be nine blocks of one function are nine
  calls now, and a call is trivially reorderable in a way a block never was. The order is
  load-bearing at four points at least: identity is decided over the WHOLE log before any Card
  exists, the merge fold unions evidence before verdicts read it, the operator overlay wins over
  enrichment and ranking (docs/23 decision 27), and `actionable`/`selection_ready` read the FINAL
  status.

The AST pins below are the cheap half. The behavioural drivers under them are what actually holds
the line: a permuted log that must fold to the same Cards, an operator pin that must beat a rank,
and an identity conflict created only by the LAST event in the log.
"""
from __future__ import annotations

import ast
import inspect
import random

from looplab.core.models import Event
from looplab.events import card_ledger, replay
from looplab.events.replay import fold

from tests._source_scan import called_names

# The derivation's phases, in the one order `derive_cards` may run them.
_PHASES = [
    "_CardLedger",
    "_card_identity_map",
    "_seed_cards_from_receipts",
    "_link_cards_to_nodes",
    "_card_merge_aliases",
    "_card_control_ids",
    "_fold_merged_cards",
    "_apply_card_verdicts",
    "_apply_card_drops",
    "_card_building_ids",
    "_apply_card_status",
    "_apply_card_enrichment",
    "_apply_card_ranking",
    "_apply_card_operator_overlays",
    "_apply_card_actionable",
    "_apply_card_selection_readiness",
    "_publish_visible_cards",
]


# ------------------------------------------------------------------ the import direction

def test_the_card_ledger_never_imports_the_fold_module():
    """A module-level edge back to `replay` is an ImportError at startup; a function-local one is a
    cycle that only shows up under a different import order. Neither may exist, so this walks EVERY
    import node in the module rather than reading the header."""
    tree = ast.parse(inspect.getsource(card_ledger))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("looplab.events"):
            offenders.append(f"from {node.module} import ...")
        elif isinstance(node, ast.Import):
            offenders += [a.name for a in node.names if a.name.startswith("looplab.events")]
    assert not offenders, f"card_ledger reaches back into events: {offenders}"


def test_the_card_ledger_imports_nothing_above_core():
    """`events` may import only `core` — and this module is the one that had to push two shared fold
    primitives DOWN into `core/models.py` to keep that true."""
    tree = ast.parse(inspect.getsource(card_ledger))
    modules = {node.module for node in ast.walk(tree)
               if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("looplab")}
    modules |= {a.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                for a in node.names if a.name.startswith("looplab")}
    assert all(m.startswith("looplab.core.") for m in modules), sorted(modules)


def test_the_fold_reaches_the_ledger_through_exactly_one_entry_point():
    assert replay._derive_cards is card_ledger.derive_cards
    entries = [name for name in called_names(replay._finalize_fold) if "derive_cards" in name]
    assert entries == ["_derive_cards"], entries


# ------------------------------------------------------------------ the phase order

def test_the_derivation_runs_its_phases_in_the_one_order_that_is_correct():
    """AST, not substrings: a commented-out call is not an `ast.Call` node, so this cannot be
    satisfied by leaving the phase name behind in a comment while dropping the call."""
    called = [name for name in called_names(card_ledger.derive_cards) if name in set(_PHASES)]
    assert called == _PHASES, f"phase order changed:\n  got  {called}\n  want {_PHASES}"


def test_no_phase_is_orphaned_or_called_twice():
    """A phase defined and never reached is a silently dead rule; one reached twice re-applies it."""
    called = [name for name in called_names(card_ledger.derive_cards) if name in set(_PHASES)]
    assert sorted(called) == sorted(set(called))
    defined = {node.name for node in ast.walk(ast.parse(inspect.getsource(card_ledger)))
               if isinstance(node, (ast.FunctionDef, ast.ClassDef))
               and (node.name.startswith(("_apply_card", "_card_", "_seed_cards", "_link_cards",
                                          "_fold_merged", "_publish_visible")))}
    unreached = {name for name in defined if name in set(_PHASES)} - set(called)
    assert not unreached, f"phase functions never reached from derive_cards: {sorted(unreached)}"


# ------------------------------------------------------------------ the behavioural drivers

def _ev(seq: int, kind: str, **data) -> Event:
    return Event(v=1, seq=seq, ts=float(seq), type=kind, data=data)


def _independent_card_log() -> list[Event]:
    """A board built only from events with no lifecycle ordering between them.

    Every row here names a different work item or is applied by an explicit sequence number, so a
    permutation of the tail cannot legitimately change one derived Card. That includes the two
    conflicting merge receipts (X->A and X->B), which is the exact shape that broke invariant 5 once
    already: last-write-wins there sent X to A or to B depending only on byte order.
    """
    rows = [
        ("card_added", dict(id="A", statement="alpha", rationale="a", source="researcher")),
        ("card_added", dict(id="B", statement="beta", rationale="b", source="researcher")),
        ("card_added", dict(id="X", statement="gamma", rationale="g", source="researcher")),
        ("card_added", dict(id="D", statement="delta", rationale="d", source="researcher")),
        ("hypothesis_added", dict(statement="epsilon", rationale="e", source="human")),
        ("card_merged", dict(canonical="A", aliases=["X"], statement="alpha+gamma")),
        ("card_merged", dict(canonical="B", aliases=["X"], statement="beta+gamma")),
        ("card_dropped", dict(id="D", reason="operator says no", by="operator")),
        ("card_enriched", dict(id="A", confidence=0.5, lesson_refs=["lesson:one"])),
        ("card_enriched", dict(id="B", foresight_rank=2)),
        ("card_edited", dict(id="A", statement="the operator's display statement")),
        ("card_reprioritized", dict(id="B", priority=7)),
        ("card_ranked", dict(order=["B", "A"], confidence=0.25)),
    ]
    return ([_ev(0, "run_started", run_id="r", task_id="t", goal="g", direction="max")]
            + [_ev(i + 1, kind, **data) for i, (kind, data) in enumerate(rows)])


def _dump(state) -> list[tuple[str, dict]]:
    return [(cid, card.model_dump(mode="json")) for cid, card in state.cards.items()]


def test_a_board_of_independent_events_folds_identically_under_every_permutation():
    """Engine invariant 5, driven. 200 shuffles of the tail; every one must produce byte-identical
    Cards in the same order.

    This is the test that a phase decomposition can genuinely break: a phase that reads a dict built
    by an earlier phase in ITERATION order, or one moved past the phase that canonicalizes ids, goes
    order-dependent without any assertion elsewhere in the suite noticing.
    """
    events = _independent_card_log()
    expected = _dump(fold(events))
    assert len(expected) >= 4, expected           # the board is non-trivial

    rng = random.Random(20260805)
    for i in range(200):
        shuffled = events[:1] + rng.sample(events[1:], len(events) - 1)
        assert _dump(fold(shuffled)) == expected, f"permutation {i} folded to different Cards"


def test_the_operator_overlay_still_wins_over_the_ranking_phase():
    """docs/23 decision 27, and the reason `_apply_card_operator_overlays` runs AFTER
    `_apply_card_ranking`: swap those two calls and the rank silently overwrites the pin."""
    events = [
        _ev(0, "run_started", run_id="r", task_id="t", goal="g", direction="max"),
        _ev(1, "card_added", id="A", statement="alpha"),
        _ev(2, "card_added", id="B", statement="beta"),
        _ev(3, "card_ranked", order=["A", "B"]),
        _ev(4, "card_reprioritized", id="A", priority=99, source="operator", pinned=True),
    ]
    cards = fold(events).cards
    assert cards["A"].priority == 99 and cards["A"].pinned is True
    assert cards["A"].foresight_rank == 0         # the rank itself is untouched — only priority loses
    assert cards["B"].priority == 1               # the unpinned card keeps its ranked position


def test_identity_is_decided_over_the_whole_log_before_any_card_is_built():
    """`_card_identity_map` runs FIRST and scans all three durable sources. Here the conflict is
    created by the LAST event: a node whose `idea.card_id` reuses `dup` for a second statement. A
    phase that decided identity while seeding would have already materialized the first card."""
    events = [
        _ev(0, "run_started", run_id="r", task_id="t", goal="g", direction="max"),
        _ev(1, "card_added", id="dup", statement="the first statement"),
        _ev(2, "card_added", id="safe", statement="an unrelated statement"),
        _ev(3, "node_created", node_id=1, operator="draft",
            idea={"operator": "draft", "hypothesis": "a second statement", "card_id": "dup"}),
    ]
    cards = fold(events).cards
    assert "dup" not in cards, "an id claimed by two statements must materialize no Card at all"
    assert "safe" in cards
