"""What a campaign REPORTS must be what it MEASURED, and five places said otherwise.

Every test here is driven against the real file it guards, and every defect it pins was found in
the live campaign under `/var/tmp/looplab-bench` on 2026-08-23 rather than imagined:

* `compare_arms.py` printed `rectanglepacking 0.0000` under a footer reading "a 0.0000 means the
  solver was WRONG somewhere". That row's own `no_speedup.reason` is `no_valid_speedups` and the
  captured stderr says `Evaluation complete: 0/1 successful` -- the arena timed nothing. It also
  printed `pagerank 15.6726` and `rbf_interpolation 1.1852` for two task-arms that have no `.done`
  marker, i.e. that `campaign.sh` itself classifies as interrupted and still owed.
* `campaign.sh::final_banner` printed `===== arm A COMPLETE (0/1 markers) =====` and exited 0 over
  an arm whose only task was interrupted -- the marker count inside its own success banner.
* `looplab_eval.py` printed `"subset": "train"` on every line whether or not the evaluator it
  invoked could honour that, so a reverted `patch_eval_subset.py` would have leaked the graded
  split into every node of arm B with the record denying it.
* `snapshot.sh` archived `$SRC/campaign` -- a campaign that finished three days earlier -- and
  nothing from `campaign-paired/`, the live one, under a header promising "everything that cannot
  be regenerated".
* `watchdog.sh` had no check at all for the campaign being GONE, so a dead campaign reads exactly
  like a finished one on a monotone progress counter.

The shell halves are driven by EXTRACTING the real function (or running the real script) rather
than by matching source text: CLAUDE.md's tier 1. `final_banner`'s own comment already says it is a
function so a test can run it over a directory that holds a refusal; this is that test, plus the
case the refusal counter could not see.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "benchmarks"
COMPARE = BENCH / "algotune" / "compare_arms.py"
BRIDGE = BENCH / "algotune" / "looplab_eval.py"
CAMPAIGN = BENCH / "algotune" / "campaign.sh"
SNAPSHOT = BENCH / "snapshot.sh"
WATCHDOG = BENCH / "watchdog.sh"


def _by_path(path: Path, name: str):
    """`benchmarks/` holds scripts, not a package."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CA = _by_path(COMPARE, "compare_arms_under_test")
LE = _by_path(BRIDGE, "looplab_eval_under_test")
PS = _by_path(BENCH / "algotune" / "patch_eval_subset.py", "patch_eval_subset_under_test")


# ------------------------------------------------------------------------------------------------
# compare_arms: a zero the ARENA is responsible for is not a zero the solver earned
# ------------------------------------------------------------------------------------------------
# The three rows below are the shapes the live campaign actually produced, trimmed to the keys the
# reader touches. `rectanglepacking` and `spectral_clustering` are verbatim classes off
# `campaign-paired/B-*.final.json`.
_HARNESS_ZERO = {
    "speedup": 0.0, "eval_seconds": 322.7, "subset": "test",
    "no_speedup": {"reason": "no_valid_speedups",
                   "evaluator_verdict": "No valid speedup calculations from agent evaluation",
                   "speedup_reported": "N/A"},
}
_SOLVER_ZERO = {
    "speedup": 0.0, "eval_seconds": 60.1, "subset": "test",
    "no_speedup": {"reason": "invalid_results",
                   "evaluator_verdict": "Speedup N/A due to invalid results: 95/100 valid (95.0%)",
                   "instances_total": 100, "instances_valid": 95, "speedup_reported": "N/A"},
}
_GOOD = {"speedup": 2.0, "eval_seconds": 10.0, "subset": "test"}


def _campaign_dir(tmp_path: Path, rows: dict, *, markers: set[str] | None = None) -> Path:
    """A `--final-dir` holding `B-<task>.final.json` and the `.done` markers campaign.sh writes."""
    out = tmp_path / "campaign-out"
    out.mkdir(exist_ok=True)
    marked = rows.keys() if markers is None else markers
    for task, row in rows.items():
        (out / f"B-{task}.final.json").write_text(json.dumps(row), encoding="utf-8")
        (out / f"B-{task}.log").write_text("", encoding="utf-8")
        if task in marked:
            (out / f"B-{task}.done").write_text("wall=100 rc=0 cpus=0-1 lanes=1 cores_per_lane=2\n",
                                                encoding="utf-8")
    return out


def _arm_a(tmp_path: Path, scores: dict) -> Path:
    root = tmp_path / "AlgoTune"
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "reports" / "agent_summary.json").write_text(
        json.dumps({t: {"deepseek-v4-flash": {"final_speedup": f"{v:.4f}"}}
                    for t, v in scores.items()}), encoding="utf-8")
    return root


def _runs(tmp_path: Path, tasks) -> Path:
    runs = tmp_path / "runs"
    for task in tasks:
        (runs / task / "run").mkdir(parents=True, exist_ok=True)
    return runs


