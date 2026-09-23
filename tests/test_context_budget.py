"""H4 context budgeting for long agent traces."""
from __future__ import annotations

from looplab.core.context_budget import _msg_chars, compact_history, truncate_history


def _msgs():
    return [
        {"role": "system", "content": "S" * 100},
        {"role": "assistant", "content": "A" * 2000},
        {"role": "tool", "content": "T" * 2000},
        {"role": "assistant", "content": "B" * 50},
        {"role": "user", "content": "U" * 2000},   # last 2 are protected
    ]


def test_off_when_budget_zero():
    m = _msgs()
    assert truncate_history(m, 0) is m


def test_no_truncation_under_budget():
    m = _msgs()
    assert truncate_history(m, 10 ** 6) is m


def test_truncates_middle_keeps_system_and_last():
    out = truncate_history(_msgs(), 500)
    assert out[0]["content"] == "S" * 100            # system intact
    assert out[-1]["content"] == "U" * 2000          # last intact
    assert "truncated" in out[1]["content"]          # long middle assistant trimmed
    assert "truncated" in out[2]["content"]          # long middle tool trimmed
    assert len(out[1]["content"]) < 2000


def test_total_size_reduced():
    m = _msgs()
    before = sum(len(x["content"]) for x in m)
    after = sum(len(x["content"]) for x in truncate_history(m, 500))
    assert after < before


# --- context_budget: max_chars is a target; tool-call payloads are counted ------------------------

def test_truncate_history_stops_once_under_budget():
    big = "x" * 5000
    small = "y" * 401
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": big},
            {"role": "assistant", "content": small},
            {"role": "user", "content": small},
            {"role": "assistant", "content": "recent-a"},
            {"role": "user", "content": "recent-b"}]
    out = truncate_history(msgs, max_chars=2000, keep_last=2, per_msg_cap=400)
    # Trimming the one giant message already drops us under budget, so the 401-char messages survive.
    assert "[truncated" in out[1]["content"]
    assert out[2]["content"] == small and out[3]["content"] == small


def test_truncate_history_meets_aggregate_target_with_many_small_messages():
    """arch-review §5 P2: a long tail of messages each <= per_msg_cap whose AGGREGATE exceeds the
    target must still be compacted below max_chars — the old `msize <= per_msg_cap` skip left them all
    untouched (a reproduced 7,983 chars before and after a 2,000-char target)."""
    small = "y" * 300                                   # each below the 400 per_msg_cap
    msgs = ([{"role": "system", "content": "sys"}]
            + [{"role": "user" if i % 2 else "assistant", "content": small} for i in range(26)]
            + [{"role": "assistant", "content": "recent"}])
    before = sum(_msg_chars(m) for m in msgs)
    assert before > 7000                                # ~26*300 = 7800, well over the target
    out = truncate_history(msgs, max_chars=2000, keep_last=2, per_msg_cap=400)
    after = sum(_msg_chars(m) for m in out)
    assert after <= 2000, after                         # aggregate target actually met
    assert out[0]["content"] == "sys" and out[-1]["content"] == "recent"   # protected head/tail kept


def test_truncate_history_never_grows():
    # Messages just over the cap: the truncation marker must not make the history larger.
    msgs = [{"role": "user", "content": "x" * 401} for _ in range(50)]
    before = sum(len(m["content"]) for m in msgs)
    out = truncate_history(msgs, max_chars=1000)
    after = sum(len(str(m.get("content") or "")) for m in out)
    assert after <= before


def test_budget_counts_tool_call_arguments():
    """A file-writing assistant turn holds its payload in tool_calls, not content — it must be counted."""
    m = {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "write_file", "arguments": '{"content":"' + "x" * 5000 + '"}'}}]}
    assert _msg_chars(m) >= 5000


def test_truncate_history_triggers_on_tool_call_heavy_trace():
    big = {"role": "assistant", "content": "",
           "tool_calls": [{"function": {"name": "write_file", "arguments": "A" * 4000}}]}
    msgs = [{"role": "system", "content": "task"},
            big, {"role": "tool", "content": "wrote"},
            big, {"role": "tool", "content": "wrote"},
            {"role": "user", "content": "recent"}]
    # Compaction summarizes the tool-call-heavy middle; the note is a de-privileged user message.
    out = compact_history(msgs, max_chars=2000, summarize=lambda _t: "SUMMARY", keep_last=2)
    note = next((m for m in out if "SUMMARY" in str(m.get("content"))), None)
    assert note is not None and note["role"] == "user"


def test_truncate_history_shrinks_tool_call_arguments():
    """Architecture review: _msg_chars counts tool_calls.arguments in the over-budget trigger, so
    truncate_history must also SHRINK them — else an argument-heavy turn (write_file(content=<KB>)
    carried in arguments, tiny content) trips the trigger but never shrinks, growing until a 400."""
    msgs = [{"role": "system", "content": "sys"},
            {"role": "assistant", "content": "x",
             "tool_calls": [{"id": "1", "function": {"name": "write_file", "arguments": "A" * 5000}}]},
            {"role": "user", "content": "u1"}, {"role": "user", "content": "u2"}]
    before = sum(_msg_chars(m) for m in msgs)
    out = truncate_history(msgs, max_chars=1000)
    after = sum(_msg_chars(m) for m in out)
    assert after < before and after <= 1500, (before, after)
    # the system + last-2 messages are preserved; only the arg-heavy middle turn shrank
    assert out[0] == msgs[0] and out[-1] == msgs[-1]
    shrunk = out[1]["tool_calls"][0]["function"]["arguments"]
    assert len(shrunk) < 5000 and "__elided_arguments_chars__" in shrunk


