"""Hint directives: recency-aware rendering + replace (supersede) semantics in the fold."""
from __future__ import annotations

from looplab.events.eventstore import EventStore
from looplab.agents.hints import render_hint_directives
from looplab.events.replay import fold


def test_render_empty_is_blank():
    assert render_hint_directives([]) == ""
    assert render_hint_directives(None) == ""
    assert render_hint_directives([{"text": ""}, {"foo": 1}]) == ""   # no usable text


def test_render_single_is_plain():
    out = render_hint_directives([{"text": "use neural nets"}])
    assert "use neural nets" in out
    assert "MOST RECENT" not in out      # no precedence noise for a single directive


def test_render_multiple_orders_oldest_to_newest_with_precedence():
    out = render_hint_directives([{"text": "A"}, {"text": "B"}, {"text": "C"}])
    assert out.index("A") < out.index("B") < out.index("C")      # oldest first, newest last
    assert "MOST RECENT" in out
    # the recency marker is attached to the LAST (newest) directive, not an earlier one
    assert out.rstrip().endswith("follow this when they conflict")


def test_render_caps_old_directives():
    out = render_hint_directives([{"text": f"h{i}"} for i in range(9)], max_shown=3)
    assert "+6 older directive(s) superseded/omitted" in out
    assert "h8" in out and "h0" not in out      # only the last 3 shown verbatim


def test_replace_hint_supersedes_prior(tmp_path):
    s = EventStore(tmp_path / "events.jsonl")
    s.append("hint", {"text": "first"})
    s.append("hint", {"text": "second"})
    s.append("hint", {"text": "NOW", "replace": True})
    st = fold(s.read_all())
    assert [h["text"] for h in st.pending_hints] == ["NOW"]      # slate wiped, single directive


def test_append_hint_accumulates(tmp_path):
    s = EventStore(tmp_path / "events.jsonl")
    s.append("hint", {"text": "a"})
    s.append("hint", {"text": "b"})                              # no replace -> append (legacy)
    st = fold(s.read_all())
    assert [h["text"] for h in st.pending_hints] == ["a", "b"]


def test_replace_then_append(tmp_path):
    s = EventStore(tmp_path / "events.jsonl")
    s.append("hint", {"text": "old"})
    s.append("hint", {"text": "reset", "replace": True})
    s.append("hint", {"text": "added"})                         # appends onto the post-reset slate
    st = fold(s.read_all())
    assert [h["text"] for h in st.pending_hints] == ["reset", "added"]
