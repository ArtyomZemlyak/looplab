"""A tripped budget ceiling PROPAGATES; it never degrades into a verdict.

The rule is already written down at three sites in the tree — `crash_repair.py` says it verbatim at
both of its judge calls ("the hard budget stop must propagate, not degrade to a verdict") and
`memo_verify.py` implements it at its one. `trust/verifier.py` pointed the other way, and it was the
site where that costs the most:

  * `BudgetExceeded` subclasses `Exception`, so a bare `except Exception` around the judge call
    swallows it. The report then reads `n_samples=0, score=None` — byte-identical to what an
    unreachable endpoint produces — so no caller can tell "the ceiling tripped" from "the provider
    is down", and they need opposite responses.
  * The sampling loop kept going. `verify` issues `samples` judge calls in a plain `for`, so a
    ceiling that tripped on the first one still bought the rest.
  * It is SELECTION machinery: `engine/verifier_tiebreak.py` reads `per_criterion['result_sound']`
    and can move the reported champion.

The polarity is one line and one `except` ordering away from inverting again, and nothing would go
red — the swallowed case returns a perfectly well-formed report. So the guard is structural: any
broad `except` guarding a call to the shared judge funnel in `trust/` must catch `BudgetExceeded`
FIRST. AST, never substrings (CLAUDE.md tier 3), because a comment saying "we re-raise the budget
stop" is exactly the mutation this has to survive.
"""
from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from looplab.core.errors import BudgetExceeded

TRUST = pathlib.Path(__file__).resolve().parents[1] / "looplab" / "trust"

# The one paid-call funnel both verifiers share (doc 25 CT-09). A `try` that reaches it is a `try`
# around money.
_PAID_CALLS = {"structured_judge", "verify_memo", "chat", "complete"}


def _broad(handler: ast.ExceptHandler) -> bool:
    t = handler.type
    names = ([t.id] if isinstance(t, ast.Name)
             else [e.id for e in t.elts if isinstance(e, ast.Name)] if isinstance(t, ast.Tuple)
             else [])
    return bool({"Exception", "BaseException"} & set(names))


def _catches_budget(handler: ast.ExceptHandler) -> bool:
    t = handler.type
    names = ([t.id] if isinstance(t, ast.Name)
             else [e.id for e in t.elts if isinstance(e, ast.Name)] if isinstance(t, ast.Tuple)
             else [])
    return "BudgetExceeded" in names


def _paid_tries():
    """Every `ast.Try` in `trust/` whose body calls the shared judge funnel."""
    for path in sorted(TRUST.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            called = {c.func.attr if isinstance(c.func, ast.Attribute) else
                      c.func.id if isinstance(c.func, ast.Name) else ""
                      for c in ast.walk(node) if isinstance(c, ast.Call)}
            if called & _PAID_CALLS:
                yield path, node


def test_the_scan_finds_the_paid_call_sites_at_all():
    """A structural guard whose scan matches nothing passes forever. Name the floor."""
    found = list(_paid_tries())

    assert found, "the paid-call scan matched no `try` in trust/ — the guard is vacuous"
    files = {p.name for p, _ in found}
    assert "verifier.py" in files, f"verifier.py fell out of the scan; matched {sorted(files)}"


def test_no_broad_except_around_a_paid_call_swallows_the_budget_stop():
    """MUTATION: delete `except BudgetExceeded: raise` from `verifier._one_sample` -> red here."""
    offenders = []
    for path, node in _paid_tries():
        broad = [h for h in node.handlers if _broad(h)]
        if not broad:
            continue
        # Order matters as much as presence: Python takes the FIRST matching handler, so a
        # `BudgetExceeded` clause after a broad one is dead code.
        first_broad = node.handlers.index(broad[0])
        if not any(_catches_budget(h) for h in node.handlers[:first_broad]):
            offenders.append(f"{path.name}:{node.lineno}")

    assert not offenders, (
        "a broad `except` around a paid call swallows the hard budget stop, so a tripped ceiling "
        f"reads as a provider failure and the loop keeps spending: {offenders}")


def test_the_verifier_really_re_raises_rather_than_merely_declaring_it():
    """Tier 1: drive it. A client whose judge call trips the ceiling must not yield a report."""
    from looplab.trust import verifier

    class _Tripped:
        def chat(self, *a, **k):
            raise BudgetExceeded("ceiling")

    calls = {"n": 0}

    def _boom(*a, **k):
        calls["n"] += 1
        raise BudgetExceeded("ceiling")

    verifier_judge = getattr(verifier, "structured_judge", None)
    assert verifier_judge is None, (
        "structured_judge is imported at CALL time inside `_one_sample`; if that moved to module "
        "scope, patch it there instead — this test would otherwise patch nothing")

    import looplab.trust.judge as judge
    original = judge.structured_judge
    judge.structured_judge = _boom
    try:
        with pytest.raises(BudgetExceeded):
            verifier.verify("subject", "evidence", verifier.selection_criteria(),
                            client=_Tripped(), samples=5)
    finally:
        judge.structured_judge = original

    assert calls["n"] == 1, (
        f"the ceiling tripped on the first sample and {calls['n']} were issued — the whole point is "
        "that the remaining samples are never bought")


def test_the_sibling_that_already_had_the_rule_still_has_it():
    """`memo_verify` is the precedent this was aligned to; a regression there is the same defect."""
    source = inspect.getsource(__import__("looplab.trust.memo_verify", fromlist=["x"]))
    tree = ast.parse(source)
    budget_first = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Try)
        and any(_catches_budget(h) for h in node.handlers)
        and all(isinstance(s, ast.Raise) for h in node.handlers if _catches_budget(h)
                for s in h.body)]

    assert budget_first, "memo_verify no longer re-raises the budget stop"
