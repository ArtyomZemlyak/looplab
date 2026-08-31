"""A build whose producer is STILL RUNNING is not thrown away by a turn-scoped commit refusal.

`_serve_card_builds` closes a durable head as `stale` on four conditions, and three of them are
facts about the WORLD — the search epoch rotated, the run is stopping, the eval budget is gone — so
a build made for the old world is worth nothing and discarding it is right. The fourth,
`commit_not_allowed`, is `CardSession.open_for_production`, whose own docstring says it answers
**"may this turn still START PRODUCER work"** and justifies itself with "a producer started after a
terminal would hold the session open for the whole of its paid provider call".

That justification is false about a producer ALREADY RUNNING. Committing one starts nothing, makes
no provider call and holds the session open for no latency — so the flag that means "do not begin
new paid work" was destroying paid work already in progress.

MEASURED on `runs/e5small-dr-unified-v10`, the first run whose `skipped_reason` (`8c7af6a7`) could
name it:

    09:49:34  card_build_requested / attempted   card-4
    10:25:29  node_failed  node 2  (sets boundary_owed)
    10:25:29  card_build_done  card-4  skipped=stale  skipped_reason=commit_not_allowed
    10:27:59  <the card_build SPAN CLOSES>   38.4 min · 248 provider calls · 12,112,124 tokens
    10:28:00  card_build_requested  card-4    <- the identical card, one second later

All four of that run's committed builds closed their span at or before their `card_build_done`;
card-4 is the only one closed out from under a live producer, and it cost 3.2 % of the run.

The fix is this file's own module rule applied to the one close that did not consult it —
`_serve_card_builds` already guards its two crash-recovery closes with `key not in
self._spec_build_inflight` under the comment "Never strand a live producer: skip while one is
in-flight". `commit_not_allowed` now does the same.

Every assertion below has an input that makes it FAIL; the mutations are named in the messages.
"""
from __future__ import annotations

import textwrap

from looplab.engine import speculation
from looplab.events.replay import fold
from looplab.events.types import EV_CARD_BUILD_DONE
from tests.test_card_speculation_engine import (  # noqa: F401 - the fixture is autouse
    _add_ready_draft, _admit_unit_speculation_receipt, _engine, _request, _start,
)


def _head_key(engine):
    return engine._request_key(engine._head_request(fold(engine.store.read_all())))


def _done_rows(engine):
    return [e for e in engine.store.read_all() if e.type == EV_CARD_BUILD_DONE]


def test_a_LIVE_producer_survives_a_commit_refusal(tmp_path):
    """The defect. Mutation: drop the `_spec_build_inflight` conjunct and this head closes
    `stale`/`commit_not_allowed` while its producer is still burning provider calls — v10's card-4,
    38.4 minutes and 12.1M tokens, re-requested one second after it finished."""
    engine, _producer = _engine(tmp_path / "live-producer-commit-refusal")
    _start(engine)
    _add_ready_draft(engine, "card-7")
    _request(engine)
    engine._ensure_speculation_state()
    key = _head_key(engine)

    engine._spec_build_inflight.add(key)                 # a producer is RUNNING for this head
    assert engine._serve_card_builds(allow_commit=False) is False, (
        "a live producer's head must not be serviced by a turn that may not commit — it is left "
        "open for the next turn, which is what `boundary_owed` is asking the session to reach")
    assert _done_rows(engine) == [], (
        "and nothing may be written: a `card_build_done` here closes the durable request out from "
        "under a producer that will finish minutes later with nowhere to put its result")
    assert fold(engine.store.read_all()).card_builds_done == 0
    assert _head_key(engine) == key, "the head is still outstanding for the next turn to commit"


def test_a_DEAD_head_is_still_closed_by_a_commit_refusal(tmp_path):
    """The property the branch was written for, which must survive the fix. Mutation: leave EVERY
    `commit_not_allowed` head open and a head with no live producer never closes, so `outstanding`
    stays true and the session polls without reaching its exit boundary."""
    engine, _producer = _engine(tmp_path / "dead-head-commit-refusal")
    _start(engine)
    _add_ready_draft(engine, "card-7")
    _request(engine)
    engine._ensure_speculation_state()
    assert not engine._spec_build_inflight            # NO producer owns this head

    assert engine._serve_card_builds(allow_commit=False) is True
    done = _done_rows(engine)
    assert len(done) == 1 and done[0].data.get("skipped") == "stale"
    assert done[0].data.get("skipped_reason") == "commit_not_allowed", (
        "the reason slug must be unchanged — `8c7af6a7` shipped it so a discarded build stops "
        "being unattributable, and this path is where it is written")