def _compare(tmp_path: Path, rows: dict, arm_a: dict, *, markers=None) -> str:
    final = _campaign_dir(tmp_path, rows, markers=markers)
    root = _arm_a(tmp_path, arm_a)
    runs = _runs(tmp_path, rows)
    proc = subprocess.run(
        [sys.executable, str(COMPARE), "--algotune-root", str(root), "--runs-root", str(runs),
         "--final-dir", str(final)],
        capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _mean_b(out: str) -> float | None:
    match = re.search(r"^mean over (\d+) complete pairs\s+([\d.]+)\s+([\d.]+)$", out, re.M)
    return None if match is None else float(match.group(3))


def test_a_harness_zero_leaves_the_mean_and_a_solver_zero_stays_in_it():
    """THE ROW THAT STARTED IT. Both rows print `0.0`; only one is a statement about a solver.

    `rectanglepacking`'s arena produced no measurement at all, so averaging it as a zero is the
    "did not finish counted as finished wrong" the module's own docstring forbids -- and it drags
    arm B's mean down and hands arm A a win it did not earn.
    """
    with_harness_zero = {"good": _GOOD, "arena": _HARNESS_ZERO}
    with_solver_zero = {"good": _GOOD, "arena": _SOLVER_ZERO}
    arm_a = {"good": 1.0, "arena": 1.0}
    import tempfile
    with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
        harness = _compare(Path(d1), with_harness_zero, arm_a)
        solver = _compare(Path(d2), with_solver_zero, arm_a)
    # The arena's zero is not a score: one pair, mean 2.0.
    assert _mean_b(harness) == pytest.approx(2.0), harness
    assert re.search(r"^mean over 1 complete pairs", harness, re.M), harness
    # The solver's zero IS a score: two pairs, mean 1.0.
    assert _mean_b(solver) == pytest.approx(1.0), solver
    assert re.search(r"^mean over 2 complete pairs", solver, re.M), solver


def test_the_reason_travels_with_the_row():
    """A `--` used to be as bare as the `0.0000` it replaced. Both now carry the arena's own class."""
    with __import__("tempfile").TemporaryDirectory() as d:
        out = _compare(Path(d), {"good": _GOOD, "arena": _HARNESS_ZERO, "wrong": _SOLVER_ZERO},
                       {"good": 1.0, "arena": 1.0, "wrong": 1.0})
    assert "[B: no_valid_speedups]" in out, out
    assert "[B: invalid_results]" in out, out
    # And the footer no longer asserts something false about the row above it.
    assert "the ARENA producing no measurement" in out, out


def test_a_task_arm_with_no_done_marker_is_not_reported_as_a_score():
    """`campaign.sh` writes `B-<task>.final.json` BEFORE it decides the task-arm is terminal.

    Live: pagerank (15.6726) and rbf_interpolation (1.1852) each have a full score and no marker,
    because their runs were interrupted -- `record_done`'s own comment calls that "still owed".
    """
    rows = {"finished": _GOOD, "interrupted": dict(_GOOD, speedup=9.0)}
    with __import__("tempfile").TemporaryDirectory() as d:
        out = _compare(Path(d), rows, {"finished": 1.0, "interrupted": 1.0},
                       markers={"finished"})
    assert "9.0000" not in out, out
    assert "still\nOWED" in out or "still OWED" in out, out
    assert "interrupted" in out
    # and it is not silently dropped either -- it is a row, marked
    assert re.search(r"^interrupted\s+1\.0000\s+--", out, re.M), out
    assert _mean_b(out) == pytest.approx(2.0), out


def test_the_two_zero_classes_partition_the_bridges_whole_vocabulary():
    """A reason added to `NO_SPEEDUP_REASONS` and forgotten here would DEFAULT to the solver's fault.

    That default is the direction that costs arm B score, silently, which is the class of defect
    this file was corrected for -- so the partition is asserted in both directions and a new member
    on either side goes red instead of being absorbed.
    """
    declared = set(LE.NO_SPEEDUP_REASONS)
    classified = set(CA.SOLVERS_FAULT) | set(CA.NOT_SOLVERS_FAULT)
    assert declared - classified == set(), f"unclassified no_speedup reasons: {declared - classified}"
    assert classified - declared == set(), f"classified but not a reason: {classified - declared}"
    assert set(CA.SOLVERS_FAULT) & set(CA.NOT_SOLVERS_FAULT) == set()


def test_a_missing_arm_is_still_never_a_zero():
    """The promise this module always made, re-checked after the change that reached into it."""
    rows = {"both": _GOOD, "b_only": dict(_GOOD, speedup=5.0)}
    with __import__("tempfile").TemporaryDirectory() as d:
        out = _compare(Path(d), rows, {"both": 1.0})
    assert _mean_b(out) == pytest.approx(2.0), out
    assert "1 of 2 tasks are missing an arm" in out, out


# ------------------------------------------------------------------------------------------------
# the bridge: the SPLIT is read off the evaluator, never asserted
# ------------------------------------------------------------------------------------------------
_EVALUATOR_STUB = '''\
import argparse, json, sys
from pathlib import Path
ap = argparse.ArgumentParser()
ap.add_argument("--models", nargs="+")
ap.add_argument("--tasks", nargs="+")
ap.add_argument("--output", type=Path)
args = ap.parse_args()
sys.stderr.write(Path(__file__).with_name("stderr.txt").read_text(encoding="utf-8"))
args.output.parent.mkdir(parents=True, exist_ok=True)
record = {"final_speedup": "1.5000"}
if Path(__file__).with_name("expose_baseline").exists():
    # Upstream's summary carries only `final_speedup` today; the two defensive reads in the bridge
    # exist for the day it does not, and this is that day.
    record.update({"baseline_time_ms": 100.0, "optimized_time_ms": 50.0})
args.output.write_text(json.dumps({args.tasks[0]: {args.models[0]: record}}), encoding="utf-8")
'''


def _checkout(tmp_path: Path, *, patched: bool, stderr: str = "") -> Path:
    root = tmp_path / "AlgoTune"
    scripts = root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    source = _EVALUATOR_STUB
    if patched:
        # The REAL marker, taken from the patch that writes it -- not a copy of the literal.
        source = f"# {PS.MARKER}\n" + source
    (scripts / "evaluate_results.py").write_text(source, encoding="utf-8")
    (scripts / "stderr.txt").write_text(stderr, encoding="utf-8")
    return root


def _score(tmp_path: Path, root: Path, *, subset: str = "train",
           cache: Path | None = None) -> dict:
    solver = tmp_path / "solver.py"
    solver.write_text("x = 1\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(BRIDGE), "--algotune-root", str(root), "--task", "svm",
         "--solver", str(solver), "--subset", subset,
         # NEVER the default: that path is the campaign's real, shared parity cache.
         "--baseline-cache", str(cache or tmp_path / "cache.json")],
        capture_output=True, text=True, timeout=300)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    assert lines, f"no JSON line\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    return json.loads(lines[-1])


def test_an_unpatched_evaluator_is_never_reported_as_train(tmp_path):
    """`--subset train` is inert without `patch_eval_subset.py`, and the line used to claim it anyway.

    That is the train/test leak `patch_eval_subset.py`'s docstring calls "the exact class of thing
    this whole comparison exists to exclude", wearing a record that denies it.
    """
    out = _score(tmp_path, _checkout(tmp_path, patched=False))
    assert out["subset"] == "test", out
    assert out["subset_evidence"]["asked"] == "train"
    assert out["subset_evidence"]["verified"] is False
    assert out["subset_evidence"]["reason"] == "evaluator_not_patched"


def test_a_patched_evaluator_is_reported_as_train(tmp_path):
    out = _score(tmp_path, _checkout(tmp_path, patched=True))
    assert out["subset"] == "train", out
    assert out["subset_evidence"]["verified"] is True
    assert out["subset_evidence"]["reason"] == "patch_marker_present"


