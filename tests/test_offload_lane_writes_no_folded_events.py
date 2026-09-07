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
import functools
import inspect
import pathlib

import pytest

from looplab.engine import card_reservation, novelty, orchestrator
from looplab.events import types as event_types

# BOTH OFFLOAD LANES, since 2026-08-31. Every guard below parsed `card_reservation.py` ONLY, so none
# covered the BATCH wrapper `orchestrator.py::_await_batch_proposal` — the lane a run on the shipped
# default width actually takes. Same rule, same shape, two homes; a guard that names a file rather
# than a property is one refactor from covering nothing, which is this module's own subject.
#
# SCOPED TO THE OWNING FUNCTION, not to the module, and that is load-bearing rather than tidy:
# `orchestrator.py` holds FOUR `to_thread.run_sync(partial(...))` calls (`_await_batch_proposal`,
# `_bg`, `_research_overlap_loop`, `_spawn_research`), so a module-wide search for "the offload"
# would have locked onto the research lane and reported green about a region nobody asked it to
# check.
_LANES = (("per-action", card_reservation, "_stage_card_creates"),
          ("batch", orchestrator, "_await_batch_proposal"))

# OPEN[offload-sink-guards-scan-one-file] the REAL `_propose_batch` closure is still never driven
# under a watched store — the behavioural twins stub the paid callee.
# proof:`absent:def test_the_real_batch_closure@tests/test_offload_lane_writes_no_folded_events.py`
# (backtick-quoted because the literal carries a SPACE — the bare form splits on whitespace and
# the index guard refused it as malformed, which is the guard doing its job on my amendment.)
# AMENDED 2026-08-31: the scan half is CLOSED — every guard here now runs over both lanes, scoped to
# the owning function. What remains is the second half of the same review: a direct folded
# `store.append` added under `_propose_batch` (exactly the fix novelty.py's duplicate-receipt marker
# prescribes for its drop branch) would run on the worker thread with every AST guard green, because
# these follow the SHAPE and not the execution. Drive it once: stub only the provider call, watch
# the store, assert zero folded appends land off the loop thread.
_SOURCES = {label: pathlib.Path(inspect.getsourcefile(mod)).read_text(encoding="utf-8")
            for label, mod, _fn in _LANES}
# ONE parse per module, shared by every assertion below. Each test parsing its own copy was the first
# version and it made the `with`-wrapper test VACUOUSLY RED: node identity (`n is offload`) can never
# hold across two `ast.parse()` calls, so the search found nothing and the message blamed the
# product.
_TREES = {label: ast.parse(src) for label, src in _SOURCES.items()}


def _lane_function(label: str) -> ast.AST:
    """The AST of the function that OWNS this lane's offload."""
    name = next(fn for lbl, _mod, fn in _LANES if lbl == label)
    for node in ast.walk(_TREES[label]):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} is gone from the {label} lane's module — re-point this guard")


def _publishing_function(label: str) -> ast.AST:
    """Where this lane's capture->offload->publish triple LIVES.

    Since 2026-08-31 it lives one call deeper, in `novelty.py::_offload_under_proposal_sink`: the
    triple and the 4-tuple publish loop had been hand-written at four sites, and
    `test_proposal_publish_is_hoisted_once.py` now forbids a lane re-inlining either. Following the
    delegation keeps every property below DRIVEN rather than deleted — a lane that stopped
    delegating fails `test_each_lane_DELEGATES_to_the_offload_helper`, and a helper that stopped
    publishing from a `finally` fails the guards that follow.
    """
    if _delegates(label):
        for node in ast.walk(_helper_tree()):
            if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == "_offload_under_proposal_sink"):
                return node
        raise AssertionError("the offload helper is gone — re-point these guards")
    return _lane_function(label)


@functools.lru_cache(maxsize=1)
def _helper_tree() -> ast.AST:
    """Parsed ONCE. Two of the guards below compare AST nodes by IDENTITY (`n is offload`), and a
    per-call re-parse hands them nodes from two different trees — the comparison then silently
    answers False and the guard reports a missing sink that is right there. Found exactly that way.
    """
    return ast.parse(pathlib.Path(novelty.__file__).read_text())


