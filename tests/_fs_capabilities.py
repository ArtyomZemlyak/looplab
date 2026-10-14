"""Filesystem CAPABILITIES a test's subject needs, found rather than assumed (2026-10-14).

The suite is run with `--basetemp` under `/dev/shm` on the box that hosts the live runs, and tmpfs
on Linux < 6.6 refuses USER extended attributes (`OSError: [Errno 95] Operation not supported`). A
test whose subject is "which calls this interpreter audits" (`os.setxattr`/`os.removexattr` among
them) then failed on the filesystem, not on the property — while the same test passed with a
basetemp under `/tmp`. The fix is to MEASURE the capability and use a directory that has it, and to
SKIP with the exact reason only when no candidate on the box supports it. Nothing about what the test
asserts changes where the capability exists.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

import pytest

_PROBE_ATTR = "user.looplab_probe"


def supports_user_xattr(directory: Path) -> Optional[str]:
    """None when `directory`'s filesystem stores a user xattr, else the reason it does not."""
    if not hasattr(os, "setxattr"):
        return "this platform has no os.setxattr"
    probe = None
    try:
        fd, probe = tempfile.mkstemp(dir=str(directory), prefix=".xattr-probe-")
        os.close(fd)
        os.setxattr(probe, _PROBE_ATTR, b"1")
        os.removexattr(probe, _PROBE_ATTR)
        return None
    except OSError as exc:
        return f"{directory}: {exc.strerror or exc}"
    finally:
        if probe is not None:
            try:
                os.unlink(probe)
            except OSError:
                pass


@contextlib.contextmanager
def user_xattr_workdir(preferred: Path):
    """Yield `(directory, reasons)`: a directory on a filesystem that stores user xattrs —
    `preferred` when it does, else a private directory under the first of `/tmp`, `/var/tmp` or
    `tempfile.gettempdir()` that does (removed afterwards) — or `(None, reasons)` naming every
    candidate and why it could not, so the caller can skip or narrow with that exact reason."""
    reasons = []
    why = supports_user_xattr(preferred)
    if why is None:
        yield preferred, reasons
        return
    reasons.append(why)
    seen = set()
    for candidate in ("/tmp", "/var/tmp", tempfile.gettempdir()):
        real = os.path.realpath(candidate)
        if real in seen or not os.path.isdir(real):
            continue
        seen.add(real)
        why = supports_user_xattr(Path(real))
        if why is not None:
            reasons.append(why)
            continue
        made = Path(tempfile.mkdtemp(prefix="looplab-xattr-", dir=real))
        try:
            yield made, reasons
        finally:
            shutil.rmtree(made, ignore_errors=True)
        return
    yield None, reasons


@pytest.fixture
def user_xattr_dir(tmp_path):
    """`user_xattr_workdir` as a fixture that SKIPS, with every candidate's reason, when no
    filesystem on the box stores user xattrs."""
    with user_xattr_workdir(tmp_path) as (directory, reasons):
        if directory is None:
            pytest.skip("no filesystem on this box stores user xattrs: " + "; ".join(reasons))
        yield directory
