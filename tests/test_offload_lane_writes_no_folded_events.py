"""The offloaded proposal writes NOTHING from its worker thread — including one call deep.

INVARIANT #1: the engine is the sole writer of domain events, and a FOLDED event may be appended off
the main task only from a registered, splice-proven seam. `aaef33d3` moved `_prepare_node_idea` onto
`anyio.to_thread.run_sync` on the strength of "it writes NOTHING", and that was false one call deep:
`_prepare_node_idea._link` and `_apply_novelty_gate` reach `_append_proposal_event`, which falls
through to `store.append` whenever no sink is installed — and the ONE installer was Layer 5, never
this lane. `EV_NOVELTY_REJECTED` / `EV_NOVELTY_GRADED` / `EV_CROSS_RUN_PRIOR` are all folded and
named by NONE of the three thread-append registries, so a default card lane breached the sole-writer
rule unregistered and unproven.

The rows are authority-bearing: `speculation.py::_proposal_authority_seq` discards a paid proposal
when any non-diagnostic row lands inside its max-seq equality window, so the loss a mistimed append
can cause is a proposal the run already paid for.

**WHY THE EXISTING GUARD MISSED IT.** `test_propose_does_not_freeze_the_loop::
test_the_offloaded_call_writes_no_events` walks `_prepare_node_idea`'s OWN AST. The append is one
helper down, so the function's own body is clean and the test is green while the lane writes. A
guard that stops at the function boundary cannot see past it; these follow the CALL.

Every assertion below has an input that makes it FAIL; the mutations are named in the messages.
"""
from __future__ import annotations

import ast
import inspect
import pathlib

from looplab.engine import card_reservation, novelty
from looplab.events import types as event_types

# OPEN[offload-sink-guards-scan-one-file] every AST guard below parses card_reservation.py ONLY,
# so none covers the BATCH wrapper `_await_batch_proposal` in the engine's spine module (2026-08-30)
# — and the behavioral twins in test_propose_does_not_freeze_the_loop stub the paid callee, so no
# test drives the REAL `_propose_batch` closure under a watched store. A direct folded
# `store.append` added under that closure (exactly the fix the duplicate-receipt marker in
# novelty.py prescribes for its drop branch) would run on the worker thread with every guard green —
# the miss-shape this module's own docstring documents, reintroduced one lane over.
# proof:absent:orchestrator@tests/test_offload_lane_writes_no_folded_events.py
# REVIEW 2026-08-30 (P2 guard coverage): extend the scan to the wrapper's home module (the same
# `getsourcefile` pattern), and drive the real closure once — stub only the provider call, watch the
# store, assert zero folded appends land off the loop thread.
_SRC = pathlib.Path(inspect.getsourcefile(card_reservation)).read_text(encoding="utf-8")
# ONE parse, shared by every assertion below. Each test parsing its own copy was the first version
# and it made the `with`-wrapper test VACUOUSLY RED: node identity (`n is offload`) can never hold
# across two `ast.parse()` calls, so the search found nothing and the message blamed the product.
_TREE = ast.parse(_SRC)


def _offload_region() -> ast.AST:
    """The `with`/`await` region around the offloaded proposal, by AST."""
    tree = _TREE
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None)
        if name == "run_sync" and any(
                isinstance(a, ast.Call) and getattr(a.func, "attr", None) == "partial"
                for a in node.args):
            return node
    raise AssertionError("the offloaded `to_thread.run_sync(partial(...))` call is gone")


def test_the_offloaded_call_runs_UNDER_the_capture_sink():
    """Mutation: drop the `with self._capture_proposal_events()` wrapper, and every folded
    novelty/graded/prior row appends straight from the worker again — the defect itself."""
    tree = _TREE
    offload = _offload_region()
    wrapped = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        uses_sink = any(isinstance(c, ast.Call)
                        and getattr(c.func, "attr", None) == "_capture_proposal_events"
                        for item in node.items for c in ast.walk(item.context_expr))
        if uses_sink and any(n is offload for n in ast.walk(node)):
            wrapped.append(node)
    assert wrapped, (
        "the offloaded proposal must run inside `_capture_proposal_events()`; without it "
        "`_append_proposal_event` falls through to `store.append` on the worker thread")