def _delegates(label: str) -> bool:
    return any(isinstance(node, ast.Call)
               and getattr(node.func, "attr", None) == "_offload_under_proposal_sink"
               for node in ast.walk(_lane_function(label)))


@pytest.mark.parametrize("label", [lane[0] for lane in _LANES])
def test_each_lane_DELEGATES_to_the_offload_helper(label):
    """The hop the guards below follow. Without this assertion a lane could hand-roll the triple
    again and every property would silently be checked against the helper it no longer uses."""
    assert _delegates(label), (
        f"the {label} lane must reach its paid proposal through `_offload_under_proposal_sink` — "
        "that helper carries the sink, the proposal thread pool and the publish-in-`finally` rule")


def _offload_region(label: str) -> ast.AST:
    """The `to_thread.run_sync(partial(...))` call this lane offloads through, by AST.

    Follows the delegation since 2026-08-31: the two lanes hand their paid call to
    `_offload_under_proposal_sink`, so the offload itself lives there. `test_each_lane_DELEGATES_
    to_the_offload_helper` is what makes that hop safe to follow.
    """
    for node in ast.walk(_publishing_function(label)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None)
        if name != "run_sync":
            continue
        # `partial(...)` inline OR a callable handed in. The hoisted helper takes `fn` as a
        # parameter and its CALLERS build the partial, so requiring the literal `partial(` here
        # would only be asserting where the closure happens to be constructed — not that the paid
        # call left the loop thread, which is the property this module exists for.
        if node.args:
            return node
    raise AssertionError(
        f"the {label} lane's offloaded `to_thread.run_sync(partial(...))` call is gone — a paid "
        "provider wait back on the event-loop thread is the freeze this whole module guards")


@pytest.mark.parametrize("label", [lane[0] for lane in _LANES])
def test_the_offloaded_call_runs_UNDER_the_capture_sink(label):
    """Mutation: drop the `with self._capture_proposal_events()` wrapper, and every folded
    novelty/graded/prior row appends straight from the worker again — the defect itself."""
    tree = _publishing_function(label)
    offload = _offload_region(label)
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
        f"the {label} lane's offloaded proposal must run inside `_capture_proposal_events()`; "
        "without it `_append_proposal_event` falls through to `store.append` on the worker thread")


@pytest.mark.parametrize("label", [lane[0] for lane in _LANES])
def test_the_captured_intents_are_PUBLISHED_and_not_dropped(label):
    """A sink that buffers and never publishes turns an invariant breach into silent data loss —
    strictly worse, because the discard receipt (bd182357) exists precisely so a refused paid
    proposal leaves a trace. Mutation: delete the publish loop."""
    tree = _publishing_function(label)
    publishes = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", None) == "_publish_proposal_events"]
    assert publishes, (
        f"the {label} lane must publish its captured intents from the main task after the await; "
        "since 2026-08-31 that is one call to `_publish_proposal_events`, which is the only place "
        "the 4-tuple is unpacked (see test_proposal_publish_is_hoisted_once.py)")


