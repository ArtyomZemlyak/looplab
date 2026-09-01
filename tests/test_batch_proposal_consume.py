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

import ast
import inspect

import pytest

from looplab.core.models import Idea
from looplab.engine.orchestrator import Engine
from tests._source_scan import called_names, function_tree


class _Host:
    """The narrowest possible `self`: only what the two helpers actually touch."""

    _consume_batch_proposal = Engine._consume_batch_proposal
    _record_dropped_batch_cards = Engine._record_dropped_batch_cards
    # `_consume_batch_proposal` brackets the proposal with the live-progress beacon, so the narrowest
    # possible `self` now has to carry it too. Borrowed from the real Engine rather than stubbed, for
    # the reason the two above are: a stub that answers differently from the method under test is how
    # a protocol test stops testing the protocol. It needs no `store` — `_progress` emits nothing when
    # `self` has none, which is exactly this host and also every `Engine.__new__` instance in the suite.
    _progress = Engine._progress

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

# `run` is a thin broker wrapper, and the concurrent-build chunk moved out of
# `_run_with_llm_broker` into the `_handle_create_actions` phase helper (doc 25 ES-05). Re-pointed
# rather than dropped: the property is "whichever method owns the chunk goes through the shared
# consume", so it follows the code instead of naming a location that has already moved once.
# Re-pointing is only legitimate after re-checking the property where the code WENT — scanning a
# method the chunk has left is not a weaker guard, it is no guard at all.
_CALL_SITES = ("_handle_create_actions", "_stage_card_creates")

# RE-POINTED 2026-08-31, at the AWAITED WRAPPER, and the chain is pinned in TWO links rather than
# one. `56764cbd` routed both call sites through `Engine._await_batch_proposal` — the offload that
# took the minutes-long paid batch proposal off the event-loop thread — and did not move this guard
# in the same change, so both parametrizations went RED against production code whose property
# still held. That is the contract-change rule in CLAUDE.md, broken by the commit this file guards.
#
# WHY TWO LINKS AND NOT ONE. Pinning only "the call site reaches the wrapper" leaves the cheapest
# way to green a future failure being to revert a call site to the direct sync
# `_consume_batch_proposal` — i.e. a one-link guard actively REWARDS restoring the 62-minute
# event-loop freeze the offload exists to end. So the call sites must name the wrapper, and the
# wrapper must name the funnel; neither half alone is the property.
#
# The negatives ride BOTH links for the same reason they rode the call sites: what must not come
# back is READING the attribute protocol by hand, wherever the chunk now lives.


@pytest.mark.parametrize("method", _CALL_SITES)
def test_both_call_sites_go_through_the_awaited_offload(method):
    source = inspect.getsource(getattr(Engine, method))
    # AST, NOT A SUBSTRING, and this one was caught by its own mutation harness rather than
    # reasoned about. `_stage_card_creates` carries a `# proof:` line naming
    # `await self._await_batch_proposal(` verbatim, so a text pin over its source was satisfied by
    # that COMMENT — reverting the real call to the sync `_consume_batch_proposal` left the guard
    # GREEN. That is precisely the "a guard test must not be satisfiable by a COMMENT" rule, and the
    # marker this test closes exists because the same guard had already gone stale once.
    called = called_names(getattr(Engine, method))
    assert any(name.endswith("_await_batch_proposal") for name in called), (
        f"{method} no longer CALLS the offloaded batch proposal — a direct "
        "`_consume_batch_proposal(` here is the event-loop freeze restored")
    assert not any(name.endswith("_consume_batch_proposal") for name in called), (
        f"{method} calls the funnel DIRECTLY again, on the event-loop thread")
    # Assigning the attributes back to [] is each caller's own reset, which deliberately stays; what
    # must not come back is READING them.
    assert 'getattr(self, "_pending_batch_telemetry"' not in source, (
        f"{method} reads the telemetry attribute directly again")
    assert 'getattr(self, "_pending_batch_dropped"' not in source, (
        f"{method} reads the dropped attribute directly again")


def test_the_awaited_wrapper_delegates_to_the_SHARED_consume():
    """The second link. Without it the pin above is satisfiable by hollowing the wrapper out.

    AST over the ATTRIBUTE REFERENCE, not a substring: the wrapper's own docstring names
    `_consume_batch_proposal` three times, so the previous text pin stayed green with the real
    `functools.partial(self._consume_batch_proposal, ...)` deleted — and the funnel reference is a
    bare `ast.Attribute` (a partial ARGUMENT, not a Call), so a Call-name walk cannot see it
    either. Comments are not AST nodes; a reference is.
    """
    refs = [n for n in ast.walk(function_tree(Engine._await_batch_proposal))
            if isinstance(n, ast.Attribute) and n.attr == "_consume_batch_proposal"]
    assert refs, (
        "the offload must still go through the ONE funnel — reading the attribute protocol inside "
        "the wrapper is the same defect one frame down")
    source = inspect.getsource(Engine._await_batch_proposal)
    assert 'getattr(self, "_pending_batch_telemetry"' not in source
    assert 'getattr(self, "_pending_batch_dropped"' not in source


def test_the_wrapper_really_OFFLOADS_and_publishes_from_the_main_task():
    """...and it is an OFFLOAD, not a rename. Both halves, because either alone is a different bug:
    a sync call on the loop is the freeze, and a `to_thread` with no capture is the invariant-#1
    breach (`_append_proposal_event` falls through to `store.append` with no sink installed).

    AST, not substrings, for the two calls that must be PRESENT — a comment naming either would
    satisfy a text pin, which is exactly what CLAUDE.md's ladder warns about. Through
    `tests/_source_scan.py::called_names`, not a local re-parse: the shared helper resolves dotted
    targets in source order with the decode pitfalls handled once, which is the whole reason it
    exists (its docstring records the diverged private copies it replaced).
    """
    # BOTH PROPERTIES STILL HOLD; since 2026-08-31 they hold one call deeper. The wrapper delegates
    # to `_offload_under_proposal_sink`, which is where the triple lives now — see
    # `test_proposal_publish_is_hoisted_once.py` for why it was hoisted (it was hand-written at four
    # sites) and for the guard that stops a lane re-inlining it. Following the delegation here rather
    # than deleting the assertion keeps the ORIGINAL property driven: a wrapper that stopped
    # offloading, or offloaded without the sink, still fails on this line.
    called = called_names(Engine._await_batch_proposal)
    assert any(name.endswith("_offload_under_proposal_sink") for name in called), (
        "the batch wrapper must reach the paid proposal through the offload helper")
    inner = called_names(Engine._offload_under_proposal_sink)
    assert any(name.endswith("run_sync") for name in inner), (
        "the paid batch proposal must leave the event-loop thread")
    assert any(name.endswith("_capture_proposal_events") for name in inner), (
        "the folded proposal events must be BUFFERED — an uncaptured offload appends them from a "
        "worker, which invariant #1 forbids and `_proposal_authority_seq` is fenced on")


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
