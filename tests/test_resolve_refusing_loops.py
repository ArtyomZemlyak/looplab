"""`core/pathsafe.py::resolve_refusing_loops` refuses a symlink loop however it is reached.

Python 3.13's non-strict `resolve()` leaves a loop in its answer instead of raising, and the helper
asked strictly first to catch it. Review 2026-10-08: the strict pass stops at the FIRST missing
component, so a loop reached THROUGH one (`missing/../a`) raised `FileNotFoundError` there and the
non-strict answer — still naming the loop — was accepted. And on Windows a strict resolve reports a
loop as `ERROR_CANT_RESOLVE_FILENAME` (`winerror` 1921), never `ELOOP`.
"""
from __future__ import annotations

import errno
from pathlib import Path

import pytest

from looplab.core import pathsafe
from looplab.core.pathsafe import resolve_refusing_loops

_SYMLINKS = pytest.mark.posix_only("creating symlinks (os.symlink needs a privilege on Windows)")


@_SYMLINKS
@pytest.mark.parametrize("spelling", [
    ("a",),
    ("a", "x"),
    ("missing", "..", "a"),               # the review's repro: a loop behind a missing component
    ("missing", "deeper", "..", "..", "a"),
])
def test_a_loop_is_refused_however_it_is_reached(tmp_path, spelling):
    """MUTATION: return the non-strict answer after `FileNotFoundError` unchecked -> the two
    `missing/..` spellings resolve to a path inside the workdir."""
    (tmp_path / "a").symlink_to(tmp_path / "b")
    (tmp_path / "b").symlink_to(tmp_path / "a")
    with pytest.raises(RuntimeError, match="Symlink loop"):
        resolve_refusing_loops(tmp_path.joinpath(*spelling))


@_SYMLINKS
def test_what_merely_does_not_exist_yet_keeps_its_answer(tmp_path):
    """The control: a missing component, a dangling link and a link to a real directory are no
    loop, and keep the non-strict answer every caller relied on."""
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "to_real").symlink_to(real)
    (tmp_path / "dangling").symlink_to(tmp_path / "nowhere")
    base = tmp_path.resolve()
    assert resolve_refusing_loops(tmp_path / "missing" / "x") == base / "missing" / "x"
    assert resolve_refusing_loops(tmp_path / "missing" / ".." / "real") == base / "real"
    assert resolve_refusing_loops(tmp_path / "to_real" / "new.json") == base / "real" / "new.json"
    assert resolve_refusing_loops(tmp_path / "dangling") == base / "nowhere"


def test_windows_reports_a_loop_as_cant_resolve_filename(tmp_path, monkeypatch):
    """Driven through the Windows shape on any host: a strict resolve that raises an `OSError`
    carrying `winerror` 1921 is a loop. MUTATION: ask `errno == ELOOP` alone -> the non-strict
    answer is returned."""
    real_resolve = Path.resolve

    def strict_says_cant_resolve(self, strict=False):
        if strict:
            exc = OSError(errno.EINVAL, "The name of the file cannot be resolved by the system")
            exc.winerror = 1921
            raise exc
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", strict_says_cant_resolve)
    with pytest.raises(RuntimeError, match="Symlink loop"):
        resolve_refusing_loops(tmp_path / "a")
    assert pathsafe._WINERROR_CANT_RESOLVE_FILENAME == 1921
