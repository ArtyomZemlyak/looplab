"""The instrument that decides docs/60 A16 and A17, driven on a span tree of the real shape.

Neither repair can be argued from an impression, and neither can be measured on this box (the
probe corpus lives on the bench stand). So the tool is what ships, and it is driven here on a
synthetic `spans.jsonl` built to the shape the real one has — a phase span with `tool` and
`generation` children — with the four cases that decide each item.

The rule this file exists to hold is the one docs/58 §58.11 was written for: a measurement whose
VALUE the instrument cannot see must be reported as unseen, never folded in as a zero. A tool that
silently reads a cut span preview as `speedup 0.0` would report every long result as a collapse.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "benchmarks" / "session_measurements.py"
_spec = importlib.util.spec_from_file_location("session_measurements", BENCH)
sm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sm)


def _spans(tmp_path: Path, rows: list[dict]) -> str:
    path = tmp_path / "spans.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return str(path)


def _tool(parent, start, tool, payload, output=""):
    return {"kind": "tool", "parent_id": parent, "start": start,
            "attributes": {"tool": tool, "input": payload, "output": output, "phase": "plan_step"}}


def _measure(parent, start, value):
    return _tool(parent, start, "run_dev_command", '{"name": "eval_train"}',
                 f'{{"speedup": {value}, "subset": "train"}}')


def _write(parent, start, tool="write_file"):
    return _tool(parent, start, tool, '{"path": "solver.py"}', "wrote solver.py")


def _gen(parent, cost=0.01, phase="plan_step"):
    return {"kind": "generation", "parent_id": parent, "start": 0.0,
            "attributes": {"cost": cost, "phase": phase}}


def test_a_session_that_wrote_after_its_best_is_the_case_A16_is_about(tmp_path):
    """measure 10 -> write -> measure 200 -> write -> measure 20: the shipped state is the last
    write, which came after the best measurement."""
    path = _spans(tmp_path, [
        _gen("s1"),
        _measure("s1", 1.0, 10.0), _write("s1", 2.0),
        _measure("s1", 3.0, 200.0), _write("s1", 4.0),
        _measure("s1", 5.0, 20.0),
    ])
    row, = sm.sessions(path)
    assert row["measurements"] == 3 and row["unparsed"] == 0 and row["writes"] == 2
    assert row["best"] == 200.0 and row["last"] == 20.0
    assert row["wrote_after_best"] is True and row["last_is_best"] is False
    assert row["gap"] == 180.0


def test_a_session_whose_last_measurement_is_its_best_reports_nothing_to_recover(tmp_path):
    path = _spans(tmp_path, [
        _gen("s1"), _measure("s1", 1.0, 10.0), _write("s1", 2.0), _measure("s1", 3.0, 200.0),
    ])
    row, = sm.sessions(path)
    assert row["last_is_best"] is True and row["wrote_after_best"] is False and row["gap"] == 0.0


def test_an_unreadable_measurement_is_counted_in_neither_direction(tmp_path):
    """A span preview cut mid-JSON is not a score of zero. Folding it in would report every long
    result as a collapse — the exact shape of error docs/58 §58.11 is about."""
    cut = _tool("s1", 3.0, "run_dev_command", '{"name": "eval_train"}',
                '{"subset": "train", "speed')          # the preview cap fell here
    path = _spans(tmp_path, [_gen("s1"), _measure("s1", 1.0, 42.0), cut, _write("s1", 4.0)])
    row, = sm.sessions(path)
    assert row["measurements"] == 2 and row["unparsed"] == 1
    assert row["best"] == 42.0 and row["last"] == 42.0, "the unreadable call must not become 0.0"
    assert "0.0" not in sm.report([("p", row)]).split("median best-minus-last")[0].split("\n")[-2]


def test_a_dev_command_that_is_not_the_measurement_is_not_counted(tmp_path):
    path = _spans(tmp_path, [
        _gen("s1"),
        _tool("s1", 1.0, "run_dev_command", '{"name": "lint"}', '{"speedup": 999.0}'),
        _measure("s1", 2.0, 5.0),
    ])
    row, = sm.sessions(path)
    assert row["measurements"] == 1 and row["best"] == 5.0, "only `eval_train` is a graded number"


def test_a_silent_session_is_named_and_its_turns_counted(tmp_path):
    """A17's population: the session that spends turns and writes nothing. `calls_before_first_write`
    is the whole call count when there is no write, which is what makes p90 meaningful."""
    path = _spans(tmp_path, [_gen("s1"), _measure("s1", 1.0, 1.0), _measure("s1", 2.0, 2.0)])
    row, = sm.sessions(path)
    assert row["wrote_nothing"] is True and row["calls_before_first_write"] == 2
    text = sm.report([("probe", row)])
    assert "wrote NOTHING at all : 1/1" in text


def test_a_read_only_phase_is_not_a_session(tmp_path):
    """`plan`, `propose` and `stages` cannot commit a working set, so "the best edit" is not a
    thing that exists there and neither repair would touch them."""
    rows = [{"kind": "generation", "parent_id": "s9", "start": 0.0,
             "attributes": {"cost": 0.5, "phase": "plan"}},
            {"kind": "tool", "parent_id": "s9", "start": 1.0,
             "attributes": {"tool": "read_file", "input": '{"path": "a.py"}', "phase": "plan"}}]
    assert sm.sessions(_spans(tmp_path, rows)) == []


def test_the_reading_states_its_own_sample(tmp_path):
    """The report must name the denominator: a percentage without one is the defect this
    programme's own audit spent a section on."""
    path = _spans(tmp_path, [
        _gen("s1"), _measure("s1", 1.0, 10.0), _measure("s1", 2.0, 5.0), _write("s1", 3.0),
        _gen("s2"), _measure("s2", 1.0, 7.0), _write("s2", 2.0),
    ])
    text = sm.report([("probe", r) for r in sm.sessions(path)])
    assert "2 writing phases, 2 with a readable measurement" in text
    assert "wrote AFTER their best measurement : 2/2" in text
    assert "READ IT AS" in text, "a number without a reading is a number nobody can act on"


def test_a_missing_or_unreadable_file_is_empty_not_an_exception(tmp_path):
    assert sm.sessions(str(tmp_path / "nope.jsonl")) == []
    broken = tmp_path / "broken.jsonl"
    broken.write_text("not json\n{\n", encoding="utf-8")
    assert sm.sessions(str(broken)) == []