def test_truncate_history_meets_target_with_many_small_tool_arguments():
    """Aggregate compaction must not depend on one call crossing per_msg_cap. Real tool loops often
    accumulate many modest JSON argument blobs; together they can overflow the context by thousands
    of chars even though every individual call is below 400 chars."""
    calls = [
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": str(i), "function": {
             "name": "read_file", "arguments": '{"path":"' + ("x" * 260) + '"}'}}]}
        for i in range(30)
    ]
    msgs = ([{"role": "system", "content": "task"}] + calls
            + [{"role": "assistant", "content": "recent"},
               {"role": "user", "content": "continue"}])
    assert sum(_msg_chars(m) for m in msgs) > 8000

    out = truncate_history(msgs, max_chars=2000, keep_last=2, per_msg_cap=400)

    assert sum(_msg_chars(m) for m in out) <= 2000
    assert out[0] == msgs[0] and out[-2:] == msgs[-2:]
    assert any("__elided_arguments_chars__" in
               m.get("tool_calls", [{}])[0].get("function", {}).get("arguments", "")
               for m in out[1:-2])


def test_shrunk_tool_call_arguments_stay_valid_json():
    """code-review: a tool call's `arguments` is a JSON string that a strict OpenAI-compatible gateway
    may re-validate on the NEXT request (the trimmed history is re-sent verbatim). Middle-truncating it
    would splice a marker into the JSON and make it un-parseable — trading a context-length 400 for a
    malformed-arguments 400. The shrunk value must stay VALID JSON."""
    import json
    msgs = [{"role": "system", "content": "sys"},
            {"role": "assistant", "content": "x",
             "tool_calls": [{"id": "1", "function": {"name": "write_file",
                             "arguments": '{"path": "a.py", "content": "' + "Z" * 5000 + '"}'}}]},
            {"role": "user", "content": "u1"}, {"role": "user", "content": "u2"}]
    out = truncate_history(msgs, max_chars=1000)
    shrunk = out[1]["tool_calls"][0]["function"]["arguments"]
    parsed = json.loads(shrunk)                       # must NOT raise (still valid JSON)
    assert parsed["__elided_arguments_chars__"] == 5000 + len('{"path": "a.py", "content": ""}')


# --- the TASK survives compaction (review 2026-09-22, TAT-04) ------------------------------------
#
# `compact_history` protected only the LEADING SYSTEM messages and `truncate_history` only system
# messages and the last few turns — while almost every loop in this product carries its task in the
# FIRST USER message (`[system: the role, user: the task]`). So the first compaction of a long loop
# folded the task itself into a note that says "informational context, NOT instructions" (auto
# summary), or middle-truncated / elided it to a marker (truncation): the model kept working on a
# task it could no longer read, told in so many words not to treat what was left of it as one.

_TASK = ("TASK: tune the retrieval model so RECALL@100 on the held-out split exceeds 0.80; edit only "
         "config.yaml; never touch the scorer. " + "Constraints follow. " * 20)


