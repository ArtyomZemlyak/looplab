"""A memo where nine fields of ten validate must keep the nine.

THE DEFECT, traced end to end on `runs/e5small-dr-unified-v7`. Both of that run's deep-research
memos came back with a real `summary`, 64 `sources`, and EVERY list empty — no directions, no
questions, no experiments, no findings, no claims — against a corpus base rate of one empty memo in
101. The model had not gone quiet: its last generation reads verbatim *"I have everything I need.
Let me emit the final research memo. [tool_calls: emit]"*, at ~136 turns against a 300-turn nudge and
a 500-turn force. The payload was created and then thrown away.

`agents/deep_research.py::_finalize` threw it away:

    try:
        return self._assemble(_MemoOut.model_validate(args), memo, sources)
    except Exception:                       # a junk emit must not crash the run
        memo.summary = redact_persisted_text((args or {}).get("summary", "") …)
        memo.sources = sources
        return memo

ANY validation failure discarded every field except `summary` and `sources` — exactly the observed
shape. The trigger was `question_concepts`, the only `list[list[str]]` in `_MemoOut`: a model
returning the natural flat shape `["loss/contrastive", "training/x"]` instead of
`[["loss/contrastive"], …]` took nine good fields down with it, including the directions the whole
question machinery feeds on. Two full deep-research passes (203 tool calls and 64 sources on the
first alone) were discarded that way.

THE ALL-OR-NOTHING CATCH IS THE DEFECT, not the field. Any field added to `_MemoOut` in future meets
the same hazard, which is why the fix is here and not only in the schema.

Every assertion below has an input that makes it fail; the mutations are named in the messages.
"""
from __future__ import annotations

from looplab.agents.deep_research import _MemoOut


def _finalizer():
    """A bare `DeepResearcher` — `_finalize` is a pure function of its arguments."""
    from looplab.agents.deep_research import DeepResearcher

    return DeepResearcher.__new__(DeepResearcher)


def _memo():
    from looplab.core.models import ResearchMemo

    return ResearchMemo()


_GOOD = {
    "summary": "the run is fresh; three siblings give a clear picture",
    "reasoning": "deliberation",
    "findings": ["f1", "f2"],
    "recommended_directions": ["d1", "d2", "d3"],
    "open_questions": ["Is the plateau an under-training artifact?"],
    "next_experiments": ["set loss.temperature to 0.01"],
}


def test_a_FLAT_question_concepts_no_longer_takes_the_whole_memo_with_it():
    """The exact payload shape that emptied both of v7's memos."""
    args = dict(_GOOD, question_concepts=["loss/contrastive", "training/negative-mining"])
    out = _finalizer()._finalize(args, _memo(), [])

    assert out.recommended_directions == ["d1", "d2", "d3"], (
        "MUTATION: restore the bare `except: return summary-only` and this goes red — it is the "
        "state that discarded two full deep-research passes on v7")
    assert out.open_questions == ["Is the plateau an under-training artifact?"]
    assert out.next_experiments == ["set loss.temperature to 0.01"]
    assert out.findings == ["f1", "f2"]
    assert out.summary.startswith("the run is fresh")
    # The offending field itself is the only thing lost: it falls back to its declared default
    # rather than carrying the malformed value. `ResearchMemo.question_concepts` was ADDED while
    # fixing this — `_assemble` had been assigning a field the model did not declare, so it raised
    # mid-assembly on every well-formed memo and never reached `memo.sources` on the next line.
    assert out.question_concepts == []
    # And the assembly now COMPLETES: `sources` is assigned after `question_concepts`, so a memo
    # that carries it is proof the call did not die half way.
    assert out.sources == []


def test_a_WELL_FORMED_memo_is_untouched():
    """The happy path must not move: no re-validation, no dropped field, byte-identical content."""
    args = dict(_GOOD, question_concepts=[["loss/contrastive"]])
    out = _finalizer()._finalize(args, _memo(), [])
    assert out.recommended_directions == ["d1", "d2", "d3"]
    assert out.question_concepts == [["loss/contrastive"]]
    assert out.findings == ["f1", "f2"]


def test_the_REFUSAL_is_recorded_and_not_silent(caplog):
    """A field the engine dropped must be visible, or the next one is found the same way this was.

    `_admissible_beliefs` sets the house precedent one module over: what it refuses to register it
    still says out loud.
    """
    import logging

    args = dict(_GOOD, question_concepts=["flat"])
    with caplog.at_level(logging.WARNING):
        _finalizer()._finalize(args, _memo(), [])
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "question_concepts" in logged, (
        "MUTATION: drop the log and a discarded field becomes invisible — which is how this cost "
        "two runs' memos before anyone noticed")


def test_a_MEMO_THAT_IS_JUNK_THROUGHOUT_still_degrades_to_summary_only():
    """The original guarantee survives: a junk emit must not crash the run.

    Retrying without the offending keys must not become a way to accept nonsense — when nothing
    validates even after the drop, the summary-only fallback is still what happens.
    """
    out = _finalizer()._finalize({"summary": "s", "findings": "not-a-list",
                                  "recommended_directions": 7}, _memo(), [])
    assert out.summary == "s"
    assert out.recommended_directions == []


def test_a_NON_DICT_emit_is_survived():
    out = _finalizer()._finalize(None, _memo(), [])
    assert "(empty memo)" in out.summary or out.summary == ""


def test_SEVERAL_bad_fields_are_all_dropped_and_the_rest_kept():
    """One retry has to remove every offender, not just the first."""
    args = dict(_GOOD, question_concepts=["flat"], claims="not-a-list")
    out = _finalizer()._finalize(args, _memo(), [])
    assert out.recommended_directions == ["d1", "d2", "d3"], (
        "MUTATION: drop only the first error's field and this goes red")
    assert out.claims == []


def test_the_field_that_triggered_it_is_the_only_nested_list_in_the_schema():
    """Why `question_concepts` and nothing else — recorded so the next nested field is noticed."""
    props = _MemoOut.model_json_schema()["properties"]
    nested = [name for name, spec in props.items()
              if spec.get("type") == "array" and spec.get("items", {}).get("type") == "array"]
    assert nested == ["question_concepts"], (
        f"a second nested-list field appeared ({nested}) — it carries the same shape hazard and "
        "needs the same thought about what the model will naturally return")
