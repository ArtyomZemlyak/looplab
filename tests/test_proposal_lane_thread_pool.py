"""Every offloaded PROPOSAL rides its own thread pool, not anyio's shared default.

`evaluate.py::_evaluate` offloads `self._run_eval` with NO limiter and holds one of anyio's 40
default tokens for the eval's whole multi-hour duration. `eval_parallel` is admitted to 1024 by
`core/config.py` and `parallel_build` to 64, so at an operator-raised width the evals pin the pool
and a PAID proposal queues behind them before its offloaded call even begins — board starvation
through the pool rather than through the loop, and invisible in every span because the wait happens
before the work starts.

`evaluate.py::_watch_limiter` already made exactly this argument for the watchdog tick and gave it a
dedicated pool of 8. The three proposal lanes got none until 2026-08-31.
"""
from __future__ import annotations

import ast
import pathlib

from looplab.engine.novelty import _PROPOSAL_THREADS, proposal_limiter

ROOT = pathlib.Path(__file__).resolve().parents[1]

# The offloaded proposal entry points, by the name that reaches `functools.partial`. Bounded on
# purpose: `test_the_lane_set_still_matches_the_sink_installers` below fails if a fourth module
# starts installing the proposal sink, so this set cannot quietly fall behind the tree.
PROPOSAL_TARGETS = {
    "_consume_batch_proposal": "looplab/engine/orchestrator.py",
    "_prepare_node_idea": "looplab/engine/card_reservation.py",
    "_prepare_raw_card_stage": "looplab/engine/speculation.py",
}


def _offload_calls(tree):
    """Every `…to_thread.run_sync(…)` call in a module, by AST — never a substring."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "run_sync":
            yield node


def _partial_target(call):
    """The name the offload actually runs, whether wrapped in `functools.partial` or passed bare."""
    for arg in call.args:
        if isinstance(arg, ast.Call) and getattr(arg.func, "attr", None) == "partial":
            inner = arg.args[0] if arg.args else None
            return getattr(inner, "attr", None) or getattr(inner, "id", None)
        name = getattr(arg, "attr", None) or getattr(arg, "id", None)
        if name:
            return name
    return None


def test_one_pool_per_process_and_it_is_not_the_default():
    """A limiter rebuilt per call is not a bound at all, and the whole point is to be OFF the
    default pool the evals hold."""
    import anyio

    assert proposal_limiter() is proposal_limiter(), "one pool, not one per call"
    assert proposal_limiter().total_tokens == _PROPOSAL_THREADS

    async def _check():
        assert proposal_limiter() is not anyio.to_thread.current_default_thread_limiter(), (
            "the proposal pool must not BE anyio's shared default — that is the pool the evals pin")
    anyio.run(_check)


def test_the_pool_admits_every_lane_that_can_be_in_flight_at_once():
    """The size is derived, not picked. The two arms of the create path are one `if` on the loop
    task and the speculative raw stage is gated by a boolean, so at most two lanes coexist."""
    assert _PROPOSAL_THREADS >= 2, (
        f"{_PROPOSAL_THREADS} tokens cannot admit the create-path lane and the speculative raw "
        "stage at the same time — the fix would deadlock the thing it was written to unblock")
    spec = (ROOT / "looplab/engine/speculation.py").read_text()
    assert "_spec_raw_stage_inflight" in spec, (
        "the raw stage's one-at-a-time bound is what makes 2 the floor; if it is gone, re-derive "
        "the size instead of trusting this number")


def test_every_offloaded_proposal_passes_the_limiter():
    """The rule, over the tree. A lane that forgets the kwarg silently returns to the shared pool."""
    offenders = []
    for target, rel in PROPOSAL_TARGETS.items():
        path = ROOT / rel
        tree = ast.parse(path.read_text())
        found = False
        for call in _offload_calls(tree):
            if _partial_target(call) != target:
                continue
            found = True
            if not any(kw.arg == "limiter" for kw in call.keywords):
                offenders.append(f"{rel}:{call.lineno} offloads {target} onto the DEFAULT pool")
        if not found:
            offenders.append(f"{rel}: no offload of {target} found — re-point this rule")
    assert not offenders, "\n  " + "\n  ".join(offenders)


def test_the_lane_set_still_matches_the_sink_installers():
    """A fourth proposal lane must not be able to appear without this file noticing.

    Derived from the tree the same way the sink-installer doc guard derives it: a module that opens
    `_capture_proposal_events` is a proposal lane. `novelty.py` itself DEFINES the sink and is not a
    lane, so it is excluded by name rather than by a substring that would also hide a real lane.
    """
    installers = set()
    for path in (ROOT / "looplab/engine").glob("*.py"):
        if path.name == "novelty.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "attr", None) == "_capture_proposal_events"):
                installers.add(f"looplab/engine/{path.name}")
    assert installers == set(PROPOSAL_TARGETS.values()), (
        f"the proposal lanes moved: sink installers are {sorted(installers)} but this file guards "
        f"{sorted(set(PROPOSAL_TARGETS.values()))} — add the new lane's offload target above")


def test_the_offload_walk_can_actually_fail():
    """NON-VACUITY, both halves: the walk must find a bare offload and must read a partial target."""
    bare = ast.parse("await anyio.to_thread.run_sync(functools.partial(self._consume_batch_proposal, s))")
    call = next(_offload_calls(bare))
    assert _partial_target(call) == "_consume_batch_proposal"
    assert not any(kw.arg == "limiter" for kw in call.keywords), (
        "a call with no limiter must read as having none, or the rule above passes on anything")
