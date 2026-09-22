"""The named `posix_only` gates — one spelling per POSIX mechanism a test's SUBJECT depends on.

`tests/conftest.py::pytest_collection_modifyitems` is what turns `@pytest.mark.posix_only(...)` into
a skip where `os.name != "posix"`, and refuses one that names no mechanism. These names exist so the
same mechanism is worded once: the Windows report then groups by what was skipped and why, and a
reviewer can check a gate against the mechanism it claims (review 2026-09-22, the first Windows CI
leg that executed tests — GitHub Actions run 35785582444).

A gate belongs on a test only when the MECHANISM is the subject. A test whose subject is product
behaviour meant to work on Windows is fixed or reported, never gated — see the conftest note on why
there is deliberately no dynamic "skip anything that launches bash" net.
"""
from __future__ import annotations

import pytest

# `bash` on a Windows runner resolves to System32\bash.exe, the WSL launcher, which prints
# "Windows Subsystem for Linux has no installed distributions" (UTF-16) and exits 1. The harness
# under test is POSIX shell all the way down (taskset, flock, /proc, setsid, `date +%s`).
BASH_HARNESS = pytest.mark.posix_only(
    "the bash bench harness (benchmarks/**/*.sh) — on Windows `bash` is the WSL launcher stub")

# The same launcher stub, for a test whose fixture is an operator's own bash script rather than the
# bench harness (`shutil.which("bash")` FINDS the stub, so a "bash is not installed" skip does not
# fire there).
BASH_SCRIPT = pytest.mark.posix_only(
    "a bash script run as `bash <file>` — on Windows `bash` is the WSL launcher stub")

# Windows has no `os.fork` and no "fork" multiprocessing context; the subject is fork semantics
# (a forked checker, a child that inherits the parent's memory, a crash between two writes).
FORK = pytest.mark.posix_only("os.fork / the 'fork' multiprocessing start method")

# Windows has no process groups in the POSIX sense: no os.killpg / os.getpgid / os.setsid, and no
# signal.SIGKILL. A test whose subject is "the whole GROUP dies" is about that mechanism.
PROCESS_GROUPS = pytest.mark.posix_only(
    "POSIX process groups and signals (os.killpg / os.getpgid / signal.SIGKILL)")

# The `fcntl` module does not exist on Windows; `interprocess_lock` uses msvcrt byte locks there,
# which are a different mechanism with different semantics (mandatory, per byte range).
FLOCK = pytest.mark.posix_only("fcntl advisory locks (flock)")

# `os.sched_getaffinity` is Linux-only; the bench lane model pins evaluations to CPU sets.
CPU_AFFINITY = pytest.mark.posix_only("Linux CPU affinity (os.sched_getaffinity / taskset lanes)")

# Windows `st_mode` is synthesized from the read-only attribute (0o666 / 0o444 for every file) and
# `os.chmod` can only toggle that attribute: "group/world-readable", "0600", "0000" do not exist.
MODE_BITS = pytest.mark.posix_only(
    "POSIX permission bits (Windows os.chmod sets only the read-only attribute)")

# `os.chown`, `os.geteuid`, the xattr family and `os.mkfifo` are absent on Windows.
POSIX_ONLY_OS_CALLS = pytest.mark.posix_only(
    "os.chown / os.geteuid / os.setxattr / os.mkfifo — absent on Windows")

# The Linux process table (`/proc/<pid>/…`, `/proc/self/fd`).
PROCFS = pytest.mark.posix_only("the Linux /proc filesystem")

# The read fence's POSIX path branch, driven through the probe seam with '/'-rooted literals: the
# sep-leading absolute test, the syscall-free relative bail, `//` and `/./` spellings, `dir_fd`
# joins through /proc/self/fd, the POSIX system layout `_too_broad` guards. On Windows `_NT` sends
# every path through `abspath` instead, so these literals are not paths there; that branch is driven
# by tests/test_read_fence_on_windows.py, which runs on every platform.
FENCE_POSIX_PATHS = pytest.mark.posix_only(
    "the read fence's POSIX path branch ('/'-rooted literals, the relative bail, dir_fd)")

# An extensionless `#!/bin/sh` file executed as a program: Windows has no shebang exec.
SHEBANG = pytest.mark.posix_only("an extensionless `#!` script executed as a program")
