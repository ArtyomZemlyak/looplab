"""The `resolved` authority tier, demonstrated end to end by the shipped example.

The retired marker `applied-config-glob-undeclared` (runtime/applied_params.py) recorded that no task declares
`applied_config_glob`, so the `resolved` tier — the only reader that can see a value the eval process
settled FOR ITSELF — is inert and every record binds at `committed` authority. Its closing condition was "one line in the repo task's `eval.metric`, plus a run that records
`authority: "resolved"` on a real node". This file is that second leg, made deterministic: no GPU, no
LLM, no run.

WHY IT MATTERS, measured. `rubertlite-dr-unified-v8` node 8 declares `n_epochs: 15`, its committed
carrier AGREES with the declaration, and the config the process resolved says 8 — on a node that
recorded a metric. Two documents that agree with each other and are both wrong about what executed
are invisible to any reading of committed bytes.

WHY THE EXAMPLE NESTS. `config.json` stays flat because that is what a human edits; the trainer
resolves it into `train.x`. A declared coordinate needs at least two dotted parts
(`declared_numeric_params`: "a bare `lr` is a word, not a path"), so the flat committed carrier
cannot answer a legal declaration and the resolved one can. That asymmetry IS the tier's reason to
exist, and real trainers reproduce it — the operator edits one shape, the framework settles another.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

from looplab.runtime import applied_params as ap

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "repo_example"


def _ran_example() -> pathlib.Path:
    """A workdir holding the example AFTER its eval has run — what a node's workdir looks like."""
    work = pathlib.Path(tempfile.mkdtemp())
    for f in EXAMPLE.iterdir():
        if f.is_file():
            shutil.copy2(f, work / f.name)
    proc = subprocess.run([sys.executable, "ttrain.py"], cwd=work,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return work


def _declared_glob() -> str:
    task = json.loads((ROOT / "examples" / "repo_task.json").read_text())
    return (task.get("task", task))["eval"]["metric"]["applied_config_glob"]


def test_the_example_task_declares_the_glob():
    """The marker's first leg, and the thing its `absent:applied_config_glob@examples` predicate
    watches. A declaration pointing at nothing would be the dead branch `param_carriers.py:89`
    refuses one module over, which is why the next test drives it rather than trusting it."""
    assert _declared_glob() == "resolved_config.json"


def test_the_eval_writes_the_config_it_resolved():
    """Without this the glob elects nothing and the tier stays inert no matter what is declared."""
    work = _ran_example()
    written = json.loads((work / "resolved_config.json").read_text())
    assert written == {"train": {"x": 0.0}}, written


def test_the_record_binds_at_RESOLVED_authority():
    """The marker's second leg. `authority` is the whole point: `committed` says "these are the bytes
    staged before the attempt", `resolved` says "this is what the process settled"."""
    work = _ran_example()
    rec = ap.bind_applied_params({"train.x": 0.0}, str(work),
                                 carriers=["config.json"],
                                 applied_config_glob=_declared_glob())
    assert rec is not None, "a bound carrier that answers a declaration must produce a record"
    assert rec["authority"] == ap.APPLIED_RESOLVED
    assert rec["checked"] == 1 and rec["declared"] == 1
    assert rec["applied"] == {"train.x": 0.0}
    assert rec["diverged"] == []


def test_the_committed_carrier_ALONE_cannot_answer_and_that_is_the_point():
    """The asymmetry the tier exists for, asserted rather than described: with no glob the same
    declaration is unanswerable, because the flat committed carrier holds a one-part path."""
    work = _ran_example()
    rec = ap.bind_applied_params({"train.x": 0.0}, str(work), carriers=["config.json"])
    assert rec is None or rec.get("applied") == {}, (
        "the flat committed carrier must not be able to satisfy a two-part declaration")


def test_a_loose_glob_is_REFUSED_rather_than_guessed():
    """`*.json` matches config.json, metrics.json and resolved_config.json. The resolver refuses
    instead of picking one — the same ceiling-on-guessing the suffix matcher applies. This is why a
    task must declare an EXACT enough pattern, and why `docs/reference/goal-dense-retrieval.md`
    names a path rather than a wildcard directory."""
    work = _ran_example()
    row, refused = ap._resolved_carrier(str(work), "*.json", since=None, confine=None)
    assert row is None and refused == "ambiguous"
    row, refused = ap._resolved_carrier(str(work), "resolved_config.json", since=None, confine=None)
    assert row is not None and not refused
