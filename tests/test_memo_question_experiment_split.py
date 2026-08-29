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


def test_every_field_the_prompt_NAMES_exists_in_the_emit_schema():
    """A prompt that asks for a field the schema lacks is a feature shipped INERT.

    THIS IS THE GUARD FOR A BUG THAT SHIPPED. `open_questions` / `next_experiments` were added to
    `ResearchMemo` and to `_SYSTEM` and NOT to `_MemoOut` — the class `_emit_spec` hands to the
    provider as the tool's parameters. The model was asked for fields it had no slot to write into
    and did the only thing it could. Measured on the fresh `runs/e5small-dr-unified-v5`:
    `open_questions` 0, `next_experiments` 0, `recommended_directions` 11.

    Re-derived from the prompt text rather than listed, so a NEW field named in the prompt is
    covered the day it is written. Backticked names only — the prompt also mentions tool names and
    prose, and a substring sweep would demand a schema slot for `update_plan`.
    """
    import re
    from looplab.agents.deep_research import _MemoOut, _SYSTEM, DeepResearcher

    schema = set(_MemoOut.model_json_schema()["properties"])
    # The prompt's own instruction sentence: "call `emit` exactly once with: a `summary` …".
    asked = set(re.findall(r"`([a-z_]+)`", _SYSTEM.split("call `emit` exactly once")[1]))
    # DECLARED, not pattern-matched. A tool call and an example id are indistinguishable from a
    # field to any parser, and a heuristic that guessed would eventually guess a real field away.
    # `_PROMPT_NON_FIELD_NAMES` makes the exclusion explicit, so adding an example to the prompt
    # fails this guard until it is listed — a small, visible cost that keeps the guard able to fire.
    from looplab.agents.deep_research import _PROMPT_NON_FIELD_NAMES
    missing = sorted(asked - _PROMPT_NON_FIELD_NAMES - schema)
    assert not missing, (
        f"the prompt asks for {missing} and the emit schema has no slot for them — the model "
        f"cannot answer and the field ships inert")


def test_the_emit_schema_and_the_durable_model_agree_on_the_split():
    """The two halves must exist on BOTH: `_MemoOut` is what the model fills, `ResearchMemo` is what
    is stored. A field on one and not the other is silently dropped at the boundary."""
    from looplab.agents.deep_research import _MemoOut

    emit = set(_MemoOut.model_json_schema()["properties"])
    stored = set(ResearchMemo.model_json_schema()["properties"])
    for field in ("open_questions", "next_experiments", "recommended_directions"):
        assert field in emit, f"{field} is not fillable by the model"
        assert field in stored, f"{field} is not storable"


def _registered_with_concepts(memo_d: dict, admitted: list[str]) -> list[dict]:
    """The exact alignment `_record_deep_research` evaluates before appending each question."""
    raw_questions = [q for q in memo_d.get("open_questions", []) if str(q).strip()]
    per_question = memo_d.get("question_concepts") or []
    by_statement = {}
    for index, statement in enumerate(raw_questions):
        row = per_question[index] if index < len(per_question) else None
        if isinstance(row, list) and row:
            by_statement[str(statement).strip()] = row
    return [{"statement": d, **({"concepts": by_statement[d]} if d in by_statement else {})}
            for d in admitted]


def test_a_question_carries_the_concepts_at_ITS_OWN_position():
    memo = {"open_questions": ["does distillation help", "does a momentum queue help"],
            "question_concepts": [["training/distillation"], ["training/negative-mining"]]}
    rows = _registered_with_concepts(memo, ["does a momentum queue help"])
    assert rows[0]["concepts"] == ["training/negative-mining"], (
        "alignment is POSITIONAL — resolving by order of ADMISSION would hand this question the "
        "first row's concepts, which is a confident falsehood about what it is about")


def test_a_short_or_missing_concepts_list_leaves_a_question_untagged():
    """Checked, not trusted. A memo that filled two questions and one concept row must not make
    the second question borrow the first's."""
    memo = {"open_questions": ["q1", "q2"], "question_concepts": [["loss/contrastive"]]}
    rows = _registered_with_concepts(memo, ["q1", "q2"])
    assert rows[0]["concepts"] == ["loss/contrastive"]
    assert "concepts" not in rows[1]


def test_a_memo_that_drew_no_distinction_gets_no_concepts_at_all():
    """`questions` falls back to `recommended_directions` for an old memo, and THOSE positions mean
    nothing — resolving against them would attach concepts by coincidence of order."""
    memo = {"recommended_directions": ["a", "b"], "question_concepts": [["loss/contrastive"]]}
    rows = _registered_with_concepts(memo, ["a", "b"])
    assert all("concepts" not in r for r in rows)


def test_an_empty_concept_row_is_not_a_membership():
    memo = {"open_questions": ["q1"], "question_concepts": [[]]}
    assert "concepts" not in _registered_with_concepts(memo, ["q1"])[0]


def test_the_sanitizer_keeps_alignment_and_applies_the_fold_s_own_bound():
    out = sanitize_research_memo_payload({
        "summary": "s", "open_questions": ["q1", "q2"],
        "question_concepts": [["loss/contrastive", "x/y"], ["training/distillation"]]})
    assert len(out["question_concepts"]) == len(out["open_questions"])
    assert out["question_concepts"][1] == ["training/distillation"]


def test_question_concepts_is_fillable_by_the_model():
    """The lesson from the split shipping inert: a field the prompt names must have a schema slot."""
    from looplab.agents.deep_research import _MemoOut
    assert "question_concepts" in _MemoOut.model_json_schema()["properties"]
