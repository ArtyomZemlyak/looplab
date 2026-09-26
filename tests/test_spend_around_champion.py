"""What a run spent to REACH its champion, and what it spent after the answer was already in hand.

Doc 56 §131 measured it by hand over one campaign's meter logs — 32.6 % of the money ($20.57 of
$63.11) went after the node that ends up the champion had been evaluated — and doc 67 67.3 recorded
that no report of a single run could say it. `events/token_spend.py::spend_around_champion` splits
the run's own DURABLE ledger at the champion's terminal, through the fold, and `looplab tokens`
prints the line. Every test here writes a real event log and reads it back.
"""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from looplab.cli import app
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.token_spend import spend_around_champion


def _log(tmp_path, rows):
    """`rows` is a list of ("usage", tokens, cost[, usage_id]) / ("node", id, metric) steps. A usage
    row is priced exactly when it states a cost, the way `engine/costs.py` records a provider that
    bills nothing through this client (`priced_calls: 0`)."""
    rd = tmp_path / "run"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "run", "task_id": "t", "goal": "g", "direction": "max"})
    for step in rows:
        if step[0] == "usage":
            payload = {"calls": 1, "priced_calls": int(step[2] > 0), "prompt_tokens": step[1] // 2,
                       "completion_tokens": step[1] - step[1] // 2, "total_tokens": step[1],
                       "cost": step[2]}
            if len(step) > 3:
                payload["usage_id"] = step[3]
            store.append("llm_usage", payload)
        else:
            _, node_id, metric = step
            store.append("node_created", {"node_id": node_id, "parent_ids": [], "operator": "draft",
                                          "idea": {"operator": "draft", "params": {},
                                                   "rationale": "seed"}, "code": "pass\n"})
            store.append("node_evaluated", {"node_id": node_id, "generation": 0, "metric": metric,
                                            "violations": []})
    return rd, store


def test_the_ledger_is_split_at_the_champions_terminal(tmp_path):
    """Node 1 wins; everything billed after its terminal — node 2's build and anything else — is
    spend after the answer was in hand. The three parts add up to the fold's own ledger."""
    rd, store = _log(tmp_path, [("usage", 1000, 0.10), ("node", 0, 0.5),
                                ("usage", 3000, 0.30), ("node", 1, 0.9),
                                ("usage", 2000, 0.20), ("node", 2, 0.7),
                                ("usage", 4000, 0.40)])
    events = store.read_all()
    state = fold(events)
    split = spend_around_champion(events, state)
    assert split["node_id"] == 1
    assert split["reach"] == {"tokens": 4000, "calls": 2, "priced_calls": 2, "cost": 0.4}
    assert split["after"] == {"tokens": 6000, "calls": 2, "priced_calls": 2, "cost": 0.6}
    assert split["ledger_starts_after"] is False
    assert split["total"]["tokens"] == state.llm_cost["total_tokens"] == 10000
    assert split["after_share_tokens"] == pytest.approx(0.6)
    assert split["after_share_cost"] == pytest.approx(0.6)
    assert split["after_seconds"] is not None and split["after_seconds"] >= 0


def test_the_split_uses_the_folds_own_dedup_rule(tmp_path):
    """An outbox recovery re-appends a row under the SAME `usage_id`; the fold counts it once, and a
    hand sum here would disagree with the ledger line `looplab tokens` prints above it."""
    rd, store = _log(tmp_path, [("usage", 1000, 0.10, "u1"), ("usage", 1000, 0.10, "u1"),
                                ("node", 0, 0.9), ("usage", 500, 0.05, "u2"),
                                ("usage", 500, 0.05, "u2")])
    events = store.read_all()
    split = spend_around_champion(events, fold(events))
    assert split["reach"]["tokens"] == 1000 and split["after"]["tokens"] == 500
    assert split["total"]["tokens"] == 1500


