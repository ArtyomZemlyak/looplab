"""Two seams where a name was written twice and nothing checked the two agreed (doc 25 SC-16, SE-06).

* SC-16 — every `CONTROL_SPECS` entry spelled its event type twice, as the mapping key and as
  `ControlSpec.event_type`. A copy-paste could pair one event's key with another's spec, and nothing
  asserted otherwise.
* SE-06 — the merged-alias-id derivation appeared byte-identically at two sites in
  `card_selection.py`, each preceded by its own copy of the explanation for WHY alias membership
  (not `Card.merged_into`) is the proof of a merge.
"""
from __future__ import annotations

import inspect

import pytest

from looplab.core.models import Card, RunState
from looplab.search.card_selection import merged_alias_ids
from looplab.serve.protocol import CONTROL_EVENTS
from looplab.serve.run_commands import CONTROL_SPECS


# ------------------------------------------------------------------ SC-16: the control registry

def test_every_spec_carries_the_event_type_it_is_keyed_under():
    """The mismatch this makes structurally impossible would not raise anywhere. It would surface as
    a control running under the WRONG engine policy — a NO_SPAWN intent waking a dead engine, or an
    ENSURE_RUNNING command quietly never spawning one."""
    mismatched = [key for key, spec in CONTROL_SPECS.items() if key != spec.event_type]
    assert not mismatched, f"spec keyed under a different event type: {mismatched}"


def test_the_event_type_is_spelled_once_per_entry():
    from looplab.serve import run_commands

    source = inspect.getsource(run_commands)
    # Scoped to the ENTRIES, not the comprehension below them that stamps the key onto each spec.
    entries = source[source.index("_CONTROL_POLICIES: dict[str, tuple[EnginePolicy, str]] = {"):
                     source.index("CONTROL_SPECS: dict[str, ControlSpec] = {")]
    assert "ControlSpec(" not in entries, "an entry constructs its own spec again"
    assert "_spec(" not in entries, "the pass-through constructor came back"
    assert entries.count("EV_") == entries.count(":") - 1, (
        "an entry names its event type more than once — the mismatch this rewrite removed")


def test_every_control_event_still_makes_an_explicit_choice():
    """The registry's own invariant, unchanged by the rewrite: a new `CONTROL_EVENTS` member must
    choose an engine policy rather than fall into an unsafe default."""
    assert set(CONTROL_SPECS) == set(CONTROL_EVENTS)


def test_the_deletion_pass_through_alias_is_gone():
    """`_storage_pending` was `return _pending(...)` — a second name for one behaviour, used
    interchangeably with the first, so a reader had to check whether they differed."""
    from looplab.serve import deletion_service

    assert not hasattr(deletion_service, "_storage_pending")
    assert hasattr(deletion_service, "_pending")


# ------------------------------------------------------------------ SE-06: merged alias ids

def _card(state: RunState, card_id: str, *, aliases=()) -> Card:
    card = Card(id=card_id, statement=f"card {card_id}")
    if aliases:
        card.aliases = list(aliases)
    state.cards[card_id] = card
    return card


def test_a_merged_id_is_proven_by_ALIAS_MEMBERSHIP_not_by_merged_into():
    """The fold collapses a merged Card OUT of `state.cards` and records only its id in the
    canonical's `.aliases`; `Card.merged_into` is never actually assigned. Keying on `merged_into`
    would find nothing and treat every merged sibling as a corrupt chain."""
    state = RunState()
    canonical = _card(state, "c1", aliases=["c2", "c3"])
    assert merged_alias_ids(state) == {"c2", "c3"}
    assert canonical.merged_into is None, "the fold does not assign merged_into"
    assert "c2" not in state.cards, "a merged card is ABSENT, not marked"


def test_an_absent_id_that_is_NOT_an_alias_is_not_reported_as_merged():
    """The fail-closed half. An absent id with no merge receipt is a corrupt or partial ownership
    chain, and the caller must NOT skip it — it has to reach `pairs` so the counterfactual fails
    closed instead of proceeding on an incomplete picture."""
    state = RunState()
    _card(state, "c1", aliases=["c2"])
    assert "c9" not in merged_alias_ids(state)


@pytest.mark.parametrize("junk", [None, "", 0, [], {}, 5])
def test_a_non_string_or_empty_alias_is_ignored(junk):
    """A hostile or partially-written alias list must not put a falsy id into a membership set that
    decides whether a sibling is skipped."""
    state = RunState()
    card = _card(state, "c1")
    card.aliases = [junk, "c2"]
    assert merged_alias_ids(state) == {"c2"}


def test_a_card_with_no_alias_attribute_at_all_is_tolerated():
    """`getattr(card, "aliases", None)` — an older persisted Card shape has no such field, and a
    replay of an old log must not raise inside a selection helper."""
    state = RunState()

    class _Old:
        pass

    state.cards["c1"] = _Old()
    assert merged_alias_ids(state) == frozenset()


def test_neither_selection_site_re_derives_the_set():
    from looplab.search import card_selection

    for name in ("_counterfactual_owned_selection_state", "_reserved_speculative_slots"):
        source = inspect.getsource(getattr(card_selection, name))
        assert "merged_alias_ids(state)" in source, f"{name} does not use the shared helper"
        assert 'getattr(card, "aliases"' not in source, f"{name} re-derives the alias set"


def test_the_derivation_exists_in_exactly_one_place():
    from looplab.search import card_selection

    source = inspect.getsource(card_selection)
    assert source.count('for alias in (getattr(card, "aliases", None) or [])') == 1