def test_the_evaluators_own_statement_outranks_the_marker(tmp_path):
    """A patched file that nonetheless scored the OTHER half is the leak actually happening.

    The marker says what the file CAN do; the log line says what this invocation DID, and when they
    disagree the line wins and the disagreement is named.
    """
    said_test = "WARNING - LOOPLAB scoring on the 'test' split (100 problems)\n"
    out = _score(tmp_path, _checkout(tmp_path, patched=True, stderr=said_test))
    assert out["subset"] == "test", out
    assert out["subset_evidence"]["reason"] == "subset_mismatch"
    assert out["subset_evidence"]["verified"] is False
    assert out["subset_evidence"]["problems"] == 100


def test_the_evaluator_agreeing_is_the_strongest_evidence(tmp_path):
    said_train = "WARNING - LOOPLAB scoring on the 'train' split (77 problems)\n"
    out = _score(tmp_path, _checkout(tmp_path, patched=True, stderr=said_train))
    assert out["subset"] == "train"
    assert out["subset_evidence"]["reason"] == "evaluator_said_so"
    assert out["subset_evidence"]["problems"] == 77


def test_the_bridge_and_the_patch_name_the_same_marker():
    """Two files, one literal. A duck-typed seam is registry-guarded (CLAUDE.md)."""
    assert LE._SUBSET_PATCH_MARKER == PS.MARKER


def test_the_bridge_regex_matches_the_line_the_patch_actually_writes():
    """DERIVED from `patch_eval_subset.py`'s own inserted source, not from a copy of the format.

    The patch builds the line with an f-string inside a string literal, so the test re-evaluates
    THAT f-string and matches the bridge's regex against the result. Renaming the message on either
    side goes red; a comment carrying the words cannot satisfy it, because the line is EXECUTED.
    """
    source = PS.DATASET_PATCH
    call = re.search(
        r'logging\.warning\(f"(?P<a>[^"]*)"\s*\n\s*f"(?P<b>[^"]*)"\)', source)
    assert call is not None, f"the patch no longer logs the split line:\n{source}"
    fmt = call.group("a") + call.group("b")
    _ll_subset, test_problems = "train", [None] * 42
    rendered = eval("f" + repr(fmt), {}, {"_ll_subset": _ll_subset, "test_problems": test_problems})
    match = LE._SUBSET_SCORED_RE.search(rendered)
    assert match is not None, f"the bridge cannot read its own evidence line: {rendered!r}"
    assert match.group("subset") == "train"
    assert match.group("n") == "42"


def test_the_patch_logs_the_split_where_a_forkserver_child_can_be_heard():
    """WARNING and not INFO, and the recording is why.

    The block runs in a `ProcessPoolExecutor` child under `forkserver`; `setup_logging` never ran
    there, so `logging.lastResort` prints WARNING and above and DROPS INFO. Verified against the
    real 104 KB evaluator stderr in `tests/fixtures/`: zero occurrences of the INFO line four lines
    below this one, while the parent's INFO lines are all present.
    """
    assert "logging.warning(f\"LOOPLAB scoring on the" in PS.DATASET_PATCH
    recorded = (Path(__file__).resolve().parent / "fixtures"
                / "algotune_eval_invalid_results_stderr.txt").read_text(encoding="utf-8")
    assert "test problems for" not in recorded, (
        "an INFO line from inside the evaluation now reaches stderr; re-derive whether WARNING is "
        "still required")


def test_subset_evidence_stays_out_of_the_extra_metrics_sweep(tmp_path):
    """`json_line_extras` takes EVERY other numeric key off this line with no declaration.

    `subset_evidence.problems` is a number, so a top-level spelling would enter the operator's
    metrics table, the Pareto front and the MLflow export as an undeclared `auto` measurement --
    the population CLAUDE.md's `extra_metrics` rule is written about, and the reason `no_speedup` is
    a nested object too. Driven through the real reader.
    """
    from looplab.runtime.sandbox import json_line_extras, json_line_metric
    said = "WARNING - LOOPLAB scoring on the 'train' split (100 problems)\n"
    out = _score(tmp_path, _checkout(tmp_path, patched=True, stderr=said))
    line = json.dumps(out)
    assert json_line_metric(line, "speedup") == pytest.approx(1.5)
    assert json_line_extras(line, "speedup") == {"eval_seconds": pytest.approx(out["eval_seconds"])}


# ------------------------------------------------------------------------------------------------
# campaign.sh: the driver does not say COMPLETE over an arm it did not finish
# ------------------------------------------------------------------------------------------------
def _final_banner(out_dir: Path, arm: str, ntasks: int, tasks: str) -> subprocess.CompletedProcess:
    """Run the REAL `final_banner` out of the real script, with nothing else from it.

    Extracted by brace matching rather than reimplemented: the property is about that function's
    behaviour, and a copy in a test is a copy that drifts.
    """
    source = CAMPAIGN.read_text(encoding="utf-8")
    start = source.index("final_banner() {")
    end = source.index("\n}\n", start) + 3
    body = source[start:end]
    script = f'set -u\nLANE_COUNT=1\nCORES_PER_LANE=2\n{body}\nfinal_banner "$1" "$2" "$3" "$4"\n'
    return subprocess.run(["bash", "-c", script, "bash", str(out_dir), arm, str(ntasks), tasks],
                          capture_output=True, text=True, timeout=60)


def test_the_banner_never_says_complete_over_a_task_arm_with_no_marker(tmp_path):
    """Reproduced on this box: `===== arm A COMPLETE (0/1 markers) =====`, exit 0, nothing measured.

    `record_done` writes NO marker and NO `.refused` for rc 130/137/143 -- the interrupted family --
    so the refusal counter, which was the only thing that could stop this banner, never saw them.
    """
    out = tmp_path / "camp"
    out.mkdir()
    (out / "B-alpha.done").write_text("wall=1 rc=0\n", encoding="utf-8")
    (out / "B-beta.log").write_text("", encoding="utf-8")     # ran, interrupted, no marker
    proc = _final_banner(out, "B", 2, "alpha beta")
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "arm B COMPLETE" not in proc.stdout, proc.stdout
    assert "UNFINISHED" in proc.stdout
    assert "beta" in proc.stdout


def test_the_banner_still_says_complete_when_every_task_arm_has_one(tmp_path):
    out = tmp_path / "camp"
    out.mkdir()
    for task in ("alpha", "beta"):
        (out / f"B-{task}.done").write_text("wall=1 rc=0\n", encoding="utf-8")
    proc = _final_banner(out, "B", 2, "alpha beta")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "arm B COMPLETE (2/2 markers)" in proc.stdout


