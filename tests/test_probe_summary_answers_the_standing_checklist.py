"""Sweep item 9's seven questions, computed once instead of hand-rolled every sweep.

For three sweeps running I answered "champion, test against train, money by phase, spend after the
last evaluated node, eval_train count, does the model use the reference module" by writing the same
throwaway script again. Each rewrite is a chance to compute a slightly different thing and call it
the same number.

It earned its keep on the first run: it flagged `remPde`'s champion as carrying a kernel, which
contradicted §72 and §73.2 -- both of which called that run plain Python. The champion has four
`@njit` kernels behind an indented `try:` import that an earlier grep never saw. Two sections were
corrected.
"""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "benchmarks" / "probe_summary.py"


def _mk_probe(root, name, task, *, nodes, costs_before, costs_after, test=None,
              champion=None, eval_trains=0):
    """A probe tree shaped like the real one: run/{events,spans}.jsonl + final.json + champion."""
    run = root / "model-probes" / name / "runs" / task / "run"
    run.mkdir(parents=True, exist_ok=True)
    t = 1000.0
    events, spans = [], []

    def gen(ts, cost, phase="plan_step"):
        spans.append({"name": "generation", "start": ts, "duration_s": 1.0,
                      "attributes": {"cost": cost, "phase": phase}})

    for c in costs_before:
        t += 1
        gen(t, c)
    for m in nodes:
        t += 1
        events.append({"type": "node_evaluated", "ts": t, "data": {"metric": m}})
    for c in costs_after:
        t += 1
        gen(t, c)
    for i in range(eval_trains):
        spans.append({"name": "tool", "start": t + i,
                      "attributes": {"tool": "run_dev_command", "args": {"name": "eval_train"}}})

    (run / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    (run / "spans.jsonl").write_text("".join(json.dumps(s) + "\n" for s in spans))
    probe = root / "model-probes" / name
    if test is not None:
        (probe / "final.json").write_text(json.dumps({"speedup": test, "subset": "test"}))
    if champion is not None:
        (probe / "champion_solver.py").write_text(champion)
    return probe


def _run(*roots):
    r = subprocess.run([sys.executable, str(TOOL), *[str(x) for x in roots]],
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def test_it_reports_spend_before_the_first_node_not_only_after_the_last(tmp_path):
    """§72's finding: the waste metric pointed at the wrong end of the run."""
    _mk_probe(tmp_path, "p1", "t", nodes=[10.0],
              costs_before=[0.8], costs_after=[0.2], test=10.0)
    out = _run(tmp_path)
    row = next(l for l in out.splitlines() if l.startswith("p1"))
    assert "80%" in row, f"spend BEFORE the first node is not reported: {row}"
    assert "20%" in row, f"spend AFTER the last node is not reported: {row}"


def test_a_kernel_behind_an_indented_import_is_still_a_kernel(tmp_path):
    """The exact miss that put a false row into two sections of docs/56."""
    body = ("try:\n    from numba import njit\n    _HAVE_NUMBA = True\n"
            "except Exception:\n    _HAVE_NUMBA = False\n\nif _HAVE_NUMBA:\n    @njit\n"
            "    def k(x):\n        return x\n")
    _mk_probe(tmp_path, "p2", "t", nodes=[5.0], costs_before=[0.5], costs_after=[0.5],
              test=5.0, champion=body)
    out = _run(tmp_path)
    row = next(l for l in out.splitlines() if l.startswith("p2"))
    assert "kernel" in row and "plain python" not in row, (
        "a numba kernel behind an indented try: import was read as plain Python -- the miss that "
        f"produced the corrected rows in §72 and §73.2: {row}"
    )


def test_plain_python_is_still_called_plain_python(tmp_path):
    _mk_probe(tmp_path, "p3", "t", nodes=[5.0], costs_before=[0.5], costs_after=[0.5],
              test=5.0, champion="def solve(x):\n    return x\n")
    out = _run(tmp_path)
    row = next(l for l in out.splitlines() if l.startswith("p3"))
    assert "plain python" in row, f"a kernel was invented where there is none: {row}"


def test_a_probe_in_two_roots_is_reported_once_and_the_fuller_copy_wins(tmp_path):
    """Live tree and archive hold the same run; the archive copy can be stale."""
    live, arch = tmp_path / "live", tmp_path / "arch"
    _mk_probe(live, "dup", "t", nodes=[9.0], costs_before=[0.6, 0.3], costs_after=[0.1], test=9.0)
    _mk_probe(arch, "dup", "t", nodes=[9.0], costs_before=[0.2], costs_after=[], test=9.0)
    # ARCHIVE FIRST, deliberately. With the live root first, "keep the first one seen" and "keep
    # the fuller one" agree, and mutation showed the test passed with the freshness rule deleted.
    out = _run(arch, live)
    rows = [l for l in out.splitlines() if l.startswith("dup")]
    assert len(rows) == 1, f"one probe reported {len(rows)} times:\n{out}"
    assert "1.0000" in rows[0], (
        f"the STALE archive copy won over the fuller live one: {rows[0]}"
    )


def test_a_probe_with_no_final_json_says_so_instead_of_inventing_a_score(tmp_path):
    _mk_probe(tmp_path, "p4", "t", nodes=[3.0], costs_before=[0.5], costs_after=[0.5])
    out = _run(tmp_path)
    row = next(l for l in out.splitlines() if l.startswith("p4"))
    assert " - " in row or row.rstrip().endswith("-") or "  -" in row, (
        f"a probe with no test result did not report it as absent: {row}"
    )


def test_a_final_json_without_a_number_is_absent_not_zero(tmp_path):
    """A run whose final.json carries `speedup: null` has NO score; 0.0 is a real and terrible one.

    The first version of the absent-score test used a probe with no final.json at all, so the line
    that coerces the value never ran -- mutation turned it into `float(sp or 0.0)` and the file
    stayed green while every unscored probe would have been reported as a zero.
    """
    p = _mk_probe(tmp_path, "p6", "t", nodes=[3.0], costs_before=[0.5], costs_after=[0.5])
    (p / "final.json").write_text(json.dumps({"speedup": None, "subset": "test"}))
    out = _run(tmp_path)
    row = next(l for l in out.splitlines() if l.startswith("p6"))
    assert "0.0000" not in row, (
        f"a null speedup was reported as a score of zero, which is a different fact: {row}"
    )


def test_eval_train_is_counted_and_other_dev_commands_are_not(tmp_path):
    p = _mk_probe(tmp_path, "p5", "t", nodes=[1.0], costs_before=[0.5], costs_after=[0.5],
                  test=1.0, eval_trains=3)
    run = p / "runs" / "t" / "run"
    extra = {"name": "tool", "start": 5000,
             "attributes": {"tool": "run_dev_command", "args": {"name": "check"}}}
    with open(run / "spans.jsonl", "a") as fh:
        fh.write(json.dumps(extra) + "\n")
    out = _run(tmp_path)
    row = next(l for l in out.splitlines() if l.startswith("p5"))
    assert row.split()[-3] == "3" or " 3 " in row, (
        f"eval_train count is wrong -- `check` should not be counted: {row}"
    )


def test_an_empty_box_says_so(tmp_path):
    out = _run(tmp_path)
    assert "no probes" in out
