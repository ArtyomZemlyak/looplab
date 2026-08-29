"""A zero from the AlgoTune bridge must say WHICH zero it is.

THE INCIDENT, measured on the arm-B campaign run `runs-armb/spectral_clustering` (2026-08-21). The
bridge scored node_2 and wrote this to the node's own `score.log`:

    {"speedup": 0.0, "eval_seconds": 110.5, "subset": "train",
     "baseline_source": "in-harness (record exposes no baseline_time_ms to cache)"}

The evaluator that produced it had said, on the stderr `looplab_eval.py` captured and then dropped:

    WARNING - Failed evaluations: 1
    WARNING -   spectral_clustering/diag2: Speedup N/A due to invalid results: 95/100 valid (95.0%)

95 of 100. The solver reproduced the reference on ninety-five instances and was five edge cases from
a working submission. What the agent was handed was `0.0`, three times, and from it it concluded
that "replicating the reference is ANSWERED and FAILED", discarded the approach, and spent the rest
of a $1.00 budget on a lesson the harness had already contradicted.

That is not a scoring bug — the SPEEDUP is right, and this file changes nothing about it. It is an
information bug with a measurable cost, and its shape is that ONE printed value stood for at least
four different experiments: wrong on every instance, wrong on five of a hundred, crashed on the
first, and never finished. `test_the_four_zeros_are_distinguishable` is the whole property.

WHAT IS ACTUALLY REACHABLE, and why the fixture is a recording rather than a mock. AlgoTune builds a
per-instance `invalid_solution_analysis` (the code context of the `is_solution` line that rejected
the solution) and shows arm A's own agent up to three of them — `message_writer.py:726-750`. It
CANNOT reach this bridge: `evaluator/main.py:1160` attaches that list only when a `baseline_manager`
was passed and `scripts/evaluate_results.py` passes none, and the summary that script writes carries
literally `{"final_speedup": "<str>"}` and nothing else. So the bridge's only channel is the
evaluator's stderr, and what is on it is a question of fact, not of design. It was answered by
RUNNING the real thing: `tests/fixtures/algotune_eval_invalid_results_stderr.txt` is 104 KB of the
actual stderr of `scripts/evaluate_results.py` scoring that same node_2 solver on the
`spectral_clustering` train split (2026-08-22, `taskset -c 88-91`, 458 s), and the summary fixture
beside it is the file that run wrote. Both are verbatim; the only edit any test makes is
substituting the per-invocation model directory name, which is a pid.

The recording settled three things that guessing would have got wrong:

  1. The aggregate verdict IS there (`Speedup N/A due to invalid results: 94/100 valid (94.0%)`) —
     from the PARENT process, where `setup_logging` ran.
  2. `Validation stats: 94/100 valid` is NOT there, and neither is anything else logged at INFO
     inside the evaluation. The `ProcessPoolExecutor` children start under `forkserver`, never run
     `setup_logging`, and fall through to `logging.lastResort`, which is WARNING-and-above.
  3. Which is exactly why the task's OWN rejections survive: `is_solution` calls `logging.error`, and
     `lastResort` prints those as `ERROR:root:<message>`. 17 such lines in this recording, three
     distinct, and they name the reason in the reference's own words ("Detected argmax over a
     contiguous k-column window (hard fail)"). 139 of AlgoTune's 155 task modules call
     `logging.error`, so this is not a property of one task.

Point 3 is a windfall and the keys are named so as not to over-claim it: 17 lines against 6 invalid
instances, because one rejection logs several checks. `instances_invalid` counts instances;
`is_solution_errors` gives reasons with occurrence counts and never pretends to be a per-instance
list.

    OPEN[is-solution-errors-rank-by-frequency] the three shown are the three most FREQUENT, and a
    harness-internal error outnumbers task rejections. Measured on the real verification run
    (2026-08-22): `get_fresh_solve_callable_with_module_reload: Class 'Solver' not found in solver
    module` fired 100 times from `isolated_benchmark.py`'s daemonic fallback while the reference was
    timed, against 8/5/4 for the real rejections — 4 distinct kinds into 3 slots, one real rejection
    dropped. Nothing at this boundary can separate the two (both are one `logging.error` string on
    the same accidental channel), so `is_solution_errors_distinct` keeps the omission VISIBLE
    instead of hiding it, and the cap stays a one-constant operator decision.
    proof:present:_MAX_IS_SOLUTION_EXAMPLES@benchmarks/algotune/looplab_eval.py

THE PER-INSTANCE ANALYSIS NOW ARRIVES TOO, and it took a patch to the checkout rather than a better
parser here. `evaluate_code_on_dataset` attached AlgoTune's own `invalid_solution_analysis` — the
code context of the `is_solution` line that rejected an instance, which arm A's agent is shown three
of — only under `if baseline_manager and ...`, an argument that chooses where the REFERENCE TIMINGS
come from and that `scripts/evaluate_results.py` does not pass; and `update_single_result` wrote a
summary of `final_speedup` alone. `benchmarks/algotune/patch_invalid_solution_analysis.py` removes
the gate and widens the summary row, `setup_algotune.sh` applies and verifies it, and the block
below carries it into `no_speedup`. Measured end to end on 2026-08-25 against a COPY of the campaign
checkout (the live one was scoring an arm), a `convex_hull` solver that drops one hull vertex on a
deterministic fifth of instances, warm reference timings, `ALGOTUNE_EVAL_WORKERS=6`, 191 s::

    "evaluator_verdict": "Speedup N/A due to invalid results: 86/100 valid (86.0%)",
    "is_solution_errors": [{"message": "Not all points are contained within the convex hull.",
                            "count": 20}],
    "invalid_solution_analysis": ["  370:         # Check that all points are contained within ...
                                   372:             if self._point_outside_hull(point, hull_points):
                                   373:                 logging.error(\"Not all points are ...\")
                                 > 374:                 return False", ... ]

The control is the same solver, the same task and the same regime against the same checkout with
`--revert` applied: identical verdict, identical counts, and no `invalid_solution_analysis` key at
all. That is what makes the key the patch's doing and not the run's.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "benchmarks" / "algotune" / "looplab_eval.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
REAL_STDERR = (FIXTURES / "algotune_eval_invalid_results_stderr.txt").read_text(encoding="utf-8")
REAL_SUMMARY = (FIXTURES / "algotune_eval_invalid_results_summary.json").read_text(encoding="utf-8")
# The model directory the recording was made under. `looplab_eval.py` builds a per-invocation one
# (`<--model>-<pid>`), so every replay rewrites this string and only this string.
RECORDED_MODEL = "REC-90409"
RECORDED_TASK = "spectral_clustering"


def _by_path(path: Path, name: str):
    """Import a `benchmarks/algotune/*.py` script by path — they are scripts, not a package."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LE = _by_path(BRIDGE, "looplab_eval_under_test")


