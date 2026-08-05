"""One owner for `_propose_batch`'s three-attribute result protocol (doc 25 ES-08).

`_propose_batch` (novelty.py) does not return its results. It signals them through three instance
attributes — `_pending_batch_telemetry`, `_pending_batch_dropped`, `_pending_batch_novelty_gated` —
and two call sites read that protocol by hand: `run`'s concurrent-build chunk and
`_stage_card_creates`.

Two rules in that hand-written reading are load-bearing and silent when wrong:

* **telemetry must be PADDED to align 1:1 with the ideas.** A short list shifts every later idea's
  telemetry onto the wrong node, so a build emits another proposal's
  hypothesis_ranked/foresight_selected receipts.
* **`dropped` must be SNAPSHOTTED before anyone resets the attribute**, because each caller resets at
  the point its own durability ordering requires — `run` after the reservations are durable,
  `_stage_card_creates` in a `finally`.

Neither shows up as an exception; both show up as a run whose audit trail describes work it did not
do. So the protocol has one owner now, and these pin it.
"""
from __future__ import annotations

import inspect

import pytest

from looplab.core.models import Idea
from looplab.engine.orchestrator import Engine


class _Host:
    """The narrowest possible `self`: only what the two helpers actually touch."""

    _consume_batch_proposal = Engine._consume_batch_proposal
    _record_dropped_batch_cards = Engine._record_dropped_batch_cards

    def __init__(self, ideas, *, telemetry=None, dropped=None):
        self._ideas = ideas
        self._telemetry = telemetry
        self._dropped = dropped
        self.proposed: list[tuple[object, int]] = []
        self.recorded: list[dict] = []

    def _propose_batch(self, state, width):
        self.proposed.append((state, width))
        if self._telemetry is not None:
            self._pending_batch_telemetry = list(self._telemetry)
        if self._dropped is not None:
            self._pending_batch_dropped = list(self._dropped)
        return list(self._ideas)

    def _record_node_less_card(self, idea, *, reason, steering_context):
        self.recorded.append({"idea": idea, "reason": reason, "steering": steering_context})


def _idea(text="an idea"):
    return Idea(operator="improve", params={}, rationale=text)


# ------------------------------------------------------------------ the consume protocol

def test_the_batch_is_proposed_exactly_once_at_the_requested_width():
    host = _Host([_idea(), _idea()])
    ideas, _telemetry, _dropped = host._consume_batch_proposal("the-state", 2)
    assert host.proposed == [("the-state", 2)]
    assert len(ideas) == 2


def test_short_telemetry_is_PADDED_so_it_aligns_one_to_one_with_the_ideas():
    """The silent one. `zip(chunk, ideas, telemetry)` truncates to the SHORTEST — a short telemetry
    list therefore drops the tail of the batch entirely, so ideas that were proposed and gated never
    get built at all."""
    host = _Host([_idea("a"), _idea("b"), _idea("c")], telemetry=[{"x": 1}])
    ideas, telemetry, _dropped = host._consume_batch_proposal(None, 3)
    assert len(telemetry) == len(ideas) == 3
    assert telemetry[0] == {"x": 1} and telemetry[1:] == [None, None]


def test_absent_telemetry_becomes_one_None_per_idea():
    host = _Host([_idea(), _idea()])
    _ideas, telemetry, _dropped = host._consume_batch_proposal(None, 2)
    assert telemetry == [None, None]


def test_longer_telemetry_is_left_alone_rather_than_truncated():
    """Over-length telemetry means the producer knows something the consumer does not; silently
    cutting it would hide that rather than let the `zip` bound it."""
    host = _Host([_idea()], telemetry=[{"a": 1}, {"b": 2}])
    _ideas, telemetry, _dropped = host._consume_batch_proposal(None, 1)
    assert len(telemetry) == 2


@pytest.mark.parametrize("attribute", ["_pending_batch_telemetry", "_pending_batch_dropped"])
def test_the_results_are_SNAPSHOTTED_not_aliased(attribute):
    """Each caller resets the attributes at the point its own durability ordering requires, and
    `run`'s reset happens BEFORE the drop loop it feeds.

    The reset as written rebinds (`self._pending_batch_* = []`), which an alias would survive — so
    this is specifically about IN-PLACE mutation: a `.clear()` here, or a producer that reuses its
    buffer between batches, would empty the very list the caller is about to record from, and every
    rejected proposal would silently lose its node-less Card. Returning a copy makes that
    unreachable rather than merely unlikely, which is why the identity is asserted directly.
    """
    host = _Host([_idea()], telemetry=[{"a": 1}], dropped=[{"idea": _idea(), "reason": "r"}])
    _ideas, telemetry, dropped = host._consume_batch_proposal(None, 1)
    returned = {"_pending_batch_telemetry": telemetry, "_pending_batch_dropped": dropped}[attribute]
    assert returned is not getattr(host, attribute), f"{attribute} was aliased, not snapshotted"
    getattr(host, attribute).clear()
    assert returned, "an in-place clear of the producer's buffer emptied the caller's snapshot"


