"""Δk is computed one way, and that way is in git before the probes it will read finish.

docs/56 §432 buys three $2.00 probes to measure what the fifth node adds, and names three readings
BEFORE the money. An analysis written when the data arrive can honour those readings word for word
and still choose the mean over the median, or the node's own score over the running best, or node
id over terminal order — and land wherever it likes. §433's own table turns from "decays gently"
into "stops at the third node" on exactly one of those choices.

So the arithmetic is a committed instrument with its choices pinned here. Each test below is one
choice that could have gone the other way and would have changed the answer.
"""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("node_gains_under_test",
                                               REPO / "benchmarks" / "node_gains.py")
ng = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ng)


def _run(tmp_path: Path, probe: str, task: str, metrics, salvaged=()) -> Path:
    """A probe tree holding one run whose terminals land in the order given."""
    d = tmp_path / probe / "runs" / task / "run"
    d.mkdir(parents=True)
    rows = []
    for i, m in enumerate(metrics):
        rows.append({"v": 1, "seq": i, "type": "node_evaluated",
                     "data": {"node_id": len(metrics) - i, "metric": m}})
    for m in salvaged:
        rows.append({"v": 1, "seq": 900, "type": "node_evaluated",
                     "data": {"node_id": 99, "metric": m, "metric_salvaged": True}})
    (d / "events.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return tmp_path


def _table(tmp_path: Path) -> str:
    by_task, runs, salvaged = ng.collect([tmp_path])
    buf = io.StringIO()
    ng.report(by_task, runs, salvaged, out=buf)
    return buf.getvalue()


def test_delta_is_the_running_best_not_the_nodes_own_score(tmp_path):
    """A third node scoring 5 after a second scoring 100 adds NOTHING, and Δ3 must be 0 rather than
    −95. The question phase 1 asks is "did the k-th node beat what was already there", and a
    negative Δ would average away exactly the runs where the answer is no."""
    _run(tmp_path, "p", "t", [10.0, 100.0, 5.0])
    series, _ = ng.run_series(tmp_path / "p" / "runs" / "t" / "run" / "events.jsonl")
    assert series == [10.0, 100.0, 5.0]
    out = _table(tmp_path)
    line3 = [l for l in out.splitlines() if l.strip().startswith("3 ")][0]
    assert " 0 " in line3 and "-95" not in line3, line3


def test_the_share_that_improved_is_printed_beside_the_median_and_the_mean(tmp_path):
    """One number would BE the choice. Three runs where one improves hugely and two not at all:
    mean says the node is worth 30, median says 0, and the share says one in three. §433 is the
    record of the first two disagreeing about the same corpus."""
    for name, metrics in (("a", [1.0, 91.0]), ("b", [1.0, 1.0]), ("c", [1.0, 0.5])):
        _run(tmp_path, name, "t", metrics)
    out = _table(tmp_path)
    line2 = [l for l in out.splitlines() if l.strip().startswith("2 ")][0]
    assert "33%" in line2, line2
    assert " 0 " in line2, ("the median must be 0", line2)
    assert "30" in line2, ("the mean must be 30", line2)


def test_the_order_is_the_terminals_not_the_node_ids(tmp_path):
    """Nodes are built concurrently and ids are reserved before the work, so the k-th node a run
    LEARNED about is the k-th terminal. The fixture numbers its ids backwards on purpose."""
    _run(tmp_path, "p", "t", [5.0, 50.0])
    out = _table(tmp_path)
    assert "лучшее после k" in out
    line1 = [l for l in out.splitlines() if l.strip().startswith("1 ")][0]
    assert "5" in line1 and "50" not in line1, ("node id order would put 50 first", line1)


def test_a_salvaged_node_is_not_a_measurement(tmp_path):
    """`feasible_nodes` excludes a salvaged metric because it was not measured on the search's own
    protocol. The corpus has none, so this changes nothing today — which is why it is worth pinning
    now rather than after a corpus that does."""
    _run(tmp_path, "p", "t", [10.0], salvaged=[999.0])
    out = _table(tmp_path)
    assert "спасённых узлов пропущено: 1" in out, out
    assert "999" not in out, "a salvaged node reached the table:\n" + out


def test_where_the_champion_stands_is_reported(tmp_path):
    """§433's sharpest line: 82 % of champions stand at node 1 or 2. It is a different statistic
    from Δk and must not be re-derived by a reader from the Δ table."""
    _run(tmp_path, "a", "t", [1.0, 90.0])
    _run(tmp_path, "b", "t", [90.0, 1.0])
    out = _table(tmp_path)
    assert "чемпион стоит на —" in out
    assert "узел 1: 1 (50%)" in out and "узел 2: 1 (50%)" in out, out


def test_a_root_with_no_runs_refuses_instead_of_printing_zeros(tmp_path):
    """A path with no data is a mistyped path far more often than a finding, and a table of zeros
    reads as a measurement."""
    assert ng.main([str(tmp_path)]) == 2


def test_a_missing_root_refuses(tmp_path):
    assert ng.main([str(tmp_path / "nope")]) == 2
