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
    # A real run always has events before its first node -- `run_started`, `llm_usage`, and so on --
    # and `summarise` skips a tree with an empty events.jsonl entirely. A fixture without one is a
    # run that cannot exist, and it made the no-nodes case look like a disappearing probe.
    events, spans = [{"type": "run_started", "ts": t, "data": {}}], []

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


# ------------------------------------------------------- a missing score is not one fact either


def test_a_missing_score_comes_with_the_probe_s_own_explanation(tmp_path):
    """`accEE` had no TEST because champion extraction died on an import, on top of an auto-pause.

    Both sentences were in the probe's own two log files, and finding them took six commands. The
    same shape as the zeros section: the diagnosis exists, in a file nothing read.
    """
    p = _mk_probe(tmp_path, "acc", "t", nodes=[221.539], costs_before=[0.3], costs_after=[0.7])
    (p / "probe.log").write_text(
        "[02:17:33] ===== start =====\n"
        "[04:02:54] прогон rc=0 за 6321с\n"
        "could not fold /x/runs/t/run: ModuleNotFoundError: No module named 'looplab'\n"
        "[04:02:54] чемпион: НЕТ\n")
    out = _run(tmp_path)
    assert "ModuleNotFoundError" in out, (
        "a probe with no test score gave no reason, and the reason was in its own log:\n" + out
    )
    # The HEADER too, not just the line: mutation showed that deleting it left the detail floating
    # unlabelled among the per-probe output, where a reader has no idea what it is asserting.
    assert "probes with NO test score" in out, (
        "the reason line is printed with nothing saying what it is:\n" + out
    )


def test_a_pause_is_reported_when_that_is_the_reason(tmp_path):
    p = _mk_probe(tmp_path, "pz", "t", nodes=[5.0], costs_before=[0.5], costs_after=[0.5])
    (p / "run.log").write_text(
        "run=run task=t finished=False\n"
        "stop: PAUSED (node 2) — resumable, NOT finished\n"
        "  pause reason: auto-paused: a Developer session crashed\n")
    out = _run(tmp_path)
    assert "PAUSED" in out, "a paused run's own stop line is not surfaced:\n" + out


def test_a_scored_probe_gets_no_excuse_line(tmp_path):
    """The section is for probes with NO score; a scored one must not appear in it."""
    p = _mk_probe(tmp_path, "ok2", "t", nodes=[5.0], costs_before=[0.5], costs_after=[0.5], test=5.0)
    (p / "probe.log").write_text("could not fold something irrelevant\n")
    out = _run(tmp_path)
    tail = out.split("probes with NO test score", 1)
    assert len(tail) == 1 or "ok2" not in tail[1].split("per-probe detail")[0], (
        "a probe that HAS a test score was listed among those that do not:\n" + out
    )


def test_run_finished_is_not_used_as_the_discriminator(tmp_path):
    """It is absent from four probes on this box that all scored a test perfectly well.

    Reading its absence as "unfinished" would mark accPde, remDL3, remEE and remEE2 as failures.
    """
    import inspect
    src = (REPO / "benchmarks" / "probe_summary.py").read_text()
    i = src.index("def _why_no_test")
    body = src[i:src.index("def summarise")]
    assert "run_finished" not in body.split('"""')[2:] or True  # docstring may mention it
    code = body.split('"""')[-1]
    assert "run_finished" not in code, (
        "run_finished crept into the discriminator; its absence means nothing on its own"
    )


def test_an_unscored_run_with_nodes_is_told_it_is_recoverable_for_free(tmp_path):
    """A run that spent its budget and reached a node has already paid for everything expensive.

    `accEE` sat unscored for twenty hours after an import bug that was fixed the same morning.
    Recovering it cost two commands and $0, and the score came back 224.8846 -- within 0.2 % of the
    figure the brief had been carrying with no evidence behind it on this box. Nothing said the
    recovery was possible, which is the only reason it waited.
    """
    p = _mk_probe(tmp_path, "acc2", "t", nodes=[221.539], costs_before=[0.3], costs_after=[0.7])
    (p / "probe.log").write_text("could not fold /x: ModuleNotFoundError: No module named 'looplab'\n")
    out = _run(tmp_path)
    assert "recoverable for $0" in out, (
        "an unscored run holding an evaluated node was not told it can be re-scored for nothing:\n"
        + out
    )
    assert "extract_champion.py" in out and "--subset test" in out, (
        "the recovery is named but not spelled out, so it still costs a reconstruction:\n" + out
    )
    assert "looplab resume" not in out, (
        "resume continues the RUN and spends more money; accEE had already spent $1.0042 of $1.00, "
        "so resuming would break the budget contract that makes it comparable"
    )


def test_an_unscored_run_with_no_nodes_is_not_promised_a_recovery(tmp_path):
    """Nothing to extract: a run that never evaluated a node cannot be re-scored at any price."""
    p = _mk_probe(tmp_path, "empty2", "t", nodes=[], costs_before=[0.4], costs_after=[])
    (p / "probe.log").write_text("could not fold /x: ModuleNotFoundError\n")
    out = _run(tmp_path)
    block = out.split("probes with NO test score", 1)[-1].split("per-probe detail")[0]
    assert "empty2" in block, "the unscored run vanished entirely"
    assert "recoverable for $0" not in block, (
        "a run with zero evaluated nodes was promised a free recovery there is nothing to do:\n"
        + block
    )
