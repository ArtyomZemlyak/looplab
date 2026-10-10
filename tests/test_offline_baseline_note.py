"""An offline run whose best score is the task's own baseline says so (doc 74 EB-04).

`--kind dataset --backend toy` printed `BEST node 2: metric=10 params={}` and nothing else, and 10
read as a model score: without a model the dataset Developer is a fixed template that counts the
data file's rows. `cli/__init__.py::offline_baseline_note` adds one line under BEST in `run`,
`resume` and `inspect`. Driven here through the REAL CLI on both sides of the rule: a dataset run
(nothing tuned) prints it, a quadratic run (the toy optimizer tuned `x`/`y`) does not.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from looplab.cli import app, offline_baseline_note

ROOT = Path(__file__).resolve().parents[1]
NOTE = "note: backend=toy"


def _run(*args):
    out = CliRunner().invoke(app, ["run", "--no-genesis", "--backend", "toy", *args])
    assert out.exit_code == 0, out.output
    return out.output


def test_an_offline_dataset_run_names_its_score_a_baseline_in_run_and_inspect(tmp_path):
    run_dir = tmp_path / "ds"
    output = _run("--kind", "dataset", "--goal", "predict target", "--direction", "max",
                  "--data", str(ROOT / "examples/dataset_example/data.csv"),
                  "--out", str(run_dir), "--max-nodes", "3")
    lines = output.splitlines()
    best = next(i for i, line in enumerate(lines) if line.startswith("BEST node"))
    assert "params={}" in lines[best]
    assert lines[best + 1].startswith(NOTE), output
    inspected = CliRunner().invoke(app, ["inspect", str(run_dir)])
    assert inspected.exit_code == 0 and NOTE in inspected.output, inspected.output


def test_an_offline_run_that_tuned_its_params_gets_no_note(tmp_path):
    output = _run("--kind", "quadratic", "--goal", "min (x-3)^2", "--direction", "min",
                  "--out", str(tmp_path / "q"), "--max-nodes", "4")
    assert "BEST node" in output and NOTE not in output, output


def test_the_rule_reads_engine_facts_only():
    tuned = SimpleNamespace(idea=SimpleNamespace(params={"x": 1.0}))
    untuned = SimpleNamespace(idea=SimpleNamespace(params={}))
    state = lambda best: SimpleNamespace(best=lambda: best)          # noqa: E731
    assert offline_baseline_note(state(untuned), "llm") == ""        # a model run never gets it
    assert offline_baseline_note(state(untuned), None) == ""         # unknown backend reads as llm
    assert offline_baseline_note(state(tuned), "toy") == ""
    assert offline_baseline_note(state(None), "toy") == ""
    assert offline_baseline_note(state(untuned), "toy").startswith(NOTE)
