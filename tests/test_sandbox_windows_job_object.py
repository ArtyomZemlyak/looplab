"""The Windows Job Object tree kill (the open item `windows-tree-kill-is-not-atomic`).

EVERY test here is skipped on every platform this project can run: the box is Linux, the CI is
Linux, and `looplab/runtime/sandbox.py`'s job path is therefore UNEXECUTED CODE. That is the honest
state of it, and a skipped test says so where a passing one built on a faked `kernel32` would not:
faking `CreateJobObjectW`/`AssignProcessToJobObject` and asserting the fake was called proves the
call is in the source, which the reader can already see, and proves nothing about the only questions
that matter — does the assignment take, does the terminate reach a grandchild the enumeration never
saw, and does the handle's lifetime kill a HEALTHY eval.

So each test below drives the real Win32 objects through real processes and observes the real
outcome. Running them needs a Windows box:

    python -m pytest tests/test_sandbox_windows_job_object.py

Until that has been done, the job path is written, not verified.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from looplab.runtime import sandbox

pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="Windows Job Objects (kernel32) — this suite runs on POSIX")


# A parent that spawns ONE detached grandchild and then sleeps. The grandchild appends a byte to
# `heartbeat` ten times a second forever, which is how a test observes "still alive" without psutil
# and without a pid that could have been reused: a dead process cannot grow a file.
_PARENT_SPAWNS_A_GRANDCHILD = (
    "import subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, '-c',\n"
    "    \"import time,sys\\n\"\n"
    "    \"f=open(sys.argv[1],'ab',buffering=0)\\n\"\n"
    "    \"\\nwhile True:\\n f.write(b'.')\\n time.sleep(0.1)\\n\", sys.argv[1]])\n"
    "time.sleep(600)\n"
)


def _stopped_growing(path, settle: float = 1.5) -> bool:
    """True when nothing has appended to `path` for `settle` seconds — i.e. the writer is gone."""
    time.sleep(0.5)
    before = os.path.getsize(path)
    time.sleep(settle)
    return os.path.getsize(path) == before


def _wait_for_heartbeat(path, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return
        time.sleep(0.05)
    raise AssertionError(f"the grandchild never wrote {path}")


def test_terminating_the_job_kills_a_grandchild_no_enumeration_would_have_found(tmp_path):
    """The property the whole item is about: membership is inherited, so the kill needs no walk."""
    heartbeat = tmp_path / "beat"
    proc = subprocess.Popen([sys.executable, "-c", _PARENT_SPAWNS_A_GRANDCHILD, str(heartbeat)],
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        sandbox._attach_windows_job(proc)
        job = getattr(proc, "_looplab_job", None)
        assert job is not None and job.attached, "the child was never assigned to a job"
        _wait_for_heartbeat(heartbeat)
        assert job.terminate()
        assert _stopped_growing(heartbeat), "the grandchild survived TerminateJobObject"
        assert proc.wait(timeout=20) is not None
    finally:
        sandbox._close_windows_job(proc)
        if proc.poll() is None:
            proc.kill()


def test_closing_the_handle_kills_the_survivors(tmp_path):
    """JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: the handle IS the tree's lifetime."""
    heartbeat = tmp_path / "beat"
    proc = subprocess.Popen([sys.executable, "-c", _PARENT_SPAWNS_A_GRANDCHILD, str(heartbeat)],
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        sandbox._attach_windows_job(proc)
        assert getattr(proc, "_looplab_job", None) is not None
        _wait_for_heartbeat(heartbeat)
        sandbox._close_windows_job(proc)
        assert _stopped_growing(heartbeat), "closing the job handle did not reap the tree"
    finally:
        if proc.poll() is None:
            proc.kill()


def test_attaching_a_job_does_not_kill_a_healthy_child(tmp_path):
    """The catastrophic failure mode, pinned: with KILL_ON_JOB_CLOSE an early handle drop is a KILL.

    `_attach_windows_job` hands the job to the Popen so its lifetime is the caller's. If anything
    ever drops it earlier — a local that goes out of scope, a stray `close()`, a finalizer running
    on a still-live object — every Windows eval dies mid-run with no diagnosis. This is the test
    that would catch it, and it is the reason the path must not be trusted until it has been run.
    """
    heartbeat = tmp_path / "beat"
    proc = subprocess.Popen([sys.executable, "-c", _PARENT_SPAWNS_A_GRANDCHILD, str(heartbeat)],
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        sandbox._attach_windows_job(proc)
        _wait_for_heartbeat(heartbeat)
        time.sleep(2.0)
        assert proc.poll() is None, "attaching the job killed the child"
        assert not _stopped_growing(heartbeat), "attaching the job killed the grandchild"
    finally:
        sandbox._close_windows_job(proc)
        if proc.poll() is None:
            proc.kill()


def test_run_argv_cancel_reaps_the_whole_tree(tmp_path):
    """End to end through the one spawn choke point: an operator abort must leave nothing running."""
    heartbeat = tmp_path / "beat"
    cancel = threading.Event()
    threading.Timer(6.0, cancel.set).start()

    rc, _out, _err, timed_out = sandbox.run_argv(
        [sys.executable, "-c", _PARENT_SPAWNS_A_GRANDCHILD, str(heartbeat)],
        str(tmp_path), timeout=120.0, cancel=cancel)

    assert rc != 0 and not timed_out
    assert heartbeat.exists(), "the grandchild never started, so the kill proved nothing"
    assert _stopped_growing(heartbeat), "run_argv returned with a descendant still running"


def test_a_child_that_cannot_be_assigned_falls_back_rather_than_failing(tmp_path):
    """Every job failure must degrade to today's taskkill/psutil path, never to a failed launch."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        del proc._handle          # the shape `_attach_windows_job` treats as "no job available"
        sandbox._attach_windows_job(proc)
        assert getattr(proc, "_looplab_job", None) is None
    finally:
        if proc.poll() is None:
            proc.kill()
