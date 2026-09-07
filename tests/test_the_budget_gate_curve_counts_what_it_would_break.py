"""A gate that stops runs must be scored on the nodes it would have stopped, not only the money.

The proposal on the table was "refuse a new node when `limit - spent` is below the p75 node cycle",
p75 being $0.4481. Measured over the 76-run corpus on 2026-09-03, that gate stops 74 empty cycles
($4.4743) and **54 cycles that produced a real node**, one of them the 277.23 that is the best
`edge_expansion` node in the corpus. At $0.10 it stops 61 empty cycles, redirects $1.5354, and the
one real node it costs scored 0.

So the tool has to report both columns, and this file pins that it does — a version that counted
only the savings would have recommended the threshold that destroys the corpus's best node.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

import budget_gate_curve as bgc  # noqa: E402


def _run(tmp_path: Path, name: str, rows: list[dict]) -> None:
    d = tmp_path / name / "runs" / "t" / "run"
    d.mkdir(parents=True)
    (d / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _usage(ts: float, cost: float) -> dict:
    return {"ts": ts, "type": "llm_usage", "data": {"cost": cost}}


def _node(ts: float, node_id: int, metric: float) -> dict:
    return {"ts": ts, "type": "node_evaluated", "data": {"node_id": node_id, "metric": metric}}


def _corpus(tmp_path: Path) -> list[dict]:
    # A run that spends $0.95, then opens a cycle with $0.05 left and evaluates NOTHING.
    _run(tmp_path, "wasteful", [{"ts": 0.5, "type": "run_started", "data": {}},
                                _usage(1.0, 0.50), _node(2.0, 0, 100.0),
                                _usage(3.0, 0.45), _node(4.0, 1, 120.0),
                                _usage(5.0, 0.05)])
    # A run that opens its last cycle with exactly $0.25 left and lands a 277.23 out of it.
    _run(tmp_path, "lucky", [{"ts": 0.5, "type": "run_started", "data": {}},
                             _usage(1.0, 0.75), _node(2.0, 0, 10.0),
                             _usage(3.0, 0.05), _node(4.0, 1, 277.23)])
    return bgc.load(str(tmp_path))


def test_a_cheap_gate_cuts_the_waste_and_costs_nothing(tmp_path):
    got = bgc.at_gate(_corpus(tmp_path), 0.06)
    assert got["empty_cut"] == 1, got
    assert abs(got["redirected"] - 0.05) < 1e-9, got
    assert got["real_lost"] == 0 and got["best_lost"] == 0.0, got


def test_the_expensive_gate_reports_the_node_it_would_have_killed(tmp_path):
    """This is the whole point: at a higher threshold the tool must NAME the 277.23 it destroys."""
    got = bgc.at_gate(_corpus(tmp_path), 0.30)
    assert got["real_lost"] >= 1, got
    assert abs(got["best_lost"] - 277.23) < 1e-9, (
        "the best node the gate would have prevented is not being reported, so the threshold "
        "would be chosen on savings alone")


def test_a_live_run_can_be_excluded(tmp_path):
    """A run still in flight has spent everything after its (non-existent) last node -- §148.1."""
    _corpus(tmp_path)
    _run(tmp_path, "inflight", [{"ts": 0.5, "type": "run_started", "data": {}},
                                _usage(1.0, 0.20)])
    assert {r["name"] for r in bgc.load(str(tmp_path))} == {"wasteful", "lucky", "inflight"}
    kept = bgc.load(str(tmp_path), exclude={"inflight"})
    assert {r["name"] for r in kept} == {"wasteful", "lucky"}
    # An in-flight run has no last node, so ALL of its spend reads as "after the last node" and
    # it donates a phantom empty cycle the moment the gate is above what it has left.
    with_live = bgc.at_gate(bgc.load(str(tmp_path)), 1.05)
    without = bgc.at_gate(kept, 1.05)
    assert with_live["empty_cut"] == without["empty_cut"] + 1, (with_live, without)
    assert abs((with_live["redirected"] - without["redirected"]) - 0.20) < 1e-9


def test_the_cycle_price_is_measured_between_evaluations(tmp_path):
    got = sorted(round(x, 4) for x in bgc.cycles(_corpus(tmp_path)))
    # wasteful: 0.50 before node 0, 0.45 between node 0 and 1; lucky: 0.75 then 0.05.
    assert got == [0.05, 0.45, 0.50, 0.75], got


def test_a_run_with_exactly_the_gate_left_is_allowed_to_proceed(tmp_path):
    """"Below the gate" means below it. Mutating `>=` to `>` survived every other test here.

    `lucky` opens its second cycle holding exactly $0.25 and lands the 277.23 out of it. At a gate
    of $0.25 that cycle must be permitted; one cent of boundary error costs the corpus's best node.
    The amounts are binary-exact on purpose -- the first draft used 1.00 - 0.92, whose float value
    is 0.07999999999999996, and the test failed against CORRECT code.
    """
    got = bgc.at_gate(_corpus(tmp_path), 0.25)
    assert got["real_lost"] == 0, got
    assert got["best_lost"] == 0.0, got
