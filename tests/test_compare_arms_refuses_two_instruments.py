"""`compare_arms.py` refuses to pair two numbers it cannot show were measured on one instrument.

docs/58 §58.3: width moves a speedup ~1.6× on this box and eight arm-B campaign numbers carried no
width; all twenty arm-B files said `baseline_source: in-harness`, so whether the two arms shared a
baseline was not established. The table averaged them anyway, because "no evidence of a mismatch"
read as a match. Since 2026-09-06 every arm-B line carries `eval_workers` + `eval_regime.key` +
`baseline_cache_sha256` and every `.done` marker carries `eval_workers=`/`regime=`/`baseline_sha256=`
(`campaign.sh::ruler_fields`), which is the only place arm A's number can carry an identity at all.
`pair_refusal` reads both and answers WHY, and the answer is printed on the row and in the footer
where the number would have been counted.

Two older defects in the same file close here too, each with its own falsifier:

* an operator-skipped task-arm whose stale values survived from an EARLIER campaign was still
  averaged into the means six lines above a footer saying it never ran (`agent_summary.json` is a
  merge target; `B-<task>.final.json` persists across campaigns);
* the closing block was printed TWICE -- both sides of a 2026-08-29 merge conflict kept at once.

Driven through the real `main()` over real campaign directories, the way an operator runs it.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "benchmarks" / "algotune" / "compare_arms.py"


def _by_path(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CA = _by_path(SCRIPT, "compare_arms_two_instruments_under_test")

SHA1 = "ab" * 32
SHA2 = "cd" * 32


def _ident(regime="__w22x1r3", workers="22", sha=SHA1) -> dict:
    return {"regime": regime, "eval_workers": workers, "baseline_sha256": sha}


# ------------------------------------------------------------------------------------------------
# the rule, as a truth table
# ------------------------------------------------------------------------------------------------

def test_matching_identities_pair():
    assert CA.pair_refusal(_ident(), _ident()) is None


def test_a_side_with_no_width_at_all_is_refused_and_named():
    why = CA.pair_refusal({"regime": None, "eval_workers": None, "baseline_sha256": SHA1}, _ident())
    assert why and "arm A" in why and "width" in why, why
    why = CA.pair_refusal(_ident(), {"regime": None, "eval_workers": None, "baseline_sha256": SHA1})
    assert why and "arm B" in why, why


def test_different_regimes_are_refused_with_both_keys_in_the_sentence():
    why = CA.pair_refusal(_ident(regime="__lane22r3", workers="1"), _ident())
    assert why and "__lane22r3" in why and "__w22x1r3" in why, why


def test_different_widths_are_refused_even_when_only_the_width_is_stated():
    """A row that carries only `eval_workers` (no regime block) is still comparable ON the width."""
    a = {"regime": None, "eval_workers": "24", "baseline_sha256": SHA1}
    b = {"regime": None, "eval_workers": "1", "baseline_sha256": SHA1}
    why = CA.pair_refusal(a, b)
    assert why and "24 workers" in why and "B 1" in why, why
    assert CA.pair_refusal(a, dict(a)) is None


def test_a_missing_baseline_identity_is_refused_on_either_side():
    for side, a, b in (("A", _ident(sha=None), _ident()), ("B", _ident(), _ident(sha=None))):
        why = CA.pair_refusal(a, b)
        assert why and f"arm {side}" in why and "baseline identity" in why, why


def test_different_baselines_are_refused_with_both_digests_in_the_sentence():
    why = CA.pair_refusal(_ident(sha=SHA1), _ident(sha=SHA2))
    assert why and SHA1[:12] in why and SHA2[:12] in why, why


# ------------------------------------------------------------------------------------------------
# where the identity is READ from
# ------------------------------------------------------------------------------------------------

def test_arm_a_identity_comes_off_its_marker_and_the_unknown_spellings_are_absences(tmp_path):
    (tmp_path / "A-demo.done").write_text(
        f"wall=2100 rc=0 state=ran_to_completion cpus=0-21 eval_workers=22 regime=__w22x1r3 "
        f"baseline_sha256={SHA1} ok_calls=40 attempt=a1\n")
    assert CA.ruler_identity(tmp_path, "A", "demo") == _ident()
    (tmp_path / "A-cold.done").write_text(
        "wall=2100 rc=0 state=ran_to_completion eval_workers=22 regime=__w22x1r3 "
        "baseline_sha256=none attempt=a1\n")
    assert CA.ruler_identity(tmp_path, "A", "cold")["baseline_sha256"] is None
    (tmp_path / "A-blind.done").write_text(
        "wall=2100 rc=0 state=ran_to_completion eval_workers=? regime=? baseline_sha256=? "
        "attempt=a1\n")
    assert CA.ruler_identity(tmp_path, "A", "blind") == {"regime": None, "eval_workers": None,
                                                         "baseline_sha256": None}


def test_arm_b_identity_prefers_its_result_line_and_falls_back_to_its_marker(tmp_path):
    (tmp_path / "B-demo.done").write_text(
        f"wall=2100 rc=0 state=ran_to_completion eval_workers=22 regime=__w22x1r3 "
        f"baseline_sha256={SHA2} attempt=a1\n")
    (tmp_path / "B-demo.final.json").write_text(json.dumps(
        {"speedup": 2.0, "subset": "test", "eval_workers": "22",
         "eval_regime": {"key": "__w22x1r3", "workers": 22}, "baseline_cache_sha256": SHA1}))
    assert CA.ruler_identity(tmp_path, "B", "demo")["baseline_sha256"] == SHA1
    # a pre-2026-09-06 line says nothing; the marker stamped after the same pass answers
    (tmp_path / "B-old.done").write_text(
        f"wall=2100 rc=0 state=ran_to_completion eval_workers=22 regime=__w22x1r3 "
        f"baseline_sha256={SHA2} attempt=a1\n")
    (tmp_path / "B-old.final.json").write_text(json.dumps({"speedup": 2.0, "subset": "test"}))
    assert CA.ruler_identity(tmp_path, "B", "old") == _ident(sha=SHA2)
    # and `eval_regime.workers` (nested int) stands in for a missing top-level `eval_workers`
    (tmp_path / "B-nested.final.json").write_text(json.dumps(
        {"speedup": 2.0, "eval_regime": {"key": "__w4x1r3", "workers": 4}}))
    assert CA.ruler_identity(tmp_path, "B", "nested")["eval_workers"] == "4"


# ------------------------------------------------------------------------------------------------
# end to end
# ------------------------------------------------------------------------------------------------

def _campaign(tmp: Path, tasks: dict) -> Path:
    """`tasks`: name -> (arm A marker text, arm B row dict, arm B marker text, arm A score|None)."""
    root = tmp / "bench"
    (root / "AlgoTune" / "reports").mkdir(parents=True)
    summary = {t: {"gateway/deepseek-v4-flash": {"final_speedup": spec[3]}}
               for t, spec in tasks.items() if spec[3] is not None}
    (root / "AlgoTune" / "reports" / "agent_summary.json").write_text(json.dumps(summary))
    final = root / "campaign-final"
    final.mkdir(parents=True)
    for task, (a_marker, b_row, b_marker, _a) in tasks.items():
        (root / "runs-B" / task / "run").mkdir(parents=True)
        (root / "runs-B" / task / "run" / "events.jsonl").write_text("")
        (final / f"B-{task}.final.json").write_text(json.dumps(b_row))
        if b_marker:
            (final / f"B-{task}.done").write_text(b_marker)
        if a_marker:
            (final / f"A-{task}.done").write_text(a_marker)
    return root


def _run(root: Path, *extra: str) -> str:
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--algotune-root", str(root / "AlgoTune"),
         "--runs-root", str(root / "runs-B"), "--final-dir", str(root / "campaign-final"), *extra],
        capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _marker(sha=SHA1, regime="__w22x1r3", workers="22", state="ran_to_completion") -> str:
    return (f"wall=2100 rc=0 state={state} eval_workers={workers} regime={regime} "
            f"baseline_sha256={sha} attempt=a1\n")


def _row(speedup=1.5, sha=SHA1, regime="__w22x1r3", workers="22") -> dict:
    return {"speedup": speedup, "subset": "test", "eval_workers": workers,
            "eval_regime": {"key": regime, "workers": int(workers)}, "baseline_cache_sha256": sha}


def test_two_instruments_are_printed_and_not_averaged_and_the_row_says_why(tmp_path):
    root = _campaign(tmp_path, {
        "same": (_marker(), _row(2.0), _marker(), 3.0),
        "regime": (_marker(regime="__lane22r3", workers="1"), _row(2.0), _marker(), 3.0),
        "baseline": (_marker(sha=SHA2), _row(2.0), _marker(), 3.0),
        "legacy": ("wall=2100 rc=0 state=ran_to_completion attempt=a1\n",
                   {"speedup": 2.0, "subset": "test"},
                   "wall=2100 rc=0 state=ran_to_completion attempt=a1\n", 3.0),
    })
    out = _run(root)
    assert "mean over 1 complete pairs" in out, out
    # TABLE rows start at column 0; the footer names the same tasks indented, so key on the row.
    lines = {name: next(ln for ln in out.splitlines() if ln.startswith(name + " "))
             for name in ("same", "regime", "baseline", "legacy")}
    # the numbers are SHOWN on every row -- this is a refusal to average, not to report
    for name in ("regime", "baseline", "legacy"):
        assert "3.0000" in lines[name] and "2.0000" in lines[name], lines[name]
        assert "NOT PAIRED" in lines[name], lines[name]
    assert "NOT PAIRED" not in lines["same"], lines["same"]
    assert "__lane22r3" in lines["regime"], lines["regime"]
    assert SHA2[:12] in lines["baseline"], lines["baseline"]
    assert "pre-2026-09-06" in lines["legacy"], lines["legacy"]
    # and the footer counts them, by name, with the reason
    assert "3 task(s) have a number on BOTH sides and are NOT in the mean" in out, out
    tail = out.split("NOT in the mean", 1)[1]
    assert "regime" in tail and "baseline" in tail and "legacy" in tail


def test_matching_instruments_still_pair(tmp_path):
    root = _campaign(tmp_path, {"one": (_marker(), _row(2.0), _marker(), 3.0),
                                "two": (_marker(), _row(4.0), _marker(), 1.0)})
    out = _run(root)
    assert "mean over 2 complete pairs" in out, out
    assert "NOT PAIRED" not in out and "NOT in the mean" not in out, out


def test_a_skipped_task_arm_with_stale_values_on_both_sides_is_not_averaged(tmp_path):
    """The `skip-with-stale-final-still-pairs` defect: an operator skip THIS campaign, a number
    left in the merge target and a persisting `final.json` from an earlier one. Both sides have a
    number, and until 2026-09-06 that was enough to average them under a footer saying the task
    never ran."""
    root = _campaign(tmp_path, {
        "stale": ("wall=0 rc=0 state=operator_skip attempt=a1\n", _row(1.5), _marker(), 2.5),
        "live": (_marker(), _row(2.0), _marker(), 3.0),
    })
    out = _run(root)
    assert "mean over 1 complete pairs" in out, out        # `live` only
    row = next(ln for ln in out.splitlines() if ln.startswith("stale"))
    assert "2.5000" in row and "1.5000" in row, row       # still shown, with its state
    assert "SKIPPED by the operator" in out, out


def test_the_tail_is_printed_once(tmp_path):
    """The `compare-arms-prints-its-tail-twice` defect. Every closing sentence appears exactly
    once, on a campaign that exercises every footer branch, with `--reference` on."""
    root = _campaign(tmp_path, {
        "same": (_marker(), _row(2.0), _marker(), 3.0),
        "owed": (_marker(), _row(9.0), "", 3.0),                       # no B marker: still owed
        "cut": (_marker(state="wall_cut").replace("rc=0", "rc=124"), _row(2.0), _marker(), 3.0),
        "skip": ("wall=0 rc=0 state=operator_skip attempt=a1\n", _row(1.5), _marker(), None),
        "arena": (_marker(), {**_row(0.0), "no_speedup": {"reason": "no_valid_speedups"}},
                  _marker(), 3.0),
    })
    # a shipped reference model beside ours, so the reference table has a row to print
    summary = root / "AlgoTune" / "reports" / "agent_summary.json"
    data = json.loads(summary.read_text())
    data["same"]["GPT-5.4"] = {"final_speedup": 7.0}
    summary.write_text(json.dumps(data))
    out = _run(root, "--reference")
    for sentence in ("speedup = baseline_ms / optimized_ms", "still OWED", "SKIPPED by the operator",
                     "the ARENA producing no measurement", "WALL CLOCK", "Shipped reference models",
                     "best reference GPT-5.4", "missing an arm and are EXCLUDED"):
        assert out.count(sentence) == 1, f"{sentence!r} printed {out.count(sentence)} times:\n{out}"