@pytest.mark.parametrize("label", [lane[0] for lane in _LANES])
def test_the_publish_is_NOT_gated_on_the_idea_forming(label):
    """`idea is None` is a REFUSED proposal — exactly the case the discard receipt was written for:
    a paid propose that produced no card used to leave no trace at all, which `bd182357` measured at
    24.1 min / 81 calls / 4.27M tokens on v8.

    THE PROPERTY IS "NOT GATED ON THE OFFLOAD'S OUTCOME", and its first spelling asserted something
    weaker — that no enclosing `if` mentioned the word "idea". That is the per-action lane's own
    local name, so extending the guard to the batch lane made it VACUOUS there: a mutant gating the
    publish on `if result and result[0]:` survived, because the batch wrapper's local is `result`.
    The mutation harness is what said so, and the names have been DERIVED from the lane ever since.

    Since the hoist (2026-08-31) the publish lives in `_offload_under_proposal_sink`'s `finally`,
    where the property holds by CONSTRUCTION — a `finally` cannot be conditioned on a return value
    that may not exist. So the assertion moves with it and gets stronger: the publish must sit in a
    `finally`, and no `if` inside the helper may stand between the offload and it.

    Mutation: turn the `finally` into a plain post-`try` publish, or wrap it in `if result:`.
    """
    tree = _publishing_function(label)
    publish = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "_publish_proposal_events"),
        None)
    assert publish is not None, (
        f"the {label} lane's publish is gone — this guard has lost its subject")

    unconditional = [t for t in ast.walk(tree) if isinstance(t, ast.Try)
                     and any(x is publish for n in t.finalbody for x in ast.walk(n))]
    assert unconditional, (
        f"the {label} lane's publish must run from a `finally`, which is what makes it independent "
        "of whether the paid call returned an idea, returned nothing, or raised")

    gates = [n for n in ast.walk(tree) if isinstance(n, ast.If)
             and any(x is publish for x in ast.walk(n))]
    assert not gates, (
        f"the {label} lane's publish is under {len(gates)} `if` — a refused proposal is exactly "
        "when the receipt matters most, and any condition here restores the silence bd182357 ended")


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


@pytest.mark.parametrize("label", [lane[0] for lane in _LANES])
def test_the_publish_SURVIVES_a_raise_from_the_offloaded_call(label):
    """A buffered receipt must not be lost because the paid call ended badly.

    `_reject_and_repropose` appends `budget_exceeded` through this very sink and then RE-RAISES —
    its own docstring says "appended BEFORE re-raising so the rejection is on the log even though
    the run is ending" — and both shipped researchers propagate `BudgetExceeded`. Buffering the
    intents silently made the publish conditional on a CLEAN RETURN: pre-offload every row was
    durable at emit time.

    MUTATION: turn the `finally` back into a plain post-`with` loop and this goes red — the publish
    then sits outside the handler that a raise unwinds through.
    """
    owner = _publishing_function(label)
    # `next(..., None)` and an assertion, NOT a bare `next`: a missing publish must read as a
    # FAILURE with this file's own message, not as a StopIteration error whose traceback says
    # nothing about the property. Caught by the mutation pass that hand-rolled the lane again.
    publish = next(
        (n for n in ast.walk(owner)
         if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "_publish_proposal_events"),
        None)
    assert publish is not None, (
        f"the {label} lane no longer publishes through `_publish_proposal_events` — either it "
        "hand-rolled the loop again (see test_proposal_publish_is_hoisted_once.py) or the publish "
        "is gone entirely")
    protected = [n for n in ast.walk(owner)
                 if isinstance(n, ast.Try)
                 and any(x is publish for handler in n.finalbody for x in ast.walk(handler))]
    assert protected, (
        f"the {label} lane must publish its captured intents from a `finally`; on the clean-return "
        "path alone a BudgetExceeded from inside the offload discards every buffered receipt")


@pytest.mark.parametrize("label", [lane[0] for lane in _LANES])
def test_the_buffer_is_BOUND_before_the_try_so_the_finally_can_read_it(label):
    """The half that turns the fix into an AttributeError if it is half-applied: a `finally` that
    reads `captured` needs the name bound BEFORE the `try`, or a raise inside the `with`'s own
    setup unwinds into a NameError that masks the original exception.

    MUTATION: delete the `captured = []` line above the `try`.
    """
    fn = _publishing_function(label)
    tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try)
             and any(isinstance(x, ast.Name) and x.id == "captured"
                     for h in n.finalbody for x in ast.walk(h))]
    assert tries, f"the {label} lane has no `finally` reading `captured`"
    bound_before = [n for n in ast.walk(fn)
                    if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "captured" for t in n.targets)
                    and n.lineno < min(t.lineno for t in tries)]
    ann = [n for n in ast.walk(fn)
           if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
           and n.target.id == "captured" and n.value is not None
           and n.lineno < min(t.lineno for t in tries)]
    assert bound_before or ann, (
        f"the {label} lane must bind `captured` before the `try`, or the `finally` raises "
        "NameError and hides the real failure")
