"""Two tuples that read as coverage and had no reader at all.

CLAUDE.md records the shape by name for `CARD_BUILD_SKIP_REASONS`: "a tuple that reads as coverage
nothing derives from". Both of these were introduced with the change that needed them and then
referenced only from a docstring — `EXTRA_METRIC_BACKFILL_KEYS` by the `:data:` role 34 lines below
it, `GATE_WRITE_OUTCOMES` by `apply_trust_gate`'s own — while the code beside each hard-coded the
same words. Add a fourth backfill key or a fourth gate outcome and every reader misses it, with two
authoritative-looking registries still sitting there.

The fix is not to delete them: each really is the vocabulary its neighbours must agree on. The fix
is the two-way scan every other registry in this tree has, so a member added to one side and not
the other is red rather than silently unread.
"""
from __future__ import annotations

import ast
import inspect

from looplab.core.models import EXTRA_METRIC_BACKFILL_KEYS, normalize_extra_metric_backfill
from looplab.events import trust_gate
from looplab.events.trust_gate import GATE_WRITE_OUTCOMES


def _string_constants(fn) -> set[str]:
    return {n.value for n in ast.walk(ast.parse(inspect.getsource(fn).lstrip()))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def test_the_backfill_normalizer_reads_exactly_the_registered_keys():
    """The normalizer is what a reader actually gets, so the registry has to describe IT.

    MUTATION: add a fourth key to `EXTRA_METRIC_BACKFILL_KEYS` and not to the normalizer -> red,
    naming it; drop one from the normalizer -> red the other way.
    """
    spelled = _string_constants(normalize_extra_metric_backfill)

    missing = [k for k in EXTRA_METRIC_BACKFILL_KEYS if k not in spelled]
    assert not missing, f"registered but never normalized: {missing}"


def test_the_normalizer_produces_only_registered_keys():
    """The other direction, DRIVEN rather than scanned: whatever a hand-edited log throws at it, the
    output may only carry words the registry names."""
    hostile = {"backfilled": True, "backfilled_at": 1.0, "precision_decimals": {"m": 3},
               "something_else": "x", "backfilled_by": "nobody"}
    out = normalize_extra_metric_backfill(hostile)

    assert set(out) <= set(EXTRA_METRIC_BACKFILL_KEYS), (
        f"the normalizer emitted a key the registry does not name: "
        f"{sorted(set(out) - set(EXTRA_METRIC_BACKFILL_KEYS))}")
    assert set(out) == {"backfilled", "backfilled_at", "precision_decimals"}, (
        "and the non-vacuous half: a full record must actually fill every one of them")
    assert normalize_extra_metric_backfill({"backfilled": False}) == {}, (
        "a record that does not assert the claim is dropped WHOLE — see the normalizer")


def test_every_outcome_apply_trust_gate_can_RETURN_is_registered():
    """`GATE_WRITE_OUTCOMES` exists "so a caller's match cannot silently miss one" — and nothing
    derived from it, so a fourth outcome would have been returned to two callers that match on the
    bare constants and handle neither.

    Re-derived from the function's own `ast.Return` nodes, which is the shape
    `tests/test_card_build_skip_reasons.py` uses for the same question.

    MUTATION: add a `return "wedged"` arm without registering it -> red, naming it.
    """
    tree = ast.parse(inspect.getsource(trust_gate.apply_trust_gate).lstrip())
    returned = {n.value.value for n in ast.walk(tree)
                if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str)}
    named = {n.value.id for n in ast.walk(tree)
             if isinstance(n, ast.Return) and isinstance(n.value, ast.Name)}
    resolved = {getattr(trust_gate, n) for n in named if isinstance(getattr(trust_gate, n, None), str)}

    unknown = (returned | resolved) - set(GATE_WRITE_OUTCOMES)
    assert not unknown, f"an outcome nothing registered: {sorted(unknown)}"
    assert returned | resolved, "the scan must actually find the returns it is about"


def test_the_registry_names_no_outcome_nothing_can_produce():
    """The other direction — a registered word no arm returns is the `cancelled` defect, and it
    reads as a case every caller must handle."""
    src = inspect.getsource(trust_gate)
    for outcome in GATE_WRITE_OUTCOMES:
        constant = next((name for name, value in vars(trust_gate).items()
                         if isinstance(value, str) and value == outcome
                         and name.startswith("GATE_WRITE_")), None)
        assert constant, f"{outcome!r} has no named constant"
        assert src.count(constant) >= 3, (
            f"{constant} appears {src.count(constant)}x — its definition, the registry, and at "
            "least one arm that returns it")
