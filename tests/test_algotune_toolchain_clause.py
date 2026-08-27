"""The card banned what the arena permits, and the models believed it 1,204 times.

THE DEFECT. `rules_clause` derives the ban list from the validator's own `DISALLOWED_CALLS` —
`compile`, `eval`, `exec`, `gc.get_objects` — and then glossed it: "the arena refuses runtime code
generation ... so an idea that turns on generating and compiling specialised code is not one it can
accept, however fast the result would be."

Those four names are BUILTIN CALLS. The table says nothing about a JIT, and `numba` sits in the
same validator's `PROTECTED_MODULES` beside numpy, scipy and ortools — the list of libraries a
solver is EXPECTED to import. The gloss inverted that into a prohibition.

WHAT IT COST. Across arm B's twenty campaign runs and the probes, the models raise numba or Cython
1,204 times and talk themselves out of it every time: "Alternative: use numba? Not available
probably." / "Use numba JIT — probably not installed, and might be considered runtime codegen
(forbidden: no compile/eval/exec)." / "Cython/numba (not available, and arena restricts)." Not one
of our twenty champions compiles anything. Of the seventeen published champions for `convex_hull`,
NINE do, and they are the newest models; across all 2,595 published solvers, 472 use numba and 80
ship Cython. Ours: zero and zero.

Both halves of the models' belief were false, and the second half was our own sentence.

The card now also NAMES the toolchain, measured by importing it in the interpreter that scores.
Silence is what the guess grew in: the card was precise about n and about the reference time and
said nothing about what could be used on them.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAKE_TASK = ROOT / "benchmarks" / "algotune" / "make_task.py"
ARENA = Path("/var/tmp/looplab-bench/AlgoTune")
VENV = ARENA / ".venv" / "bin" / "python"

pytestmark = pytest.mark.skipif(
    not (ARENA / "AlgoTuneTasks" / "convex_hull").is_dir() or not VENV.exists(),
    reason="needs the AlgoTune checkout and its venv on this box")


@pytest.fixture(scope="module")
def goal(tmp_path_factory) -> str:
    out = tmp_path_factory.mktemp("card")
    proc = subprocess.run(
        [sys.executable, str(MAKE_TASK), "--algotune-root", str(ARENA), "--task", "convex_hull",
         "--out-dir", str(out), "--deliver", "--one-card", "--enforce-rules"],
        capture_output=True, text=True, timeout=900)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads((out / "algotune_convex_hull.json").read_text(
        encoding="utf-8"))["goal"]


def test_the_card_no_longer_says_compilation_is_unacceptable(goal):
    """The exact sentence that produced the self-censoring, and any paraphrase of it."""
    assert "refuses runtime code generation" not in goal
    assert "not one it can accept" not in goal
    assert "NOT a ban on compilation" in goal


def test_the_card_names_the_compilers_with_the_versions_this_box_really_has(goal):
    """Measured, not listed: every version in the sentence must match what the SCORING interpreter
    reports, or the card is advertising a package that is broken in this venv."""
    claimed = dict(re.findall(r"\b([a-z_]+)==([0-9][^,\s.]*(?:\.[^,\s]*)?)", goal))
    assert "numba" in claimed, claimed
    assert "cython" in claimed, claimed
    for name, version in claimed.items():
        got = subprocess.run(
            [str(VENV), "-c",
             f"import importlib;m=importlib.import_module({name!r});print(getattr(m,'__version__',''))"],
            capture_output=True, text=True, timeout=120)
        assert got.returncode == 0, f"{name}: card names it, venv cannot import it\n{got.stderr}"
        assert got.stdout.strip().startswith(version.split("+")[0].rstrip(".")), (
            f"{name}: card says {version}, venv says {got.stdout.strip()}")


def test_the_promise_is_true_the_arena_really_accepts_a_compiled_solver(tmp_path):
    """The claim the card now makes, checked against the validator that enforces it.

    This is the assertion that matters: a card may state a permission only if the thing it permits
    actually passes. Run the REAL `check_code_for_tampering` on three snippets — a numba solver, a
    Cython `setup.py`, and the builtins the rule is genuinely about.
    """
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import sys, json\n"
        f"sys.path.insert(0, {str(ARENA)!r})\n"
        "from AlgoTuner.security.code_validator import check_code_for_tampering\n"
        "NJIT = '''\\nimport numpy as np\\nfrom numba import njit\\n\\n"
        "@njit(cache=True, fastmath=True)\\ndef _s(a):\\n    t = 0.0\\n"
        "    for i in range(a.shape[0]):\\n        t += a[i]\\n    return t\\n\\n"
        "class Solver:\\n    def solve(self, problem, **kwargs):\\n"
        "        return _s(np.asarray(problem['x'], dtype=np.float64))\\n'''\n"
        "CY = '''\\nfrom setuptools import setup\\nfrom Cython.Build import cythonize\\n"
        "setup(ext_modules=cythonize('fast.pyx'))\\n'''\n"
        "BUILTIN = '''\\nclass Solver:\\n    def solve(self, problem, **kwargs):\\n"
        "        return eval(compile('1+1', '<s>', 'eval'))\\n'''\n"
        "def verdict(src):\n"
        "    r = check_code_for_tampering(src)\n"
        "    r = r[1] if isinstance(r, tuple) else r\n"
        "    return str(r or '')\n"
        "print(json.dumps({'njit': verdict(NJIT), 'cython': verdict(CY), 'builtin': verdict(BUILTIN)}))\n",
        encoding="utf-8")
    out = subprocess.run([str(VENV), str(probe)], capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stdout + out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])

    assert got["njit"] == "", f"the arena REFUSED an @njit solver: {got['njit']}"
    assert got["cython"] == "", f"the arena REFUSED a cythonize setup.py: {got['cython']}"
    # and the rule the ban is really about still bites, or the card would be permitting too much
    assert "compile" in got["builtin"], got["builtin"]
