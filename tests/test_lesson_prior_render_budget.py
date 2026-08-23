"""A cross-run lesson must reach a role's prompt WHOLE, or the delivery chain is decoration.

Measured 2026-08-23 on `runs/e5small-dr-unified-v4`. Every link worked: the lesson was untagged
(so shared with both roles), its task matched exactly, the priors block reached `card_build`
(562 spans), and top-5 ranking PICKED it. The Developer of node 8 was then shown:

    A pipeline stage that short-circuits and skips (re)writing its declared artifact when the
    [redacted preview: original_chars=372 sha256=a29ce9b4...]

`max_chars` is a budget for the visible text AND for the truncation receipt, and that receipt is
111 characters of sha256. Eighty-nine characters of guidance survived; the sentence stops exactly
before "so every stage must write its declared artifact unconditionally on each run". Nodes 6, 8
and 9 each hit that stage failure independently.

The receipt itself is right and is NOT changed here — a truncated persisted value must stay
identifiable. What changes is that a caller wanting N visible characters now ASKS for N plus what
the receipt costs, derived rather than guessed.
"""
from __future__ import annotations

import pytest

from looplab.core.redact import truncation_receipt_chars
from looplab.engine.lessons_priors import LESSON_STATEMENT_CHARS
from looplab.trust.cross_run import cross_run_text

# The real row from the shared store, verbatim — 372 characters.
_REAL_LESSON = (
    "A pipeline stage that short-circuits and skips (re)writing its declared artifact when the file "
    "already exists leaves a stale artifact that silently freezes the metric, so every stage must "
    "write its declared artifact unconditionally on each run (node #2's mine stage exited 0 yet left "
    "a pre-existing negatives.parquet, which the artifact-contract check correctly rejected)."
)


def _render(statement: str, *, budget: int = LESSON_STATEMENT_CHARS) -> str:
    """Exactly what `_render_role_prior` does for one statement."""
    return cross_run_text(statement,
                          max_chars=budget + truncation_receipt_chars(statement),
                          single_line=True, entropy=True).strip()


def test_the_actionable_half_of_a_real_lesson_survives():
    """THE PROPERTY, on the row that cost three nodes. Not "it is longer now" — the specific clause
    that tells a Developer what to DO has to be present."""
    out = _render(_REAL_LESSON)
    assert "redacted preview" not in out, out
    assert "must write its declared artifact unconditionally on each run" in out
    assert out.startswith("A pipeline stage that short-circuits")


def test_the_old_budget_is_what_destroyed_it():
    """The counter-example, pinned so the regression is visible rather than argued: at the shipped
    `max_chars=200` the same row loses its verb."""
    old = cross_run_text(_REAL_LESSON, max_chars=200, single_line=True, entropy=True).strip()
    assert "redacted preview" in old
    assert "unconditionally on each run" not in old
    visible = old.split("[redacted preview")[0]
    assert len(visible) < 100, f"expected ~89 visible chars under the old cap, got {len(visible)}"


def test_the_receipt_cost_is_derived_not_guessed():
    """It tracks the digit count of `original_chars`, so a hard-coded 111 is right only until a
    statement crosses a power of ten."""
    assert truncation_receipt_chars("x" * 99) == truncation_receipt_chars("x" * 100) - 1
    assert truncation_receipt_chars("x" * 999) == truncation_receipt_chars("x" * 1000) - 1


def test_a_statement_at_the_budget_is_whole_and_one_past_it_is_honest():
    """Both directions. The budget is a real ceiling — beyond it the receipt still appears, because
    a silently shortened lesson is worse than one that says it was shortened."""
    assert "redacted preview" not in _render("y" * LESSON_STATEMENT_CHARS)
    long_one = "y" * (LESSON_STATEMENT_CHARS + 500)
    out = _render(long_one)
    assert "redacted preview" in out
    assert len(out.split("[redacted preview")[0]) >= LESSON_STATEMENT_CHARS - 2


