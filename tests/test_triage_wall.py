"""The CRASH/TIMEOUT triage judge gets a WALL, and it is the one the repair after it already had.

THE MEASUREMENT (`runs/e5small-dr-unified-v8` node 2, 2026-08-27)
----------------------------------------------------------------
The `train` stage timed out at 09:07:19. The engine did not return to work until `node_repaired` at
11:07:32 — two hours, both H200s at 0%. The operation spans split it:

    triage         node 2 attempt 1 :  88.3 min   206 provider calls   19,156,560 tokens
    inline_repair  node 2 attempt 1 :  31.9 min    46 provider calls    2,467,305 tokens
    triage         node 3 attempt 1 :   4.3 min    18 provider calls      405,677 tokens

What the 88 minutes bought was ONE 663-line file. `training/loss.py` was read 78 times through 34
distinct windows, and the identical twelve-window sweep — `(1,60) (61,60) … (561,40)` — repeats
verbatim SIX times. 664 of its 667 lines were re-served at least five times; one nine times. The
transcript grew 14,548 -> 160,671 prompt tokens and all 206 calls re-sent it.

WHY NOTHING ALREADY IN THE TREE STOPPED IT — each of these was checked against that session, not
assumed, because the cheap fix in each case would have been to retune one of them:

* `StuckDetector` catches 1-cycles and 2-cycles and says so in its own docstring. The longest
  CONSECUTIVE identical (action, observation) run in the session was **2**, against a threshold of 4.
* Generalising it to exact adjacent p-cycles would not have helped: other tools interleave, so the
  first exact repeat at ANY period 1..20 lands at tool call **248 of 278**.
* The round-robin gap was already known, and was already answered with a NUDGE —
  `tool_loop._REPEAT_NOTE`, *"we only TELL the model it is repeating itself so it can stop on its
  own"*. It fired **57 times** in this one triage. The model did not stop. This wall is that rung
  escalated on evidence, not instead of it.
* `agent_emit_after` / `agent_emit_force` (300 / 500) are TURN counts sized for the pilot's
  self-driving loop. 206 turns fits inside both, so the whole 88 minutes sat within a healthy turn
  budget. Nothing was measuring wall clock, which is the quantity the dark GPU is denominated in.
* A session-scoped "this exact call+result has been served m times" rung was measured and REFUSED:
  over 2,472 sessions the max-serve distribution is {1: 1782, 2: 285, 3: 181, 4: 100, 5: 48, 6: 35,
  >=7: 41}, and this pathological session's own max is only **5** — lower than 41 healthy ones. At
  m=3 it fires on 259 of the 586 sessions with >=40 calls. The signal does not separate them.

WHY 1200 s. Across the 124 triage decisions in the eight runs carrying `spans.jsonl`, the worst
prior triage wall is 11.6 min and the worst prior call count is 91. A 20-minute ceiling fires on none
of them and would have cut this one at 20 min instead of 88. It is deliberately the SAME number as
`developer_session_time_budget_s`: triage and the repair it hands to run one after the other on the
same eval-blocking thread, and two unequal walls on one blocked thread would be a number nobody
could explain.
"""
from __future__ import annotations

import ast
import inspect

from looplab.agents.unified_agent import UnifiedAgent
from looplab.core.config import Settings


class _Client:
    """The pilot client is never reached: every test below replaces `drive_tool_loop` itself."""


