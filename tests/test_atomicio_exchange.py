"""The two DEGRADING primitives in `core/atomicio`, and why they are not their fail-closed siblings.

`atomicio` owns three rename shapes and two fsync tiers, and the pairs deliberately disagree about
what an unavailable capability means:

  * `durable_no_replace_rename` RAISES `ENOTSUP` — for a quarantine move, losing the no-replace flag
    loses the guarantee itself, and a silent downgrade to a replacing rename destroys whatever a
    concurrent operation just created.
  * `exchange_paths_if_supported` returns FALSE — losing `RENAME_EXCHANGE` costs only atomicity, and
    the caller has a correct ordered fallback (retire -> publish -> delete) it must not be denied.
  * `strict_fsync_parent` RAISES — a paid-work claim that is not durably visible can authorize a
    duplicate paid call.
  * `best_effort_fsync_parent` returns quietly — a derived artifact has no such external side effect,
    and on an object-store FUSE mount the sync may legitimately fail or hang.

That inversion looks like an inconsistency, which is exactly why it gets tests: the plausible
"cleanup" is to make both halves of each pair behave alike, and either direction breaks a caller.
Making the exchange raise would stop `serve/uibuild.py` publishing a bundle it can publish correctly
on geesefs; making the no-replace rename degrade would let a reset silently clobber an archive.
"""
from __future__ import annotations

import ctypes
import errno
import os
import sys

import pytest

from looplab.core import atomicio
from looplab.core.atomicio import (
    best_effort_fsync_parent,
    durable_no_replace_rename,
    exchange_paths_if_supported,
)


def _bundle(root, name, marker):
    directory = root / name
    (directory / "assets").mkdir(parents=True)
    (directory / "index.html").write_text(marker, encoding="utf-8")
    return directory


@pytest.fixture
def no_kernel_primitive(monkeypatch):
    """A libc with no `renameat2`/`renamex_np` — an old kernel, or an exotic platform."""

    class _Stripped:
        def __getattr__(self, name):
            if name in {"renameat2", "renamex_np"}:
                raise AttributeError(name)
            return getattr(ctypes.CDLL(None, use_errno=True), name)

    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: _Stripped())


# ----------------------------------------------------------------- the exchange


def test_two_directories_swap_names_in_one_step(tmp_path):
    """Both names stay occupied throughout — the property `os.replace` cannot give a directory."""
    first = _bundle(tmp_path, "dist", "old")
    second = _bundle(tmp_path, "staged", "new")
    if not exchange_paths_if_supported(second, first):
        pytest.skip("this filesystem has no directory exchange")

    assert (first / "index.html").read_text(encoding="utf-8") == "new"
    assert (second / "index.html").read_text(encoding="utf-8") == "old"


def test_files_swap_too(tmp_path):
    first, second = tmp_path / "a.txt", tmp_path / "b.txt"
    first.write_text("A", encoding="utf-8")
    second.write_text("B", encoding="utf-8")
    if not exchange_paths_if_supported(first, second):
        pytest.skip("this filesystem has no exchange")

    assert first.read_text(encoding="utf-8") == "B"
    assert second.read_text(encoding="utf-8") == "A"


def test_os_replace_is_not_an_alternative_for_directories(tmp_path):
    """Why this primitive exists at all: the ordinary atomic replace refuses a non-empty directory,
    so a caller without the exchange has no single-step publish available and needs the ordered
    fallback rather than a differently-spelled swap."""
    first = _bundle(tmp_path, "dist", "old")
    second = _bundle(tmp_path, "staged", "new")
    with pytest.raises(OSError) as exc:
        os.replace(second, first)
    assert exc.value.errno in {errno.ENOTEMPTY, errno.EEXIST, errno.EACCES}


def test_a_missing_path_is_a_soft_false_not_an_error(tmp_path):
    """A name that does not exist yet is a CREATE, not a swap. The caller's first-ever publish takes
    the plain-rename path, and must not have to catch an exception to discover that."""
    existing = _bundle(tmp_path, "staged", "new")
    assert exchange_paths_if_supported(existing, tmp_path / "never-created") is False
    assert (existing / "index.html").read_text(encoding="utf-8") == "new"
    assert not (tmp_path / "never-created").exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="renameat2 is Linux-specific")
def test_a_missing_kernel_primitive_DEGRADES_where_the_no_replace_rename_REFUSES(
        tmp_path, no_kernel_primitive):
    """THE property, and the one a "consistency" cleanup would destroy.

    Same simulated kernel, same module, opposite answers — because the two callers lose different
    things. `durable_no_replace_rename` must raise: without the flag its destination could be
    clobbered, so continuing is worse than stopping. The exchange must not: without the flag its
    caller still has a recoverable ordered publish, and raising would deny it a correct operation.
    """
    first = _bundle(tmp_path, "dist", "old")
    second = _bundle(tmp_path, "staged", "new")

    assert exchange_paths_if_supported(second, first) is False
    assert (first / "index.html").read_text(encoding="utf-8") == "old", "nothing may have moved"
    assert (second / "index.html").read_text(encoding="utf-8") == "new"

    source = tmp_path / "events.jsonl"
    source.write_text("a line\n", encoding="utf-8")
    with pytest.raises(OSError) as exc:
        durable_no_replace_rename(source, tmp_path / "events.archive.jsonl", label="replay archive")
    assert exc.value.errno == errno.ENOTSUP


