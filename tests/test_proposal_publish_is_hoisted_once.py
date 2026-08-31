"""The capture->offload->publish triple, and the 4-tuple it feeds, live in ONE place.

Before 2026-08-31 the tuple was unpacked and appended at FOUR sites — the two offloaded proposal
lanes' `finally` blocks and `speculation.py`'s two `result.audit_events` loops — and the batch
wrapper's own docstring stated the sole-writer rule it then re-implemented one lane over. Four
copies are four chances to forget a field: a fifth tuple member, or a tail-fenced `append_many`,
would have to land in all of them or diverge silently.

POLICY is deliberately NOT hoisted. Layer 5 publishes a raw stage's prefix only on the branches
where the work was really handed on; the offload lanes publish unconditionally because a refused
proposal is when the receipt matters most. The `if` stays at the call site in both directions —
this file guards the MECHANICS, and `test_offload_lane_writes_no_folded_events.py` guards the policy.
"""
from __future__ import annotations

import ast
import inspect
import pathlib

from looplab.engine.novelty import NoveltyGateMixin

ROOT = pathlib.Path(__file__).resolve().parents[1]
OWNER = "looplab/engine/novelty.py"


def _unpacks_the_audit_tuple(node) -> bool:
    """A `for a, b, c, d in …:` loop that appends a TRACED row — the shape copied four times.

    The trace keywords are what make this the proposal-audit publish and not any other 4-tuple
    loop: the tree has several (`events/traceview.py`, `tools/cross_run_tools.py`) that unpack four
    values and append somewhere, and flagging those would make the rule noise. Driven both ways in
    `test_the_ast_shapes_can_actually_fail`.
    """
    if not isinstance(node, ast.For):
        return False
    target = node.target
    if not (isinstance(target, ast.Tuple) and len(target.elts) == 4):
        return False
    for inner in ast.walk(node):
        if not (isinstance(inner, ast.Call) and getattr(inner.func, "attr", None) == "append"):
            continue
        kwargs = {kw.arg for kw in inner.keywords}
        if {"trace_id", "span_id"} <= kwargs:
            return True
    return False


def test_the_publish_loop_exists_exactly_once_and_it_is_in_the_sink_s_own_module():
    """Mutation: paste the loop back into any lane and this names the file and the line."""
    sites = []
    for path in sorted((ROOT / "looplab").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if _unpacks_the_audit_tuple(node):
                sites.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert sites == [s for s in sites if s.startswith(OWNER)], (
        "the proposal audit tuple is unpacked outside its owning module — call "
        f"`_publish_proposal_events` instead:\n  " + "\n  ".join(sites))
    assert len(sites) == 1, f"one copy, not {len(sites)}: {sites}"


def test_the_offload_triple_is_not_hand_written_outside_the_helper():
    """`_capture_proposal_events` may still be installed WORKER-side (Layer 5 ferries its prefix out
    through `SpecRawStageResult.audit_events` rather than publishing it). What must not recur is a
    lane that opens the sink around its OWN `to_thread` offload by hand."""
    offenders = []
    for path in sorted((ROOT / "looplab").rglob("*.py")):
        if str(path.relative_to(ROOT)) == OWNER:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.With, ast.AsyncWith)):
                continue
            opens_sink = any(
                isinstance(item.context_expr, ast.Call)
                and getattr(item.context_expr.func, "attr", None) == "_capture_proposal_events"
                for item in node.items)
            if not opens_sink:
                continue
            offloads = any(isinstance(inner, ast.Call)
                           and getattr(inner.func, "attr", None) == "run_sync"
                           for inner in ast.walk(node))
            if offloads:
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, (
        "these lanes hand-roll capture->offload — use `_offload_under_proposal_sink`, which also "
        f"carries the proposal pool and the publish-in-finally rule:\n  " + "\n  ".join(offenders))


def test_the_helper_publishes_from_a_finally_and_defaults_to_the_proposal_pool():
    """Both durability properties, read off the helper itself rather than off a call site."""
    src = inspect.getsource(NoveltyGateMixin._offload_under_proposal_sink)
    fn = ast.parse(src.lstrip()).body[0]
    tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try) and n.finalbody]
    assert tries, "the publish must be in a `finally` or a raise discards the buffer"
    published = any(isinstance(c, ast.Call)
                    and getattr(c.func, "attr", None) == "_publish_proposal_events"
                    for t in tries for n in t.finalbody for c in ast.walk(n))
    assert published, "the `finally` must be the thing that publishes"

    defaults = [c for c in ast.walk(fn)
                if isinstance(c, ast.Call) and getattr(c.func, "attr", None) == "setdefault"]
    assert any(isinstance(c.args[0], ast.Constant) and c.args[0].value == "limiter"
               for c in defaults if c.args), (
        "a new lane must get the proposal pool by DEFAULT — starving by omission is the whole "
        "hazard `proposal_limiter` was added for")


def test_publish_is_tolerant_of_an_empty_or_absent_buffer_and_counts_what_landed():
    """Driven, not read: the `finally` runs on paths where the sink never filled."""
    class _Store:
        def __init__(self): self.rows = []
        def append(self, event_type, data, *, trace_id=None, span_id=None):
            self.rows.append((event_type, data, trace_id, span_id))

    engine = NoveltyGateMixin.__new__(NoveltyGateMixin)
    engine.store = _Store()
    assert engine._publish_proposal_events(None) == 0
    assert engine._publish_proposal_events([]) == 0
    assert engine._publish_proposal_events([("novelty_rejected", {"a": 1}, "t", "s")]) == 1
    assert engine.store.rows == [("novelty_rejected", {"a": 1}, "t", "s")]


def test_the_ast_shapes_can_actually_fail():
    """NON-VACUITY for both walks — a matcher that always answers False guards nothing."""
    traced = "for a, b, c, d in rows:\n    self.store.append(a, b, trace_id=c, span_id=d)"
    assert _unpacks_the_audit_tuple(ast.parse(traced).body[0])
    assert not _unpacks_the_audit_tuple(ast.parse("for a, b in rows:\n    x.append(a)").body[0])
    assert not _unpacks_the_audit_tuple(ast.parse("for a, b, c, d in rows:\n    total += a").body[0])
    # The discriminator itself: four values appended WITHOUT the trace keywords is one of the
    # unrelated loops this rule must leave alone, or the guard becomes noise nobody reads.
    assert not _unpacks_the_audit_tuple(
        ast.parse("for a, b, c, d in rows:\n    out.append((a, b))").body[0])
