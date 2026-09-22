"""The two Windows filesystem rules a Windows-only branch is written against, as test doubles.

The suite runs on POSIX, and a product branch written for Windows (`if os.name == "nt": ...`) is
otherwise never executed until the Windows CI leg reports it — which is how the defects of review
2026-09-22 (GitHub Actions run 35785582444) went unseen: every one was on a branch nobody could run.
These doubles reproduce exactly the rule each branch exists for, no more:

* `FakeMsvcrt` — byte-range locks held per HANDLE (`msvcrt.locking`): a byte held through one open
  refuses every other open with EACCES, even in the same process.
* `refuse_readonly_unlink` — `DeleteFileW` refuses an entry carrying FILE_ATTRIBUTE_READONLY with
  `[WinError 5] Access is denied`, where POSIX consults only the parent directory. On Windows that
  attribute IS what `os.chmod(path, 0o444)` sets, so "lacks the owner-write bit" is its double.

A test switches `os.name` to "nt" only around the call under test (pathlib picks its flavour from
it at construction time) and restores it before asserting.
"""
from __future__ import annotations

import errno
import os
import stat
import types


class FakeMsvcrt(types.ModuleType):
    LK_UNLCK, LK_LOCK, LK_NBLCK = 0, 1, 2

    def __init__(self):
        super().__init__("msvcrt")
        self.held: dict = {}

    def locking(self, fd, mode, nbytes):
        info = os.fstat(fd)
        key = (info.st_dev, info.st_ino)
        if mode == self.LK_UNLCK:
            if self.held.get(key) == fd:
                del self.held[key]
            return
        if self.held.get(key, fd) != fd:
            raise OSError(errno.EACCES, "Permission denied")
        self.held[key] = fd


def refuse_readonly_unlink(monkeypatch) -> list:
    """Make `os.unlink`/`os.remove` refuse a read-only entry the way Windows does. Returns the list
    of refused paths, so a test can prove the rule actually fired."""
    real_unlink = os.unlink
    refused: list = []

    def _unlink(path, *, dir_fd=None):
        info = os.lstat(path, dir_fd=dir_fd) if dir_fd is not None else os.lstat(path)
        if not stat.S_ISLNK(info.st_mode) and not info.st_mode & stat.S_IWUSR:
            refused.append(path)
            raise PermissionError(errno.EACCES, "Access is denied (emulated read-only)", path)
        if dir_fd is not None:
            return real_unlink(path, dir_fd=dir_fd)
        return real_unlink(path)

    monkeypatch.setattr(os, "unlink", _unlink)
    monkeypatch.setattr(os, "remove", _unlink)
    return refused
