"""Three seams where two copies had to agree and nothing checked that they did.

* EV-09 — the force-confirm/force-ablate fold handlers, and the four sites that PURGE those queues.
  Each queue is two lists (a legacy bare-id list and a generation-stamped record list) that must
  move together; the engine's `_pending_forced_*` readers consult the stamped list FIRST, so a
  record left behind wins over an id the caller believed it had removed.
* EM-14 — a batch concept applier the steward never invoked, one import away from the module's whole
  invariant (the steward PROPOSES; the operator applies through a typed action or HTTP CAS).
* EC-12 — the producer fault text and the notification swallow, each written out three times across
  the speculation producers.
"""
from __future__ import annotations

import inspect

import pytest

from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.events import replay
from looplab.events.replay import _purge_node_requests, _queue_forced_request


def _live(st: RunState, nid: int, attempt: int = 0) -> Node:
    node = Node(id=nid, parent_ids=[], operator="mutate",
                idea=Idea(operator="mutate"), status=NodeStatus.pending)
    node.attempt = attempt
    st.nodes[nid] = node
    return node


# ------------------------------------------------------------------ EV-09: the queue pair

def test_a_stamped_intent_queues_the_id_AND_its_generation():
    st = RunState()
    _live(st, 3, attempt=2)
    _queue_forced_request(st, {"node_id": 3, "generation": 2},
                          st.confirm_requests, st.confirm_request_generations)
    assert st.confirm_requests == [3]
    assert st.confirm_request_generations == [{"node_id": 3, "generation": 2}]


def test_an_intent_for_a_SINCE_RESET_lifecycle_is_dropped():
    """The generation CAS. A control authored against the old code must not be applied to the new
    one — silently re-running a confirm the operator asked for on different code."""
    st = RunState()
    _live(st, 3, attempt=5)
    _queue_forced_request(st, {"node_id": 3, "generation": 2},
                          st.confirm_requests, st.confirm_request_generations)
    assert st.confirm_requests == [] and st.confirm_request_generations == []


def test_a_tombstoned_or_aborted_node_never_queues():
    st = RunState()
    node = _live(st, 3, attempt=0)
    node.tombstoned = True
    _queue_forced_request(st, {"node_id": 3, "generation": 0},
                          st.ablate_requests, st.ablate_request_generations)
    assert st.ablate_requests == []

    st2 = RunState()
    _live(st2, 4, attempt=0)
    st2.aborted_nodes.append(4)
    _queue_forced_request(st2, {"node_id": 4, "generation": 0},
                          st2.ablate_requests, st2.ablate_request_generations)
    assert st2.ablate_requests == []


def test_a_legacy_unstamped_intent_for_a_not_yet_created_node_still_binds():
    """Old logs predate the stamp. Refusing them would retroactively drop operator intents that the
    engine has always honoured — a replay of an existing run changing its own history."""
    st = RunState()
    _queue_forced_request(st, {"node_id": 9}, st.confirm_requests, st.confirm_request_generations)
    assert st.confirm_requests == [9]
    assert st.confirm_request_generations == [], "a legacy intent must not invent a generation"


def test_an_unstamped_intent_for_an_EXISTING_node_is_not_admitted_blind():
    st = RunState()
    _live(st, 9, attempt=1)
    _queue_forced_request(st, {"node_id": 9}, st.confirm_requests, st.confirm_request_generations)
    assert st.confirm_requests == [9] and st.confirm_request_generations == [
        {"node_id": 9, "generation": 1}], "an existing node stamps the current generation"


def test_both_handlers_delegate_to_the_one_queueing_rule():
    for name in ("_on_force_confirm", "_on_force_ablate"):
        source = inspect.getsource(getattr(replay, name))
        assert "_queue_forced_request(" in source, f"{name} re-derives the queueing rule"
        assert "_control_generation_matches" not in source, f"{name} re-derives the generation CAS"


# ------------------------------------------------------------------ EV-09: the purge

def _queued(st: RunState) -> None:
    st.confirm_requests = [1, 2]
    st.confirm_request_generations = [{"node_id": 1, "generation": 0}, {"node_id": 2, "generation": 0}]
    st.ablate_requests = [1, 2]
    st.ablate_request_generations = [{"node_id": 1, "generation": 0}, {"node_id": 2, "generation": 0}]


def test_a_purge_moves_BOTH_lists_of_each_queue():
    """The failure this single-sources: the engine reads the STAMPED list first, so an id removed
    from the bare list while its record survives is a request that still fires."""
    st = RunState()
    _queued(st)
    _purge_node_requests(st, {1})
    assert st.confirm_requests == [2] and st.ablate_requests == [2]
    assert [r["node_id"] for r in st.confirm_request_generations] == [2]
    assert [r["node_id"] for r in st.ablate_request_generations] == [2]