# ------------------------------------------------------------------------------------------------
# A fake AlgoTune checkout that REPLAYS the recording
# ------------------------------------------------------------------------------------------------
# Everything about the evaluator that the bridge can observe: its argv contract, its stdout, its
# stderr, and the summary file it leaves behind. A real run of it is 458 s, which is why this is a
# stub — but the BYTES are the real ones, so the parser is never tested against a format somebody
# imagined. `--models` is echoed back into both the summary and the stderr so the replay is
# self-consistent with whatever per-pid directory the bridge chose this time.
_STUB = '''\
import argparse, json, sys
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--models", nargs="+")
ap.add_argument("--tasks", nargs="+")
ap.add_argument("--output", type=Path)
args = ap.parse_args()
model = args.models[0]

stdout = Path(__file__).with_name("recorded_stdout.txt").read_text(encoding="utf-8")
stderr = Path(__file__).with_name("recorded_stderr.txt").read_text(encoding="utf-8")
summary = Path(__file__).with_name("recorded_summary.json").read_text(encoding="utf-8")

sys.stdout.write(stdout.replace({recorded_model!r}, model))
sys.stderr.write(stderr.replace({recorded_model!r}, model))
if summary.strip():
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(summary.replace({recorded_model!r}, model), encoding="utf-8")
sys.exit(0)
'''


def _checkout(tmp_path: Path, *, stderr: str, summary: str, stdout: str = "") -> Path:
    root = tmp_path / "AlgoTune"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "evaluate_results.py").write_text(
        _STUB.format(recorded_model=RECORDED_MODEL), encoding="utf-8")
    (scripts / "recorded_stdout.txt").write_text(stdout, encoding="utf-8")
    (scripts / "recorded_stderr.txt").write_text(stderr, encoding="utf-8")
    (scripts / "recorded_summary.json").write_text(summary, encoding="utf-8")
    return root


def _score(tmp_path: Path, root: Path, *, task: str = RECORDED_TASK, solver_src: str = "x = 1",
           extra: tuple[str, ...] = ()) -> dict:
    """Run the bridge as the campaign runs it and parse its one JSON line."""
    solver = tmp_path / "solver.py"
    solver.write_text(solver_src, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(BRIDGE), "--algotune-root", str(root), "--task", task,
         "--solver", str(solver), "--subset", "train",
         # NEVER the default: that path is the campaign's real, shared parity cache.
         "--baseline-cache", str(tmp_path / "cache.json"), *extra],
        capture_output=True, text=True, timeout=300)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    assert lines, f"the bridge printed no JSON line\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    return json.loads(lines[-1])


# ------------------------------------------------------------------------------------------------
# The defect, driven end to end against the real recorded bytes
# ------------------------------------------------------------------------------------------------

def test_the_real_recorded_run_reports_why_not_just_zero(tmp_path):
    """The whole incident, replayed: the same solver, the same stderr, the same summary.

    Before this change the bridge printed `{"speedup": 0.0, "eval_seconds": ..., "subset": ...,
    "baseline_source": ...}` and nothing else — every assertion below the first fails on it.
    """
    root = _checkout(tmp_path, stderr=REAL_STDERR, summary=REAL_SUMMARY)
    out = _score(tmp_path, root)

    assert out["speedup"] == 0.0, "the SPEEDUP semantics do not move: N/A is still not a number"

    why = out["no_speedup"]
    assert why["reason"] == "invalid_results"
    # The harness's own sentence, verbatim — not a re-derivation of it.
    assert why["evaluator_verdict"] == \
        "Speedup N/A due to invalid results: 94/100 valid (94.0%)"
    assert (why["instances_valid"], why["instances_total"], why["instances_invalid"]) == (94, 100, 6)
    assert why["validity_pct"] == 94.0
    # "N/A", not 0.0: the distinction between "the harness refused to score" and "it scored zero".
    assert why["speedup_reported"] == "N/A"

    # The reference's own rejections, which is the part that tells a proposer what to FIX.
    messages = [row["message"] for row in why["is_solution_errors"]]
    assert "Detected argmax over a contiguous k-column window (hard fail)." in messages
    assert len(why["is_solution_errors"]) <= LE._MAX_IS_SOLUTION_EXAMPLES
    assert [row["count"] for row in why["is_solution_errors"]] == \
        sorted((row["count"] for row in why["is_solution_errors"]), reverse=True)
    # 17 lines, 6 invalid instances: the keys must not let those two be read as each other.
    assert why["is_solution_error_lines"] == 17
    assert why["is_solution_errors_distinct"] == 3
    assert why["is_solution_error_lines"] != why["instances_invalid"]


