"""A champion stopped being one file the moment the card allowed compilation, and the extractor was never told.

THE DEFECT. `extract_champion.py` wrote ONE file — the node's `solver.py` — and `looplab_eval.py`
submits whatever sits BESIDE it (`src.parent.iterdir()`). A node that also committed a `.pyx` and a
`setup.py` therefore shipped without them: no `build_ext` ran in the scored directory, and the
solver's own import of the extension failed.

MEASURED on the live corpus. `sol10` node 10 committed three files — `solver.py`,
`_edge_expansion.pyx`, `setup.py` — and scored 261.1071 on train. Its TEST pass returned:

    {"speedup": 0.0, "eval_seconds": 3.5,
     "no_speedup": {"reason": "solver_unloadable",
                    "evaluator_verdict": "Failed to import optimized solver: .../
                     _edge_expansion_native.py: cannot open shared object file: No such file"}}

Re-extracting the SAME champion with this fix and re-scoring it on the same lane against the same
cached reference gives `build_ext ok` and **259.677**. The best number this project has produced was
thrown away by its own extractor, and `solver_unloadable` reads like a broken solver rather than a
harness that failed to deliver the submission.

`campaign.sh:768` calls this same script, so a compiled champion would have scored zero in a
campaign too — which is why this is pinned rather than left to the probe scripts.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTRACT = ROOT / "benchmarks" / "algotune" / "extract_champion.py"


def _run_log(path: Path, files: dict[str, str]) -> None:
    """One evaluated node whose committed working set is `files`."""
    events = [
        {"v": 1, "seq": 0, "ts": 1.0, "type": "run_started",
         "data": {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"}},
        # `files` rides on `node_created` — checked against the live log rather than assumed:
        # nine of sol10's rows carry the three-file working set there and nothing else carries it.
        {"v": 1, "seq": 1, "ts": 2.0, "type": "node_created",
         "data": {"node_id": 0, "parent_ids": [], "operator": "draft", "files": files,
                  "idea": {"operator": "draft", "params": {}, "rationale": "r"}}},
        {"v": 1, "seq": 2, "ts": 4.0, "type": "node_evaluated",
         "data": {"node_id": 0, "metric": 261.1071}},
    ]
    path.mkdir(parents=True, exist_ok=True)
    (path / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


SUBMISSION = {
    "solver.py": "from _ext import go\n\n\nclass Solver:\n    def solve(self, p, **k):\n        return go(p)\n",
    "_ext.pyx": "# cython: language_level=3\ncpdef double go(list a):\n    return 1.0\n",
    "setup.py": "from setuptools import setup\nfrom Cython.Build import cythonize\nsetup(ext_modules=cythonize('_ext.pyx'))\n",
}


def test_every_file_the_champion_committed_lands_beside_the_solver(tmp_path):
    run = tmp_path / "run"
    _run_log(run, SUBMISSION)
    out = tmp_path / "submit" / "champion_solver.py"

    proc = subprocess.run(
        [sys.executable, str(EXTRACT), "--run-dir", str(run), "--out", str(out)],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    assert out.read_text(encoding="utf-8") == SUBMISSION["solver.py"]
    # the two that used to be left behind, and the whole reason the 261 scored 0.0
    assert (out.parent / "_ext.pyx").read_text(encoding="utf-8") == SUBMISSION["_ext.pyx"]
    assert (out.parent / "setup.py").read_text(encoding="utf-8") == SUBMISSION["setup.py"]
    assert "_ext.pyx" in proc.stdout and "setup.py" in proc.stdout, proc.stdout


def test_the_operator_s_planted_files_are_not_shipped_as_the_candidate_s_work(tmp_path):
    """`reference_<task>.py` and `description.txt` are put in the workspace BY US. They are not the
    candidate's submission, and `looplab_eval.py` refuses them anyway; writing them here would put
    the grader's own source into the scored directory."""
    run = tmp_path / "run"
    _run_log(run, {**SUBMISSION,
                   "reference_t.py": "# the grader's own solver\n",
                   "description.txt": "the task\n"})
    out = tmp_path / "submit" / "champion_solver.py"

    subprocess.run([sys.executable, str(EXTRACT), "--run-dir", str(run), "--out", str(out)],
                   capture_output=True, text=True, timeout=120, check=True)

    assert (out.parent / "_ext.pyx").exists()
    assert not (out.parent / "reference_t.py").exists()
    assert not (out.parent / "description.txt").exists()


def test_a_single_file_champion_is_unchanged(tmp_path):
    """The campaign has twenty of these and none of them may grow a neighbour."""
    run = tmp_path / "run"
    _run_log(run, {"solver.py": SUBMISSION["solver.py"]})
    out = tmp_path / "submit" / "champion_solver.py"

    proc = subprocess.run(
        [sys.executable, str(EXTRACT), "--run-dir", str(run), "--out", str(out)],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert sorted(p.name for p in out.parent.iterdir()) == ["champion_solver.py"]
    assert "more:" not in proc.stdout