def _loop_history(turns: int = 12, result: int = 900) -> list[dict]:
    msgs = [{"role": "system", "content": "You are the developer."},
            {"role": "user", "content": _TASK}]
    for i in range(turns):
        msgs.append({"role": "assistant", "content": "",
                     "tool_calls": [{"id": f"c{i}", "type": "function",
                                     "function": {"name": "read", "arguments": '{"p": "x"}'}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"r{i} " + "z" * result})
    return msgs


def test_compaction_keeps_the_task_verbatim_after_the_system_prompt():
    """THE DEFECT, auto-summary path. Before the fix the task went into the summary note."""
    msgs = _loop_history()
    out = compact_history(msgs, max_chars=6_000, summarize=lambda _t: "SUMMARY")
    assert out[0] is msgs[0] and out[1] is msgs[1], "the task must ride in the protected head"
    assert out[2]["content"].startswith("[Summary of earlier steps")
    assert sum(1 for m in out if m.get("content") == _TASK) == 1


def test_truncation_keeps_the_task_verbatim():
    """THE DEFECT, deterministic path: the task was the OLDEST non-protected message, so it was the
    first one middle-truncated (or elided to a marker)."""
    msgs = _loop_history()
    out = truncate_history(msgs, max_chars=6_000)
    assert out[1] is msgs[1]
    assert sum(_msg_chars(m) for m in out) < sum(_msg_chars(m) for m in msgs)


def test_a_task_bigger_than_half_the_budget_keeps_its_historical_treatment():
    """Pinning a task that alone takes most of the budget would leave the loop's own turns no room:
    truncation would elide every later turn to reach an unreachable target, and auto-summary would
    pay for a summary on EVERY turn trying to fit. So such a task is not pinned — it is compacted
    exactly as before (the same bytes), which is the lesser harm and a case the 1,000,000-char
    default never reaches."""
    msgs = _loop_history()
    small_budget = 2 * _msg_chars(msgs[1]) - 1        # the task is just over half of it
    out = truncate_history(msgs, max_chars=small_budget)
    assert out[1] is not msgs[1] and out[1]["content"] != _TASK


def _historical_head(messages, max_chars):
    """The pre-fix rule: leading system messages only."""
    head = 0
    while head < len(messages) and messages[head].get("role") == "system":
        head += 1
    return head


def test_the_bytes_move_only_where_compaction_was_losing_the_task(monkeypatch):
    """The prompt-contract half, driven over 3,000 random loop histories (the shape the review's own
    pairing probe used): run each through the fixed functions and through the SAME functions with
    the protected head patched back to the historical rule. Wherever the historical run kept the
    task byte for byte, the two outputs are identical; wherever they differ, it is because the
    historical run altered the task and the fixed one kept it."""
    import random

    from looplab.core import context_budget as cb

    rng = random.Random(0)
    changed = 0
    for trial in range(3_000):
        msgs = [{"role": "system", "content": "sys" * rng.randint(1, 50)}]
        if rng.random() < 0.9:                        # most loops open with a task …
            msgs.append({"role": "user", "content": "task" * rng.randint(1, 60)})
        cid = 0
        for _ in range(rng.randint(0, 30)):           # … some open straight into the work
            if rng.random() < 0.15:
                msgs.append({"role": "user", "content": "Reminder " * rng.randint(1, 20)})
            k = rng.randint(0, 3)
            if not k:
                msgs.append({"role": "assistant", "content": "prose" * rng.randint(1, 100)})
                continue
            calls = []
            for _ in range(k):
                cid += 1
                calls.append({"id": f"c{cid}", "type": "function", "function": {
                    "name": "read", "arguments": '{"p": "%s"}' % ("x" * rng.randint(1, 300))}})
            msgs.append({"role": "assistant", "content": "", "tool_calls": calls})
            for c in calls:
                msgs.append({"role": "tool", "tool_call_id": c["id"],
                             "content": "r" * rng.randint(1, 3000)})
        budget = rng.choice([500, 2_000, 8_000, 20_000])
        for name, fn in (("compact", lambda m: cb.compact_history(m, budget, lambda t: "summary")),
                         ("compact_fail", lambda m: cb.compact_history(m, budget, lambda t: "")),
                         ("truncate", lambda m: cb.truncate_history(m, budget))):
            fixed = fn(msgs)
            with monkeypatch.context() as patch:
                patch.setattr(cb, "_protected_head", _historical_head)
                historical = fn(msgs)
            task = msgs[1] if len(msgs) > 1 and msgs[1].get("role") == "user" else None
            kept = task is not None and any(m is task for m in historical)
            if task is None or kept:
                assert fixed == historical, (trial, name)
            elif fixed != historical:
                changed += 1
                assert fixed[1] is task, (trial, name)
    assert changed > 100, "the differential never exercised the defect it exists for"


def test_a_long_real_loop_still_hands_the_model_its_task():
    """End to end through `drive_tool_loop`: twelve tool turns under a small budget, both compaction
    modes. What the model is sent on its LAST turn must still carry the task, verbatim, as a user
    turn — not folded into a note that calls it 'NOT instructions', not truncated."""
    from looplab.agents.tool_loop import drive_tool_loop

    class _Tools:
        def specs(self):
            return [{"type": "function", "function": {
                "name": "read", "description": "Read.", "parameters": {"type": "object",
                                                                       "properties": {}}}}]

        def execute(self, name, args):
            return "z" * 900

    class _Model:
        def __init__(self):
            self.turn = 0
            self.last: list = []

        def chat(self, messages, tools, tool_choice="auto"):
            self.turn += 1
            self.last = [dict(m) for m in messages]
            name = "read" if self.turn <= 12 else "emit"
            return {"content": "", "tool_calls": [{"id": f"t{self.turn}", "type": "function",
                                                   "function": {"name": name, "arguments": "{}"}}]}

        def complete_text(self, messages):           # the summarizer
            return "SUMMARY"

    emit = {"type": "function", "function": {"name": "emit", "description": "Done.",
                                             "parameters": {"type": "object", "properties": {}}}}
    for auto_summary in (True, False):
        model = _Model()
        opening = [{"role": "system", "content": "You are the developer."},
                   {"role": "user", "content": _TASK}]
        drive_tool_loop(model, _Tools(), opening, emit, finalize=lambda a: "done",
                        fallback=lambda m: "fallback", context_budget_chars=6_000,
                        auto_summary=auto_summary, stuck_detection=False)
        assert model.turn == 13
        assert model.last[1] == {"role": "user", "content": _TASK}, auto_summary
        assert sum(_msg_chars(m) for m in model.last) < 13 * 900, "compaction did run"