def test_a_real_speedup_is_untouched(tmp_path):
    """The scored path keeps its exact shape — no `no_speedup`, no new keys, same number.

    This is the guard on the constraint the fix was given: the explanation goes BESIDE the number,
    it does not get to move it. The summary shape is the one `_find_result` documents from a real
    2026-08-19 run.
    """
    summary = json.dumps({RECORDED_TASK: {RECORDED_MODEL: {"final_speedup": "0.9963"}}})
    root = _checkout(tmp_path, stderr="2026-08-22 04:07:31 - INFO - Evaluation complete: 1/1 "
                                      "successful\n", summary=summary)
    out = _score(tmp_path, root)
    assert out["speedup"] == pytest.approx(0.9963)
    assert "no_speedup" not in out
    assert "stderr_tail" not in out


# ------------------------------------------------------------------------------------------------
# The property itself: one printed value may not stand for four experiments
# ------------------------------------------------------------------------------------------------

# The four outcomes the old line could not tell apart, each built from the FORMAT STRING AlgoTune
# itself writes the verdict with (`scripts/evaluate_results.py:607/546`, read 2026-08-22) rather than
# from a sentence invented here. The timeout case never reaches a verdict at all — that is the point
# of it: the evaluator was still running.
_VERDICT_LINE = "2026-08-22 04:07:31 - WARNING -   {task}/{model}: {msg}"
_FOUR_ZEROS = {
    "wrong on every instance": "Speedup N/A due to invalid results: 0/100 valid (0.0%)",
    "wrong on five of a hundred": "Speedup N/A due to invalid results: 95/100 valid (95.0%)",
    "crashed": "Critical error: solver_exception",
    "nothing timed": "No valid speedup calculations from agent evaluation",
}


def _replay_verdict(tmp_path, msg: str) -> dict:
    stderr = "\n".join([
        "2026-08-22 04:07:31 - WARNING - Failed evaluations: 1",
        _VERDICT_LINE.format(task=RECORDED_TASK, model=RECORDED_MODEL, msg=msg),
        f"2026-08-22 04:07:31 - INFO - Updated summary for {RECORDED_TASK}/{RECORDED_MODEL}: N/A",
    ]) + "\n"
    root = _checkout(tmp_path, stderr=stderr, summary=REAL_SUMMARY)
    return _score(tmp_path, root)


def test_the_four_zeros_are_distinguishable(tmp_path):
    """Four experiments, four different printed lines. On today's bridge all four are byte-identical
    apart from `eval_seconds`, and a reader — human or model — has no way to tell them apart."""
    seen = {}
    for label, msg in _FOUR_ZEROS.items():
        out = _replay_verdict(tmp_path / label.replace(" ", "_"), msg)
        assert out["speedup"] == 0.0
        seen[label] = json.dumps({k: v for k, v in out.items() if k != "eval_seconds"},
                                 sort_keys=True)

    # The timed-out one, which cannot be replayed through a verdict because there is none: the
    # evaluator never finished. `--timeout 0` fires the same branch the campaign's 7200 s does.
    root = _checkout(tmp_path / "timeout", stderr="", summary=REAL_SUMMARY)
    timed_out = _score(tmp_path / "timeout", root, extra=("--timeout", "0"))
    assert timed_out["speedup"] == 0.0
    assert timed_out["no_speedup"]["reason"] == "evaluator_timeout"
    seen["timed out"] = json.dumps({k: v for k, v in timed_out.items() if k != "eval_seconds"},
                                   sort_keys=True)

    assert len(set(seen.values())) == len(seen), \
        "two of these five outcomes print the SAME line:\n" + "\n".join(
            f"  {label}: {line}" for label, line in sorted(seen.items()))

    # And the two invalid-result cases differ in the NUMBERS, not merely in some free text — a
    # proposer reading "95/100" learns something a proposer reading "0/100" must not.
    assert json.loads(seen["wrong on every instance"])["no_speedup"]["instances_valid"] == 0
    assert json.loads(seen["wrong on five of a hundred"])["no_speedup"]["instances_valid"] == 95


@pytest.mark.parametrize("label,msg", sorted(_FOUR_ZEROS.items()))
def test_each_verdict_gets_its_registered_class(tmp_path, label, msg):
    """The class is machine-readable, so a downstream reader never has to regex the sentence."""
    out = _replay_verdict(tmp_path, msg)
    reason = out["no_speedup"]["reason"]
    assert reason in LE.NO_SPEEDUP_REASONS
    assert reason != "unknown", f"{msg!r} is one of THEIR nine error_message shapes"
    assert out["no_speedup"]["evaluator_verdict"] == msg


def test_a_verdict_shape_we_do_not_know_still_carries_the_sentence(tmp_path):
    """`unknown` is an honest answer and must not be a silent one: upstream may add a tenth
    `error_message`, and when it does the operator still gets the words."""
    out = _replay_verdict(tmp_path, "Solver ate the dataset (new upstream wording)")
    assert out["no_speedup"]["reason"] == "unknown"
    assert out["no_speedup"]["evaluator_verdict"] == "Solver ate the dataset (new upstream wording)"


