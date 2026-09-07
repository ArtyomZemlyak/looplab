"""A read loop that walks a file one line at a time defeats every net the loop has.

`oldCK8` (2026-09-03) spent $0.9574 of its $1.00 inside `propose` and produced no node, reading
`reference_edge_expansion.py` 189 times with `{"lines":1,"start_line":25}`, `26`, `27`… Nothing
caught it: `repeat_streak` keys on identical arguments and the arguments incremented (192 of 194
reads had streak 1), the identical-result note keys on identical results and each read returned a
different line, and `agent_max_turns` is 0 in every probe.

So the signature has to be the PATH, ignoring the arguments entirely — which is what
`benchmarks/read_loops.py` counts, and what these tests pin. Corpus context for the threshold: the
next-worst triple after oldCK8's 186 is 38, and the ordinary probe re-reads that same file 25-38
times inside `plan_step`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

import read_loops  # noqa: E402


def _span(kind, **attrs):
    return {"kind": kind, "start": 1.0, "attributes": attrs}


def _run(tmp_path: Path, name: str, spans) -> Path:
    d = tmp_path / name / "runs" / "t" / "run"
    d.mkdir(parents=True)
    (d / "spans.jsonl").write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")
    return d / "spans.jsonl"


def _walk(path: str, phase: str, n: int):
    """`n` reads of one path with a DIFFERENT range each time — oldCK8's exact shape."""
    return [_span("tool", tool="repo_read", phase=phase,
                  input=json.dumps({"lines": 1, "path": path, "start_line": i}),
                  output=f"line {i}", repeat_streak=1)
            for i in range(1, n + 1)]


def test_differing_ranges_still_count_as_the_same_file(tmp_path):
    spans = _walk("reference.py", "propose", 40) + [
        _span("generation", phase="propose", cost="0.90"),
        _span("generation", phase="deep_research", cost="0.05"),
    ]
    got = {(ph, p): (n, c) for ph, p, n, c in read_loops.loops(str(_run(tmp_path, "p", spans)))}
    assert got[("propose", "reference.py")][0] == 40, (
        "the counter keyed on arguments again; incrementing start_line must not hide the loop")
    # The turn's price is the phase's spend, not the read's own -- a read costs a whole turn.
    assert abs(got[("propose", "reference.py")][1] - 0.90) < 1e-9


def test_the_same_path_in_two_phases_is_two_findings(tmp_path):
    spans = _walk("ref.py", "propose", 30) + _walk("ref.py", "plan_step", 5) + [
        _span("generation", phase="propose", cost="0.5"),
        _span("generation", phase="plan_step", cost="0.2")]
    rows = read_loops.loops(str(_run(tmp_path, "p", spans)))
    got = {(ph, p): n for ph, p, n, _c in rows}
    assert got[("propose", "ref.py")] == 30 and got[("plan_step", "ref.py")] == 5, got


def test_the_threshold_keeps_ordinary_re_reading_out(tmp_path):
    """Every probe in the corpus re-reads the reference 25-38 times in plan_step. That is the
    background, and a detector that reports it reports nothing."""
    _run(tmp_path, "ordinary", _walk("ref.py", "plan_step", 20) +
         [_span("generation", phase="plan_step", cost="0.3")])
    _run(tmp_path, "runaway", _walk("ref.py", "propose", 186) +
         [_span("generation", phase="propose", cost="0.96")])
    names = [r[1] for r in read_loops.scan(str(tmp_path), threshold=25)]
    assert names == ["runaway"], names
    assert {r[1] for r in read_loops.scan(str(tmp_path), threshold=10)} == {"runaway", "ordinary"}


def test_a_write_or_a_grep_is_not_a_read(tmp_path):
    spans = [_span("tool", tool="write_file", phase="propose",
                   input=json.dumps({"path": "solver.py"}), output="ok") for _ in range(50)]
    spans += [_span("tool", tool="repo_grep", phase="propose",
                    input=json.dumps({"path": "ref.py"}), output="hit") for _ in range(50)]
    spans += [_span("generation", phase="propose", cost="0.5")]
    assert read_loops.loops(str(_run(tmp_path, "p", spans))) == []


def test_a_torn_log_and_a_missing_file_are_survivable(tmp_path):
    d = tmp_path / "p" / "runs" / "t" / "run"
    d.mkdir(parents=True)
    (d / "spans.jsonl").write_text(
        json.dumps(_span("tool", tool="repo_read", phase="propose",
                         input='{"path":"a.py"}', output="x")) + "\nnot json\n{\"torn\": ",
        encoding="utf-8")
    assert read_loops.loops(str(d / "spans.jsonl")) == [("propose", "a.py", 1, 0.0)]
    assert read_loops.loops(str(tmp_path / "nope.jsonl")) == []
