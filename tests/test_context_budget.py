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