def test_a_missing_attribute_is_an_empty_result_not_an_AttributeError():
    """`_propose_batch` may fail before it sets anything; the consumer must still produce a usable
    (empty) batch rather than crash the loop."""
    host = _Host([])
    assert host._consume_batch_proposal(None, 0) == ([], [], [])


# ------------------------------------------------------------------ the drop loop

def test_every_rejected_proposal_gets_its_node_less_card():
    host = _Host([])
    first, second = _idea("one"), _idea("two")
    host._record_dropped_batch_cards([
        {"idea": first, "reason": "novelty", "steering_context": ["ctx"]},
        {"idea": second, "reason": "budget"},
    ])
    assert [row["idea"] for row in host.recorded] == [first, second]
    assert host.recorded[0]["steering"] == ["ctx"] and host.recorded[1]["steering"] == []


def test_a_missing_or_blank_reason_defaults_rather_than_reading_as_no_cause():
    """A card whose reason silently became "" reads on the board as a drop with no cause."""
    host = _Host([])
    host._record_dropped_batch_cards([{"idea": _idea(), "reason": ""},
                                      {"idea": _idea(), "reason": None},
                                      {"idea": _idea()}])
    assert {row["reason"] for row in host.recorded} == {"proposal_rejected"}


def test_the_reason_is_truncated_to_the_shared_bound():
    host = _Host([])
    host._record_dropped_batch_cards([{"idea": _idea(), "reason": "x" * 500}])
    assert len(host.recorded[0]["reason"]) == 160


@pytest.mark.parametrize("junk", [None, [], [None], ["not a dict"], [{}], [{"idea": "not an Idea"}],
                                  [{"reason": "no idea key"}]])
def test_junk_drop_rows_are_skipped_without_raising(junk):
    """The dropped list crosses from the proposal layer; a malformed row must cost one card, not the
    whole batch's audit trail."""
    host = _Host([])
    host._record_dropped_batch_cards(junk)
    assert host.recorded == []


def test_a_junk_row_does_not_stop_the_valid_ones_after_it():
    host = _Host([])
    good = _idea("survivor")
    host._record_dropped_batch_cards(["junk", {"idea": good, "reason": "r"}])
    assert [row["idea"] for row in host.recorded] == [good]


# ------------------------------------------------------------------ both call sites, and the trap

# `run` is a thin broker wrapper, and doc 25 ES-05 lifted the whole `creates` branch out of
# `_run_with_llm_broker` again — so the concurrent-build chunk now lives in `_handle_create_actions`.
# This tuple must MOVE with it. Scanning the method the chunk has left is not a weaker guard, it is
# no guard at all, and re-pointing it is only legitimate after re-checking that the property still
# holds where the code went (it does: that helper calls both shared owners and hand-reads neither
# attribute).
_CALL_SITES = ("_handle_create_actions", "_stage_card_creates")


@pytest.mark.parametrize("method", _CALL_SITES)
def test_both_call_sites_go_through_the_shared_consume(method):
    source = inspect.getsource(getattr(Engine, method))
    assert "_consume_batch_proposal(" in source, f"{method} reads the attribute protocol by hand"
    # Assigning the attributes back to [] is each caller's own reset, which deliberately stays; what
    # must not come back is READING them.
    assert 'getattr(self, "_pending_batch_telemetry"' not in source, (
        f"{method} reads the telemetry attribute directly again")
    assert 'getattr(self, "_pending_batch_dropped"' not in source, (
        f"{method} reads the dropped attribute directly again")


@pytest.mark.parametrize("method", _CALL_SITES)
def test_neither_call_site_re_inlines_the_drop_loop(method):
    source = inspect.getsource(getattr(Engine, method))
    assert "_record_dropped_batch_cards(" in source
    assert "proposal_rejected" not in source, f"{method} re-derives the default reason"


def test_the_staticmethods_around_the_new_helpers_are_still_staticmethods():
    """A near-miss worth a permanent guard: inserting a method between `@staticmethod` and its `def`
    re-decorates the NEW function and silently demotes the old one to an instance method, so its
    first argument becomes `self`. It surfaced as `_node_id_ceiling() takes 2 positional arguments
    but 3 were given` — 57 tests red, and only because that helper happens to be called everywhere.
    """
    for name in ("_node_id_ceiling", "_canonical_card_id"):
        assert isinstance(inspect.getattr_static(Engine, name), staticmethod), (
            f"Engine.{name} lost its @staticmethod — check for a method inserted under its decorator")
    for name in ("_consume_batch_proposal", "_record_dropped_batch_cards"):
        assert not isinstance(inspect.getattr_static(Engine, name), staticmethod)
        assert list(inspect.signature(getattr(Engine, name)).parameters)[0] == "self"
