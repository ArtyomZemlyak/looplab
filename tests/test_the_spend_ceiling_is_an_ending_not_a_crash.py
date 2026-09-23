"""Reaching `llm_budget_usd` is how a run ENDS, and it was being recorded as a provider failure.

`BudgetExceeded` is an `Exception`, so the developer session's blanket
`except Exception -> "(developer error: …)"` turned "this run has spent its $1.00" into the
developer-crash sentinel. The orchestrator answers that sentinel by PAUSING the run with
*"auto-paused: a Developer session crashed (LLM unreachable or a hard error, unresolved within the
node) — resume once it's fixed"*.

Measured over the probe corpus on 2026-09-04: of the 105 runs that reached full budget, **88 end
cleanly with `run_finished / budget_exhausted` and 16 end paused** — every one of the 16 at or past
its ceiling (median spend $1.0041 against $1.00) and every one paused **0.1–0.2 s after its last LLM
call**, which is the next call being refused, not a provider going away. The message left those runs
marked OWED WORK when they were complete, and §213 records what that costs: I read it, resumed
`freeB3`, and it spent $0.1056 past its cap before being stopped by pid.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from looplab.core.errors import BudgetExceeded
from looplab.core.models import DEVELOPER_ERROR_PREFIX, is_developer_error

REPO = Path(__file__).resolve().parents[1]


def test_budget_exceeded_is_an_exception_so_a_blanket_handler_catches_it():
    """The premise. If this ever stops being true the fixes below are dead code, not safety."""
    assert issubclass(BudgetExceeded, Exception)


def test_the_developer_handler_re_raises_the_ceiling_before_the_blanket_catch():
    src = (REPO / "looplab" / "adapters" / "repo_developer.py").read_text(encoding="utf-8")
    # PARSED, not grepped. The first version asserted `"raise" in <handler text>` and a mutation
    # that replaced the statement with `pass` stayed GREEN, because the comment above it says
    # "Re-raised rather than translated". A word in prose is not a control-flow statement.
    import ast
    tree = ast.parse(src)
    handlers = [h for node in ast.walk(tree) if isinstance(node, ast.Try)
                for h in node.handlers
                if isinstance(h.type, ast.Name) and h.type.id in ("BudgetExceeded",
                                                                  "OperatorRefusal")]
    assert handlers, ("repo_developer no longer catches the refusal family before its blanket "
                      "handler; the ceiling is back to being reported as a developer crash")
    assert any(isinstance(stmt, ast.Raise) for h in handlers for stmt in ast.walk(h)), (
        "the refusal handler does not re-raise; it swallows the ceiling instead")
    # AND THE FAULTS MUST STILL GET THE SENTINEL. Mutation turned the guard into `if False:`, which
    # sends an outage or a bad key down the re-raise path too -- the run then dies instead of
    # pausing, and the circuit breaker a 403 blowout of 67 dead nodes was written for never
    # engages. The guard has to be a real test of `is_run_ending`, not a constant, so the assertion
    # is on the If's CONDITION rather than on the presence of the branch.
    # EITHER SPELLING OF THE SAME QUESTION. `is_run_ending` is `isinstance(exc, BudgetExceeded)`;
    # `budget_stop_leaf` walks `exceptions`/`__cause__`/`__context__` for the same class. The
    # `except BudgetExceeded: raise` clause pinned above already takes every BARE ceiling, so the
    # narrow predicate is unreachable in this handler and the wider one is the only thing that can
    # still answer YES — pinning the narrow NAME would pin the dead branch. The property is that the
    # handler ASKS, not which of the two it asks with.
    guards = [n for h in handlers for n in ast.walk(h)
              if isinstance(n, ast.If)
              and ("is_run_ending" in ast.dump(n.test)
                   or "budget_stop_leaf" in ast.dump(n.test))]
    assert guards, ("the handler no longer asks `is_run_ending`, so every operator refusal takes "
                    "the same path -- either all of them re-raise or none of them do")
    assert any(isinstance(stmt, ast.Return) for g in guards for stmt in ast.walk(g)), (
        "the non-ending branch no longer returns the developer-crash sentinel")
    # ORDER IS THE WHOLE FIX: a re-raise placed after `except Exception` never runs. That is now
    # DRIVEN below (`test_a_ceiling_raised_inside_the_build_ends_the_run_instead_of_pausing_it`).
    # It used to be `src.index("except OperatorRefusal as e:") < src.index(<the blanket handler's
    # full line, noqa comment included>)` — anchored on a COMMENT to tell this handler from the
    # file's two other blanket ones, and satisfied by any comment spelling `except OperatorRefusal
    # as e:` above the blanket while the real clause sat after it (review 2026-09-22, TST-05).


# The build session, driven through the ONE documented seam — `looplab.agents.agent.drive_tool_loop`
# — as tests/test_repo_run_epilogue.py drives it: the stages and plan phases answer minimally, and
# the implement session raises whatever the case under test says the provider raised.
_FIXTURE = REPO / "tests" / "fixtures" / "repo_fixture"


def _developer_whose_build_raises(monkeypatch, raised: BaseException):
    import sys

    import looplab.agents.agent as agent_mod
    from looplab.adapters.repo_task import EvalSpec, LLMRepoDeveloper, RepoTask

    def fake_loop(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        name = emit_spec["function"]["name"]
        if name == "declare_stages":
            return finalize({"stages": [{"name": "train", "command": ["python", "train.py"]}]})
        if name == "propose_plan":
            return finalize({"steps": [{"title": "A", "detail": "a"}, {"title": "B", "detail": "b"}]})
        raise raised

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)
    task = RepoTask(id="r", goal="g", direction="max", editable_path=str(_FIXTURE),
                    edit_surface=["*.py"], protect=[],
                    eval=EvalSpec(command=[sys.executable, "main.py"],
                                  metric={"kind": "stdout_json", "key": "metric"}))
    return LLMRepoDeveloper(object(), task, plan_decompose=False)


def _idea():
    from looplab.core.models import Idea

    return Idea(operator="draft", params={}, rationale="x")


def _wrapped_ceiling():
    from looplab.core.errors import ConfigRefusal

    wrapped = ConfigRefusal("the session failed")
    wrapped.__cause__ = BudgetExceeded("LLM spend ceiling reached: $1.0003 of the $1.0000")
    return wrapped


@pytest.mark.parametrize("make", [
    # A BARE ceiling: taken by the `except BudgetExceeded: raise` clause.
    lambda: BudgetExceeded("LLM spend ceiling reached: $1.0003 of the $1.0000"),
    # A WRAPPED one: the only kind the `except OperatorRefusal` clause has left to decide, which it
    # answers with `budget_stop_leaf`. Moving that clause AFTER the blanket `except Exception` would
    # keep every clause the AST test above pins and still turn this ending into a crash sentinel.
    _wrapped_ceiling,
], ids=["bare", "wrapped"])
def test_a_ceiling_raised_inside_the_build_ends_the_run_instead_of_pausing_it(monkeypatch, make):
    """The order of the handlers, as behaviour: a spend ceiling reached inside the developer session
    leaves `_run` as the exception it is. Returned as `(developer error: …)` it would PAUSE a run
    that had reached its end — the 16 of 105 full-budget runs this module's docstring measured."""
    raised = make()
    dev = _developer_whose_build_raises(monkeypatch, raised)
    with pytest.raises(type(raised)) as info:
        dev._run(_idea())
    assert info.value is raised


