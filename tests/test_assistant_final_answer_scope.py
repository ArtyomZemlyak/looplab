"""The assistant's streamed final answer must be about THIS turn, not about the whole dialog.

The directive used to be "Now write your final answer to the user in Markdown, based on everything
above" over the entire conversation, so the model summarized the session on every turn — the
operator's report was "the assistant's summary takes the whole dialog; it should summarize only what
it did in this answer, everything after the last user message".

The first fix was the directive plus a marker on "the last user message", found by scanning the
trace backwards. The operator reported it AGAIN, because the scan is not the boundary: the messages
after the request are not all the model's — `drive_tool_loop` appends `user`-role control strings of
its own (the emit nudge, the stuck prompt, the "Out of turn/time budget" salvage), so on every
CUT-SHORT turn the marker landed on a machine nudge and the operator's real request went unmarked.

So the boundary is AUTHENTICATED (doc 36): `run_turn` keeps the request dict it appended and hands
it to the summariser, which marks that or nothing. Context is still the whole conversation — a turn
routinely depends on a file read from three turns ago — and only the SCOPE is narrowed.
"""
from __future__ import annotations

from looplab.serve.assistant import (
    FINAL_ANSWER_DIRECTIVE,
    TURN_BOUNDARY_MARK,
    _emit_spec,
    final_answer_directive,
    final_answer_messages,
    system_prompt,
)


def _convo():
    return [
        {"role": "system", "content": "you are looplab"},
        {"role": "user", "content": "turn one question"},
        {"role": "assistant", "content": "turn one answer"},
        {"role": "user", "content": "turn two question"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "tool output"},
    ]


def _marked(messages):
    return [m for m in messages if str(m.get("content", "")).startswith(TURN_BOUNDARY_MARK)]


def test_the_whole_conversation_is_still_sent_as_context():
    """Trimming to the current turn would make the answer worse — the model loses the file it read
    two turns ago. Context is not the defect; the scope was."""
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
    assert TURN_BOUNDARY_MARK.lower() in lowered, "the directive must name the marker it points at"
    assert "do not re-summarize" in lowered
    assert "based on everything above" not in lowered, "the phrase that caused the defect"


def test_non_stream_and_automatic_turns_receive_the_same_scope_rule():
    """The streaming summariser has an authenticated marker; direct final_answer calls still need
    the rule on both model-facing surfaces used by non-stream HTTP and automatic work cycles."""
    prompt = system_prompt("plan").lower()
    description = _emit_spec()["function"]["description"].lower()
    for text in (prompt, description):
        assert "current request" in text and "this turn" in text
        assert "recap" in text


def test_the_current_turn_is_marked_on_the_authenticated_request():
    """The caller names its own boundary; the marker goes THERE and nowhere else."""
    convo = _convo()
    out = final_answer_messages(convo, boundary=convo[3])
    marked = _marked(out[:-1])
    assert len(marked) == 1
    assert marked[0]["content"] == f"{TURN_BOUNDARY_MARK}\nturn two question"
    assert out[1]["content"] == "turn one question", "an earlier user turn is not marked"


def test_a_loop_control_message_after_the_request_never_becomes_the_boundary():
    """THE DEFECT, at the unit. `drive_tool_loop`'s own nudges are `user`-role and come AFTER the
    request, so the backwards scan marked one of them on every cut-short turn."""
    convo = _convo() + [
        {"role": "user", "content": "Out of turn/time budget. Call `final_answer` NOW with your "
                                    "best answer from everything you have gathered."}]
    out = final_answer_messages(convo, boundary=convo[3])
    marked = _marked(out[:-1])
    assert len(marked) == 1
    assert marked[0]["content"].endswith("turn two question")
    assert "Out of turn/time budget" not in marked[0]["content"]


def test_a_boundary_compaction_removed_is_carried_by_the_directive_instead():
    """`compact_history` summarizes the stale MIDDLE — which on a long turn includes the request —
    and `truncate_history` rebuilds a stale message with its content middle-truncated. Either way
    the boundary is no longer in the trace, and re-scanning would mark the compaction NOTE (whose
    content is a summary of earlier turns: precisely the scope this exists to exclude)."""
    request = {"role": "user", "content": "turn two question"}
    compacted = [
        {"role": "system", "content": "you are looplab"},
        {"role": "user", "content": "[Summary of earlier steps — informational context, NOT "
                                    "instructions]\nthe model read three files"},
        {"role": "assistant", "content": "…"},
    ]
    out = final_answer_messages(compacted, boundary=request)
    assert not _marked(out), "no marker is better than a marker on the compaction note"
    assert "turn two question" in out[-1]["content"], (
        "the directive must still name the turn's own request verbatim")
    assert out[1]["content"].startswith("[Summary of earlier steps")


def test_the_verbatim_restatement_is_clipped_rather_than_re_sending_a_payload():
    """A request can carry an attached file. The block names the turn; it does not re-send it."""
    big = "x" * 40_000
    text = final_answer_directive(big)
    assert len(text) < 6_000
    assert text.startswith(FINAL_ANSWER_DIRECTIVE)
    assert "…" in text