def _drive_capturing(seen):
    def _drive(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        seen.append(opts)
        return finalize({"action": "repair", "rationale": "r"})
    return _drive


def _triage(agent, **kw):
    node = type("N", (), {"id": 1, "code": ""})()
    return agent.triage_crash(node, "boom", 1, **kw)


def _run(make_agent, **triage_kw) -> dict:
    """Drive one `triage_crash` with the loop stubbed; return the options it was handed."""
    seen: list[dict] = []
    import looplab.agents.agent as agent_mod
    original = agent_mod.drive_tool_loop
    agent_mod.drive_tool_loop = _drive_capturing(seen)
    try:
        _triage(make_agent(), **triage_kw)
    finally:
        agent_mod.drive_tool_loop = original
    assert len(seen) == 1
    return seen[0]


def _agent(**kw):
    return UnifiedAgent(researcher=object(), developer=object(), pilot_client=_Client(), **kw)


# ------------------------------------------------------------------ the wall itself
def test_an_unwalled_triage_loop_gets_the_triage_wall():
    """The defect, stated as a property: with no wall configured anywhere, the loop that blocks the
    eval thread must not be handed an unlimited one."""
    opts = _run(lambda: _agent(triage_time_budget_s=1200.0), tools=object())
    assert opts.get("time_budget_s") == 1200.0


def test_the_wall_does_not_depend_on_the_per_call_toolset():
    """It is about the THREAD this loop blocks, not about which tools this call brought — so it must
    land on the no-`tools` branch too. (Mutation: move the application inside `_pilot_emit`'s
    `extra_tools is not None` block and this is the test that goes red.)"""
    opts = _run(lambda: _agent(triage_time_budget_s=1200.0))
    assert opts.get("time_budget_s") == 1200.0


def test_an_operators_own_finite_wall_is_never_shortened():
    """Shortening an operator's explicit budget is the same sin as `0 + n` turning their "no cap"
    into a cap, read from the other end. A configured 60 s stays 60 s even though 1200 is on offer."""
    opts = _run(lambda: _agent(agent_time_budget_s=60.0, triage_time_budget_s=1200.0),
                tools=object())
    assert opts.get("time_budget_s") == 60.0


def test_a_wall_of_zero_leaves_the_loop_unwalled():
    """The default for a hand-built agent is 0 = unlimited, so every caller that never asked for a
    wall is unchanged. (Mutation: default `wall_when_unbounded` to 1200 and this goes red.)"""
    opts = _run(lambda: _agent(), tools=object())
    assert not opts.get("time_budget_s")
    assert UnifiedAgent.__init__.__kwdefaults__ is None or True  # ctor default asserted below
    sig = inspect.signature(UnifiedAgent.__init__)
    assert sig.parameters["triage_time_budget_s"].default == 0.0


def test_the_pilots_own_action_loop_is_not_walled_by_this():
    """Scope. `choose_action` is not the eval-blocking failure path and carries no measurement of its
    own, so the wall must not leak onto it. (Mutation: pass `wall_when_unbounded` at the
    `choose_action` call site too and this goes red.)"""
    seen: list[dict] = []
    import looplab.agents.agent as agent_mod
    original = agent_mod.drive_tool_loop
    agent_mod.drive_tool_loop = _drive_capturing(seen)
    try:
        from looplab.core.models import RunState
        _agent(triage_time_budget_s=1200.0).choose_action(
            RunState(), [{"kind": "stop"}], {"kind": "stop"})
    finally:
        agent_mod.drive_tool_loop = original
    assert len(seen) == 1
    assert not seen[0].get("time_budget_s")


# ------------------------------------------------------------------ the number, and its wiring
def test_the_wall_is_the_same_number_the_repair_after_it_already_had():
    """One blocked thread, one number. If these two ever diverge, the comment in `core/config.py`
    that justifies 1200 by pointing at its neighbour has stopped being true."""
    s = Settings()
    assert s.triage_time_budget_s == 1200.0
    assert s.triage_time_budget_s == s.developer_session_time_budget_s


def test_the_factory_hands_the_setting_to_the_agent():
    """A default nobody threads is a default nobody has. Asserted on the AST rather than by building
    the whole role stack, so a RENAME of either side is what goes red."""
    import looplab.agents.factory as factory
    tree = ast.parse(inspect.getsource(factory))
    threaded = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "UnifiedAgent"
        and any(kw.arg == "triage_time_budget_s" for kw in node.keywords)
    ]
    assert len(threaded) == 1
    call = next(kw.value for kw in threaded[0].keywords if kw.arg == "triage_time_budget_s")
    # `getattr(settings, "triage_time_budget_s", 1200.0)` — the name and the fallback both matter:
    # a typo'd attribute name would silently fall back and the wall would look wired while being
    # whatever the literal says.
    assert isinstance(call, ast.Call) and call.func.id == "getattr"
    assert call.args[1].value == "triage_time_budget_s"
    assert call.args[2].value == Settings().triage_time_budget_s
