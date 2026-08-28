"""The diagnostician's account leads the text the Developer repairs from.

THE DEFECT, recorded in the tree against itself until 2026-08-28: `crash_repair.py`'s
`check_false_positive` directive says *"Read its rationale above before you touch anything"* and the
prompt did not carry the rationale. That kind exists for exactly one situation — the declared check
refused a run and the diagnostician, reading the same log afterwards with more of it, believes the
CHECK was wrong — so the Developer was handed only the refusal being disputed.

MEASURED on `runs/rubertlite-dense-retrieval`: node 1's diagnosis reads "the run actually reached
val recall@100=0.8114 (matching the known-good baseline 0.81), yet the verifier flagged" it, and
that number appeared NOWHERE in the repair context. n9 and n16 refute their checker with validation
recall from the same log. Asking a Developer to fix a run whose numbers it has just been told are
correct is how a working experiment gets broken to satisfy a wrong check.

The rule is a named function and not an inline `if` because its call site is three hundred lines
inside `_evaluate`, where no test can reach it — CLAUDE.md's guard-test ladder, tier 2.

Every assertion below has an input that makes it FAIL; the mutations are named in the messages.
"""
from __future__ import annotations

import ast
import inspect
import pathlib

from looplab.engine import crash_repair, evaluate
from looplab.engine.failure_diagnosis import REASON_SOURCE_ENGINE, diagnosis_repair_lead

_SUMMARY = "the run reached val recall@100=0.8114, matching the 0.81 baseline; the assert is stale"


def test_a_triage_sourced_reason_LEADS_with_the_diagnosis():
    """Mutation: return `""` always. The `check_false_positive` directive then points at text that
    is not there, which is the defect this closes."""
    lead = diagnosis_repair_lead(_SUMMARY, "triage", "AssertionError: epochs != 15")
    assert _SUMMARY in lead
    assert lead.endswith("\n\n"), "the lead must separate itself from the error text it precedes"


def test_an_ENGINE_final_reason_gets_no_lead():
    """`REASON_SOURCE_ENGINE` means the engine classified the failure from its own clock, watchdog
    or `stat`. Mutation: drop the conjunct, and an engine-measured fact is dressed up as a model's
    opinion — the ownership split `failure_diagnosis.py` exists to keep."""
    assert diagnosis_repair_lead(_SUMMARY, REASON_SOURCE_ENGINE, "timeout after 22000s") == ""


def test_a_summary_ALREADY_in_the_error_is_not_said_twice():
    """The watchdog path prepends its own sentence upstream, so on a `not_learning` kill the two
    texts can be the same words. Mutation: drop the containment check, and one finding printed twice
    reads as two independent findings agreeing — which is worse than either alone."""
    err = f"The live training watchdog stopped this run: {_SUMMARY}\nFix what it named."
    assert diagnosis_repair_lead(_SUMMARY, "triage", err) == ""


def test_no_summary_means_no_lead():
    """Mutation: emit the preamble with an empty summary, and the Developer reads "the diagnostician
    concluded:" followed by nothing — a claim that a diagnosis exists when none does."""
    for empty in (None, "", "   ", 42, [_SUMMARY]):
        assert diagnosis_repair_lead(empty, "triage", "boom") == ""


def test_a_junk_error_text_never_raises():
    """`err` is assembled from several optional pieces upstream. Mutation: call `.strip()` on a
    non-string and the repair path dies inside the one branch that exists to save a good run."""
    assert _SUMMARY in diagnosis_repair_lead(_SUMMARY, "triage", None)


def test_the_repair_path_actually_CALLS_it_before_building_the_developer_text():
    """Tier 3 of the ladder, and its limits are known: this proves the call is in the text and that
    it precedes the `_repair_error_context` that consumes it, not that it executed. Mutation: build
    `_err_in` from `err` alone again and the function above becomes dead code that still passes
    every test in this file."""
    src = pathlib.Path(inspect.getsourcefile(evaluate)).read_text()
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "diagnosis_repair_lead"]
    assert calls, "the repair path must call the rule, not re-spell it inline"
    ctx = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
           and isinstance(n.func, ast.Attribute) and n.func.attr == "_repair_error_context"]
    assert ctx and min(c.lineno for c in ctx) > min(c.lineno for c in calls), (
        "the lead must be built BEFORE the error context that carries it to the Developer")


def test_the_directive_still_tells_the_developer_to_read_it():
    """The two halves are one contract: if the sentence is ever dropped from the prompt, the lead
    above becomes unexplained text. Mutation: delete the sentence, and this fails."""
    src = pathlib.Path(inspect.getsourcefile(crash_repair)).read_text()
    # AST, AND THE SUBSTRING VERSION OF THIS WAS VACUOUS THE DAY IT WAS WRITTEN: the fix's own
    # explanatory comment quotes the sentence, so `"Read its rationale above" in src` was satisfied
    # by a COMMENT while the mutant that deleted it from the PROMPT survived. The literal has to be
    # a string constant the module actually returns.
    prompt_literals = [n.value for n in ast.walk(ast.parse(src))
                       if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert any("Read its rationale above" in lit for lit in prompt_literals), (
        "the directive must still TELL the Developer to read the rationale the lead now supplies")
    # THE SLUG IS ASSEMBLED, NEVER SPELLED. `tests/test_open_item_index.py` scans the whole tree for
    # the bracket form and does not care that this occurrence is an assertion ABOUT a closed item —
    # writing it literally re-declares the marker here and the index guard goes red demanding a
    # `proof:` clause. Same trap as `claimpin` not being able to tell a pin from a quoted example.
    retired = "OPEN" + "[" + "cfp-rationale-not-in-repair-prompt" + "]"
    assert retired not in src, (
        "the marker's own closing condition was that this claim becomes true — closing is a "
        "deletion, and a re-added marker would mean the claim is false again")
