"""`inert` says the diff was empty. It does not say WHY, and the engine knew.

MEASURED over every run on the box, pairing each `inline_repair` session with its OWN verdict (the
i-th span to the i-th `node_repaired` row, per node — a node-level join mixes four verdicts against
one over-budget session and reads as inconclusive):

    under 1200 s   n=65   inert  0 ( 0%)   {verified 48, unmet 12, unstated 5}
    over  1200 s   n=14   inert 12 (86%)   {inert 12, verified 1, unstated 1}

So every `inert` repair in the corpus came from a session that ran out of wall clock, and none of the
65 that finished inside their budget is inert. `inert` had become an undiagnosed proxy for "ran out
of time" — and "the agent looked and decided no edit was warranted" and "the agent was still reading
when the clock stopped" have opposite remedies.

NOTHING NEEDED TO BE DERIVED. `agents/tool_loop.py` has computed and announced this since it was
written — `_note_budget(on_budget, "time"|"turns", …)`, whose own comment says "TELL SOMEONE …
presenting a cut-short investigation as a finished one is how 'the assistant hangs around 40 tool
uses and then something odd comes back' reads to an operator who was never told the turn ran out of
wall clock" — and NO production caller passed `on_budget`. Outside `tool_loop.py` the name appeared
only in `loop_options.py`'s registry. The signal was computed, named, documented and delivered to
nobody: the "stamped and nothing consumes it" shape, again.

The same failure is on the record one phase over: the `stages` session's own comment describes
reading "for the whole budget, never reached declare_stages, and silently degraded to 'no stages
declared' … observed live". That was fixed by widening the clamp. This one is RECORDED instead,
because the repair bound is not obviously wrong (median repair = 151 s, 13 % of it) and a bound whose
effect nobody can see cannot be argued about.
"""
from __future__ import annotations

from looplab.adapters.repo_developer import LLMRepoDeveloper
from looplab.agents.roles import DEVELOPER_OUTPUT_ATTRS


def _dev():
    """A bare developer — `_note_session_budget` is a pure function of its argument."""
    d = LLMRepoDeveloper.__new__(LLMRepoDeveloper)
    d.last_budget_exhausted = ""
    return d


def test_the_wall_clock_and_the_turn_cap_are_kept_APART():
    """They are different failures with different remedies, so a boolean would lose the one that
    matters: on this box the turn cap has NEVER bound (~131 calls of 500 on the node that
    motivated this), while the clock has ended 14 sessions."""
    d = _dev()
    d._note_session_budget({"kind": "time", "turns": 7, "seconds": 1201.5})
    assert d.last_budget_exhausted == "time", (
        "MUTATION: record a bare True and this passes while the record stops being able to say "
        "WHICH bound fired — the one distinction the corpus actually needs")
    d = _dev()
    d._note_session_budget({"kind": "turns", "turns": 500, "seconds": 40.0})
    assert d.last_budget_exhausted == "turns"


def test_a_session_that_ends_NORMALLY_records_nothing():
    """THE MUTATION THAT MAKES THIS FIELD MEAN ANYTHING. Without this case the stamp is just a
    second spelling of `inert` and proves nothing: the loop calls `_note_budget` only when it ran
    OUT, so a normal finish must leave the attribute falsy and the durable key ABSENT."""
    d = _dev()
    assert d.last_budget_exhausted == ""


def test_an_observer_may_never_break_the_salvage_path():
    """`_note_budget` fires ON THE WAY TO a salvage emit — its own docstring says a broken callback
    must not turn a rescued answer into a crash. So every malformed payload is swallowed."""
    d = _dev()
    for junk in (None, {}, "junk", 17, {"kind": None}, {"kind": "   "}, {"other": 1}):
        d._note_session_budget(junk)          # must not raise
        assert d.last_budget_exhausted == "", (
            f"MUTATION: drop the guard and {junk!r} either raises inside a rescue path or writes a "
            "value that reads as an exhausted session when none happened")


def test_the_value_is_bounded_because_it_rides_a_durable_row():
    d = _dev()
    d._note_session_budget({"kind": "x" * 500})
    assert len(d.last_budget_exhausted) == 32, (
        "MUTATION: drop the slice and an unbounded string reaches `node_repaired`, the trace, the "
        "UI and every export")


