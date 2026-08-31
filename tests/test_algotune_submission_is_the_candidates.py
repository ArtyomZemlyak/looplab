"""The submission is what the CANDIDATE wrote, not what the operator left in the directory.

`submission_files` enumerated every non-directory file beside the solver. `run_probe.sh` extracts
the champion into the PROBE ROOT, where the operator's own `probe.log`, `run.log` and `final.json`
already sit, so every probe's evidence line named three files the model never wrote — and the copy
loop shipped them into the scored directory beside the solver.

MEASURED over the 69-probe corpus on 2026-08-29: the loop has ever written exactly two kinds of
file (301 `.py`, 86 `.pyx`) and never once a `.log` or a `.json`, while the probe roots held 136
`.log` and 65 `.json` files. So the filter below is not a guess about what a candidate might
submit; it is the arena's own editing surface, which `AlgoTuner/editor/editor_functions.py`
dispatches on by suffix (`.py`, `.pyx`, `.pxd`) with `setup.py`/`pyproject.toml` as the build
recipe. The line number this docstring used to carry is gone for the reason `48953f00` gave for the
one beside `SUBMITTABLE_SUFFIXES`: a line number silently re-points at whatever moves above it, and
this one points into a THIRD-PARTY checkout that no guard in this repo can re-derive at all.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks" / "algotune"))
import looplab_eval  # noqa: E402


def _names(d):
    return [p.name for p in looplab_eval.submission_files(d / "champion_solver.py")]


def test_the_operators_probe_artefacts_are_not_the_submission(tmp_path):
    (tmp_path / "champion_solver.py").write_text("class Solver: pass\n", encoding="utf-8")
    (tmp_path / "probe.log").write_text("[13:31] fence closed\n", encoding="utf-8")
    (tmp_path / "run.log").write_text("rc=0\n", encoding="utf-8")
    (tmp_path / "final.json").write_text('{"speedup": 124.631}\n', encoding="utf-8")
    assert _names(tmp_path) == []


def test_a_real_cython_submission_still_travels_whole(tmp_path):
    """The fix must not shrink the submission it exists to carry: dsIF5's five files."""
    for n in ("champion_solver.py", "_pollard_rho.pyx", "setup.py", "squfof_py.py", "helper.pxd"):
        (tmp_path / n).write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "probe.log").write_text("noise\n", encoding="utf-8")
    (tmp_path / "final.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
    assert _names(tmp_path) == ["_pollard_rho.pyx", "helper.pxd", "pyproject.toml",
                                "setup.py", "squfof_py.py"]


def test_the_build_decision_is_not_fed_the_operators_files(tmp_path):
    """A `.pyx` with no recipe must still be refused — and refused NAMING only the model's files."""
    (tmp_path / "champion_solver.py").write_text("class Solver: pass\n", encoding="utf-8")
    (tmp_path / "kernel.pyx").write_text("cdef int f(): return 1\n", encoding="utf-8")
    (tmp_path / "run.log").write_text("noise\n", encoding="utf-8")
    run_build, note = looplab_eval.build_decision(_names(tmp_path))
    assert run_build is False
    assert "kernel.pyx" in note and "run.log" not in note and "final.json" not in note


def test_the_planted_reference_is_still_kept_out(tmp_path):
    """The protect list is a `.py` filter and must survive the surface filter."""
    (tmp_path / "champion_solver.py").write_text("class Solver: pass\n", encoding="utf-8")
    (tmp_path / "reference_pde_heat1d.py").write_text("# grader\n", encoding="utf-8")
    (tmp_path / "description.txt").write_text("task\n", encoding="utf-8")
    (tmp_path / "helper.py").write_text("y = 2\n", encoding="utf-8")
    assert _names(tmp_path) == ["helper.py"]
