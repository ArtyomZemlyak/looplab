"""The Windows rules a Windows-only branch (or a Windows path) is written against, as test doubles.

The suite runs on POSIX, and a product branch written for Windows (`if os.name == "nt": ...`) is
otherwise never executed until the Windows CI leg reports it — which is how the defects of review
2026-09-22 (GitHub Actions run 35785582444) went unseen: every one was on a branch nobody could run.
These doubles reproduce exactly the rule each branch exists for, no more:

* `FakeMsvcrt` — byte-range locks held per HANDLE (`msvcrt.locking`): a byte held through one open
  refuses every other open with EACCES, even in the same process.
* `refuse_readonly_unlink` — `DeleteFileW` refuses an entry carrying FILE_ATTRIBUTE_READONLY with
  `[WinError 5] Access is denied`, where POSIX consults only the parent directory. On Windows that
  attribute IS what `os.chmod(path, 0o444)` sets, so "lacks the owner-write bit" is its double.
* `windows_glob` — `glob.glob` keeps the pattern's literal prefix as written and joins every MATCHED
  component with `os.sep`, which is "\\" there: `C:\\...\\bench/model-probes\\p1\\runs\\...`. Code
  that splits such an answer on "/model-probes/" finds nothing to split.
* `non_utf8_child_env` — text I/O given no `encoding=` decodes with the ANSI code page (cp1252 on
  the runner), not UTF-8; a child Python under this environment decodes with a non-UTF-8 codec
  too, the builtin `open()` included.

A test switches `os.name` to "nt" only around the call under test (pathlib picks its flavour from
it at construction time) and restores it before asserting.
"""
from __future__ import annotations

import errno
import glob
import os
import re
import stat
import subprocess
import sys
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


_MAGIC = re.compile(r"[*?[]")


def windows_glob(monkeypatch) -> list:
    """Make `glob.glob` answer the way it does on Windows, and keep every answer OPENABLE here.

    Windows' glob hands back the directories before the pattern's first wildcard exactly as written
    and joins each MATCHED component with `os.path.join`, i.e. "\\": `R/model-probes/*/runs` answers
    `R/model-probes\\p1\\runs`. This re-spells each real match that way and sets `os.sep` to "\\";
    the re-spelled answer gets a symlink under exactly that name (a POSIX file name may contain
    "\\"), so `open`/`stat`/`getmtime` on it still work and only code that PARSES the string on "/"
    sees the difference -- the defect this double exists for. A pattern with no wildcard is handed
    back unchanged, as Windows does. Returns the answers handed out, so a test can prove it fired."""
    real_glob = glob.glob
    handed: list = []

    def _glob(pattern, *args, **kwargs):
        pattern = os.fspath(pattern)
        hits = real_glob(pattern, *args, **kwargs)
        parts = pattern.split("/")
        first = next((i for i, part in enumerate(parts) if _MAGIC.search(part)), None)
        if first is None or first == 0:
            return hits
        prefix = "/".join(parts[:first])
        out = []
        for hit in hits:
            if not hit.startswith(prefix + "/"):
                out.append(hit)
                continue
            spelled = prefix + "\\" + hit[len(prefix) + 1:].replace("/", "\\")
            if not os.path.lexists(spelled):
                os.symlink(hit, spelled)
            out.append(spelled)
        handed.extend(out)
        return out

    monkeypatch.setattr(glob, "glob", _glob)
    monkeypatch.setattr(os, "sep", "\\")
    return handed


def non_utf8_child_env() -> dict:
    """An environment in which a CHILD Python's implicit text codec is not UTF-8.

    The POSIX stand-in for a Windows runner's cp1252, for what the in-process double cannot reach
    (the builtin `open()`): the C locale with UTF-8 mode switched off leaves the child ASCII, so a
    UTF-8 byte read through the locale codec fails the way a cp1252-undefined one does there. The
    child is asked, and a box where it still answers UTF-8 SKIPS rather than passes vacuously."""
    import pytest

    env = {k: v for k, v in os.environ.items()
           if not k.startswith("LC_") and k not in {"LANG", "LANGUAGE", "PYTHONIOENCODING"}}
    env.update(LC_ALL="C", PYTHONUTF8="0")
    got = subprocess.run(
        [sys.executable, "-c", "import locale; print(locale.getpreferredencoding(False))"],
        capture_output=True, text=True, env=env, timeout=60).stdout.strip()
    if not got or got.replace("-", "").lower() in {"utf8", "cp65001"}:
        pytest.skip(f"a child under LC_ALL=C still decodes as {got or '?'}: no non-UTF-8 codec here")
    return env
