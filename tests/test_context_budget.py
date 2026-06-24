"""H4 context budgeting for long agent traces."""
from __future__ import annotations

from looplab.context_budget import truncate_history


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
