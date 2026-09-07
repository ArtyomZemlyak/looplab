"""The arena scores a compiled candidate only if its venv has pip, and ours did not.

`AlgoTune/scripts/evaluate_results.py:266` runs `python -m pip install . --no-deps
--force-reinstall --no-cache-dir` over the candidate directory the moment a `setup.py` appears.
The arena venv is a `uv venv`, which does not install pip, so that branch answered
`Setup install failed: ... No module named pip`, the evaluator turned it into
`no_speedup{reason: compilation_failed}` and the node scored 0.0.

MEASURED 2026-08-28 over 19 runs: eight independent runs wrote `.pyx` + `setup.py`, hit this, and
DELETED their own extension 0.2-2.4 minutes later. All 35 `delete_file` calls in the whole corpus
are `.pyx` or `setup.py`. The two runs that got through scored 204-261 train and 207/259 test; the
ones that deleted scored 27.2-48.8 -- a 5-9x gap against a ~10% ruler noise. The benchmark's own
published champion for edge_expansion (Gemini 3.1) ships exactly `.pyx` + `setup.py`, so what was
broken is OUR environment, not the arena's intent.

This case is an ENVIRONMENT assertion on purpose: it fails on a fresh checkout, which is when it
needs to be seen. `benchmarks/box-jhub-l40s.sh::_algotune_ensure_pip` is the repair, and it
installs pip ALONE from the ensurepip wheel -- `python -m ensurepip` would also drop setuptools
65.5.0 over the 84.0.0 in place, a downgrade underneath live evaluations.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_ARENA = Path("/var/tmp/looplab-bench/AlgoTune")
_PY = _ARENA / ".venv" / "bin" / "python"
_SETUP = Path(__file__).resolve().parents[1] / "benchmarks" / "box-jhub-l40s.sh"

pytestmark = pytest.mark.skipif(not _PY.exists(), reason="arena venv not present off the bench box")


def test_the_arena_interpreter_has_pip():
    """Without this the whole compiled-extension branch of the scorer answers 0.0."""
    proc = subprocess.run([str(_PY), "-m", "pip", "--version"], capture_output=True, text=True)
    assert proc.returncode == 0, (
        "the arena venv has no pip, so `evaluate_results.py` will report "
        f"`Setup install failed: No module named pip` and score every compiled node 0.0: {proc.stderr}")


def test_setuptools_was_not_downgraded_by_the_repair():
    """`python -m ensurepip` bundles setuptools 65.5.0; the venv holds 84.0.0 and must keep it."""
    out = subprocess.run([str(_PY), "-c", "import setuptools;print(setuptools.__version__)"],
                         capture_output=True, text=True).stdout.strip()
    major = int(out.split(".")[0])
    assert major >= 84, f"setuptools was rolled back to {out} -- the ensurepip default did this"


_SITE = _ARENA / ".venv" / "lib" / "python3.11" / "site-packages"


def test_a_setup_py_candidate_actually_installs(tmp_path):
    """The end-to-end path, run with the scorer's own argv rather than a proxy for it.

    ISOLATED THE SAME WAY THE BRIDGE ISOLATES IT (`looplab_eval.py`, `PIP_TARGET`). The first
    version of this test ran the scorer's argv bare, so it installed `_kern` into the arena's SHARED
    site-packages -- the exact contamination channel `d439c966` closed in the bridge, left open in
    the test that verifies the bridge's premise. Caught on 2026-08-28 at 21:20, when the suite
    dropped `_kern.cpython-311-x86_64-linux-gnu.so` and `kern-0.0.0.dist-info` into the venv WHILE
    TWO PROBE EVALUATIONS WERE RUNNING against it. No probe imports the name `_kern` (checked
    against the whole corpus, not by substring -- `edge_expansion_kernel` matches `_kern` and is
    not it), so nothing was mismeasured; the write into a live ruler is the defect.
    """
    (tmp_path / "_kern.pyx").write_text("def add(int a, int b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "setup.py").write_text(
        "from setuptools import setup\nfrom Cython.Build import cythonize\n"
        "setup(name='kern', ext_modules=cythonize('_kern.pyx', language_level=3))\n", encoding="utf-8")
    target = tmp_path / "piptarget"
    target.mkdir()
    before = {p.name for p in _SITE.glob("*kern*")}
    env = dict(os.environ, PIP_TARGET=str(target),
               PYTHONPATH=os.pathsep.join([str(target), os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep))
    proc = subprocess.run(
        [str(_PY), "-m", "pip", "install", ".", "--no-deps", "--force-reinstall", "--no-cache-dir"],
        cwd=tmp_path, capture_output=True, text=True, timeout=600, env=env)
    assert proc.returncode == 0, f"the scorer's own install command failed: {proc.stderr[-600:]}"
    check = subprocess.run([str(_PY), "-c", "import _kern;print(_kern.add(1, 2))"],
                           cwd=tmp_path, capture_output=True, text=True, env=env)
    assert check.stdout.strip() == "3", check.stderr[-400:]
    # The refuter. Drop PIP_TARGET from `env` above and this is what fails.
    assert {p.name for p in _SITE.glob("*kern*")} == before, (
        "this test installed into the arena's SHARED site-packages, which every concurrent "
        "evaluation imports from -- a test must never write into the ruler it is measuring with")


def test_the_repair_is_pinned_in_the_box_script_and_installs_pip_alone():
    """A repair nobody can re-run is a repair that dies with this checkout."""
    body = _SETUP.read_text(encoding="utf-8")
    assert "_algotune_ensure_pip" in body, "the box script lost its pip repair"
    assert "--no-deps" in body and "pip-*.whl" in body, \
        "the repair must install the bundled pip wheel alone"
    assert "ensurepip\n" not in body.replace("import ensurepip", ""), \
        "calling `python -m ensurepip` would drag setuptools 65.5.0 in with it"
