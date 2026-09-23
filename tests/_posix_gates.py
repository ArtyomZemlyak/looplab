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

# A DIRECTORY fsync through a descriptor opened on the directory. Windows exposes no portable
# directory handle to sync, so there `atomicio` orders a publish with MOVEFILE_WRITE_THROUGH
# (`_windows_move_write_through`) and its parent-sync helpers are no-ops by design -- a branch the
# tests beside each gated one drive on every platform.
DIR_FSYNC = pytest.mark.posix_only(
    "a directory fsync through an fd opened on the directory (Windows: MOVEFILE_WRITE_THROUGH)")

# The Linux kernel's no-replace rename, `renameat2(..., RENAME_NOREPLACE)` through libc, driven with
# a fake libc. Windows never loads libc here: `durable_no_replace_rename` moves through
# `MoveFileExW` without MOVEFILE_REPLACE_EXISTING, which refuses an existing destination itself.
RENAMEAT2 = pytest.mark.posix_only(
    "Linux renameat2(RENAME_NOREPLACE) through libc (Windows: MoveFileExW without REPLACE_EXISTING)")

# Replacing (`os.replace`/rename(2)) a file ANOTHER handle holds open: on POSIX the rename is a
# directory operation and the open descriptor keeps the old inode, which is the race these tests
# stage. Windows refuses the replace itself (a sharing violation) while any handle without
# FILE_SHARE_DELETE is open, so the race cannot be staged there (review 2026-09-22 round 2).
REPLACE_UNDER_AN_OPEN_HANDLE = pytest.mark.posix_only(
    "replacing a file another handle holds open (Windows refuses the replace: a sharing violation)")

# rename(2)/unlink(2) consult only the DIRECTORY's permissions, so a 0444 entry is replaced or
# removed like any other. These tests pin the POSIX side of a platform branch -- that it does NOT
# juggle the attribute -- whose Windows side (DeleteFileW/MoveFileEx refuse a READONLY entry) is
# driven on every platform beside each one.
DIRECTORY_OPS_IGNORE_READONLY = pytest.mark.posix_only(
    "rename(2)/unlink(2) of a read-only entry (POSIX: a directory operation; Windows refuses it)")

# `shutdown(SHUT_RDWR)` returning EOF to a `recv()` ALREADY BLOCKED in another thread. Linux does;
# Winsock leaves the blocked receive waiting (measured on the Windows CI leg, run 35804658308: the
# drain was still blocked 10 s after the shutdown). A test whose subject is that kernel behaviour is
# gated; what the idle guard does on Windows is recorded where the guard lives.
SHUTDOWN_WAKES_A_BLOCKED_RECV = pytest.mark.posix_only(
    "shutdown(SHUT_RDWR) waking a recv() blocked in another thread (Winsock does not)")

# An open that REFUSES a final symlink (`os.O_NOFOLLOW`, ELOOP). Windows has no such flag: its open
# follows the link, and what refuses the swap there is the caller's identity check instead — which
# is driven on every platform beside each gated test by taking the flag away.
NOFOLLOW_OPEN = pytest.mark.posix_only(
    "os.O_NOFOLLOW (an open that refuses a final symlink; Windows' open follows it)")

# The dev probe's KERNEL read rung: Landlock `open(O_PATH)`s each grant inside the interpreter the
# audit hook is already live in. Windows has no kernel rung -- and its hook normalizes a directory
# grant's trailing separator away, harmlessly, since an `open` of a directory reads nothing.
KERNEL_READ_RUNG = pytest.mark.posix_only(
    "the dev probe's Landlock kernel read rung (each grant opened O_PATH under the live hook)")