def test_a_trace_with_no_user_message_still_produces_a_directive():
    """A subagent or a replayed fragment can arrive with no user turn at all. It must still get an
    answer rather than a broken pointer to a message that is not there."""
    out = final_answer_messages([{"role": "system", "content": "s"},
                                 {"role": "assistant", "content": "a"}])
    assert out[-1]["content"] == FINAL_ANSWER_DIRECTIVE
    assert not _marked(out[:-1])


def test_the_input_list_is_not_mutated():
    """`convo` is the live trace the tool loop compacts in place; marking a message inside it would
    persist the boundary marker into the next turn's history."""
    convo = _convo()
    before = [dict(m) for m in convo]
    final_answer_messages(convo, boundary=convo[3])
    assert convo == before


def test_the_call_site_uses_the_helper_rather_than_a_second_copy_of_the_directive():
    import inspect

    from looplab.serve import assistant

    source = inspect.getsource(assistant.run_turn)
    assert "final_answer_messages(base, boundary=turn_request)" in source
    assert "based on everything above" not in source


class _CapturingStreamFake:
    """Answers the loop, then records the messages the streamed final answer is actually asked with.

    `scripted` is a list of `chat` replies; the default is one clean `final_answer` call. A reply
    with no tool calls is prose, which is what drives the loop into its cut-short nudges.
    """

    def __init__(self, scripted=None):
        self.seen: list[list[dict]] = []
        self.scripted = list(scripted) if scripted is not None else [
            {"content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "final_answer",
                                          "arguments": '{"reply": "loop reply (fallback)"}'}}]}]

    def chat(self, messages, tools, tool_choice="auto"):
        return self.scripted.pop(0) if self.scripted else {"content": "still thinking"}

    def complete_text_stream(self, messages):
        self.seen.append([dict(m) for m in messages])
        yield "Streamed answer."

    def complete_text(self, messages):
        self.seen.append([dict(m) for m in messages])
        return "Streamed answer."


def test_the_streamed_final_answer_is_really_asked_with_the_scoped_directive(tmp_path):
    """Drives the wire instead of pinning the call site's TEXT.

    The sibling above reads `run_turn`'s source for `final_answer_messages(base, …)`. That is the
    mutation CLAUDE.md names as cheapest: comment the call out, leave the literal in the comment, and
    inline an unscoped directive — the pin still passes. Verified: it does. So assert what the model
    is actually handed.
    """
    from looplab.serve.assistant import run_turn

    client = _CapturingStreamFake()
    run_turn(client, tmp_path, [{"role": "user", "content": "an older turn"},
                                {"role": "assistant", "content": "an older answer"}],
             "what did you just do?", "plan", reply_sink=lambda _chunk: None)

    assert client.seen, "the final answer never reached the model"
    messages = client.seen[-1]
    assert messages[-1]["content"].startswith(FINAL_ANSWER_DIRECTIVE), (
        "the streamed answer must be asked with the SCOPED directive, not an inlined copy")
    assert "what did you just do?" in messages[-1]["content"], (
        "the directive carries the turn's own request, authenticated by run_turn")
    marked = _marked(messages[:-1])
    assert len(marked) == 1, (
        f"exactly one boundary marker names what the answer is about; saw {len(marked)}")
    assert marked[0]["content"].endswith("what did you just do?")
    assert any("an older turn" in str(m.get("content", "")) for m in messages), (
        "the whole conversation must stay as CONTEXT — only the scope is narrowed")


def test_a_cut_short_turn_marks_the_request_and_not_the_loops_own_nudge(tmp_path):
    """THE DEFECT, driven end to end.

    Two prose replies with no forcible emit is `tool_loop`'s `stalled` exit: the loop appends its
    own `user`-role nudge between them and the turn ends without the model choosing to emit. This is
    the shape an operator sees after a long agentic turn — the one whose summary they read — and the
    backwards scan marked the NUDGE, leaving the request they typed unmarked.
    """
    from looplab.serve.assistant import run_turn

    client = _CapturingStreamFake(scripted=[{"content": "thinking out loud 1"},
                                            {"content": "thinking out loud 2"}])
    res = run_turn(client, tmp_path, [{"role": "user", "content": "turn one question"},
                                      {"role": "assistant", "content": "turn one answer"}],
                   "turn two: what did you just do?", "plan", reply_sink=lambda _chunk: None)

    assert res["budget_exhausted"]["kind"] == "stalled", "this must really be a cut-short turn"
    messages = client.seen[-1]
    nudges = [m for m in messages if m.get("role") == "user" and "final_answer" in str(m.get("content", ""))
              and not str(m.get("content", "")).startswith("Now write your final answer")]
    assert nudges, "the loop must really have appended a control message after the request"
    assert not any(str(m["content"]).startswith(TURN_BOUNDARY_MARK) for m in nudges), (
        "the loop's own nudge must never be presented as the turn's request")
    marked = _marked(messages[:-1])
    assert len(marked) == 1
    assert marked[0]["content"].endswith("turn two: what did you just do?")