def test_the_banner_still_refuses_a_refusal(tmp_path):
    """The case it always caught, re-checked after the branch above was put in front of it."""
    out = tmp_path / "camp"
    out.mkdir()
    (out / "B-alpha.done").write_text("wall=1 rc=0\n", encoding="utf-8")
    (out / "B-beta.refused").write_text("wall=2 rc=2 cpus=0-1 evidence=none\n", encoding="utf-8")
    proc = _final_banner(out, "B", 2, "alpha beta")
    assert proc.returncode == 3, proc.stdout
    assert "NOT MEASURED" in proc.stdout
    assert "arm B COMPLETE" not in proc.stdout


# ------------------------------------------------------------------------------------------------
# campaign.sh: arm A must reach the same meter arm B reaches
# ------------------------------------------------------------------------------------------------
_CONFIG = """\
global:
  spend_limit: 0.02
models:
  gateway/deepseek-v4-flash:
    model_name: "openai/deepseek-v4-flash"
    spend_limit: 1.0
  openrouter/deepseek/deepseek-v4-flash-0731:
    spend_limit: 1.0
"""


def _fake_algotune(tmp_path: Path) -> Path:
    root = tmp_path / "AlgoTune"
    (root / "AlgoTuner" / "config").mkdir(parents=True)
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "activate").write_text(": # nothing to activate\n", encoding="utf-8")
    (root / "AlgoTuner" / "config" / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    return root


def _arm_a_preflight(tmp_path: Path, key: str, meter: str) -> subprocess.CompletedProcess:
    root = _fake_algotune(tmp_path)
    env = dict(os.environ, ARM="A", ALGOTUNE_ROOT=str(root), BUDGET_USD="1.0",
               ALGOTUNE_MODEL_KEY=key, METER_BASE=meter, TASKS="__none__", SNAPSHOT="0",
               CAMPAIGN_OUT=str(tmp_path / "out"), CAMPAIGN_WS=str(tmp_path / "ws"),
               CAMPAIGN_RUNS=str(tmp_path / "runs"))
    return subprocess.run(["bash", str(CAMPAIGN)], capture_output=True, text=True, timeout=180,
                          env=env)


@pytest.mark.skipif(not (BENCH / "algotune" / "campaign.sh").exists(), reason="no campaign.sh")
def test_arm_a_refuses_a_model_entry_that_would_bypass_the_meter(tmp_path):
    """AlgoTuner names the litellm model `model_info.get("model_name", <the config KEY>)`.

    For an `openrouter/...` key litellm resolves the base as
    `api_base or litellm.api_base or OPENROUTER_API_BASE or "https://openrouter.ai/api/v1"` -- the
    `OPENAI_BASE_URL` this driver exports per task is nowhere in that chain. So the DEFAULT
    `ALGOTUNE_MODEL_KEY` with METER_BASE set sends arm A to the provider directly: no shared RPM
    queue, no shared price table, no rows in meter.jsonl, and a banner still saying "(metered)".
    """
    proc = _arm_a_preflight(tmp_path, "openrouter/deepseek/deepseek-v4-flash-0731",
                            "http://127.0.0.1:8801")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "BYPASS the meter" in proc.stderr, proc.stderr
    # it refused BEFORE running anything
    assert "arm A |" not in proc.stdout, proc.stdout


def test_arm_a_accepts_an_openai_shaped_entry(tmp_path):
    proc = _arm_a_preflight(tmp_path, "gateway/deepseek-v4-flash", "http://127.0.0.1:8801")
    assert "honours OPENAI_BASE_URL" in proc.stdout, proc.stdout + proc.stderr
    assert "BYPASS" not in proc.stderr


def test_an_unmetered_campaign_is_not_second_guessed(tmp_path):
    """No METER_BASE, no claim: the check is about a PROMISE the banner makes, not about routing."""
    proc = _arm_a_preflight(tmp_path, "openrouter/deepseek/deepseek-v4-flash-0731", "")
    assert "BYPASS" not in proc.stderr, proc.stderr
    assert proc.returncode != 2, proc.stdout + proc.stderr


# ------------------------------------------------------------------------------------------------
# snapshot.sh: it archives the campaign that is running, and says what it could not find
# ------------------------------------------------------------------------------------------------
def _bench_root(tmp_path: Path, campaigns, *, with_reports: bool = True) -> Path:
    root = tmp_path / "bench"
    for name in campaigns:
        (root / name).mkdir(parents=True)
        (root / name / "B-task.done").write_text("wall=1 rc=0\n", encoding="utf-8")
        (root / name / "B-task.final.json").write_text('{"speedup": 1.5}', encoding="utf-8")
    for name in ("meter", "logs", "looplab/benchmarks/algotune/.baseline_times"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "meter" / "meter.jsonl").write_text("{}\n", encoding="utf-8")
    # Our own checkout and the per-run evidence became snapshot sources on 2026-08-30, after a
    # container restart proved that recording our HEAD in PROVENANCE.txt was not the same as keeping
    # it. They are built here so that `with_reports=False` still leaves EXACTLY ONE source absent --
    # these tests are about a missing source being named and counted, and that reading only survives
    # if the fixture is otherwise complete.
    subprocess.run(["git", "init", "-q"], cwd=root / "looplab", check=True)
    (root / "looplab" / "kept.py").write_text("# tracked\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root / "looplab", check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"],
                   cwd=root / "looplab", check=True)
    run = root / "model-probes" / "dsX" / "runs" / "r1" / "run"
    run.mkdir(parents=True)
    (run / "events.jsonl").write_text('{"type": "node_evaluated"}\n', encoding="utf-8")
    if with_reports:
        (root / "AlgoTune" / "reports").mkdir(parents=True)
        (root / "AlgoTune" / "reports" / "agent_summary.json").write_text("{}", encoding="utf-8")
    return root


def _snapshot(tmp_path: Path, campaigns, **kw) -> tuple[subprocess.CompletedProcess, Path]:
    root = _bench_root(tmp_path, campaigns, **kw)
    dest = tmp_path / "snapshots"
    proc = subprocess.run(["bash", str(SNAPSHOT), str(dest)], capture_output=True, text=True,
                          timeout=180, env=dict(os.environ, BENCH_ROOT=str(root)))
    return proc, dest


