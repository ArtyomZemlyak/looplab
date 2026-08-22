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

    OPEN[algotune-invalid-analysis-stops-at-the-evaluator] the per-instance `is_solution` CODE
    CONTEXT arm A is shown cannot reach arm B's bridge: `evaluate_code_on_dataset` attaches
    `invalid_solution_analysis` only under a `baseline_manager`, and `scripts/evaluate_results.py`
    calls it without one and writes a summary of `final_speedup` alone. Closing it means patching
    the AlgoTune checkout (a `patch_*.py` beside the others), not the bridge.
    proof:absent:invalid_solution_analysis@benchmarks/algotune/setup_algotune.sh

    Pinned on `setup_algotune.sh` rather than on this bridge because the bridge already NAMES the
    analysis in prose, and a proof an index can satisfy from a COMMENT is the one thing an open item
    may not rest on. `setup_algotune.sh` enumerates every deviation applied to the third-party
    checkout, so the day this item closes, the step that closes it lands there as a command.
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


def _bridge():
    """Import `looplab_eval.py` by path — it is a script under `benchmarks/`, not a package."""
    spec = importlib.util.spec_from_file_location("looplab_eval_under_test", BRIDGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LE = _bridge()


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