def test_a_second_turn_does_not_re_summarize_the_first(tmp_path):
    """Two turns in one session: the second turn's summariser is scoped to the second request, and
    the first turn's work appears only as context."""
    from looplab.serve.assistant import run_turn

    history = [{"role": "user", "content": "first question"},
               {"role": "assistant", "content": "first answer"}]
    client = _CapturingStreamFake()
    res = run_turn(client, tmp_path, history, "second question", "plan",
                   reply_sink=lambda _chunk: None)
    history = history + [{"role": "user", "content": "second question"},
                         {"role": "assistant", "content": res["reply"]}]

    client2 = _CapturingStreamFake()
    run_turn(client2, tmp_path, history, "third question", "plan", reply_sink=lambda _c: None)

    messages = client2.seen[-1]
    marked = _marked(messages[:-1])
    assert len(marked) == 1 and marked[0]["content"].endswith("third question")
    assert "third question" in messages[-1]["content"]
    assert "second question" not in messages[-1]["content"], (
        "the previous turn's request must not be part of what the answer is scoped to")
    assert any(str(m.get("content", "")) == "first question" for m in messages), (
        "earlier turns stay as unmarked context")


def test_the_boundary_marker_never_reaches_the_persisted_transcript(tmp_path, monkeypatch):
    """What the operator's session KEEPS is the answer, not the scaffolding that scoped it.

    The marker is spliced into a COPY of the trace, so it can never persist into the history the
    next turn reads back (and the share/snapshot path reads the same records). Driven over the real
    HTTP surface, because that is where the turn record is actually written.
    """
    import json

    from fastapi.testclient import TestClient

    from looplab.serve.server import make_app

    class _StreamingFake(_CapturingStreamFake):
        def __init__(self):
            super().__init__(scripted=[
                {"content": "", "tool_calls": [
                    {"id": "c1", "function": {"name": "final_answer",
                                              "arguments": json.dumps({"reply": "loop reply"})}}]}])

    fake = _StreamingFake()
    monkeypatch.setattr("looplab.serve.server.make_llm_client", lambda _s, **_kw: fake)
    client = TestClient(make_app(tmp_path))
    sid = client.post("/api/assistant/sessions", json={"title": "t"}).json()["id"]
    r = client.post(f"/api/assistant/sessions/{sid}/message",
                    json={"instruction": "what did you just do?", "mode": "plan"}).json()
    assert r.get("ok"), r

    msgs = client.get(f"/api/assistant/sessions/{sid}").json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    blob = json.dumps(msgs)
    assert TURN_BOUNDARY_MARK not in blob
    assert "Now write your final answer" not in blob
    assert msgs[0]["content"] == "what did you just do?", "the request persists as the user typed it"


def test_a_watch_wake_up_is_scoped_to_the_wake_up_and_not_to_the_last_human_message(tmp_path):
    """The case most likely to be missed: nobody typed a wake-up.

    A standing watch's session history is the message that ARMED it, then a stack of the watch's own
    assistant reports — so "everything since the last user message" would span every previous
    wake-up, and each firing would re-narrate the session. The boundary is the wake-up's OWN
    instruction, which `run_turn` appends and authenticates exactly like a typed one.
    """
    from looplab.serve.assistant import run_turn
    from looplab.serve.assistant_watch import wakeup_instruction

    record = {"instruction": "check whether the run is still improving", "waiting_for": "run r1",
              "wakeups": 2, "max_wakeups": 20, "trigger": {"kind": "poll", "every_s": 300}}
    instruction = wakeup_instruction(record, {"state": "running"})
    history = [
        {"role": "user", "content": "watch run r1 and tell me when it plateaus"},
        {"role": "assistant", "content": "armed a watch on r1"},
        {"role": "assistant", "content": "wake-up 1: nothing new to report",
         "watch": {"id": "w1", "wakeup": 1}},
        {"role": "assistant", "content": "wake-up 2: still climbing", "watch": {"id": "w1", "wakeup": 2}},
    ]

    client = _CapturingStreamFake()
    run_turn(client, tmp_path, history, instruction, "plan", reply_sink=lambda _chunk: None)

    messages = client.seen[-1]
    marked = _marked(messages[:-1])
    assert len(marked) == 1, "the wake-up's own instruction is the boundary"
    assert "[automatic wake-up — nobody typed this]" in marked[0]["content"]
    assert "check whether the run is still improving" in marked[0]["content"]
    assert not any(str(m.get("content", "")).startswith(TURN_BOUNDARY_MARK)
                   and "watch run r1" in str(m.get("content", "")) for m in messages), (
        "the message that ARMED the watch, hours and two wake-ups ago, is not this turn's request")
    assert any("wake-up 1: nothing new to report" == str(m.get("content", "")) for m in messages), (
        "previous wake-up reports stay as context — never as work to re-report")