# ------------------------------------------------------------------------------------------------
# The traps that were actually hit while building this
# ------------------------------------------------------------------------------------------------

def test_the_verdict_is_not_read_off_the_updated_summary_line(tmp_path):
    """`Updated summary for <task>/<model>: N/A` matches the verdict regex, on the same task, two
    lines below the real verdict and inside the same five-line window.

    A "last match wins" scan read exactly that and reported `evaluator_verdict: "N/A"` with
    `reason: unknown` — for the very run this change was built from. Measured 2026-08-22 against the
    fixture below, which is why the scan is confined to the `Failed evaluations:` block AND takes
    the first match in it."""
    assert f"Updated summary for {RECORDED_TASK}/{RECORDED_MODEL}: N/A" in REAL_STDERR
    verdict = LE._verdict_from_stderr(REAL_STDERR, RECORDED_TASK)
    assert verdict.startswith("Speedup N/A due to invalid results:")
    assert verdict != "N/A"


def test_a_verdict_for_another_task_is_not_ours(tmp_path):
    """One bridge invocation scores one (task, model), but the evaluator's stderr is a shared
    format and `--tasks` is a list upstream. A verdict about a different task must not be
    reported as this node's."""
    stderr = ("2026-08-22 04:07:31 - WARNING - Failed evaluations: 1\n"
              + _VERDICT_LINE.format(task="discrete_log", model=RECORDED_MODEL,
                                     msg="Speedup N/A due to invalid results: 3/100 valid (3.0%)")
              + "\n")
    assert LE._verdict_from_stderr(stderr, RECORDED_TASK) == ""
    assert LE._verdict_from_stderr(stderr, "discrete_log").startswith("Speedup N/A")


def test_worker_warnings_are_not_mistaken_for_rejections():
    """`WARNING:root:CODE_DIR not set when initializing DaCe` is emitted by every run of every task
    through the same unconfigured-root-logger channel. It is noise, and admitting WARNING here would
    put it at the top of the reasons list on tasks whose `is_solution` logs nothing."""
    assert "WARNING:root:CODE_DIR not set" in REAL_STDERR
    rows, _, _ = LE._is_solution_errors(REAL_STDERR)
    assert all("CODE_DIR" not in row["message"] for row in rows)


def test_a_rejection_message_is_bounded():
    """A traceback pasted into `logging.error` must not become the JSON line."""
    huge = "ERROR:root:" + ("x" * 5000)
    rows, lines, distinct = LE._is_solution_errors(huge)
    assert (lines, distinct) == (1, 1)
    assert len(rows[0]["message"]) == LE._MAX_IS_SOLUTION_CHARS


# ------------------------------------------------------------------------------------------------
# The registry (CLAUDE.md: a duck-typed seam is registry-guarded, in BOTH directions)
# ------------------------------------------------------------------------------------------------

