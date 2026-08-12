"""The assistant's streamed final answer must be about THIS turn, not about the whole dialog.

The directive used to be "Now write your final answer to the user in Markdown, based on everything
above" over the entire conversation, so the model summarized the session on every turn — the
operator's report was "the assistant's summary takes the whole dialog; it should summarize only what
it did in this answer, everything after the last user message".

This is a PROMPT bug, not a context bug, and the fix keeps the distinction: the full conversation
stays as context (a turn routinely depends on a file read from three turns ago), while the directive
and an explicit boundary marker name what the answer is about.
"""
from __future__ import annotations

from looplab.serve.assistant import FINAL_ANSWER_DIRECTIVE, final_answer_messages


def _convo():
    return [
        {"role": "system", "content": "you are looplab"},
        {"role": "user", "content": "turn one question"},
        {"role": "assistant", "content": "turn one answer"},
        {"role": "user", "content": "turn two question"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "tool output"},
    ]


def test_the_whole_conversation_is_still_sent_as_context():
    """Trimming to the current turn would make the answer worse — the model loses the file it read
    two turns ago. Context is not the defect; the directive was."""
    out = final_answer_messages(_convo())
    assert len(out) == len(_convo()) + 1
    assert out[0]["content"] == "you are looplab"
    assert any(m.get("role") == "tool" for m in out)
    assert out[1]["content"] == "turn one question", "earlier turns pass through untouched"


def test_the_directive_scopes_the_answer_to_this_turn():
    out = final_answer_messages(_convo())
    directive = out[-1]
    assert directive["role"] == "user"
    assert directive["content"] == FINAL_ANSWER_DIRECTIVE
    lowered = directive["content"].lower()
    assert "last user message" in lowered
    assert "do not re-summarize" in lowered
    assert "based on everything above" not in lowered, "the phrase that caused the defect"


def test_the_current_turn_is_marked_on_the_last_user_message_only():
    """"The last user message" is something the model must FIND in a trace that may hold dozens of
    tool results. Marking it is what makes the scope unambiguous."""
    out = final_answer_messages(_convo())
    marked = [m for m in out[:-1] if str(m.get("content", "")).startswith("[current turn")]
    assert len(marked) == 1
    assert marked[0]["content"] == "[current turn — answer this]\nturn two question"
    assert out[1]["content"] == "turn one question", "an earlier user turn is not marked"


def test_a_trace_with_no_user_message_still_produces_a_directive():
    """A subagent or a replayed fragment can arrive with no user turn at all. It must still get an
    answer rather than a broken pointer to a message that is not there."""
    out = final_answer_messages([{"role": "system", "content": "s"},
                                 {"role": "assistant", "content": "a"}])
    assert out[-1]["content"] == FINAL_ANSWER_DIRECTIVE
    assert not any(str(m.get("content", "")).startswith("[current turn") for m in out[:-1])


def test_the_input_list_is_not_mutated():
    """`convo` is the live trace the tool loop compacts in place; marking a message inside it would
    persist the boundary marker into the next turn's history."""
    convo = _convo()
    before = [dict(m) for m in convo]
    final_answer_messages(convo)
    assert convo == before


def test_the_call_site_uses_the_helper_rather_than_a_second_copy_of_the_directive():
    import inspect

    from looplab.serve import assistant

    source = inspect.getsource(assistant.run_turn)
    assert "final_answer_messages(base)" in source
    assert "based on everything above" not in source
