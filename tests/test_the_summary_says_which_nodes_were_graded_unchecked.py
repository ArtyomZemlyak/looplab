"""A node graded on code written after its last `check` is counted and named.

MEASURED 2026-09-02 over the 111 evaluated nodes whose tool order could be reconstructed from the
spans: 4 of the 12 nodes with a WRITE after their last `check` scored zero (33 %), against 3 of the
99 without one (3.0 %). Exact one-sided Fisher p = 0.0024.

The summary REPORTS it and nothing acts on it: telling the Developer "you have edited since your
last check" is a behaviour change, and §92 is this notebook's standing answer to behaviour changes
proposed off an observational split -- their effect is unmeasurable without an arm that lacks them.
Making the quantity visible is what a sweep can do without one.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from benchmarks.probe_summary import summarise  # noqa: E402


def _write(run: Path, events, spans):
    run.mkdir(parents=True, exist_ok=True)
    (run / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    (run / "spans.jsonl").write_text(
        "\n".join(json.dumps(s) for s in spans) + "\n", encoding="utf-8")


def _check(ts):
    return {"kind": "tool", "start": ts,
            "attributes": {"tool": "run_dev_command", "input": json.dumps({"name": "check"})}}


def _write_span(ts):
    return {"kind": "tool", "start": ts, "attributes": {"tool": "write_file"}}


def _node(ts, metric):
    return {"type": "node_evaluated", "ts": ts, "data": {"node_id": 0, "metric": metric}}


def test_a_write_after_the_last_check_is_counted(tmp_path):
    run = tmp_path / "p" / "runs" / "t" / "run"
    _write(run, [_node(100.0, 0.0)], [_check(10.0), _write_span(50.0)])
    assert summarise(run)["graded_unchecked"] == 1


def test_a_write_before_the_check_is_not(tmp_path):
    """MUTATION GUARD: comparing against the FIRST check, or ignoring order, counts this one."""
    run = tmp_path / "p" / "runs" / "t" / "run"
    _write(run, [_node(100.0, 12.0)], [_write_span(10.0), _check(50.0)])
    assert summarise(run)["graded_unchecked"] == 0


def test_a_write_after_the_evaluation_belongs_to_the_next_node(tmp_path):
    """The window is (last check, this evaluation) -- a write that came later is the next node's."""
    run = tmp_path / "p" / "runs" / "t" / "run"
    _write(run, [_node(100.0, 12.0)], [_check(10.0), _write_span(150.0)])
    assert summarise(run)["graded_unchecked"] == 0


def test_a_node_that_was_never_checked_is_a_different_fact(tmp_path):
    """Counting it here would merge "graded unchecked" with "never checked at all", and the corpus
    number this exists to carry is about the first."""
    run = tmp_path / "p" / "runs" / "t" / "run"
    _write(run, [_node(100.0, 0.0)], [_write_span(50.0)])
    s = summarise(run)
    assert s["graded_unchecked"] == 0 and s["graded_nodes"] == 1


def test_each_node_is_judged_against_its_own_window(tmp_path):
    run = tmp_path / "p" / "runs" / "t" / "run"
    _write(run,
           [_node(100.0, 5.0), _node(300.0, 0.0)],
           [_check(10.0), _check(150.0), _write_span(200.0)])
    s = summarise(run)
    assert s["graded_unchecked"] == 1 and s["graded_nodes"] == 2