def test_nothing_to_split_is_none_not_zero(tmp_path):
    """No champion (nothing evaluated) and no ledger at all are both SILENCE: "0 after" about a run
    that has no champion, or no record of what it spent, would be a claim, not a measurement."""
    _rd, store = _log(tmp_path, [("usage", 1000, 0.10)])
    events = store.read_all()
    assert spend_around_champion(events, fold(events)) is None
    other = tmp_path / "other"
    other.mkdir()
    rd2, store2 = _log(other, [("node", 0, 0.9)])
    events2 = store2.read_all()
    assert fold(events2).llm_cost is None
    assert spend_around_champion(events2, fold(events2)) is None


def test_a_ledger_the_record_cannot_place_is_not_split(tmp_path):
    """A pre-ledger log's only record is a cumulative `llm_cost` roll-up written wherever the roll-up
    happened — often at finalize, which read as "everything was spent after the champion" (reach 0,
    after 100 %). No split is printed rather than that one; and the same when the fold's legacy
    BASE roll-up sits after the champion's terminal."""
    rd = tmp_path / "legacy"
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "legacy", "task_id": "t", "goal": "g", "direction": "max"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {}, "rationale": "seed"},
                                  "code": "pass\n"})
    store.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": 0.9, "violations": []})
    store.append("llm_cost", {"cost": 1.5, "calls": 9, "total_tokens": 9000})
    events = store.read_all()
    assert fold(events).llm_cost is not None
    assert spend_around_champion(events, fold(events)) is None, "a roll-up has no per-call position"
    store.append("llm_usage", {"calls": 1, "total_tokens": 100, "cost": 0.01})
    events = store.read_all()
    assert spend_around_champion(events, fold(events)) is None, "the base roll-up sits after it"


def test_an_unpriced_ledger_says_unpriced_not_zero_dollars(tmp_path):
    rd, store = _log(tmp_path, [("usage", 1000, 0.0), ("node", 0, 0.9), ("usage", 3000, 0.0)])
    (rd / "spans.jsonl").write_text("", encoding="utf-8")
    out = CliRunner().invoke(app, ["tokens", str(rd)])
    line = next(ln for ln in out.output.splitlines() if ln.startswith("champion"))
    assert "(unpriced) spent to reach it" in line and "$" not in line, line


def test_the_line_needs_only_the_ledger_not_the_spans(tmp_path):
    """The split reads the durable ledger alone, so a run whose trace sidecar is gone still gets it
    before `tokens` explains why the per-phase split is unavailable."""
    rd, _store = _log(tmp_path, [("usage", 1000, 0.10), ("node", 0, 0.9), ("usage", 3000, 0.30)])
    out = CliRunner().invoke(app, ["tokens", str(rd)])
    assert out.exit_code == 2
    assert any(ln.startswith("champion") for ln in out.output.splitlines()), out.output


def test_looplab_tokens_prints_the_line(tmp_path):
    """THE SURFACE, driven: `looplab tokens` over a run with a spans sidecar prints the split under
    the ledger it reconciles against."""
    rd, store = _log(tmp_path, [("usage", 1000, 0.10), ("node", 0, 0.9), ("usage", 3000, 0.30)])
    (rd / "spans.jsonl").write_text(json.dumps({
        "span_id": "s1", "kind": "generation", "name": "g", "start": 1.0, "duration_s": 1.0,
        "attributes": {"phase": "propose", "usage": {"prompt": 2000, "completion": 2000,
                                                      "total": 4000}}}) + "\n", encoding="utf-8")
    out = CliRunner().invoke(app, ["tokens", str(rd)])
    assert out.exit_code == 0, out.output
    line = next(ln for ln in out.output.splitlines() if ln.startswith("champion"))
    assert "node 0" in line and "1,000 tokens ($0.1000) spent to reach it" in line
    assert "3,000 tokens ($0.3000; 75.0 % of tokens, 75.0 % of cost) spent after it" in line


