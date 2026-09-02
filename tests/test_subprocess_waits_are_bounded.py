"""No test in this suite may wait on a child process without a bound.

MEASURED 2026-09-01 on a full-suite run: `py-spy dump` of the live pytest process showed

    select (selectors.py:415) / _communicate (subprocess.py:2115) / communicate (subprocess.py:1209)
    / run (subprocess.py:550) / test_spans_become_real_recording_otel_spans (test_otel_bridge.py:47)

parked there for 65 minutes. `ps -eo pid,ppid` showed no child of that pid — the subprocess had
already exited — while the pytest process held BOTH ends of its own capture pipes, so the read
would never see EOF. The same file passes in isolation in seconds, so the wedge is an interaction
with something earlier in the suite (`tests/test_tracing.py` forks); the CULPRIT is not what these
tests guard. The HARNESS is: an AST sweep found 78 subprocess waits in tests/, 20 with `timeout=`
and 58 without, so the only bound on a wedged child was whatever the caller wrapped around pytest.

A test that cannot fail can only hang, and a hang reports no assertion. `conftest.py` gives every
unbounded wait a default so the wedge becomes a named failure; these tests drive that.
"""
from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

import pytest



def _installed_bound() -> float:
    """The bound the LIVE wrapper would apply.

    Read through `subprocess.run.__globals__` rather than by importing conftest: pytest loads
    conftest under its own module name, so `import tests.conftest` yields a SECOND module object
    whose globals the installed closure never reads. Patching that copy changes nothing and the
    test silently proves nothing — which is exactly what the first draft of this file did.
    """
    return subprocess.run.__globals__["_SUBPROCESS_WAIT_BOUND_S"]


def test_an_unbounded_run_is_bounded_by_the_installed_fixture(monkeypatch):
    # Drives the REAL autouse wrapper, not a re-implementation of it: the closure reads the bound
    # from the conftest module at call time, so lowering it here makes a call that passes NO
    # timeout raise in one second. Without the fixture this call runs for 30s and passes.
    monkeypatch.setitem(subprocess.run.__globals__, "_SUBPROCESS_WAIT_BOUND_S", 1.0)
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run([sys.executable, "-c", "import time; time.sleep(30)"])


def test_a_wedged_child_fails_instead_of_hanging():
    # A child that never exits must raise, not block. This is the whole point: with no bound this
    # call would run until something outside pytest killed it.
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)


def test_an_explicit_timeout_is_never_overridden():
    # `setdefault`, not assignment: a test that deliberately drives timeout behaviour keeps its own
    # (much shorter) bound, or this fixture would silently make those tests take 900s to fail.
    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        subprocess.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)
    # `run` reports the REMAINING budget, not the value passed in, so pin the magnitude: the
    # caller's half-second survived and the 900s default did not replace it.
    assert excinfo.value.timeout < 1.0
    assert excinfo.value.timeout != _installed_bound()


def test_communicate_is_bounded_too():
    # 78 waits, and not all of them go through `run` — `Popen.communicate` is the other shape, and
    # it is the exact frame the 65-minute wedge was parked in.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            proc.communicate(timeout=1)
    finally:
        proc.kill()
        proc.wait()


def test_the_bound_is_a_disaster_bound_not_a_performance_budget():
    # Long enough that an honest slow test (a real engine run over geesefs while a training run
    # holds the box) never trips it, short enough that a wedge is caught inside one suite run.
    assert 300.0 <= _installed_bound() <= 1800.0


def test_the_bound_is_installed_by_an_autouse_fixture():
    # A fixture nobody applies is the defect one level up — the same shape as a health snapshot
    # nobody reads. AST, not a substring: a decorator mentioning autouse in a comment must not pass.
    tree = ast.parse(pathlib.Path(__file__).resolve().parent.joinpath("conftest.py").read_text())
    fixture = next((n for n in tree.body
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "_bound_every_subprocess_wait"), None)
    assert fixture is not None, "conftest no longer bounds subprocess waits"
    autouse = [
        kw for dec in fixture.decorator_list if isinstance(dec, ast.Call)
        for kw in dec.keywords
        if kw.arg == "autouse" and isinstance(kw.value, ast.Constant) and kw.value.value is True
    ]
    assert autouse, "_bound_every_subprocess_wait is not autouse — it would bound nothing"

    # And it must patch BOTH shapes; patching only `run` leaves the frame the wedge was in.
    patched = {ast.unparse(a.args[1]) for a in ast.walk(fixture)
               if isinstance(a, ast.Call) and isinstance(a.func, ast.Attribute)
               and a.func.attr == "setattr" and len(a.args) >= 2}
    assert {"'run'", "'communicate'"} <= patched, patched