def _emitted_reasons() -> set[str]:
    """Every literal this module hands to `_no_speedup` as its `reason`, from the real AST.

    A substring scan would be satisfied by a commented-out call; `ast.Call` nodes are not comments.
    """
    tree = ast.parse(BRIDGE.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        if name != "_no_speedup" or not node.args:
            continue
        first = node.args[0]
        assert isinstance(first, ast.Constant) and isinstance(first.value, str), \
            "a computed reason cannot be registry-checked; pass a literal"
        found.add(first.value)
    return found


def test_every_emitted_reason_is_registered():
    unregistered = _emitted_reasons() - set(LE.NO_SPEEDUP_REASONS)
    assert not unregistered, f"call sites emit unregistered reasons: {sorted(unregistered)}"


def test_every_verdict_class_is_registered():
    """The other producer: `_VERDICT_REASONS` OVERRIDES the call site's reason, so a typo there is
    the same defect one table over."""
    classes = {reason for _, reason in LE._VERDICT_REASONS}
    assert classes <= set(LE.NO_SPEEDUP_REASONS), sorted(classes - set(LE.NO_SPEEDUP_REASONS))


def _fallback_reasons() -> set[str]:
    """`unknown` has no call site — it is the FALLBACK in two functions, and both are load-bearing.

    Derived from the AST of those two functions rather than asserted by hand, so deleting either
    fallback (which would make an unrecognised verdict crash, or let an unregistered reason ship)
    takes this with it.
    """
    tree = ast.parse(BRIDGE.read_text(encoding="utf-8"))
    functions = {node.name: node for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef)}
    found = set()
    for name in ("_no_speedup", "_emit"):
        assert name in functions, f"{name} is gone; the reason vocabulary has no floor"
        literals = {n.value for n in ast.walk(functions[name])
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        assert "unknown" in literals, f"{name} no longer falls back to a registered reason"
        found |= literals & set(LE.NO_SPEEDUP_REASONS)
    return found


def test_the_registry_has_no_dead_members():
    """The second direction. A member nobody can produce is a word a reader will look for and never
    find, which is how a vocabulary rots into decoration."""
    produced = (_emitted_reasons() | _fallback_reasons()
                | {reason for _, reason in LE._VERDICT_REASONS})
    dead = set(LE.NO_SPEEDUP_REASONS) - produced
    assert not dead, f"registered but unproducible: {sorted(dead)}"


def test_the_verdict_table_matches_algotunes_own_error_messages():
    """Each prefix must be a real `result.error_message` prefix, and the table must stay DISJOINT —
    first match wins, so one prefix shadowing another would silently reclassify."""
    prefixes = [prefix for prefix, _ in LE._VERDICT_REASONS]
    assert len(set(prefixes)) == len(prefixes)
    for i, a in enumerate(prefixes):
        for b in prefixes[i + 1:]:
            assert not a.startswith(b) and not b.startswith(a), f"{a!r} shadows or is shadowed by {b!r}"


# ------------------------------------------------------------------------------------------------
# The invariant, at the one exit
# ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("speedup", [0.0, 0, -1.0, None, "N/A"])
def test_emit_never_ships_a_bare_non_positive_speedup(capsys, speedup):
    """`_emit` is the choke point, so this is a property of the FILE and not of one branch: no
    future path can print an unexplained zero without going through here."""
    LE._emit({"speedup": speedup})
    printed = json.loads(capsys.readouterr().out.strip())
    assert printed["no_speedup"]["reason"] in LE.NO_SPEEDUP_REASONS


def test_an_unregistered_reason_degrades_rather_than_crashes(capsys):
    """A vocabulary slip must not become a node with NO metric: `metric_salvage` DISCARDS those,
    which is strictly worse than a scored zero with a slightly wrong label — the same trade the
    timeout branch was already fixed for once."""
    LE._emit({"speedup": 0.0, "no_speedup": {"reason": "typo_reason"}})
    printed = json.loads(capsys.readouterr().out.strip())
    assert printed["no_speedup"]["reason"] == "unknown"
    assert printed["no_speedup"]["reason_unregistered"] == "typo_reason"


def test_a_positive_speedup_is_printed_untouched(capsys):
    LE._emit({"speedup": 1.5, "subset": "train"})
    assert json.loads(capsys.readouterr().out.strip()) == {"speedup": 1.5, "subset": "train"}


def test_the_bridge_has_exactly_one_printer():
    """The invariant above is only worth having while `_emit` is the only way out. Two `print`
    statements are tolerated and named: `_emit`'s own, and the `--enforce-rules` refusal, which
    this change was asked to leave alone and which already explains itself."""
    tree = ast.parse(BRIDGE.read_text(encoding="utf-8"))
    printers = [node for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "print"
                and not any(isinstance(kw.value, ast.Attribute) and kw.value.attr == "stderr"
                            for kw in node.keywords)]
    assert len(printers) == 2, \
        f"{len(printers)} stdout printers; every JSON line must leave through _emit"


# ------------------------------------------------------------------------------------------------
# The per-instance analysis: WHY, and not only HOW OFTEN
# ------------------------------------------------------------------------------------------------
# This was an open item in this module's docstring until 2026-08-25, and its own line said what
# closing it would take: "patching the AlgoTune checkout (a `patch_*.py` beside the others), not the
# bridge". Both halves are here, and the marker is deleted because closing IS the deletion.
#
# THE TWO REASONS IT COULD NOT GET HERE, neither of them in `looplab_eval.py`:
#
#   1. `AlgoTuner/utils/evaluator/main.py` accumulates `all_invalid_analyses` across every chunk and
#      then attaches it only under `if baseline_manager and all_invalid_analyses:`.
#      `baseline_manager` selects where the REFERENCE TIMINGS come from -- its three other uses are
#      all in the first thirty lines of that function -- and `scripts/evaluate_results.py` resolves
#      the timings itself and passes `baseline_times=` instead. So the argument is None under the
#      one caller this bridge uses, and the list is discarded unread.
#   2. `update_single_result` writes `{"final_speedup": "<str>"}` and nothing else, and that summary
#      is the bridge's ONLY structured channel (`_find_result`).
#
# So the tests below drive `patch_invalid_solution_analysis.py`'s OWN anchor strings, EXECUTED. A
# test that asserted the patch file contains some text would be satisfied by a comment; these build
# a miniature checkout out of the anchors, run the patch on it, import the result and CALL it.

PATCH = _by_path(ROOT / "benchmarks" / "algotune" / "patch_invalid_solution_analysis.py",
                 "patch_invalid_solution_analysis_under_test")
SUBSET_PATCH = _by_path(ROOT / "benchmarks" / "algotune" / "patch_eval_subset.py",
                        "patch_eval_subset_beside_it")
SETUP = ROOT / "benchmarks" / "algotune" / "setup_algotune.sh"

# Three contexts in the shape `ResultAggregator._extract_invalid_contexts` actually produces —
# copied down from the 2026-08-25 verification run, not invented. `ValidationContext.
# format_for_display()` returns the numbered source of the reference's own `is_solution` with `>`
# on the line that rejected the solution, and NO header: the "Invalid Example #1:" wrapper is
# added later by `message_writer`, for arm A's chat transcript, and never reaches the summary.
# The third is Case 2 of that function, which has no source line to point at.
_CONTEXTS = [
    "\n".join([
        "  370:         # Check that all points are contained within or on the boundary",
        "  371:         for point in points:",
        "  372:             if self._point_outside_hull(point, hull_points):",
        '  373:                 logging.error("Not all points are contained within the hull.")',
        "> 374:                 return False",
    ]),
    "\n".join([
        "  364:             # Cross product should be positive for counter-clockwise ordering",
        "  365:             cross_product = v1[0] * v2[1] - v1[1] * v2[0]",
        "  366:             if cross_product < 0:",
        '> 367:                 logging.error("Hull is not convex or not ordered ccw.")',
        "  368:                 return False",
    ]),
    "Problem: 47\nIssue: Solver returned None (no output)",
]


def _summary_with(analysis, *, speedup: str = "N/A") -> str:
    row = {"final_speedup": speedup}
    if analysis is not None:
        row["invalid_solution_analysis"] = analysis
    return json.dumps({RECORDED_TASK: {RECORDED_MODEL: row}})


def test_the_analysis_reaches_the_no_speedup_block(tmp_path):
    """THE ITEM. The same recorded stderr, and a summary written by a PATCHED checkout.

    On the pre-2026-08-25 bridge this fails with `KeyError: 'invalid_solution_analysis'`: the key
    is right there in the record `_find_result` returned, and nothing reads it.
    """
    root = _checkout(tmp_path, stderr=REAL_STDERR, summary=_summary_with(_CONTEXTS))
    out = _score(tmp_path, root)

    why = out["no_speedup"]
    # Everything the bridge already said still stands, unmoved.
    assert out["speedup"] == 0.0
    assert why["reason"] == "invalid_results"
    assert (why["instances_valid"], why["instances_total"]) == (94, 100)
    # ...and now the part that says WHICH check rejected the solution.
    assert why["invalid_solution_analysis"] == _CONTEXTS
    assert "Not all points are contained within the hull" in \
        why["invalid_solution_analysis"][0]
    assert why["invalid_solution_analysis"][0].splitlines()[-1].startswith("> 374:"), \
        "the `>` marker is what names the line that rejected the solution"

    # And it stays OUT of the metric plumbing, the reason `no_speedup` is a nested object at all:
    # `json_line_extras` sweeps every other top-level key into undeclared `auto` extra_metrics.
    from looplab.runtime.sandbox import json_line_extras, json_line_metric
    line = json.dumps(out)
    assert json_line_metric(line, "speedup") == 0.0
    assert json_line_extras(line, "speedup") == {"eval_seconds": pytest.approx(out["eval_seconds"])}


def test_an_unpatched_checkout_is_silent_and_not_wrong(tmp_path):
    """The key is ABSENT, never empty, when the checkout does not carry the patch.

    Same contract as `ALGOTUNE_EVAL_SUBSET`, which an unpatched `evaluate_results.py` ignores: the
    bridge must run against a freshly cloned or `--revert`ed checkout and print exactly what it
    printed before, so that nothing downstream can come to require the new key.
    """
    root = _checkout(tmp_path, stderr=REAL_STDERR, summary=REAL_SUMMARY)
    why = _score(tmp_path, root)["no_speedup"]
    assert "invalid_solution_analysis" not in why
    # The stderr-scraped residual is what such a checkout still gets, and it is untouched.
    assert why["is_solution_errors_distinct"] == 3


def test_a_pathological_context_cannot_become_the_json_line(tmp_path):
    """This line is read by `runtime/sandbox.py` and lands in a node's `score.log`; one context
    with a dataset pasted into it may not be the whole of it. A cut must SAY it was cut."""
    huge = "x" * (LE._MAX_ANALYSIS_CHARS * 4)
    root = _checkout(tmp_path, stderr=REAL_STDERR,
                     summary=_summary_with([huge, huge, huge, huge, huge]))
    shown = _score(tmp_path, root)["no_speedup"]["invalid_solution_analysis"]
    assert len(shown) == LE._MAX_ANALYSIS_EXAMPLES
    assert shown[0].startswith("x" * LE._MAX_ANALYSIS_CHARS)
    assert "truncated" in shown[0]
    assert len(shown[0]) < LE._MAX_ANALYSIS_CHARS * 2


@pytest.mark.parametrize("junk", [None, [], "a string, not a list", [None, 3, ""], {"a": 1}])
def test_a_summary_that_says_nothing_usable_adds_no_key(tmp_path, junk):
    """Upstream may reshape this file; it has before (`_find_result`). Every shape that is not a
    non-empty list of non-empty strings must degrade to the pre-patch line, not to `[]` or a crash.
    """
    root = _checkout(tmp_path, stderr=REAL_STDERR, summary=_summary_with(junk))
    assert "invalid_solution_analysis" not in _score(tmp_path, root)["no_speedup"]


def test_identical_contexts_are_not_collapsed(tmp_path):
    """PER-INSTANCE, and the verification run is why this is pinned rather than left to taste.

    The real 2026-08-25 run against a convex_hull solver wrong on 14 of 100 instances sampled three
    of them and all three had failed the SAME check — `evaluate_summary.json` carried three
    byte-identical contexts. Deduplicating here would make "3 shown" mean a different thing on this
    line than it means to arm A, which is shown the same list undeduplicated; and the collapsed view
    already exists one key up, in `is_solution_errors`, WITH its occurrence count.
    """
    root = _checkout(tmp_path, stderr=REAL_STDERR,
                     summary=_summary_with([_CONTEXTS[0]] * 3))
    why = _score(tmp_path, root)["no_speedup"]
    assert why["invalid_solution_analysis"] == [_CONTEXTS[0]] * 3


def test_a_scored_run_still_carries_nothing(tmp_path):
    """The positive path keeps its exact shape even when the record HAS an analysis on it."""
    root = _checkout(tmp_path, stderr="2026-08-22 04:07:31 - INFO - Evaluation complete\n",
                     summary=_summary_with(_CONTEXTS, speedup="1.4200"))
    out = _score(tmp_path, root)
    assert out["speedup"] == pytest.approx(1.42)
    assert "no_speedup" not in out
    assert "invalid_solution_analysis" not in out


# ------------------------------------------------------------------------------------------------
# The patch itself, EXECUTED against a checkout built from its own anchors
# ------------------------------------------------------------------------------------------------

_MAIN_STUB = '''\
import logging


class AttributedList(list):
    pass


def evaluate_code_on_dataset(all_invalid_analyses, baseline_manager=None):
    attributed_results = AttributedList([])
{anchor}
        # Limit to first 3 invalid analyses as per the original logic
        attributed_results.invalid_solution_analysis = all_invalid_analyses[:3]
    return attributed_results
'''

_EVAL_STUB = '''\
import json
import logging
from dataclasses import dataclass
from pathlib import Path


@dataclass
class EvaluationResult:
    """Result of evaluating a single model on a task."""

    task_name: str
    display_model_name: str
    speedup: float | None
    success: bool
{result_anchor}

def run_one(result, results):
    if True:
        try:
            if True:
{capture_anchor}
        finally:
            pass
    return result


def load_split(task_instance):
    if True:
        try:
            if True:
{dataset_anchor}
                evaluate(data_subset="test",)
                return test_problems
        finally:
            pass


def evaluate(**kwargs):
    return kwargs


def update_single_result(result, summary_file):
    summary_data = {{}}
    for _attempt in range(1):
        try:
            if True:
                if result.success and result.speedup is not None:
                    speedup_str = f"{{result.speedup:.4f}}"
                else:
                    speedup_str = "N/A"

                if result.task_name not in summary_data:
                    summary_data[result.task_name] = {{}}

{summary_anchor}

                with open(summary_file, "w") as f:
                    json.dump(summary_data, f, indent=2)
                return
        finally:
            pass
'''


def _fake_checkout(tmp_path: Path) -> Path:
    """An AlgoTune-shaped tree whose two files are made OUT OF the patch's own anchor strings.

    The anchors are not copied here: they are read off the patch module, so a patch that stops
    matching upstream and gets its anchors re-derived takes this fixture with it, and a patch whose
    anchors no longer occur in its own fixture cannot pass.
    """
    root = tmp_path / "AlgoTune"
    main = root / "AlgoTuner" / "utils" / "evaluator" / "main.py"
    script = root / "scripts" / "evaluate_results.py"
    main.parent.mkdir(parents=True)
    script.parent.mkdir(parents=True)
    main.write_text(_MAIN_STUB.format(anchor=PATCH.MAIN_ANCHOR), encoding="utf-8")
    script.write_text(_EVAL_STUB.format(
        result_anchor=PATCH.RESULT_ANCHOR,
        capture_anchor=PATCH.CAPTURE_ANCHOR,
        dataset_anchor=SUBSET_PATCH.DATASET_ANCHOR,
        summary_anchor=PATCH.SUMMARY_ANCHOR), encoding="utf-8")
    # The fixture has to be a real file before the patch touches it, or the patch is being proved
    # against something that was never valid Python.
    for path in (main, script):
        ast.parse(path.read_text(encoding="utf-8"))
    return root


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_gate_that_discarded_the_analysis_is_gone_and_the_attach_happens(tmp_path):
    """REASON 1, executed. `baseline_manager=None` is what `evaluate_results.py` passes.

    Before the patch this function returns a list with no `invalid_solution_analysis` attribute at
    all, whatever the workers found.
    """
    root = _fake_checkout(tmp_path)
    main = root / "AlgoTuner" / "utils" / "evaluator" / "main.py"

    before = _load(main, "at_main_before")
    assert not hasattr(before.evaluate_code_on_dataset(["a", "b"], baseline_manager=None),
                       "invalid_solution_analysis"), "the fixture no longer reproduces the defect"

    assert PATCH.main(["--algotune-root", str(root)]) == 0
    after = _load(main, "at_main_after")
    assert after.evaluate_code_on_dataset(["a", "b"], baseline_manager=None) \
        .invalid_solution_analysis == ["a", "b"]
    # Upstream's own cap of three is upstream's, and the patch does not touch it.
    assert after.evaluate_code_on_dataset(list("abcde"), baseline_manager=None) \
        .invalid_solution_analysis == ["a", "b", "c"]


def test_the_summary_the_patch_writes_is_the_row_the_bridge_reads(tmp_path):
    """REASON 2, executed, and the round trip closed: THEIR writer -> OUR reader.

    `update_single_result` is called for real, on a real `EvaluationResult`, and the JSON it leaves
    on disk is handed to `looplab_eval.py::_find_result` — the same function the bridge uses. Two
    files agreeing on a key is the seam this is guarding, so neither side gets to be a mock.
    """
    root = _fake_checkout(tmp_path)
    script = root / "scripts" / "evaluate_results.py"
    assert PATCH.main(["--algotune-root", str(root)]) == 0
    module = _load(script, "at_eval_after")

    result = module.EvaluationResult(task_name=RECORDED_TASK, display_model_name=RECORDED_MODEL,
                                     speedup=None, success=False)
    assert result.invalid_solution_analysis == [], "the field must default to empty, never None"
    result.invalid_solution_analysis = list(_CONTEXTS)
    out = tmp_path / "evaluate_summary.json"
    module.update_single_result(result, out)

    record = LE._find_result(json.loads(out.read_text(encoding="utf-8")),
                             RECORDED_TASK, RECORDED_MODEL)
    assert record["final_speedup"] == "N/A", "the speedup field keeps its exact shape and type"
    assert LE._invalid_analysis(record["invalid_solution_analysis"]) == _CONTEXTS


def test_a_run_that_scored_writes_a_byte_identical_summary(tmp_path):
    """ADDITIVE means additive: a solver that passed every check must leave the file it left
    before, or the patch has changed the artefact every published number is read off."""
    root = _fake_checkout(tmp_path)
    script = root / "scripts" / "evaluate_results.py"
    before = _load(script, "at_eval_unpatched")
    good = dict(task_name=RECORDED_TASK, display_model_name=RECORDED_MODEL,
                speedup=1.42, success=True)
    plain = tmp_path / "plain.json"
    before.update_single_result(before.EvaluationResult(**good), plain)

    assert PATCH.main(["--algotune-root", str(root)]) == 0
    after = _load(script, "at_eval_patched")
    patched = tmp_path / "patched.json"
    after.update_single_result(after.EvaluationResult(**good), patched)

    assert patched.read_bytes() == plain.read_bytes()
    assert json.loads(patched.read_text(encoding="utf-8")) == \
        {RECORDED_TASK: {RECORDED_MODEL: {"final_speedup": "1.4200"}}}


def test_the_patch_is_idempotent_and_reverts(tmp_path):
    root = _fake_checkout(tmp_path)
    script = root / "scripts" / "evaluate_results.py"
    assert PATCH.main(["--algotune-root", str(root)]) == 0
    once = script.read_bytes()
    assert PATCH.main(["--algotune-root", str(root)]) == 0
    assert script.read_bytes() == once, "a second application is not a no-op"
    assert PATCH.main(["--algotune-root", str(root), "--revert"]) == 0
    assert PATCH.MARKER not in script.read_text(encoding="utf-8")


def test_reverting_this_patch_does_not_revert_the_train_test_split(tmp_path):
    """THE BACKUP-NAME TRAP, and it is the reason this patch does not use the plain `.orig`.

    `patch_eval_subset.py` backs the SAME file up to `evaluate_results.py.orig`, and that backup
    holds pristine upstream. A second patch reverting through it would restore the checkout to a
    state that scores every LoopLab node on the TEST split while the bridge still stamps
    `"subset": "train"` — the leak that patch's own docstring calls "the exact class of thing this
    whole comparison exists to exclude", reintroduced by an unrelated `--revert`.
    """
    root = _fake_checkout(tmp_path)
    script = root / "scripts" / "evaluate_results.py"
    subset_rc = subprocess.run(
        [sys.executable, str(ROOT / "benchmarks" / "algotune" / "patch_eval_subset.py"),
         "--algotune-root", str(root)], capture_output=True, text=True)
    assert subset_rc.returncode == 0, subset_rc.stderr
    assert SUBSET_PATCH.MARKER in script.read_text(encoding="utf-8")

    assert PATCH.main(["--algotune-root", str(root)]) == 0
    assert PATCH.main(["--algotune-root", str(root), "--revert"]) == 0

    text = script.read_text(encoding="utf-8")
    assert PATCH.MARKER not in text
    assert SUBSET_PATCH.MARKER in text, \
        "reverting the analysis patch took the train/test split patch with it"
    ast.parse(text)


def test_the_patch_refuses_a_checkout_it_does_not_recognise(tmp_path):
    """A silent half-application is the failure mode `setup_algotune.sh` step 2 was written about.
    An anchor that stops matching must be loud, and it must not leave a written file behind."""
    root = _fake_checkout(tmp_path)
    main = root / "AlgoTuner" / "utils" / "evaluator" / "main.py"
    main.write_text("def evaluate_code_on_dataset():\n    return []\n", encoding="utf-8")
    with pytest.raises(SystemExit) as caught:
        PATCH.main(["--algotune-root", str(root)])
    assert "re-derive the anchor" in str(caught.value) or "nothing matched" in str(caught.value)


# ------------------------------------------------------------------------------------------------
# `setup_algotune.sh` is the ledger of deviations, so the step must be IN it, as a command
# ------------------------------------------------------------------------------------------------

def _commands(path: Path) -> str:
    """The script with every comment-only line removed.

    The open item this closes was pinned `absent:invalid_solution_analysis@setup_algotune.sh`
    precisely so that a COMMENT could not satisfy it (CLAUDE.md: "a proof an index can satisfy from
    a COMMENT is the one thing an open item may not rest on"). The marker is deleted now, so this
    is what keeps that discipline after the fact.
    """
    return "\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                     if not line.strip().startswith("#"))


def test_setup_algotune_applies_the_patch_as_a_command():
    commands = _commands(SETUP)
    assert "patch_invalid_solution_analysis.py" in commands, \
        "the deviation is not applied by the script that enumerates every deviation"
    assert "invalid_solution_analysis" in commands, \
        "no command in setup_algotune.sh names the key it is there to deliver"


def test_setup_algotune_verifies_the_patch_took():
    """Both halves, each checked on what is actually different about it. `grep
    invalid_solution_analysis` passes on an UNTOUCHED `main.py` — upstream names the key a dozen
    times there — so the evaluator half must be checked on the GATE's absence instead."""
    commands = _commands(SETUP)
    assert "if baseline_manager and all_invalid_analyses:" in commands
    assert "scripts/evaluate_results.py" in commands


def test_the_setup_script_still_parses():
    assert subprocess.run(["bash", "-n", str(SETUP)], capture_output=True).returncode == 0