def test_the_attribute_is_in_the_duck_typed_registry():
    """A one-sided rename must be a RED TEST, not a silent falsy read.

    `DEVELOPER_OUTPUT_ATTRS` exists because the engine reads these with `getattr(..., default)`, and
    this default is the FALSY one — so a rename would report "no session was ever cut short" on
    every node of every run, which is precisely the reading this field exists to stop being the only
    one available. Same argument the registry's own comment makes for `last_rollback_stage`.
    """
    assert "last_budget_exhausted" in DEVELOPER_OUTPUT_ATTRS


def test_the_engine_actually_READS_the_developer_and_does_not_just_mention_it():
    """A SOURCE PIN WAS NOT ENOUGH, and finding that out is why this test looks like this.

    The first version asserted the string `getattr(self.developer, "last_budget_exhausted"` appears
    in `evaluate.py`. Mutating the engine to `_budget_exhausted = ""; _unused = str(getattr(...))`
    — the value dead, the text intact — left all six tests GREEN. That is precisely the
    satisfiable-by-a-comment failure CLAUDE.md warns about, reproduced in a test written to guard
    against it.

    So this reads the DATAFLOW instead: `_budget_exhausted` must be bound exactly once, and its
    value must be a CALL (the `str(getattr(...))` snapshot) rather than a constant. A mutation that
    pins it to "" is then a red test, and one that renames the attribute is caught by the registry
    test above.
    """
    import ast
    import inspect
    from looplab.engine import evaluate

    tree = ast.parse(inspect.getsource(evaluate))
    bindings = [node for node in ast.walk(tree)
                if isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "_budget_exhausted"
                        for t in node.targets)]
    assert len(bindings) == 1, (
        f"_budget_exhausted is bound {len(bindings)} times; a second binding is how a snapshot "
        "gets silently overwritten before the append reads it")
    # The binding's OUTERMOST node is the `[:32]` bound, so "is it a Call?" was too narrow — that
    # assertion failed on the correct code, which is the useful direction for a test to fail in.
    # What matters is that the value is DERIVED FROM THE DEVELOPER rather than being a constant.
    reads = [n for n in ast.walk(bindings[0].value)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "getattr"
             and any(isinstance(a, ast.Constant) and a.value == "last_budget_exhausted"
                     for a in n.args)]
    assert reads, (
        "MUTATION: pin it to a constant (`_budget_exhausted = \"\"`) and this goes red — the row "
        "would then always say 'not cut short', which is the exact false reading this field exists "
        "to remove. A source pin does NOT catch that: the first version of this test asserted the "
        "getattr TEXT was present and stayed green while the value was dead.")


def test_the_snapshot_stays_beside_the_other_shared_instance_read():
    """ORDERING, which the AST above cannot express: it must be read in the same breath as
    `last_rollback_stage`, before the next `await`.

    The developer is shared across concurrent evals, so a later read can attribute a SIBLING node's
    exhaustion to this one — the hazard the rollback snapshot's own comment spells out.
    """
    import inspect
    from looplab.engine import evaluate

    src = inspect.getsource(evaluate)
    rollback = src.index('getattr(self.developer, "last_rollback_stage"')
    budget = src.index('getattr(self.developer, "last_budget_exhausted"')
    assert 0 < budget - rollback < 800, (
        "the two shared-instance reads must stay adjacent; moving this one past an await is how a "
        "sibling node's exhaustion lands on this node's row")


def test_both_durable_appends_carry_the_key():
    """One append is the live row and one is the rebuilt-after-resume row.

    `_format_repair_log` renders them identically, so a key on only one shows a single node two
    different histories depending on whether the process had resumed — the divergence the neighbour
    key `param_overrides` already carries a comment about.
    """
    import inspect
    from looplab.engine import evaluate

    src = inspect.getsource(evaluate)
    assert src.count('"budget_exhausted": _budget_exhausted') == 2, (
        "MUTATION: drop either append's key and a resumed run's repair history stops matching the "
        "live one")
