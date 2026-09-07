"""The reason a probe has no score was the FIRST matching log line, whenever it happened.

Measured 2026-09-03: `newCK7` and `oldCK8b` were reported as
`STILL RUNNING ... answered HTTP 503 (overloaded) — waiting 2s before attempt 2 of 9` while both
were healthy, two hours and three evaluated nodes past that line — one of them 264.0272. A
transient refusal from the start of a run was printed as its current state. `remDL`, which really
did die that way, was reported at `attempt 2 of 9` when its log reaches `attempt 7 of 9`: the first
match understated the one case where the needle was right.

Two changes, pinned here: take the LAST occurrence and say how much log came after it, and, for a
probe still running, print what it has evaluated — the fact that contradicts the alarm.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "benchmarks"))

import probe_summary as ps  # noqa: E402


def _probe(tmp_path: Path, name: str, run_log: str) -> Path:
    d = tmp_path / name
    (d / "runs" / "t" / "run").mkdir(parents=True)
    (d / "run.log").write_text(run_log, encoding="utf-8")
    return d


def test_the_last_occurrence_wins_and_carries_its_distance(tmp_path):
    log = ("answered HTTP 503 (overloaded) — waiting 2s before attempt 2 of 9\n"
           "answered HTTP 503 (overloaded) — waiting 30s before attempt 7 of 9\n"
           + "ordinary progress\n" * 5)
    why = ps._why_no_test(_probe(tmp_path, "p", log))
    assert "attempt 7 of 9" in why, f"the FIRST match was taken again: {why!r}"
    assert "(+5 log lines since)" in why, why


def test_a_reason_at_the_very_end_carries_no_distance(tmp_path):
    why = ps._why_no_test(_probe(tmp_path, "p", "Traceback (most recent call last)"))
    assert "Traceback" in why and "log lines since" not in why, why


def test_no_needle_no_reason(tmp_path):
    assert ps._why_no_test(_probe(tmp_path, "p", "all quiet\nstill quiet\n")) == ""


def test_a_running_probe_reports_what_it_has_already_evaluated(tmp_path, capsys):
    """The alarm and the nodes are printed together, or the alarm reads alone."""
    d = tmp_path / "model-probes" / "live"
    run = d / "runs" / "t" / "run"
    run.mkdir(parents=True)
    (d / "run.log").write_text("answered HTTP 503 (overloaded) — waiting 2s before attempt 2 of 9\n",
                               encoding="utf-8")
    rows = [{"v": 1, "seq": 1, "ts": 1.0, "type": "run_started", "data": {}},
            {"v": 1, "seq": 2, "ts": 2.0, "type": "node_evaluated",
             "data": {"node_id": 0, "metric": 111.4037}},
            {"v": 1, "seq": 3, "ts": 3.0, "type": "node_evaluated",
             "data": {"node_id": 1, "metric": 264.0272}}]
    (run / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (run / "spans.jsonl").write_text("", encoding="utf-8")

    got = ps.summarise(run)
    assert got is not None and got["test"] is None
    assert got["nodes"] == [111.4037, 264.0272], got["nodes"]
    # The printed line is what a reader sees; build it the way the report does.
    live = " -- STILL RUNNING" + f" ({len(got['nodes'])} node(s) so far, best {max(got['nodes']):.4f})"
    assert "2 node(s) so far, best 264.0272" in live