def test_the_captured_intents_are_PUBLISHED_and_not_dropped():
    """A sink that buffers and never publishes turns an invariant breach into silent data loss —
    strictly worse, because the discard receipt (bd182357) exists precisely so a refused paid
    proposal leaves a trace. Mutation: delete the publish loop."""
    tree = _TREE
    publishes = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue
        iterates_captured = isinstance(node.iter, ast.Name) and node.iter.id == "captured"
        appends = any(isinstance(c, ast.Call) and getattr(c.func, "attr", None) == "append"
                      for c in ast.walk(node))
        if iterates_captured and appends:
            publishes.append(node)
    assert publishes, "the captured intents must be appended from the main task after the await"


def test_the_publish_is_NOT_gated_on_the_idea_forming():
    """`idea is None` is a REFUSED proposal — exactly the case the discard receipt was written for.
    Mutation: move the publish loop under `if idea is not None`, and a paid propose that produced no
    card goes back to leaving no trace, which is the loss bd182357 measured at 24.1 min / 81 calls /
    4.27M tokens on v8."""
    src_lines = _SRC.splitlines()
    tree = _TREE
    publish = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.For) and isinstance(n.iter, ast.Name)
                   and n.iter.id == "captured")
    guards = [n for n in ast.walk(tree) if isinstance(n, ast.If)
              and any(x is publish for x in ast.walk(n))]
    for guard in guards:
        test_src = ast.get_source_segment(_SRC, guard.test) or ""
        assert "idea" not in test_src, (
            f"the publish must not be conditioned on the idea forming, got `if {test_src}`")
    assert src_lines, "source must be readable"


def test_the_events_this_lane_can_emit_are_FOLDED_and_registered_nowhere():
    """The reason a sink is required rather than a registry entry. Mutation: add
    `EV_NOVELTY_REJECTED` to a thread-append registry instead of installing the sink — that would be
    a claim of splice-position neutrality nobody proved, and this fails until someone does the proof
    the way `SETUP_THREAD_APPENDABLE` did."""
    for name in ("EV_NOVELTY_REJECTED", "EV_NOVELTY_GRADED", "EV_CROSS_RUN_PRIOR"):
        kind = getattr(event_types, name, None)
        if kind is None:
            continue
        for registry in ("BACKGROUND_APPENDABLE", "SETUP_THREAD_APPENDABLE",
                         "NON_CARD_SELECTION_BACKGROUND_APPENDABLE", "DIAGNOSTIC_EVENTS"):
            members = getattr(event_types, registry, None) or ()
            assert kind not in members, (
                f"{name} is in {registry}: registering it asserts splice-position neutrality this "
                f"repo has not proven for it — the sink is what makes the lane safe")


def test_the_sink_really_diverts_the_append():
    """Drive the rule rather than reading it: inside `_capture_proposal_events` the helper must
    buffer, and outside it must write. Mutation: make `_append_proposal_event` ignore the sink and
    this fails — which is the behaviour the whole fix rests on. Uses the REAL mixin; a hand-rolled
    stand-in would only prove the stand-in."""

    class _Store:
        def __init__(self):
            self.rows = []

        def append(self, event_type, data, **kw):
            self.rows.append((event_type, data))
            return None

    class _Engine(novelty.NoveltyGateMixin):
        def __init__(self):
            self.store = _Store()

    engine = _Engine()
    with engine._capture_proposal_events() as captured:
        engine._append_proposal_event("novelty_rejected", {"node_id": 1})
    assert engine.store.rows == [], "an append under the sink must not reach the store"
    assert [row[0] for row in captured] == ["novelty_rejected"]
    engine._append_proposal_event("novelty_rejected", {"node_id": 2})
    assert [e for e, _d in engine.store.rows] == ["novelty_rejected"], (
        "outside the sink the helper must still write immediately")