@pytest.mark.parametrize("failure", [
    OSError(errno.ENOENT, "no libc in this image"),   # locked-down / non-glibc container
    AttributeError("renameat2"),
    ValueError("invalid restype"),
])
def test_a_probe_that_RAISES_degrades_too_not_only_a_missing_symbol(tmp_path, monkeypatch, failure):
    """The other entrance to the degrade, and the one nothing reached.

    `no_kernel_primitive` simulates a MISSING SYMBOL, which `getattr(libc, ..., None)` turns into an
    early `return False` without ever entering the handler. So
    `except (OSError, AttributeError, ValueError): return False` can be replaced by a bare `raise`
    with every other test in this file still green — and it is not equivalent: loading libc through
    `ctypes.CDLL` genuinely raises on a locked-down or non-glibc image, and the exception then escapes
    a primitive whose entire contract is "report False, let the caller use its fallback".
    """
    def _boom(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(ctypes, "CDLL", _boom)

    first = _bundle(tmp_path, "dist", "old")
    second = _bundle(tmp_path, "staged", "new")

    assert exchange_paths_if_supported(second, first) is False
    assert (first / "index.html").read_text(encoding="utf-8") == "old", "nothing may have moved"
    assert (second / "index.html").read_text(encoding="utf-8") == "new"


def test_the_ui_publish_still_succeeds_when_the_probe_cannot_load_libc(tmp_path, monkeypatch):
    """The consumer the soft-False exists for, end to end.

    `serve/uibuild.py::_publish_staged_dist` spells the choice as
    `dist.exists() and exchange_paths_if_supported(stage, dist)` — a False takes the ordered
    retire -> publish -> delete fallback, which is correct on every filesystem. An EXCEPTION escapes
    that guard instead and turns a publish the fallback handles perfectly into a failed
    `looplab build-ui` with the old bundle still in place."""
    from looplab.serve import uibuild

    def _boom(*_args, **_kwargs):
        raise OSError(errno.ENOENT, "no libc in this image")

    monkeypatch.setattr(ctypes, "CDLL", _boom)

    dist = _bundle(tmp_path, "dist", "old")
    stage = _bundle(tmp_path, "staged", "new")
    uibuild._publish_staged_dist(stage, dist, log=lambda _message: None)

    assert (dist / "index.html").read_text(encoding="utf-8") == "new", "the new bundle never published"
    assert not stage.exists(), "the staging name still holds a bundle"


def test_the_exchange_leaves_durability_to_its_caller(tmp_path, monkeypatch):
    """Unlike `durable_no_replace_rename`, which bundles `strict_fsync_parent`.

    Deliberate: the two contracts attract callers with opposite sync policies, so bundling the strict
    one would drag the fail-closed behaviour back in through the side door — a FUSE mount that would
    not confirm a directory sync would fail a publish that had already succeeded."""
    first = _bundle(tmp_path, "dist", "old")
    second = _bundle(tmp_path, "staged", "new")
    monkeypatch.setattr(atomicio, "strict_fsync_parent",
                        lambda path: pytest.fail("the exchange must not impose a strict sync"))

    exchange_paths_if_supported(second, first)  # supported or not, it must not sync strictly


# ----------------------------------------------------------------- best_effort_fsync_parent


def test_it_syncs_the_directory_CONTAINING_the_named_path(tmp_path, monkeypatch):
    """Same convention as `strict_fsync_parent`: you pass the entry that changed, not its parent.
    A caller that got this backwards would sync the wrong directory and publish nothing."""
    target = tmp_path / "dist"
    target.mkdir()
    synced = []
    monkeypatch.setattr(atomicio, "best_effort_fsync",
                        lambda fileno: synced.append(os.fstat(fileno)))

    best_effort_fsync_parent(target)

    assert len(synced) == 1
    assert (synced[0].st_dev, synced[0].st_ino) == (os.stat(tmp_path).st_dev,
                                                    os.stat(tmp_path).st_ino)


def test_it_closes_the_directory_descriptor(tmp_path, monkeypatch):
    """It runs on every publish; a leaked fd per rename would exhaust the process over a session."""
    target = tmp_path / "dist"
    target.mkdir()
    captured = []
    monkeypatch.setattr(atomicio, "best_effort_fsync", lambda fileno: captured.append(fileno))

    best_effort_fsync_parent(target)

    with pytest.raises(OSError) as exc:
        os.fstat(captured[0])
    assert exc.value.errno == errno.EBADF


def test_an_unopenable_parent_is_silent(tmp_path):
    """The degrading half of the pair: no directory to sync is not a durability failure to report,
    because the caller's own rename would already have failed if the parent mattered."""
    best_effort_fsync_parent(tmp_path / "no-such-dir" / "child")


def test_a_failing_sync_does_not_propagate(tmp_path, monkeypatch):
    """`best_effort_fsync` already swallows OSError from the syscall; this pins that the wrapper
    does not reintroduce a raise around it — the whole point of the best-effort tier."""
    target = tmp_path / "dist"
    target.mkdir()
    monkeypatch.setattr(atomicio.os, "fsync",
                        lambda _fd: (_ for _ in ()).throw(OSError(errno.EINVAL, "no fsync here")))

    best_effort_fsync_parent(target)
