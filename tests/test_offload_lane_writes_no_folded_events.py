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


def _offload_region(label: str) -> ast.AST:
    """The `to_thread.run_sync(partial(...))` call this lane offloads through, by AST."""
    for node in ast.walk(_lane_function(label)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None)
        if name == "run_sync" and any(
                isinstance(a, ast.Call) and getattr(a.func, "attr", None) == "partial"
                for a in node.args):
            return node
    raise AssertionError(
        f"the {label} lane's offloaded `to_thread.run_sync(partial(...))` call is gone — a paid "
        "provider wait back on the event-loop thread is the freeze this whole module guards")


@pytest.mark.parametrize("label", [lane[0] for lane in _LANES])
def test_the_offloaded_call_runs_UNDER_the_capture_sink(label):
    """Mutation: drop the `with self._capture_proposal_events()` wrapper, and every folded
    novelty/graded/prior row appends straight from the worker again — the defect itself."""
    tree = _lane_function(label)
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
    tree = _lane_function(label)
    publishes = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.For):
            continue
        iterates_captured = isinstance(node.iter, ast.Name) and node.iter.id == "captured"
        appends = any(isinstance(c, ast.Call) and getattr(c.func, "attr", None) == "append"
                      for c in ast.walk(node))
        if iterates_captured and appends:
            publishes.append(node)
    assert publishes, (
        f"the {label} lane must append its captured intents from the main task after the await")


@pytest.mark.parametrize("label", [lane[0] for lane in _LANES])
def test_the_publish_is_NOT_gated_on_the_idea_forming(label):
    """`idea is None` is a REFUSED proposal — exactly the case the discard receipt was written for.
    Mutation: move the publish loop under `if idea is not None`, and a paid propose that produced no
    card goes back to leaving no trace, which is the loss bd182357 measured at 24.1 min / 81 calls /
    4.27M tokens on v8."""
    tree = _lane_function(label)
    publish = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.For) and isinstance(n.iter, ast.Name)
                   and n.iter.id == "captured")
    # THE PROPERTY IS "NOT GATED ON THE OFFLOAD'S OUTCOME", and the first spelling of this test
    # asserted something weaker: that no enclosing `if` mentioned the word "idea". That is the
    # per-action lane's own local name, and extending the guard to the batch lane made it VACUOUS
    # there — a mutant gating the publish on `if result and result[0]:` survived it, because the
    # batch wrapper's local is called `result`. The mutation harness is what said so.
    #
    # So the names are DERIVED from the lane instead of assumed: whatever the offloaded call's
    # result is bound to, no `if` around the publish may read it. An enclosing branch that owns the
    # lane at all is legitimate and stays — `_stage_card_creates` publishes inside its multi-draft
    # selector (`len(raw) > 1 and all(... == "draft")`), which is a fact about the ACTIONS and not
    # about what the paid call came back with.
    outcome_names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if isinstance(value, ast.Await):
            value = value.value
        calls = [c for c in ast.walk(value) if isinstance(c, ast.Call)]
        if not any(getattr(c.func, "attr", None) in ("run_sync", "_await_batch_proposal")
                   for c in calls):
            continue
        for target in node.targets:
            outcome_names |= {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}
    assert outcome_names, (
        f"the {label} lane's offloaded call binds nothing — this guard has lost its subject")
    guards = [n for n in ast.walk(tree) if isinstance(n, ast.If)
              and any(x is publish for x in ast.walk(n))]
    for guard in guards:
        read = {n.id for n in ast.walk(guard.test) if isinstance(n, ast.Name)}
        leaked = read & outcome_names
        test_src = ast.get_source_segment(_SOURCES[label], guard.test) or ""
        assert not leaked, (
            f"the {label} lane's publish must not be conditioned on what the paid call returned "
            f"({sorted(leaked)}), got `if {test_src}`")


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