def test_a_purge_takes_a_SET_of_ids_at_once():
    st = RunState()
    _queued(st)
    _purge_node_requests(st, {1, 2})
    assert not st.confirm_requests and not st.ablate_requests
    assert not st.confirm_request_generations and not st.ablate_request_generations


def test_every_lifecycle_ending_event_drops_BOTH_queues():
    """All four call sites end a node's current lifecycle, and a queued intent names a LIFECYCLE,
    not a node — so none of them may drop one queue and keep the other. `node_reset` in particular
    used to spell the two queues twelve lines apart, which is how a partial purge hides."""
    st = RunState()
    _queued(st)
    _purge_node_requests(st, {1})
    assert st.confirm_requests == [2] and st.ablate_requests == [2]


def test_the_serve_time_generation_gate_still_backs_the_purge():
    """Defence in depth: even a stamped request that somehow survived is re-checked against the
    node's current `attempt` when it is served. Named, not assumed."""
    from looplab.engine.orchestrator import Engine

    source = inspect.getsource(Engine._pending_forced_ablation)
    assert 'attempt == r.get("generation")' in source


@pytest.mark.parametrize("handler", ["_on_node_tombstoned", "_on_node_reset", "_on_node_abort"])
def test_no_fold_handler_re_derives_the_purge(handler):
    source = inspect.getsource(getattr(replay, handler))
    assert "_purge_node_requests(" in source, f"{handler} re-derives the queue purge"
    assert "confirm_request_generations = [" not in source, f"{handler} filters a list by hand"


def test_the_purge_is_written_in_exactly_one_place():
    source = inspect.getsource(replay)
    assert source.count("st.ablate_request_generations = [") == 1, (
        "a second hand-rolled ablate-queue filter appeared")
    assert source.count("st.confirm_request_generations = [") == 1


# ------------------------------------------------------------------ EM-14: the dead batch applier

def test_the_batch_concept_applier_is_gone():
    """It bypassed CAS entirely — no `expected_revision`, no `action_id` — while the module's
    invariant is that the steward only PROPOSES and the operator applies through a typed single
    action or owner HTTP governance. Dead code contradicting the invariant is worse than dead."""
    from looplab.engine import concept_steward

    assert not hasattr(concept_steward, "apply_concept_curation")


def test_the_steward_still_only_proposes():
    from looplab.engine import concept_steward

    source = inspect.getsource(concept_steward)
    assert "record_concept_alias(" not in source and "record_concept_split(" not in source, (
        "the steward module writes governance records again")


# ------------------------------------------------------------------ EC-12: the producer seams

def test_the_producer_error_text_is_bounded_and_names_the_type():
    """`str(exc)` is EMPTY for several provider errors, so the type name carries the diagnosis; and
    an unbounded provider traceback repr turns one failed proposal into an unreadable durable
    result."""
    from looplab.engine.speculation import producer_error_text

    assert producer_error_text(ValueError("bad idea")) == "ValueError: bad idea"
    assert producer_error_text(ValueError("")) == "ValueError: "
    assert len(producer_error_text(ValueError("x" * 10_000))) == 2_048
    assert producer_error_text(ValueError("x"), "commit failed: ") == "commit failed: ValueError: x"


def test_the_notification_swallow_covers_all_three_teardown_errors():
    """Each means the consumer is gone or saturated, and the main task re-scans the durable slots
    anyway. Letting one escape would tear down the task group — cancelling live evaluations — over
    a hint nobody needed."""
    import anyio

    from looplab.engine.speculation import notify_producer

    class _Raises:
        def __init__(self, exc):
            self.exc = exc

        def send_nowait(self, key):
            raise self.exc

    for exc in (anyio.WouldBlock(), anyio.ClosedResourceError(), anyio.BrokenResourceError()):
        notify_producer(_Raises(exc), ("producer", ("card", 0)))


def test_an_unexpected_notification_error_is_NOT_swallowed():
    from looplab.engine.speculation import notify_producer

    class _Boom:
        def send_nowait(self, key):
            raise RuntimeError("something nobody anticipated")

    with pytest.raises(RuntimeError):
        notify_producer(_Boom(), ("producer", ("card", 0)))


def test_no_producer_re_derives_either_seam():
    from looplab.engine import speculation

    source = inspect.getsource(speculation)
    assert "[:2_048]" not in source, "a producer re-derives the bounded error text"
    assert source.count("anyio.BrokenResourceError") == 1, (
        "a producer re-derives the notification swallow")
