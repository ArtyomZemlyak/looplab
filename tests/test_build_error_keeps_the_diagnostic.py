"""A failed build must show the model ITS broken line, not the compiler's internals.

`looplab_eval.py` reported a failed `setup.py build_ext --inplace` as the LAST 400 characters of
stderr. Cython prints the line that names the mistake FIRST and its own Python traceback after it,
so the tail kept the useless half:

    solver_cy.pyx:18:13: cdef statement not allowed here     <- dropped
    Compiling solver_cy.pyx because it changed.
    [1/1] Cythonizing solver_cy.pyx
    Traceback (most recent call last):                        <- kept
      File ".../Cython/Build/Dependencies.py", line 1153 ...

MEASURED on dsFix3, 2026-08-28: the bridge reported `build_ext failed rc=1` 27 times in tool
output and the string `cdef statement not allowed` reached **0** prompts. The model was told its
build failed and shown Cython's internals instead of its own broken line. It abandoned the `.pyx`
and finished on numba at 27.99, against 106.90 and 136.18 from the two runs whose extensions
worked.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks" / "algotune"))
from looplab_eval import _build_error_digest  # noqa: E402

# PADDED ON PURPOSE. The real stderr from dsFix3 was long enough that a `[-400:]` tail fell past
# the first line; a short fixture is satisfied by the very truncation being repaired, and the first
# version of this file was -- it stayed GREEN under a mutation back to tail-only. The padding is
# what Cython actually prints between the diagnostic and the traceback.
_CYTHON = """solver_cy.pyx:18:13: cdef statement not allowed here
Compiling solver_cy.pyx because it changed.
[1/1] Cythonizing solver_cy.pyx
""" + "warning: unused variable 'tmp' [-Wunused-variable]\n" * 12 + """Traceback (most recent call last):
  File "/x/Cython/Build/Dependencies.py", line 1153, in cythonize
    cythonize_one(*args)
  File "/x/Cython/Build/Dependencies.py", line 1297, in cythonize_one
    raise CompileError(None, pyx_file)
Cython.Compiler.Errors.CompileError: solver_cy.pyx"""

_GCC = """kernel.c:41:9: error: 'n_rows' undeclared (first use in this function)
   41 |         n_rows = 3;
      |         ^~~~~~
error: command '/usr/bin/gcc' failed with exit code 1"""


def test_the_cython_diagnostic_survives_even_though_it_comes_first():
    out = _build_error_digest(_CYTHON)
    assert "cdef statement not allowed here" in out, out
    assert "solver_cy.pyx:18:13" in out, "the model needs the line number, not just the message"


def test_the_tail_is_kept_as_well_so_nothing_is_traded_away():
    out = _build_error_digest(_CYTHON)
    assert "Traceback" in out or "CompileError" in out, out


def test_a_gcc_diagnostic_is_recognised_too():
    """The shape is `<file>:<line>[:<col>]: <text>`, not a compiler name."""
    out = _build_error_digest(_GCC)
    assert "kernel.c:41:9" in out and "undeclared" in out, out


def test_stderr_with_no_diagnostic_keeps_head_AND_tail():
    text = "A" * 300 + "MIDDLE" + "B" * 300
    out = _build_error_digest(text)
    assert out.startswith("A") and out.endswith("B"), out
    assert "..." in out, "the elision must be visible"


def test_short_stderr_is_returned_whole():
    assert _build_error_digest("ld: cannot find -lfoo") == "ld: cannot find -lfoo"


def test_empty_stderr_says_so_rather_than_returning_nothing():
    assert _build_error_digest("") == "(no stderr)"
    assert _build_error_digest(None) == "(no stderr)"


def test_the_digest_is_bounded():
    """A megabyte of compiler noise must not become a megabyte of prompt."""
    out = _build_error_digest("x.pyx:1:1: bad\n" * 5000)
    assert len(out) < 900, len(out)