def test_the_repair_path_re_raises_it_too():
    src = (REPO / "looplab" / "engine" / "evaluate.py").read_text(encoding="utf-8")
    import ast
    tree = ast.parse(src)
    guards = [n for n in ast.walk(tree) if isinstance(n, ast.If)
              and ("BudgetExceeded" in ast.dump(n.test) or "is_run_ending" in ast.dump(n.test)
                   or "budget_stop_leaf" in ast.dump(n.test))]
    assert guards, "the repair path no longer asks whether the refusal is a run ENDING at all"
    assert any(isinstance(stmt, ast.Raise) for g in guards for stmt in g.body), (
        "the repair path still wraps a spend-ceiling refusal in the developer-crash sentinel")


def test_a_ceiling_refusal_would_otherwise_read_as_a_crash():
    """What the old behaviour produced, so the test says what it is preventing rather than only
    that a line exists."""
    exc = BudgetExceeded("LLM spend ceiling reached: $1.0003 of the $1.0000 set by llm_budget_usd")
    sentinel = f"{DEVELOPER_ERROR_PREFIX} {exc})"
    assert is_developer_error(sentinel), (
        "the sentinel the old handler built no longer routes to the crash path, so this test is "
        "measuring nothing")
    assert "spend ceiling" in sentinel


def test_both_handlers_still_catch_everything_else(monkeypatch):
    """The blanket handler exists for a reason -- a developer hiccup must not crash the engine --
    and narrowing it to nothing would trade one defect for a worse one.

    DRIVEN (review 2026-09-22, TST-05): an ordinary exception out of the build session comes back as
    the developer-crash sentinel, not as a raise. This used to be two substring pins — the blanket
    handler's line WITH its noqa comment, then `DEVELOPER_ERROR_PREFIX` somewhere in the 600
    characters after it — which a deleted handler still satisfied while the comment line survived."""
    dev = _developer_whose_build_raises(monkeypatch, RuntimeError("a developer hiccup"))
    out = dev._run(_idea())
    assert is_developer_error(out), out
    assert out.startswith(DEVELOPER_ERROR_PREFIX) and "a developer hiccup" in out, out