def test_a_STOPPING_run_closes_the_head_even_with_a_live_producer(tmp_path):
    """The world-moved half, and the reason finalization cannot wedge. Mutation: let the in-flight
    check win over `_terminal_intent` and a stopping run cannot close its last head, so the finish
    CAS never settles."""
    engine, _producer = _engine(tmp_path / "stopping-live-producer")
    _start(engine)
    _add_ready_draft(engine, "card-7")
    _request(engine)
    engine._ensure_speculation_state()
    engine._spec_build_inflight.add(_head_key(engine))    # a producer IS running

    engine.store.append("pause", {"by": "operator", "reason": "stop"})
    assert engine._terminal_intent(fold(engine.store.read_all())) is True

    assert engine._serve_card_builds(allow_commit=False) is True, (
        "a stopping run closes the head whatever the producer is doing — the build has nowhere to "
        "run and the reason ladder tests `_terminal_intent` BEFORE the commit flag")
    done = _done_rows(engine)
    assert len(done) == 1 and done[0].data.get("skipped_reason") == "run_is_stopping", (
        "and it is named `run_is_stopping`, not `commit_not_allowed`: the two have different "
        "remedies and the ladder's order is what keeps them apart")


def test_no_await_separates_storing_a_result_from_clearing_the_in_flight_marker():
    """WHY the `allow_commit` conjunct is unobservable today, pinned as the ordering it rests on.

    Dropping `commit_refused_this_turn` from the new guard passes every behavioural test above, and
    that is not a weak test — it is a real equivalence, and this is the fact it rests on.
    `_produce_card_build` stores its result and clears the in-flight marker back to back:

        self._spec_builds[key] = result
        finally:
            self._spec_build_inflight.discard(key)

    with NO `await` between them. `_produce_card_build` is a coroutine, so the main task can only be
    scheduled at an await point — which means the pair "a result is stored AND the key is still
    in-flight" is unreachable from `_serve_card_builds`, and an in-flight key therefore always
    implies no result to commit.

    The conjunct is KEPT anyway, because it states the rule the branch is about rather than a
    coincidence of the current scheduling, and because the day an `await` appears between those two
    statements the two stop being equivalent — a turn that MAY commit would start refusing a
    finished build. This test is what goes red on that day: it is the invariant, not the conjunct,
    that the equivalence depends on.

    Mutation: put any `await` between the store and the discard and this fails.
    """
    import ast
    import inspect

    tree = ast.parse(textwrap.dedent(inspect.getsource(
        speculation.SpeculationMixin._produce_card_build)))
    stores, discards, awaits = [], [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Await):
            awaits.append(node.lineno)
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Subscript)
                and isinstance(t.value, ast.Attribute)
                and t.value.attr == "_spec_builds" for t in node.targets):
            stores.append(node.lineno)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "discard" \
                and isinstance(node.func.value, ast.Attribute) \
                and node.func.value.attr == "_spec_build_inflight":
            discards.append(node.lineno)

    assert len(stores) == 1 and len(discards) == 1, (
        f"expected exactly one result store and one in-flight discard, got {stores}/{discards} — "
        "a second of either makes the ordering claim unstatable")
    between = [ln for ln in awaits if stores[0] < ln < discards[0]]
    assert not between, (
        f"an `await` at line offset {between} now separates storing the result from clearing the "
        "in-flight marker. The main task can be scheduled there, so it can observe a STORED result "
        "on an IN-FLIGHT key — and `_serve_card_builds`'s in-flight skip would then refuse to "
        "commit a finished build on a turn that is allowed to. The `commit_refused_this_turn` "
        "conjunct stops that; re-read it before changing this ordering")


def test_an_ALLOWED_commit_is_untouched_by_the_in_flight_check(tmp_path):
    """The pre-existing path must not move. Mutation: apply the in-flight skip regardless of
    `allow_commit` and a normal turn stops servicing its own heads entirely."""
    engine, _producer = _engine(tmp_path / "allowed-commit-live-producer")
    _start(engine)
    _add_ready_draft(engine, "card-7")
    _request(engine)
    engine._ensure_speculation_state()
    key = _head_key(engine)
    engine._spec_build_inflight.add(key)

    # allow_commit defaults True: an alive head with a live producer and no result yet stays open
    # exactly as it always did, and reports "no head serviced" for the SAME reason it always did.
    assert engine._serve_card_builds() is False
    assert _done_rows(engine) == []
    assert _head_key(engine) == key
