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
    guards = [n for h in handlers for n in ast.walk(h)
              if isinstance(n, ast.If) and "is_run_ending" in ast.dump(n.test)]
    assert guards, ("the handler no longer asks `is_run_ending`, so every operator refusal takes "
                    "the same path -- either all of them re-raise or none of them do")
    assert any(isinstance(stmt, ast.Return) for g in guards for stmt in ast.walk(g)), (
        "the non-ending branch no longer returns the developer-crash sentinel")
    # ORDER IS THE WHOLE FIX: a re-raise placed after `except Exception` never runs. Anchored on
    # the FULL comment of the handler in question -- this file has three blanket handlers and
    # `str.index` finds the first, which is a different one nine hundred lines earlier.
    blanket = "except Exception as e:  # noqa: BLE001 - never crash the engine on a developer hiccup"
    assert src.index("except OperatorRefusal as e:") < src.index(blanket)


def test_the_repair_path_re_raises_it_too():
    src = (REPO / "looplab" / "engine" / "evaluate.py").read_text(encoding="utf-8")
    import ast
    tree = ast.parse(src)
    guards = [n for n in ast.walk(tree) if isinstance(n, ast.If)
              and ("BudgetExceeded" in ast.dump(n.test) or "is_run_ending" in ast.dump(n.test))]
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


def test_both_handlers_still_catch_everything_else():
    """The blanket handler exists for a reason -- a developer hiccup must not crash the engine --
    and narrowing it to nothing would trade one defect for a worse one."""
    src = (REPO / "looplab" / "adapters" / "repo_developer.py").read_text(encoding="utf-8")
    blanket = "except Exception as e:  # noqa: BLE001 - never crash the engine on a developer hiccup"
    assert blanket in src
    body = src.split(blanket, 1)[1][:600]
    assert f"{{DEVELOPER_ERROR_PREFIX}} {{e}}" in body or "DEVELOPER_ERROR_PREFIX" in body, body[:300]


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


def test_the_catch_sites_ask_the_predicate_rather_than_a_type():
    """Both sites route on `is_run_ending`, so the distinction lives in one place. A second
    `isinstance(..., BudgetExceeded)` spelled out at a call site is how two copies of a rule drift
    (§204)."""
    for rel in ("looplab/adapters/repo_developer.py", "looplab/engine/evaluate.py"):
        src = (REPO / rel).read_text(encoding="utf-8")
        assert "is_run_ending" in src, f"{rel} no longer asks the predicate"