def test_the_snapshot_holds_the_campaign_that_is_running(tmp_path):
    """Live 2026-08-23: the 03:21 snapshot held `campaign/` (finished 08-20) and NOT
    `campaign-paired/` (17 markers, 19 scores, the whole arm-B result set), under a header saying
    "everything that cannot be regenerated". The name was hardcoded; `CAMPAIGN_OUT` is not."""
    proc, dest = _snapshot(tmp_path, ("campaign", "campaign-paired"))
    made = sorted(dest.glob("2*"))
    assert made, proc.stdout + proc.stderr
    names = {p.name for p in made[-1].iterdir()}
    assert "campaign-paired" in names, names
    assert "campaign" in names, names
    assert (made[-1] / "campaign-paired" / "B-task.final.json").exists()


def test_a_snapshot_that_could_not_copy_something_says_so_and_exits_nonzero(tmp_path):
    """It used to `return 0` on a missing path, so an EMPTY archive exited 0 and campaign.sh's
    `|| echo "(snapshot failed...)"` could never fire. An archive that is silently empty is worse
    than none: it is one somebody restores from."""
    proc, dest = _snapshot(tmp_path, ())
    assert proc.returncode == 1, proc.stdout
    assert "NO campaign markers or scores archived" in proc.stdout, proc.stdout
    assert "INCOMPLETE SNAPSHOT" in proc.stdout


def test_a_named_source_that_is_not_there_is_named_in_the_output(tmp_path):
    """The other half of the same rule: `copy()` used to `return 0` on a missing path, so the
    reports directory (arm A's ENTIRE result set -- `agent_summary.json`) could be absent from an
    archive that exited 0 and said nothing."""
    proc, dest = _snapshot(tmp_path, ("campaign-paired",), with_reports=False)
    assert proc.returncode == 1, proc.stdout
    assert "MISSING" in proc.stdout and "reports" in proc.stdout, proc.stdout
    assert "INCOMPLETE SNAPSHOT: 1 source(s) missing" in proc.stdout, proc.stdout
    # the half it COULD copy is still there -- a partial archive is still an archive
    made = sorted(dest.glob("2*"))
    assert (made[-1] / "campaign-paired" / "B-task.final.json").exists()


# ------------------------------------------------------------------------------------------------
# watchdog.sh: "ok" is a claim about a campaign that exists
# ------------------------------------------------------------------------------------------------
_STUB_BIN = {
    # Everything check_once shells out to, so the test never touches the real meter, the real
    # process table or the real disk. `start_meter.sh` is stubbed too: a watchdog test that could
    # restart a live meter is not a test anyone can run.
    "curl": "#!/bin/sh\nexit 0\n",
    "df": "#!/bin/sh\necho Avail\necho 900G\n",
    "pgrep": "#!/bin/sh\nexit ${PGREP_RC:-1}\n",
    "ps": "#!/bin/sh\nexit 0\n",
    "awk": None,        # keep the real one
}