def test_redaction_still_fires_inside_the_larger_budget():
    """The bigger budget must not become a hole: a statement carrying a credential is still masked.
    Widening a cap is only safe while the thing the cap was NOT protecting against still works."""
    poisoned = ("Setting the endpoint token to sk-abcdEFGH0123456789abcdEFGH0123456789abcdEF fixed "
                "the crash, so keep it. " + "padding. " * 20)
    out = _render(poisoned)
    assert "sk-abcdEFGH0123456789abcdEFGH0123456789abcdEF" not in out


def test_the_budget_covers_the_whole_live_store():
    """Sized from the corpus, not chosen: every statement in the shared store must arrive whole, or
    the number is wrong for the data it exists to carry."""
    import json
    from pathlib import Path

    store = Path("/home/jovyan/data/looplab-memory/lessons.jsonl")
    if not store.exists():
        pytest.skip("shared memory store not present on this box")
    cut = []
    for line in store.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            stmt = (json.loads(line) or {}).get("statement")
        except ValueError:
            continue
        if stmt and "redacted preview" in _render(stmt):
            cut.append(len(stmt))
    assert cut == [], f"{len(cut)} statement(s) still truncated, longest {max(cut) if cut else 0}"


# ---------------------------------------------------------------------------------------------
# THE WIRING. Everything above renders a statement the way the product does; none of it would
# redden if `_render_role_prior` still asked for `max_chars=200`. That is the same shape as the
# defect: a mechanism that is right and a call site that does not use it.
# ---------------------------------------------------------------------------------------------

def _ctx(statement: str):
    """The exact tuple `_scan_prior_context` returns: (notes, parsed, fp, embed, health, case_line)."""
    parsed = [(0, {"statement": statement, "outcome": "failed", "task_id": "t"})]
    health = {"complete": True, "invalid": 0, "source": 1,
              "notes_digest": "n", "lessons_digest": "l",
              "truncated": False, "unavailable": False, "scope_filtered": 0}
    return ([], parsed, ["goal:x"], (lambda text: None), health, "")


def _renderer():
    """The mixin reads the engine through `self._e`; give it the two attributes this path touches
    (`task.id` for exact-task scoring, `_embedder` is already supplied through ctx)."""
    from types import SimpleNamespace

    from looplab.engine.lessons_priors import LessonPriorsMixin

    class _Stub(LessonPriorsMixin):
        _e = SimpleNamespace(task=SimpleNamespace(id="t"), _lesson_abstractor=None)
    return _Stub()


@pytest.mark.parametrize("role", ["developer", "researcher"])
def test_the_renderer_itself_delivers_the_whole_lesson_to_BOTH_roles(role):
    """Through `_render_role_prior`, not through this file's helper — and for both roles, because
    the row that cost three nodes is UNTAGGED and untagged means shared. A fix that reached only
    the Developer would leave the Researcher reading half a sentence."""
    out = _renderer()._render_role_prior(_ctx(_REAL_LESSON), role)
    assert "must write its declared artifact unconditionally on each run" in out, out[:400]
    assert "redacted preview: original_chars=372" not in out


def test_the_renderer_still_masks_a_credential_inside_the_larger_budget():
    """Widening a cap must not become a hole, and this has to be asserted THROUGH the product line:
    the helper-level version of this test stayed green when `entropy=True` was flipped off in
    `_render_role_prior`, which is precisely the mutation that would ship a lesson store's secret
    into every role prompt."""
    # A BARE high-entropy token, deliberately: a `sk-`-prefixed or `key=`-shaped secret is masked by
    # pattern whether or not the entropy heuristic runs, so a test built on one CANNOT tell
    # `entropy=True` from `entropy=False` — it stays green through the exact mutation it claims to
    # guard. This token is caught only by the heuristic, which is what makes the assertion real.
    secret = "9f3Kq7Zx2Lm8Tn4Rb6Yc1Hd5Jf0Gs3PaWq8Uv2Ne7Mi4Xo"
    poisoned = f"The crash went away once the value {secret} was used. " + "padding. " * 20
    out = _renderer()._render_role_prior(_ctx(poisoned), "developer")
    assert secret not in out