def test_a_partly_priced_ledger_names_its_priced_calls_and_drops_the_cost_share(tmp_path):
    """A gateway that began pricing mid-run: the reach's `$0.1000` is a sum over ONE of its two
    calls, and a cost share would be a ratio of two floors (critic 2026-09-26: "100.0 % of cost")."""
    rd, _store = _log(tmp_path, [("usage", 1000, 0.0), ("usage", 1000, 0.10), ("node", 0, 0.9),
                                 ("usage", 2000, 0.20)])
    (rd / "spans.jsonl").write_text("", encoding="utf-8")
    out = CliRunner().invoke(app, ["tokens", str(rd)])
    line = next(ln for ln in out.output.splitlines() if ln.startswith("champion"))
    assert "2,000 tokens ($0.1000 over 1 of 2 calls priced) spent to reach it" in line, line
    assert "2,000 tokens ($0.2000; 50.0 % of tokens)" in line and "of cost" not in line, line


def test_a_ledger_that_starts_after_the_champion_says_the_reach_is_unrecorded(tmp_path):
    """A pre-ledger session found the champion and `looplab stop` wrote no roll-up; a resumed build
    with the ledger then billed everything after it. "0 to reach it" is not a measurement."""
    rd, store = _log(tmp_path, [("node", 0, 0.9), ("usage", 1000, 0.10)])
    events = store.read_all()
    split = spend_around_champion(events, fold(events))
    assert split["ledger_starts_after"] is True and split["rolled_up_before"] is False
    (rd / "spans.jsonl").write_text("", encoding="utf-8")
    out = CliRunner().invoke(app, ["tokens", str(rd)])
    assert "what reaching it cost is unrecorded, not zero" in out.output, out.output
    # ...and one whose roll-up counted the spend before it says the reach is that roll-up.
    rd3 = tmp_path / "rolled"
    rd3.mkdir()
    store3 = EventStore(rd3 / "events.jsonl")
    store3.append("run_started", {"run_id": "r3", "task_id": "t", "goal": "g", "direction": "max"})
    store3.append("llm_cost", {"cost": 0.5, "calls": 3, "total_tokens": 3000})
    store3.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                   "idea": {"operator": "draft", "params": {}, "rationale": "s"},
                                   "code": "pass\n"})
    store3.append("node_evaluated", {"node_id": 0, "generation": 0, "metric": 0.9,
                                     "violations": []})
    store3.append("llm_usage", {"calls": 1, "priced_calls": 1, "total_tokens": 100, "cost": 0.01})
    events3 = store3.read_all()
    split3 = spend_around_champion(events3, fold(events3))
    assert split3["ledger_starts_after"] is True and split3["rolled_up_before"] is True
    assert split3["reach"]["tokens"] == 3000
    (rd3 / "spans.jsonl").write_text("", encoding="utf-8")
    out3 = CliRunner().invoke(app, ["tokens", str(rd3)])
    assert "the reach is what the last cost roll-up before it recorded" in out3.output, out3.output


def test_the_after_window_ends_at_the_last_spend_not_the_last_row(tmp_path, monkeypatch):
    """An operator's comment a week later is not the run spending (critic 2026-09-26)."""
    import time as _time

    rd, store = _log(tmp_path, [("usage", 1000, 0.10), ("node", 0, 0.9), ("usage", 3000, 0.30)])
    events = store.read_all()
    spent_until = events[-1].ts
    week = _time.time() + 7 * 24 * 3600
    monkeypatch.setattr(_time, "time", lambda: week)
    store.append("annotation", {"node_id": 0, "text": "looked at it later"})
    monkeypatch.undo()
    events = store.read_all()
    split = spend_around_champion(events, fold(events))
    landed = next(e for e in events if e.type == "node_evaluated")
    assert split["after_seconds"] == pytest.approx(max(0.0, spent_until - landed.ts))
    # Nothing billed after the champion: no window at all, whatever rows follow it.
    rd2 = tmp_path / "quiet"
    rd2.mkdir()
    _rd2, store2 = _log(rd2, [("usage", 1000, 0.10), ("node", 0, 0.9)])
    store2.append("annotation", {"node_id": 0, "text": "done"})
    events2 = store2.read_all()
    assert spend_around_champion(events2, fold(events2))["after_seconds"] is None
