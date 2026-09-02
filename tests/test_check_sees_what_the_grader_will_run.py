"""`check` must answer about the code the EVALUATOR will run, not about a path only it can reach.

MEASURED over the 46-probe corpus, 2026-09-02. Six runs ended on a node that scored 0. Two of them
died on a Cython `CompileError` -- and both had called `check` (ten times and six times) with
`"ok": true` on every call, because the solver's extension import is guarded:

    try:
        from edge_expansion_cy import edge_expansion_count
    except ImportError:
        ...                                    # <- the path `check` was validating

Nothing here compiled, so the fallback answered and the checker certified it; `looplab_eval.py`
then ran `setup.py build_ext --inplace`, the compile failed, and the node was graded 0. A FALSE
GREEN, and it was the last thing the model saw before spending its final draw.

The same experiment surfaced the mirror image. `AlgoTune/scripts/evaluate_results.py:396` does
`sys.path.insert(0, str(code_dir))` before importing the candidate, so a solver that imports a
sibling module or a compiled extension is graded fine. This checker did not, and answered
`ModuleNotFoundError` for every instance -- 13 of 480 `check` calls across seven probes. remEEref9's
champion is that shape and scores 218.85; its own pre-flight command called it invalid.

Both tests below redden when their fix is removed; the mutations are named in each docstring.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CHECKER = REPO / "benchmarks" / "algotune" / "looplab_check.py"
ALGOTUNE = Path("/var/tmp/looplab-bench/AlgoTune")
VENV_PY = ALGOTUNE / ".venv" / "bin" / "python"

# A reference task by DUCK TYPE -- the three methods `find_task` looks for. No AlgoTune needed.
REFERENCE = '''
class Task:
    def generate_problem(self, n, random_seed=0):
        return {"n": int(n), "seed": int(random_seed)}

    def solve(self, problem):
        return {"answer": problem["n"] * 2}

    def is_solution(self, problem, solution):
        return isinstance(solution, dict) and solution.get("answer") == problem["n"] * 2
'''


def _run(workspace: Path, python: str = sys.executable) -> dict:
    done = subprocess.run(
        [python, str(CHECKER), "--reference", "reference_t.py", "--solver", "solver.py",
         "--n", "2", "--size", "4"],
        cwd=str(workspace), capture_output=True, text=True, timeout=600)
    assert done.stdout.strip(), done.stderr[-2000:]
    return json.loads(done.stdout)


def test_a_solver_that_imports_its_own_helper_is_not_called_invalid(tmp_path):
    """MUTATION: drop `sys.path.insert(0, ...)` from `_one_instance` and every row comes back
    `ModuleNotFoundError: No module named 'kern'`, i.e. INVALID INSTANCES PRESENT about a
    submission the grader scores."""
    (tmp_path / "reference_t.py").write_text(REFERENCE, encoding="utf-8")
    (tmp_path / "kern.py").write_text("def twice(n):\n    return n * 2\n", encoding="utf-8")
    (tmp_path / "solver.py").write_text(
        "from kern import twice\n"
        "class Solver:\n"
        "    def solve(self, problem, **kw):\n"
        "        return {\"answer\": twice(problem[\"n\"])}\n", encoding="utf-8")

    out = _run(tmp_path)
    assert out["ok"] is True, out
    assert out["valid"] == 2 and out["invalid"] == 0, out


def test_a_pyx_with_no_recipe_is_reported_and_is_not_an_error(tmp_path):
    """The evaluator's own rule, imported rather than re-spelled: `build_decision` does NOT compile
    a `.pyx` that has no `setup.py`/`pyproject.toml`, and grades the pure-Python path. So this must
    SAY nothing was compiled and still validate."""
    (tmp_path / "reference_t.py").write_text(REFERENCE, encoding="utf-8")
    (tmp_path / "kern.pyx").write_text("def twice(int n):\n    return n * 2\n", encoding="utf-8")
    (tmp_path / "solver.py").write_text(
        "try:\n"
        "    from kern import twice\n"
        "except ImportError:\n"
        "    def twice(n):\n"
        "        return n * 2\n"
        "class Solver:\n"
        "    def solve(self, problem, **kw):\n"
        "        return {\"answer\": twice(problem[\"n\"])}\n", encoding="utf-8")

    out = _run(tmp_path)
    assert out["ok"] is True, out
    assert "no setup.py" in out.get("build_ext", ""), (
        "the model was not told its kernel is dead weight: " + json.dumps(out)[:400])


@pytest.mark.skipif(not VENV_PY.exists(), reason="needs the AlgoTune venv (Cython + a C compiler)")
def test_a_kernel_that_cannot_compile_is_not_reported_as_ok(tmp_path):
    """THE FALSE GREEN ITSELF, on the shape that ended two runs at zero.

    MUTATION: remove the `build_gate` call from `check()` and this returns `"ok": true` with two
    valid instances -- exactly what remEE6 and remEEref6 were told, ten and six times, before the
    evaluator graded them 0."""
    (tmp_path / "reference_t.py").write_text(REFERENCE, encoding="utf-8")
    (tmp_path / "kern.pyx").write_text(
        "from cpython/nothing cimport NoSuchThing\n"
        "def twice(int n):\n"
        "    return n * 2\n", encoding="utf-8")
    (tmp_path / "setup.py").write_text(
        "from setuptools import setup\n"
        "from Cython.Build import cythonize\n"
        "setup(ext_modules=cythonize('kern.pyx'))\n", encoding="utf-8")
    (tmp_path / "solver.py").write_text(
        "try:\n"
        "    from kern import twice\n"
        "except ImportError:\n"
        "    def twice(n):\n"
        "        return n * 2\n"
        "class Solver:\n"
        "    def solve(self, problem, **kw):\n"
        "        return {\"answer\": twice(problem[\"n\"])}\n", encoding="utf-8")

    out = _run(tmp_path, str(VENV_PY))
    assert out["ok"] is False, (
        "the fallback was certified and the compile failure never mentioned: " + json.dumps(out)[:400])
    assert "kern.pyx" in out.get("error", ""), (
        "the compiler's own line did not reach the model: " + json.dumps(out)[:400])
