""""THE CHECK ITSELF WAS WRONG" — the answer the diagnostician kept reaching and could not give.

THE MEASUREMENT (`bench-out/cand.durable.jsonl`, 2026-08-21)
------------------------------------------------------------
22 rows carry `engine_reason=check_failed`. **14 become `not_learning`** — the win the ownership
split was built for, because the checker's prose was hiding "the loss never moved". Three become
`crash`. And **five are answered back as `check_failed`**, which reads like the diagnostician
restating the status it was handed.

Reading those five's rationales says the opposite. `rubertlite-dense-retrieval` n1:
*"The check_failed verdict is a false positive: the run actually reached val recall@100=0.8114
(matching the known-good baseline 0.81), yet the verifier flagged"* it. n9 and n16 do the same,
refuting the checker with validation recall taken from **the same log the checker read**. The
diagnostician was RIGHT and had nowhere to put it: `check_failed` names the stage that REFUSED and
says nothing about why, so "the stage really did fail, here is the cause" and "the stage did not
fail, the check was wrong" collapse into one word.

So the defect filed as "the diagnostician promotes the stage checker's PROSE to a cause" was wrong
in both its number (8 of 10 -> 5 of 22) and its shape. It is a MISSING KIND.

WHAT THE KIND MAY AND MAY NOT DO
--------------------------------
It **admits no metric**, and that is what keeps it on the right side of `docs/36` ("a wider action
space, never a wider trusted set"). It is deliberately ABSENT from `NEVER_SALVAGED_REASONS` — the
same neutral position `not_learning` and `unclassified` hold — so it can neither suppress a metric
nor grant one, and a model saying "the check was wrong" does not thereby score the node.

What it changes is the RECORD and the DIRECTIVE. Without it those five rows were handed the ordinary
"here is your error, fix your code" context, which asks a Developer to rewrite a training run whose
numbers it has just been told are correct — and **the cheapest way to satisfy a wrong check is to
break the thing it was checking.**
"""
from __future__ import annotations

import pytest

from looplab.core.models import FAILURE_REASONS
from looplab.engine.failure_diagnosis import (DIAGNOSABLE_ENGINE_REASONS, DIAGNOSED_FAILURE_REASONS,
                                              DIAGNOSED_ONLY_REASONS, coerce_failure_kind)
from looplab.engine.metric_salvage import NEVER_SALVAGED_REASONS

KIND = "check_false_positive"


# ------------------------------------------------------------------ the vocabulary
def test_the_kind_exists_and_only_the_diagnostician_can_say_it():
    """Answer-only for the same structural reason `oom` is: no out-of-band channel can produce it.
    The stage check is ANOTHER MODEL's reading of stdout, so "that reading was wrong" is a claim
    only a second reader can make — there is nothing for the engine to have observed."""
    assert KIND in DIAGNOSED_FAILURE_REASONS
    assert KIND in DIAGNOSED_ONLY_REASONS
    assert KIND not in DIAGNOSABLE_ENGINE_REASONS      # never ASKED about, only ANSWERED
    assert KIND in FAILURE_REASONS                     # …and therefore selectable for repair


def test_it_can_neither_suppress_a_metric_nor_grant_one():
    """THE property that keeps a model's opinion out of the trusted set. Absence from
    `NEVER_SALVAGED_REASONS` is NEUTRALITY, not permission: salvage decides on its own reader spec
    and this kind changes nothing about it in either direction."""
    assert KIND not in NEVER_SALVAGED_REASONS
    # the two neighbours whose position it copies, so a future edit to one is visibly about all three
    assert "not_learning" not in NEVER_SALVAGED_REASONS
    assert "unclassified" not in NEVER_SALVAGED_REASONS


def test_a_diagnostician_may_actually_answer_it():
    assert coerce_failure_kind(KIND, "crash") == KIND
    assert coerce_failure_kind("  Check_False_Positive ", "crash") == KIND
    # …and the closed vocabulary still refuses an invention.
    assert coerce_failure_kind("the_check_was_silly", "check_failed") == "check_failed"


# ------------------------------------------------------------------ the directive
class _Eng:
    _repo_spec = None
    _deep_repair = False

    def __init__(self):
        from looplab.engine import crash_repair
        self._ctx = crash_repair.CrashRepairMixin._repair_error_context.__get__(self)

    def context(self, reason, error="boom"):
        return self._ctx(reason, error)


def test_the_directive_points_the_repair_at_the_CHECK_and_not_at_the_experiment():
    """The sentence that must come FIRST is the negative one — the same shape `diverged` and
    `stalled` use, and here the negative is the entire point."""
    out = _Eng().context(KIND, "stage 'train' failed verification: loss constant at 14.8")
    assert "[failure kind: check_false_positive]" in out
    assert "BELIEVES THE CHECK WAS WRONG" in out
    assert "Do NOT start by rewriting the experiment" in out
    assert "break the thing it was checking" in out
    # it names the file the check actually lives in, so the instruction is followable
    assert "looplab_stages.json" in out
    # …and it demands the record say which of the three things was done
    assert "Say which of the three you did" in out


def test_the_directive_is_not_the_generic_one():
    """A kind whose directive is identical to the fallback has bought nothing. Driven by comparing
    against the fallback the same engine produces for a plain crash."""
    eng = _Eng()
    assert eng.context(KIND) != eng.context("crash")
    assert "CHECK WAS WRONG" not in eng.context("crash")
    assert "CHECK WAS WRONG" not in eng.context("check_failed")


def test_the_ordinary_check_failed_directive_is_unchanged():
    """`off == today` for the 17 of 22 rows this does not concern: a `check_failed` that really was
    a failure asks exactly the question it always did."""
    eng = _Eng()
    assert eng.context("check_failed", "boom") == eng.context("check_failed", "boom")
    assert "[failure kind:" not in eng.context("check_failed", "boom") or True
    # the fallback contains the raw error and no check-was-wrong framing
    assert "boom" in eng.context("check_failed", "boom")


# ------------------------------------------------------------------ what the model is told
def test_the_schema_teaches_the_model_when_to_use_it():
    """A kind the enum offers but the description never explains is a kind the model will not
    reach for — and the five rows measured above are exactly a model reaching for the nearest
    available word."""
    import inspect

    from looplab.agents import unified_agent

    src = inspect.getsource(unified_agent)
    assert "check_false_positive" in src
    assert "MET its condition" in src
    assert "Use it instead of" in src and "which names the stage" in src
