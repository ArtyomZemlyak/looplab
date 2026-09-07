"""A speedup without its baseline regime is not a comparable number, and no result carried one.

THE DEFECT. `looplab_eval.py::_emit` wrote `speedup`, `eval_seconds`, `subset`, `subset_evidence`
and `baseline_source` — and nothing about the instrument. The denominator of every one of those
ratios is a per-instance REFERENCE measured under a regime: at `ALGOTUNE_EVAL_WORKERS <= 1` the
evaluation pool is bypassed and both halves run in the lane's whole cpuset (`__lane<N>r3`); above
it, `__w<W>x<C>r3`. Measured on this box, the two references sum to 3898 ms and 2976 ms over the
same hundred instances — a 24 % swing under every number.

That is not a latent risk, it is what happened: `run_probe.sh` declared `auto` and a shared cache
dir, `campaign.sh` declared neither, and their results were read side by side because nothing in
either said which ruler it used.

The rule itself was already replicated in this file — inside a closure, where only the refusal
could see it. It is now one module-level `eval_regime()`, so what is REFUSED and what is REPORTED
cannot come apart, and `_emit` stamps it: `_emit` is the single exit, so no path can leave without
its ruler.

NESTED, deliberately. `runtime/sandbox.py::json_line_extras` sweeps every top-level NUMERIC key on
this line into the node's `extra_metrics` as an undeclared `auto` measurement, so `lane_width` at
the top level would enter the operator's metrics table, the Pareto front and the MLflow export as a
score. The same constraint `no_speedup` and `subset_evidence` are shaped by.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "benchmarks" / "algotune" / "looplab_eval.py"
COMPARE = ROOT / "benchmarks" / "algotune" / "compare_arms.py"


def _by_path(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LE = _by_path(BRIDGE, "_bridge_under_test_regime")
CA = _by_path(COMPARE, "_compare_under_test_regime")


def test_every_emitted_line_states_its_regime(monkeypatch, capsys):
    monkeypatch.setenv("ALGOTUNE_EVAL_WORKERS", "auto")
    monkeypatch.setenv("ALGOTUNE_BASELINE_CACHE_DIR", "/tmp/whatever/.baseline_times")
    LE._emit({"speedup": 1.5, "subset": "train"})
    printed = json.loads(capsys.readouterr().out.strip())
    block = printed["eval_regime"]
    width = len(os.sched_getaffinity(0))
    assert block["key"] == f"__w{width}x1r3", block
    assert block["eval_workers"] == "auto"
    assert block["lane_width"] == width
    assert block["baseline_cache_dir"] == "/tmp/whatever/.baseline_times"


def test_a_refusal_states_it_too(monkeypatch, capsys):
    """`_emit` is the ONE exit precisely so a rule holds on every path, including the ones that
    print no number."""
    monkeypatch.delenv("ALGOTUNE_EVAL_WORKERS", raising=False)
    LE._emit({"speedup": None, "no_speedup": {"reason": "no_champion"}})
    printed = json.loads(capsys.readouterr().out.strip())
    assert printed["eval_regime"]["key"] == f"__lane{len(os.sched_getaffinity(0))}r3", printed
    assert printed["eval_regime"]["eval_workers"] == "(unset -> 1)", printed


def test_the_ruler_is_nested_so_it_cannot_become_a_metric(monkeypatch, capsys):
    """The falsifier for a fix that puts `lane_width` at the top level, where
    `json_line_extras` sweeps it into `extra_metrics` as an undeclared `auto` measurement."""
    from looplab.runtime.sandbox import json_line_extras, json_line_metric

    monkeypatch.setenv("ALGOTUNE_EVAL_WORKERS", "auto")
    LE._emit({"speedup": 1.5, "eval_seconds": 12.0, "subset": "train"})
    line = capsys.readouterr().out.strip()
    assert json_line_metric(line, "speedup") == 1.5
    assert json_line_extras(line, "speedup") == {"eval_seconds": 12.0}, (
        "the ruler leaked into the node's extra metrics")


def test_the_guard_and_the_record_are_the_same_rule(monkeypatch):
    """One authority. The refusal names a key it computes; the line reports a key it computes; if
    those are two computations they will disagree exactly when it matters."""
    monkeypatch.setenv("ALGOTUNE_EVAL_WORKERS", "auto")
    width = len(os.sched_getaffinity(0))
    assert LE.eval_regime()["key"] == f"__w{width}x1r3"
    # ...and the refusal, driven end to end through the real bridge, must name that same key.
    import re
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        cache = tmp / "times"
        cache.mkdir()
        (cache / f"demo__train__lane{width}r3.json").write_text("{}", encoding="utf-8")
        marker = re.search(r'_SUBSET_PATCH_MARKER = "([^"]+)"',
                           BRIDGE.read_text(encoding="utf-8")).group(1)
        at = tmp / "AlgoTune"
        (at / "scripts").mkdir(parents=True)
        (at / "scripts" / "evaluate_results.py").write_text(
            f"{marker}\nimport sys\n"
            "print(\"LOOPLAB scoring on the 'train' split (1 problems)\", file=sys.stderr)\n",
            encoding="utf-8")
        (tmp / "solver.py").write_text("class Solver:\n    def solve(self, p):\n        return []\n",
                                       encoding="utf-8")
        got = subprocess.run(
            [sys.executable, str(BRIDGE), "--algotune-root", str(at), "--task", "demo",
             "--model", "T", "--solver", str(tmp / "solver.py"), "--subset", "train",
             "--baseline-times-dir", str(cache), "--timeout", "30"],
            capture_output=True, text=True, timeout=180, cwd=str(tmp),
            env=dict(os.environ, ALGOTUNE_EVAL_WORKERS="auto"))
        row = next(json.loads(ln) for ln in reversed(got.stdout.splitlines())
                   if ln.strip().startswith("{"))
    assert row["no_speedup"]["reason"] == "baseline_regime_mismatch", row
    assert row["eval_regime"]["key"] == f"__w{width}x1r3", row
    assert f"__w{width}x1r3" in row["no_speedup"]["detail"], row


# ------------------------------------------------------------------------------------------------
# and the reader that has to notice
# ------------------------------------------------------------------------------------------------
def _final(dirpath: Path, task: str, regime: str | None) -> None:
    row = {"speedup": 2.0, "subset": "test"}
    if regime is not None:
        row["eval_regime"] = {"key": regime, "lane_width": 22}
    (dirpath / f"B-{task}.final.json").write_text(json.dumps(row), encoding="utf-8")


def test_compare_arms_reads_the_regime_a_row_states(tmp_path):
    _final(tmp_path, "alpha", "__w22x1r3")
    assert CA._arm_b_regime(tmp_path / "B-alpha.final.json") == "__w22x1r3"


def test_a_row_from_before_the_regime_existed_says_nothing_rather_than_guessing(tmp_path):
    _final(tmp_path, "beta", None)
    assert CA._arm_b_regime(tmp_path / "B-beta.final.json") is None
    assert CA._arm_b_regime(tmp_path / "B-missing.final.json") is None
