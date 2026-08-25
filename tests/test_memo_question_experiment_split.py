"""A memo says which of its outputs are QUESTIONS and which are EXPERIMENTS, and only the first
become board rows.

THE DEFECT WAS ONE LINE OF PROMPT. The field was called `recommended_directions` and described to
the model as "(specific next experiments to try)" — a name contradicting its own description — so the
model correctly returned experiments and the channel filed them as directions. A direction owns no
executable action by construction, so a one-knob experiment arriving that way is unbuildable
forever: the memo had already decided the experiment and the engine could not run it.

Measured by reading all five outputs on `runs/e5small-dr-unified-v5`: exactly ONE was a genuine
family ("cross-encoder / strong-teacher distillation"); #2 was a single-knob experiment with an
exact value ("Test temperature 0.01 on the correctly-ported DCL+R-Drop loss"), #1 and #3 were two
concrete actions each with exact values.
"""
from __future__ import annotations

from looplab.core.advisory_payloads import sanitize_research_memo_payload
from looplab.core.models import ResearchMemo


def _registered(memo_d: dict) -> list[str]:
    """The exact expression `_record_deep_research` evaluates to choose board rows."""
    directions = [d for d in memo_d.get("recommended_directions", []) if str(d).strip()]
    return [q for q in memo_d.get("open_questions", []) if str(q).strip()] or directions


def test_only_the_QUESTIONS_become_board_rows_when_the_memo_split_them():
    memo = {"recommended_directions": ["does distilling from a stronger teacher help here",
                                       "set loss.temperature to 0.01"],
            "open_questions": ["does distilling from a stronger teacher help here"],
            "next_experiments": ["set loss.temperature to 0.01"]}
    assert _registered(memo) == ["does distilling from a stronger teacher help here"], (
        "a concrete one-knob experiment must not land as a row that owns no action")


def test_a_memo_that_drew_no_distinction_behaves_exactly_as_before():
    """Absence means "this memo did not split", NEVER "it has no questions" — which is what keeps
    every log on disk and every pre-split prompt folding byte-identically."""
    memo = {"recommended_directions": ["a", "b"]}
    assert _registered(memo) == ["a", "b"]
    assert _registered({"recommended_directions": ["a"], "open_questions": []}) == ["a"]


def test_blank_questions_do_not_silence_the_fallback():
    memo = {"recommended_directions": ["a"], "open_questions": ["  ", ""]}
    assert _registered(memo) == ["a"], "whitespace is not a question"


def test_the_split_survives_the_sanitizer_under_ONE_bound():
    """The compat field and its two halves share one text rule and one cap: a reader putting them
    side by side must not find one clipped where the other was not."""
    out = sanitize_research_memo_payload({
        "summary": "s",
        "open_questions": ["q" * 4000],
        "next_experiments": ["e" * 4000],
        "recommended_directions": ["r" * 4000]})
    lengths = {len(out[f][0]) for f in
               ("open_questions", "next_experiments", "recommended_directions")}
    assert len(lengths) == 1, f"the three fields clipped differently: {lengths}"


def test_the_sanitizer_caps_each_list_at_the_same_count():
    out = sanitize_research_memo_payload({
        "summary": "s", "open_questions": [f"q{i}" for i in range(40)],
        "next_experiments": [f"e{i}" for i in range(40)],
        "recommended_directions": [f"r{i}" for i in range(40)]})
    assert len(out["open_questions"]) == len(out["next_experiments"]) == 16


def test_the_model_defaults_leave_both_empty_so_an_old_memo_is_unchanged():
    memo = ResearchMemo(recommended_directions=["a"])
    assert memo.open_questions == [] and memo.next_experiments == []
    assert _registered(memo.model_dump()) == ["a"]
