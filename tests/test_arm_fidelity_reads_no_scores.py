"""Checking that the treatment differed is allowed; peeking at the outcome is not.

§190 forbids reading the arm before twelve batches, and §180 is the record of an arm re-designed by
its own noise. But finding out LATE that both sides did the same thing is how $48 buys nothing --
§195 is that failure, caught four minutes in. `benchmarks/arm_fidelity.py` asks the fidelity
question continuously and answers only it.

The property these tests exist for is negative: **the tool must not read a score.** A version that
also printed the champion would turn every fidelity check into an interim read, and no amount of
discipline reliably prevents that once the number is on the screen.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

import arm_fidelity  # noqa: E402


def _probe(root: Path, name: str, executed: int, refused: int, spans: int = 1, metric=999.0):
    run = root / name / "runs" / "edge_expansion" / "run"
    run.mkdir(parents=True)
    rows = []
    for i in range(executed):
        rows.append({"kind": "tool", "attributes": {"tool": "run_probe", "output": "exit=0",
                                                    "phase_span": f"s{i % max(1, spans)}"}})
    for i in range(refused):
        rows.append({"kind": "tool", "attributes": {"tool": "run_probe", "phase_span": "sX",
                                                    "output": "(run_probe refused: this run ...)"}})
    (run / "spans.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    # a score sitting right beside it, which the tool must ignore
    # A `run_finished` beside the score, because since 2026-09-04 the contrast is computed over
    # FINISHED probes only (§209/§212) and these fixtures are complete probes. The score stays
    # exactly where it was -- the point of this file is that the tool walks past it.
    (run / "events.jsonl").write_text(
        json.dumps({"v": 1, "seq": 1, "ts": 1.0, "type": "node_evaluated",
                    "data": {"node_id": 0, "metric": metric}}) + "\n"
        + json.dumps({"v": 1, "seq": 2, "ts": 2.0, "type": "run_finished",
                      "data": {"reason": "budget_exhausted"}}) + "\n", encoding="utf-8")
    (root / name / "final.json").write_text(json.dumps({"speedup": metric}), encoding="utf-8")


def test_it_counts_executed_and_refused_separately(tmp_path):
    _probe(tmp_path, "cap", executed=12, refused=2, spans=3)
    _probe(tmp_path, "free", executed=15, refused=0, spans=6)
    got = arm_fidelity.report(str(tmp_path), ["cap"], ["free"])
    # `finished`/`paused` joined the row on 2026-09-04 (§212): a paused run writes a `final.json`
    # too, so the state has to come from the run's own lifecycle events. The counts are still the
    # claim, so they are asserted exactly and the state keys are asserted for shape, not value.
    row = got["rows"]["cap"]
    assert {k: row[k] for k in ("executed", "refused", "spans")} == {
        "executed": 12, "refused": 2, "spans": 4}, row
    assert isinstance(row["finished"], bool) and isinstance(row["paused"], bool), row
    assert got["rows"]["free"]["executed"] == 15 and got["rows"]["free"]["refused"] == 0
    assert got["contrast"] == 3


def test_no_contrast_is_reported_as_no_contrast(tmp_path, capsys):
    _probe(tmp_path, "cap", executed=12, refused=1)
    _probe(tmp_path, "free", executed=9, refused=0)
    arm_fidelity.main(["--root", str(tmp_path), "--treat", "cap", "--control", "free"])
    out = capsys.readouterr().out
    # Reworded 2026-09-04 (§209): mid-flight the tool refuses to state a contrast at all rather
    # than printing a negative one, because a treated probe stopped at its cap against a control
    # still climbing measures the clock. Either sentence is the same claim -- nothing separates the
    # arms yet -- so accept whichever the tool is entitled to print.
    assert ("NO CONTRAST YET" in out or "no contrast to report yet" in out
            or "NO CONTRAST:" in out), out


def test_the_tool_never_prints_a_score(tmp_path, capsys):
    """The whole point. 999.0 is in events.jsonl and final.json beside every probe."""
    _probe(tmp_path, "cap", executed=3, refused=1, metric=999.0)
    _probe(tmp_path, "free", executed=9, refused=0, metric=888.0)
    arm_fidelity.main(["--root", str(tmp_path), "--treat", "cap", "--control", "free"])
    out = capsys.readouterr().out
    assert "999" not in out and "888" not in out, out


def test_the_source_does_not_reach_for_the_outcome():
    src = Path(arm_fidelity.__file__).read_text(encoding="utf-8")
    for forbidden in ("node_evaluated", "final.json", "metric", "speedup", "champion"):
        assert forbidden not in src.split('"""', 2)[2], (
            f"arm_fidelity.py mentions {forbidden!r} outside its docstring; a fidelity check that "
            "can see the outcome is an interim read with extra steps")