def _watchdog_once(tmp_path: Path, *, campaign_running: bool, owed: bool) -> str:
    root = tmp_path / "bench"
    (root / "campaign-live").mkdir(parents=True)
    (root / "logs").mkdir(parents=True)
    (root / "meter").mkdir(parents=True)
    (root / "meter" / "meter.jsonl").write_text('{"cost": 0.5}\n', encoding="utf-8")
    (root / "campaign-live" / "B-alpha.log").write_text("", encoding="utf-8")
    (root / "campaign-live" / "B-alpha.done").write_text("wall=1 rc=0\n", encoding="utf-8")
    (root / "campaign-live" / "B-beta.log").write_text("", encoding="utf-8")
    if not owed:
        (root / "campaign-live" / "B-beta.done").write_text("wall=1 rc=0\n", encoding="utf-8")

    stub = tmp_path / "bin"
    stub.mkdir()
    for name, body in _STUB_BIN.items():
        if body is None:
            continue
        path = stub / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    # A copy of the script, so the stubbed `meter/start_meter.sh` sits where $HERE expects it.
    here = tmp_path / "wd"
    (here / "meter").mkdir(parents=True)
    (here / "watchdog.sh").write_text(WATCHDOG.read_text(encoding="utf-8"), encoding="utf-8")
    (here / "meter" / "start_meter.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (here / "meter" / "start_meter.sh").chmod(0o755)

    env = dict(os.environ, BENCH_ROOT=str(root),
               PATH=f"{stub}:{os.environ['PATH']}",
               PGREP_RC="0" if campaign_running else "1")
    proc = subprocess.run(["bash", str(here / "watchdog.sh"), "once"], capture_output=True,
                          text=True, timeout=120, env=env)
    return proc.stdout


def test_the_watchdog_does_not_report_ok_over_a_dead_campaign(tmp_path):
    """The failure this file exists for, and nothing checked it.

    Every alarm in `check_once` fires only while `campaign.sh` is RUNNING. When it dies the alarms
    go quiet, the watchdog keeps the meter answering /healthz itself, and the progress counter is
    monotone -- so a frozen `17/20` reads exactly like a finished one, on a five-minute loop, all
    night.
    """
    line = _watchdog_once(tmp_path, campaign_running=False, owed=True)
    assert "] ok |" not in line, line
    assert "DEAD, not done" in line, line


def test_a_finished_campaign_with_no_driver_is_still_ok(tmp_path):
    """A campaign that finished has no driver either, and that is not a fault."""
    line = _watchdog_once(tmp_path, campaign_running=False, owed=False)
    assert "] ok |" in line, line


def test_a_live_campaign_is_not_called_dead(tmp_path):
    line = _watchdog_once(tmp_path, campaign_running=True, owed=True)
    assert "DEAD" not in line, line


def test_the_stall_window_is_wider_than_the_gateways_own_cut(tmp_path):
    """Measured: this gateway CUTS a generation at ~1800 s, and 23 such streams are in
    `meter/meter.jsonl` at 1817-1830 s. A run waiting on one writes nothing for half an hour and is
    healthy, so a 10-minute alarm is a monitor that cries wolf -- the exact cost the comment block
    around this check was already about."""
    source = WATCHDOG.read_text(encoding="utf-8")
    match = re.search(r'stall_min="\$\{WATCHDOG_STALL_MIN:-(\d+)\}"', source)
    assert match is not None, "the stall window is no longer a named, overridable quantity"
    assert int(match.group(1)) * 60 > 1830, (
        f"stall window {match.group(1)} min is inside the gateway's measured 1830 s cut")


def test_the_aggregate_baseline_cache_never_lets_train_price_test(tmp_path):
    """One cache file, two REFERENCE SETS. The key was the task alone.

    `patch_baseline_cache.py` states the rule for the per-instance cache it patches -- "It never
    caches across TASKS or across the train/test SUBSET split ... one standing in for another would
    corrupt every speedup computed from it" -- and the aggregate cache in the bridge did exactly
    that. The campaign scores every node on TRAIN and the champion once on TEST through this same
    default file, so the train baseline was the denominator waiting to be applied to the graded
    score. Driven with a summary that exposes `baseline_time_ms`, which is the shape the two
    defensive reads in the bridge exist for.
    """
    root = _checkout(tmp_path, patched=True)
    (root / "scripts" / "expose_baseline").write_text("", encoding="utf-8")
    cache = tmp_path / "shared_cache.json"
    train = _score(tmp_path, root, subset="train", cache=cache)
    (tmp_path / "solver.py").unlink()
    test = _score(tmp_path, root, subset="test", cache=cache)
    assert train["subset"] == "train" and test["subset"] == "test"
    keys = set(json.loads(cache.read_text(encoding="utf-8")))
    assert keys == {"svm__train", "svm__test"}, (
        f"the two halves share a cache entry: {keys}")


def test_a_run_that_spent_its_ceiling_is_done_even_with_no_marker(tmp_path):
    """A killed DRIVER must not turn a finished RUN into an owed one.

    Measured 2026-08-23: an operator drained the campaign at a task boundary, and the kill landed
    between "champion scored" and `record_done` for two task-arms. Both had spent their whole
    ceiling and written three nodes; reading only the marker would have dropped two real results
    from the table — the same silent exclusion the marker check was added to PREVENT, arriving from
    the other side. So the marker is asked first and the run's own terminal evidence second, and
    the evidence is written by the run rather than by the driver, which is why a killed driver
    cannot forge it.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_cmp", Path(__file__).resolve().parents[1] / "benchmarks/algotune/compare_arms.py")
    cmp_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cmp_mod)

    final = tmp_path / "campaign"
    final.mkdir()
    (final / "B-other.done").write_text("rc=0\n", encoding="utf-8")   # the dir carries markers

    def run_dir(task: str) -> Path:
        d = tmp_path / "runs-B" / task / "run"
        d.mkdir(parents=True)
        (d / "config.snapshot.json").write_text(json.dumps({"llm_budget_usd": 1.0}), encoding="utf-8")
        return d

    # 1. Spent the ceiling, said so, no marker -> done.
    d = run_dir("spent")
    (d / "events.jsonl").write_text(json.dumps(
        {"type": "run_finished", "data": {"reason": "error",
                                          "error": "LLM spend ceiling reached: $1.0017 of the $1.0000"}}) + "\n",
        encoding="utf-8")
    assert cmp_mod.marker_state(final, "B", "spent") == "done"

    # 2. Reached the ceiling by arithmetic without a terminal event (the killed-driver shape).
    d = run_dir("summed")
    (d / "events.jsonl").write_text("".join(
        json.dumps({"type": "llm_usage", "data": {"cost": 0.5}}) + "\n" for _ in range(2)),
        encoding="utf-8")
    assert cmp_mod.marker_state(final, "B", "summed") == "done"

    # 3. Cut off well under the ceiling -> still owed. This is the arm of the test that keeps the
    #    rule honest: without it, "no marker" would simply stop meaning anything.
    d = run_dir("cut")
    (d / "events.jsonl").write_text(
        json.dumps({"type": "llm_usage", "data": {"cost": 0.11}}) + "\n", encoding="utf-8")
    assert cmp_mod.marker_state(final, "B", "cut") == "unfinished"

    # 4. No run directory at all -> owed, never "done by default".
    assert cmp_mod.marker_state(final, "B", "absent") == "unfinished"


# ------------------------------------------------------------------------------------------------
# campaign_status: arm B's number comes out of arm B's files
#
# THE DEFECT, and why it is worse than "reports arm B as unmeasured". This script read
# `<algotune>/reports/agent_summary.json` for both arms. Only arm A writes that file
# (`AlgoTuner/main.py::update_summary_json`), but the rows are keyed by TASK and by MODEL NAME and
# both arms run the SAME model -- so `--arm B` did not miss, it HIT, on arm A's number.
#
# Measured 2026-08-23 against `campaign-paired/`, `--arm B` printed `kcenters 5.9454` (arm B scored
# 4.3635), `edge_expansion 0.9852` (arm B scored 24.1928), `integer_factorization 5.8271` (1.0025)
# and `discrete_log 0.9926` (1.0118), and reported the other fourteen -- every one of which has a
# number in `B-<task>.final.json` -- as "no number". Four of arm A's scores under arm B's banner,
# the arm's best result off by 24x in the direction that loses it the comparison, and nothing in
# the output a reader could use to notice.
# ------------------------------------------------------------------------------------------------
STATUS = BENCH / "algotune" / "campaign_status.py"

# The live rows, so the test is falsifiable against the campaign it was found in.
_LIVE_ARM_A = {"kcenters": 5.9454, "edge_expansion": 0.9852, "integer_factorization": 5.8271}
_LIVE_ARM_B = {"kcenters": 4.3635, "edge_expansion": 24.1928, "integer_factorization": 1.0025}


def _status(tmp_path: Path, arm: str, *, arm_a: dict, rows: dict, markers=None,
            reports: bool = True) -> subprocess.CompletedProcess:
    out = _campaign_dir(tmp_path, rows, markers=markers)
    if arm == "A":
        # Arm A leaves a `.log` and a `.done` and writes its number into AlgoTuner's summary; it has
        # no `.final.json` at all. `_campaign_dir` builds the arm-B shape, so the arm-A side is
        # added here rather than by pretending one arm's files are the other's -- which is the
        # defect this whole block is about.
        for task in sorted(set(arm_a) | set(rows)):
            (out / f"A-{task}.log").write_text("", encoding="utf-8")
            (out / f"A-{task}.done").write_text(
                "wall=7881 rc=0 state=ran_to_completion cpus=0-1 lanes=1 cores_per_lane=2 "
                "attempt=a1\n", encoding="utf-8")
    root = tmp_path / "AlgoTune"
    (root / "reports").mkdir(parents=True, exist_ok=True)
    if reports:
        (root / "reports" / "agent_summary.json").write_text(
            json.dumps({t: {"deepseek-v4-flash": {"final_speedup": f"{v:.4f}"}}
                        for t, v in arm_a.items()}), encoding="utf-8")
    # `marker_state` derives runs-<arm> from the marker directory's PARENT, so it has to exist for
    # the "no marker but the run reached its ceiling" branch to answer at all.
    for task in rows:
        (out.parent / f"runs-{arm}" / task / "run").mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [sys.executable, str(STATUS), "--algotune-root", str(root), "--out", str(out),
         "--arm", arm], capture_output=True, text=True, timeout=120, cwd=str(ROOT))


def test_arm_b_status_reads_arm_b_and_never_arm_a(tmp_path):
    """THE ROW THAT STARTED IT, three times over. Every one of these numbers exists in BOTH arms'
    files for the same task and the same model, so a reader cannot tell from the output which arm
    produced them -- which is why the assertion is that arm A's numbers are ABSENT, not merely that
    arm B's are present."""
    rows = {t: dict(_GOOD, speedup=v) for t, v in _LIVE_ARM_B.items()}
    proc = _status(tmp_path, "B", arm_a=_LIVE_ARM_A, rows=rows)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    for task, value in _LIVE_ARM_B.items():
        assert f"{value:.4f}" in proc.stdout, (task, proc.stdout)
    for task, value in _LIVE_ARM_A.items():
        assert f"{value:.4f}" not in proc.stdout, (
            f"{task} printed arm A's {value} under an arm-B banner:\n{proc.stdout}")
    assert "3 SCORED, 0 no number" in proc.stdout, proc.stdout


def test_arm_a_status_still_reads_arm_a(tmp_path):
    """The control: the file arm A writes is still the file arm A is read from, and arm B's own
    `.final.json` rows must not leak into it either."""
    rows = {t: dict(_GOOD, speedup=v) for t, v in _LIVE_ARM_B.items()}
    proc = _status(tmp_path, "A", arm_a=_LIVE_ARM_A, rows=rows)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    for value in _LIVE_ARM_A.values():
        assert f"{value:.4f}" in proc.stdout, proc.stdout
    assert "24.1928" not in proc.stdout, proc.stdout


def test_a_missing_agent_summary_is_printed_rather_than_raised(tmp_path):
    """It was an unguarded `read_text()`. Running this before arm A finished its first task -- or on
    a box that only ever runs arm B -- ended in a `FileNotFoundError` traceback instead of a status,
    and the missing FILE reads exactly like every task failing unless the tool says which it is."""
    proc = _status(tmp_path, "A", arm_a={}, rows={"kcenters": _GOOD}, reports=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "FileNotFoundError" not in proc.stderr, proc.stderr
    assert "agent_summary.json" in proc.stdout, proc.stdout
    assert "that is the FILE missing, not the tasks failing" in proc.stdout, proc.stdout


def test_arm_b_status_never_needs_arm_a_at_all(tmp_path):
    """Arm B is measurable on a box where AlgoTuner has never run. It used to be the case that no
    `agent_summary.json` meant no arm-B status either, which is the dependency the split removes."""
    rows = {t: dict(_GOOD, speedup=v) for t, v in _LIVE_ARM_B.items()}
    proc = _status(tmp_path, "B", arm_a={}, rows=rows, reports=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "3 SCORED" in proc.stdout, proc.stdout
    # and it does not print a complaint about a file it never needed
    assert "agent_summary.json" not in proc.stdout, proc.stdout


def test_arm_b_status_reads_the_reason_beside_the_zero(tmp_path):
    """`_arm_b_final` is IMPORTED from `compare_arms.py` rather than re-spelled, so the "a zero the
    ARENA is responsible for is not a score" rule holds in both reports or in neither. A second
    copy of that rule is how the two tools came to disagree in the first place."""
    rows = {"good": _GOOD, "arena": _HARNESS_ZERO, "wrong": _SOLVER_ZERO}
    proc = _status(tmp_path, "B", arm_a={}, rows=rows, reports=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "no_valid_speedups" in proc.stdout, proc.stdout          # arena: `--`, with its reason
    assert re.search(r"^\s+wrong\s+0\.0000", proc.stdout, re.M), proc.stdout   # solver: a real zero
    assert "1 SCORED" not in proc.stdout and "2 SCORED" in proc.stdout, proc.stdout


def test_arm_b_status_shows_a_wall_cut_and_keeps_it_out_of_the_median(tmp_path):
    """A `.done` for rc=124 made a wall-cut task-arm read as finished here too. It is SHOWN --
    the operator has to see the wall binding at all -- and excluded from the median, on the same
    rule `compare_arms.py` applies to the mean."""
    rows = {"clean": dict(_GOOD, speedup=2.0), "cut": dict(_GOOD, speedup=3.1223)}
    out = _campaign_dir(tmp_path, rows)
    (out / "B-cut.done").write_text("wall=14447 rc=124 state=wall_cut cpus=66-87 lanes=4 "
                                    "cores_per_lane=22 attempt=a1\n", encoding="utf-8")
    for task in rows:
        (out.parent / "runs-B" / task / "run").mkdir(parents=True, exist_ok=True)
    root = tmp_path / "AlgoTune"
    (root / "reports").mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(STATUS), "--algotune-root", str(root), "--out", str(out), "--arm", "B"],
        capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "3.1223" in proc.stdout, proc.stdout                     # shown
    assert "[wall_cut]" in proc.stdout, proc.stdout                 # and labelled
    assert "CUT AT THE WALL CLOCK" in proc.stdout, proc.stdout
    assert "median 2.0000" in proc.stdout, proc.stdout              # not averaged in
    assert "over 1 at the budget" in proc.stdout, proc.stdout


def test_a_pre_state_marker_is_still_a_wall_cut_to_compare_arms(tmp_path):
    """The 27 markers of the live campaign were written before `record_done` named the state in
    words and carry only `rc=124`. `marker_state` asks `state=wall_cut` first and the integer
    second; dropping the integer would silently reclassify five real wall cuts as clean finishes."""
    legacy = tmp_path / "camp"
    legacy.mkdir()
    (legacy / "B-cut.done").write_text("wall=14400 rc=124 cpus=22-43 lanes=4 cores_per_lane=22\n",
                                       encoding="utf-8")
    assert CA.marker_state(legacy, "B", "cut") == "wall_cut"
    (legacy / "B-new.done").write_text("wall=100 rc=124 state=wall_cut cpus=0-1 attempt=a2\n",
                                       encoding="utf-8")
    assert CA.marker_state(legacy, "B", "new") == "wall_cut"
    (legacy / "B-fine.done").write_text("wall=100 rc=0 state=ran_to_completion cpus=0-1\n",
                                        encoding="utf-8")
    assert CA.marker_state(legacy, "B", "fine") == "done"


def _run_compare(*argv: str) -> int:
    """Drive the real `main()` over a real argv, the way an operator does."""
    import sys
    old = sys.argv
    sys.argv = ["compare_arms.py", *argv]
    try:
        return CA.main()
    finally:
        sys.argv = old


def _mk(final: Path, task: str, *, a_done: str = None, b_done: str = None,
        b_speedup=None, b_reason: str = None) -> None:
    """One task's worth of campaign output, in the shapes the driver really writes."""
    final.mkdir(parents=True, exist_ok=True)
    if a_done is not None:
        (final / f"A-{task}.done").write_text(a_done, encoding="utf-8")
    if b_done is not None:
        (final / f"B-{task}.done").write_text(b_done, encoding="utf-8")
    if b_speedup is not None or b_reason:
        body = {"speedup": b_speedup, "subset": "test"}
        if b_reason:
            body["no_speedup"] = {"reason": b_reason}
        (final / f"B-{task}.final.json").write_text(json.dumps(body), encoding="utf-8")


def test_a_wall_cut_is_excluded_whichever_arm_hit_the_clock(tmp_path, capsys):
    """Until 2026-08-24 only arm B's markers were read, and arm A was wall-cut on 13 of 19 tasks
    against arm B's 3. All thirteen printed as `--`, indistinguishable from "never run", and the
    mean said nothing about one arm losing two thirds of the suite to a clock."""
    cmp_mod = CA
    final = tmp_path / "campaign"
    runs = tmp_path / "runs-B"
    at = tmp_path / "AlgoTune"
    (at / "reports").mkdir(parents=True)

    ok = "wall=100 rc=0 state=ran_to_completion\n"
    cut = "wall=14400 rc=124 state=wall_cut\n"
    for t, a_marker in (("clean", ok), ("a_was_cut", cut)):
        (runs / t / "run").mkdir(parents=True)
        _mk(final, t, a_done=a_marker, b_done=ok, b_speedup=2.0)
    (at / "reports" / "agent_summary.json").write_text(json.dumps(
        {t: {"deepseek-v4-flash": {"final_speedup": "4.0"}} for t in ("clean", "a_was_cut")}),
        encoding="utf-8")

    rc = _run_compare("--algotune-root", str(at), "--runs-root", str(runs), "--final-dir", str(final))
    out = capsys.readouterr().out
    assert rc == 0
    assert "mean over 1 complete pair" in out or "over 1 " in out, out
    assert "a_was_cut" in out and "wall" in out.lower()


def test_a_solver_wrong_on_some_instances_scores_zero_for_EITHER_arm(tmp_path, capsys):
    """The arena's rule is 100% validity or no speedup, and both arms enforce it. Only the RECORDING
    differed: arm B wrote a measured `0.0` that was averaged in, arm A wrote `"N/A"` plus a reason
    in `agent_failures.json` that nothing read, so its zero vanished from the means. Measured on one
    task: A 98/100 valid disappeared, B 95/100 valid scored 0.0 — both directions favouring arm A."""
    cmp_mod = CA
    final = tmp_path / "campaign"
    runs = tmp_path / "runs-B"
    at = tmp_path / "AlgoTune"
    (at / "reports").mkdir(parents=True)
    ok = "wall=100 rc=0 state=ran_to_completion\n"

    (runs / "a_wrong" / "run").mkdir(parents=True)
    _mk(final, "a_wrong", a_done=ok, b_done=ok, b_speedup=3.0)
    (at / "reports" / "agent_summary.json").write_text(json.dumps(
        {"a_wrong": {"deepseek-v4-flash": {"final_speedup": "N/A"}}}), encoding="utf-8")
    (at / "reports" / "agent_failures.json").write_text(json.dumps(
        {"a_wrong": {"deepseek-v4-flash": {"reason": "missing_metrics",
                                           "details": "final_eval_success=True",
                                           "timestamp_utc": "2026-08-24T00:00:00Z"}}}),
        encoding="utf-8")

    assert _run_compare("--algotune-root", str(at), "--runs-root", str(runs), "--final-dir", str(final)) == 0
    out = capsys.readouterr().out
    # It is PAIRED and scored zero, not dropped. Assert on the ROW, not on the whole page: a bare
    # `"0.0000" in out` passed even when arm A's value was dropped, because the string occurs
    # elsewhere — the first version of this test could not tell the fix from its absence.
    row = next(l for l in out.splitlines() if l.startswith("a_wrong"))
    assert "0.0000" in row and "3.0000" in row, row
    assert "--" not in row.split("3.0000")[0], row
    # and it must actually reach the mean, which is the number a reader takes away
    assert "over 1 complete pair" in out or "over 1 pair" in out, out
    assert "missing_metrics" in out


def test_an_arm_a_failure_that_is_NOT_the_solvers_fault_stays_unmeasured(tmp_path, capsys):
    """The control that keeps the rule honest. A reason that does not mean "the evaluation ran and
    the answer was wrong" must leave the row `--`: a task-arm that never got to submit is genuinely
    unmeasured, and scoring it zero would punish an arm for the harness's own failure."""
    cmp_mod = CA
    final = tmp_path / "campaign"
    runs = tmp_path / "runs-B"
    at = tmp_path / "AlgoTune"
    (at / "reports").mkdir(parents=True)
    ok = "wall=100 rc=0 state=ran_to_completion\n"
    (runs / "a_broke" / "run").mkdir(parents=True)
    _mk(final, "a_broke", a_done=ok, b_done=ok, b_speedup=3.0)
    (at / "reports" / "agent_summary.json").write_text(json.dumps(
        {"a_broke": {"deepseek-v4-flash": {"final_speedup": "N/A"}}}), encoding="utf-8")
    (at / "reports" / "agent_failures.json").write_text(json.dumps(
        {"a_broke": {"deepseek-v4-flash": {"reason": "provider_unreachable",
                                           "details": "final_eval_success=False"}}}),
        encoding="utf-8")

    assert _run_compare("--algotune-root", str(at), "--runs-root", str(runs), "--final-dir", str(final)) == 0
    out = capsys.readouterr().out
    assert "0.0000" not in out.split("a_broke")[1].split("\n")[0]
