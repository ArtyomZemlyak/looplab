"""On Windows `os.kill(pid, 0)` IS Ctrl+C — the owner-liveness probe must never send it.

MEASURED on the first Windows CI leg that executed tests (GitHub Actions run 35785582444,
review 2026-09-22, WIN-ABORT): three of four shards ended in a `KeyboardInterrupt` nobody pressed —
group 1 inside `tests/test_control_plane_liveness.py::_settle`, groups 2 and 3 in a
`threading.Condition.wait` of an unrelated later test — taking thousands of unexecuted tests with
them. `signal.CTRL_C_EVENT == 0`, and CPython's `os.kill` hands signal 0 to
`GenerateConsoleCtrlEvent`: for a pid that is not a console process-group leader the console
delivers Ctrl+C to every process attached to it. `serve/run_commands.py::_process_alive` used exactly
that call as its "dependency-free fallback" probe, and the pid it probes most is its OWN
(`os.getpid()` is what a command or execution claim records as its owner) — so on a box without the
optional `psutil` extra every claim check interrupted the server that made it.

The box running this suite is POSIX, so the Windows branch is DRIVEN here with the platform switched
for the duration of one call and kernel32 answered from a table: the property is "the Windows
branch decides from a process handle and never reaches `os.kill`", and the decision table is the
rest of the contract (a handle can outlive its process, so an open handle alone is not life).
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

pytest.importorskip("fastapi")

from looplab.serve import run_commands  # noqa: E402

# The Win32 ABI values, spelled here rather than imported: they are the contract the probe is held
# to, and a test that read them off the module under test would agree with any typo in it.
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_WAIT_FAILED = 0xFFFFFFFF
_SYNCHRONIZE = 0x00100000
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87


class _Kernel32:
    """The three kernel32 entry points the probe uses, answered from a table.

    Plain closures rather than bound methods: the probe assigns `argtypes`/`restype` on each entry
    point exactly as it would on a real ctypes function, and a bound method refuses attribute
    assignment.
    """

    def __init__(self, *, open_error: int = 0, wait_state: int = _WAIT_TIMEOUT):
        self.opened: list[tuple[int, bool, int]] = []
        self.waited: list[tuple[int, int]] = []
        self.closed: list[int] = []
        self.last_error = 0

        def open_process(access, inherit, pid):
            self.opened.append((access, inherit, pid))
            if open_error:
                self.last_error = open_error
                return 0
            return 0x1234

        def wait_for_single_object(handle, timeout_ms):
            self.waited.append((handle, timeout_ms))
            return wait_state

        def close_handle(handle):
            self.closed.append(handle)
            return 1

        self.OpenProcess = open_process
        self.WaitForSingleObject = wait_for_single_object
        self.CloseHandle = close_handle


def _probe_as_windows(monkeypatch, kernel32: _Kernel32, pid: int):
    """Run ONE `_process_alive(pid)` on the Windows branch; return (answer, os.kill calls).

    `os.name` is switched only around the single call: pathlib picks its flavour from it at
    construction time, so leaving it patched for the rest of the test would break pytest itself.
    `psutil` is made unimportable because the CI leg installs `.[dev,ui]`, which does not carry the
    optional `proc` extra — the fallback is the branch that actually runs there.
    """
    kills: list[tuple[int, int]] = []

    def _kill(target, sig):
        kills.append((target, sig))

    import ctypes

    with monkeypatch.context() as m:
        m.setitem(sys.modules, "psutil", None)
        m.setattr(ctypes, "WinDLL", lambda name, use_last_error=False: kernel32, raising=False)
        m.setattr(ctypes, "get_last_error", lambda: kernel32.last_error, raising=False)
        m.setattr(os, "kill", _kill)
        m.setattr(os, "name", "nt")
        answer = run_commands._process_alive(pid)
    return answer, kills


def test_a_live_process_is_answered_by_its_handle_and_never_signalled(monkeypatch):
    """The shape that took the shards down: the server probing its OWN pid."""
    kernel32 = _Kernel32(wait_state=_WAIT_TIMEOUT)
    answer, kills = _probe_as_windows(monkeypatch, kernel32, os.getpid())
    assert kills == [], (
        "os.kill reached on Windows — signal 0 there is CTRL_C_EVENT and interrupts every process "
        f"on the console: {kills}")
    assert answer is True
    assert [pid for _access, _inherit, pid in kernel32.opened] == [os.getpid()]
    access = kernel32.opened[0][0]
    assert access & _SYNCHRONIZE, "a wait on the handle needs SYNCHRONIZE access"
    assert kernel32.waited == [(0x1234, 0)], "the wait must not block: it is a probe, not a join"
    assert kernel32.closed == [0x1234], "every opened handle is closed again"


def test_an_exited_process_whose_handle_still_opens_is_dead(monkeypatch):
    """A handle keeps the kernel object alive after exit — this server holds one per child it
    spawned — so OpenProcess succeeding is not life; the signalled wait is death."""
    kernel32 = _Kernel32(wait_state=_WAIT_OBJECT_0)
    answer, kills = _probe_as_windows(monkeypatch, kernel32, 4242)
    assert (answer, kills, kernel32.closed) == (False, [], [0x1234])


def test_a_pid_with_no_process_is_dead(monkeypatch):
    kernel32 = _Kernel32(open_error=_ERROR_INVALID_PARAMETER)
    answer, kills = _probe_as_windows(monkeypatch, kernel32, 4242)
    assert (answer, kills, kernel32.waited, kernel32.closed) == (False, [], [], [])


@pytest.mark.parametrize("kernel32", [
    _Kernel32(open_error=_ERROR_ACCESS_DENIED),     # a foreign process: exists, not ours to open
    _Kernel32(wait_state=_WAIT_FAILED),             # the wait itself failed
], ids=["access-denied", "wait-failed"])
def test_every_ambiguous_answer_stays_unknown(monkeypatch, kernel32):
    """Unknown is the fail-closed answer both claim decisions are written around: the destructive
    one leaves an unknown owner's claim alone, the permissive one refuses to call it alive."""
    answer, kills = _probe_as_windows(monkeypatch, kernel32, 4242)
    assert (answer, kills) == (None, [])


def test_an_unloadable_kernel32_is_unknown_not_a_crash(monkeypatch):
    def _no_dll(name, use_last_error=False):
        raise OSError("kernel32 unavailable")

    import ctypes

    with monkeypatch.context() as m:
        m.setitem(sys.modules, "psutil", None)
        m.setattr(ctypes, "WinDLL", _no_dll, raising=False)
        m.setattr(os, "kill", lambda *_a: pytest.fail("os.kill reached on Windows"))
        m.setattr(os, "name", "nt")
        answer = run_commands._process_alive(4242)
    assert answer is None


def test_the_posix_fallback_still_probes_with_signal_zero(monkeypatch):
    """The fix is a Windows branch, not a new POSIX rule: signal 0 remains the probe here."""
    if os.name == "nt":
        pytest.skip("drives the POSIX signal-0 probe")
    monkeypatch.setitem(sys.modules, "psutil", None)
    assert run_commands._process_alive(os.getpid()) is True
    reaped = subprocess.Popen([sys.executable, "-c", "pass"])
    reaped.wait()
    assert run_commands._process_alive(reaped.pid) is False