def test_the_five_operator_refusals_are_not_alike():
    """Naming the distinction is the fix; this pins which side each sibling is on.

    `LLMError`, `LLMCredentialError`, `ConfigRefusal` and `EnvironmentRefusal` are FAULTS — an
    outage, a bad key, a misconfiguration — and the developer session normalises them into its crash
    sentinel on purpose, so the run pauses and "resume once it's fixed" is the right sentence.
    `BudgetExceeded` is the run REACHING ITS END with a champion in hand. Over-generalising the §228
    fix to all five would break the circuit breaker that a 403 blowout of 67 dead nodes was written
    to stop; under-generalising it is the defect itself.
    """
    from looplab.core.errors import (BudgetExceeded as B, ConfigRefusal, EnvironmentRefusal,
                                     LLMCredentialError, LLMError, OperatorRefusal, is_run_ending)
    assert is_run_ending(B("LLM spend ceiling reached: $1.0003 of the $1.0000"))
    for fault in (LLMError("gateway 503"), LLMCredentialError("401"),
                  ConfigRefusal("bad -s value"), EnvironmentRefusal("no docker")):
        assert isinstance(fault, OperatorRefusal)
        assert not is_run_ending(fault), (
            f"{type(fault).__name__} would now be re-raised instead of pausing the run; the "
            "circuit breaker exists because a 403 blowout once spun 67 dead nodes")
    assert not is_run_ending(ValueError("an ordinary bug")), (
        "an ordinary exception is neither a refusal nor an ending")


def test_a_wrapped_ceiling_is_what_the_two_catch_sites_have_left_to_decide():
    """The property, driven — and it is the reason the predicate at those sites CHANGED.

    Both handlers now stand behind an `except BudgetExceeded: raise`, so every BARE ceiling is
    already gone by the time they run. `is_run_ending` is literally `isinstance(exc,
    BudgetExceeded)`, so as the guard it could not fire at all: the only exception those clauses
    can still see is a ceiling somebody WRAPPED — re-raised as an `LLMError`/`ConfigRefusal` from
    inside the session, or carried out of a nested task group in an `ExceptionGroup`. Those are
    exactly the ones that fell through to the developer-crash sentinel and paused a finished run.
    """
    from looplab.core.errors import BudgetExceeded as B, ConfigRefusal, LLMError, is_run_ending
    from looplab.core.errors import budget_stop_leaf

    ceiling = B("LLM spend ceiling reached: $1.0003 of the $1.0000")
    wrapped: list[BaseException] = [LLMError("session failed"), ConfigRefusal("bad -s value")]
    for w in wrapped:
        w.__cause__ = ceiling
    try:
        raise ExceptionGroup("session", [RuntimeError("unrelated"), ceiling])
    except ExceptionGroup as eg:
        wrapped.append(eg)

    for w in wrapped:
        assert not is_run_ending(w), (
            f"{type(w).__name__} carrying a ceiling answers False to the narrow isinstance — "
            "that is why it cannot be the guard at these sites")
        assert budget_stop_leaf(w) is ceiling, (
            f"{type(w).__name__} must yield the ceiling it carries, or the run that spent its "
            "allowance is filed as a dead provider and paused")


def test_the_catch_sites_ask_the_leaf_walking_predicate_rather_than_a_type():
    """Statable rule, AST-checked: each site CALLS `budget_stop_leaf` and neither spells out its
    own `isinstance(..., BudgetExceeded)` behind the bare-ceiling clause. Two copies of a rule
    drift (§204) — and the earlier version of this guard asserted `"is_run_ending" in src`, which
    both files still satisfy IN COMMENTS ALONE after the predicate was swapped out. A word in
    prose is not a call (CLAUDE.md: a guard test must not be satisfiable by a COMMENT).
    """
    import ast
    # PER FUNCTION, not per file. A file-level scan is green while the guard itself is mutated
    # back to `isinstance`, because `budget_stop_leaf` is also called by the OTHER site in
    # `evaluate.py` (`_eval_settle_outcome`'s ending check). The rule is about these two clauses.
    sites = (("looplab/adapters/repo_developer.py", "_run"),
             ("looplab/engine/evaluate.py", "_eval_apply_repair"))
    for rel, fn_name in sites:
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        fns = [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn_name]
        assert len(fns) == 1, f"{rel}::{fn_name} is not a single function any more: {len(fns)}"
        called = {n.func.id for n in ast.walk(fns[0])
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "budget_stop_leaf" in called, (
            f"{rel}::{fn_name} no longer CALLS the predicate — a mention in a comment is not a "
            "guard, and that is exactly how the previous version of this test went vacuous")
        assert "is_run_ending" not in called, (
            f"{rel}::{fn_name} went back to the narrow isinstance, which cannot fire behind the "
            "`except BudgetExceeded: raise` clause that already took every bare ceiling")
