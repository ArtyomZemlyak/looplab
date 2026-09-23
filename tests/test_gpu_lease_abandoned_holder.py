"""An abandoned host GPU lease is recognised only on proof, and set aside by NAME.

2026-09-23: `minionerec-backbones-v3` waited 70 min on a lease whose holder was the v14 engine — a
zombie leader whose last thread was stuck in D state on a geesefs request, so its flock could never be
released. `engine/resources.py::abandoned_gpu_host_lease_holder` answers "abandoned" only when the
kernel lock table names ONE holder of THIS inode, that holder is a zombie and it holds no GPU context.
Every test takes a REAL flock and reads the REAL `/proc/locks`; only the process state and the GPU
query are seams, because a real zombie and a real GPU are not something a unit test can own.
"""
from __future__ import annotations

import fcntl
import os
import sys
from pathlib import Path

import pytest

from looplab.engine import resources

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux") or not Path("/proc/locks").exists(),
                                reason="the lock table and process states are read from Linux /proc")


@pytest.fixture
def held_lease(tmp_path):
    path = tmp_path / "looplab-gpu-pool-test.lock"
    handle = open(path, "a+b")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    yield path
    handle.close()


def _ask(path, state, gpu_pids):
    return resources.abandoned_gpu_host_lease_holder(path, state_of=lambda _pid: state,
                                                     gpu_pids=lambda: gpu_pids)


def test_a_zombie_holder_without_a_gpu_context_is_abandoned(held_lease):
    assert _ask(held_lease, "Z", set()) == os.getpid()


@pytest.mark.parametrize("state", ["R", "S", "D", None])
def test_a_holder_that_is_not_a_zombie_is_never_abandoned(held_lease, state):
    """D included on purpose: a thread in D state may yet wake — only a zombie LEADER never runs."""
    assert _ask(held_lease, state, set()) is None


def test_a_zombie_that_still_holds_a_gpu_context_is_not_abandoned(held_lease):
    assert _ask(held_lease, "Z", {os.getpid()}) is None


def test_an_unanswerable_gpu_query_answers_no(held_lease):
    """Fail closed: when the driver cannot be asked, the lease stays exactly where it is."""
    assert _ask(held_lease, "Z", None) is None


def test_an_unheld_lease_has_no_abandoned_holder(tmp_path):
    path = tmp_path / "free.lock"
    path.write_bytes(b"pid=1\n")
    assert _ask(path, "Z", set()) is None, "the stamp is not evidence; the kernel's lock table is"


def test_setting_aside_moves_the_name_and_a_fresh_lease_can_be_taken(held_lease):
    aside = resources.set_aside_abandoned_gpu_host_lease(held_lease, 4242)
    assert aside is not None and aside.exists() and not held_lease.exists()
    assert "abandoned-pid4242" in aside.name
    fresh = resources._try_acquire_gpu_host_lease(held_lease)
    assert fresh is not None, "the old inode stays locked; the NAME is free for a new one"
    fresh.close()
