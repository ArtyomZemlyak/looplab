"""The Windows rules a Windows-only branch (or a Windows path) is written against, as test doubles.

The suite runs on POSIX, and a product branch written for Windows (`if os.name == "nt": ...`) is
otherwise never executed until the Windows CI leg reports it — which is how the defects of review
2026-09-22 (GitHub Actions run 35785582444) went unseen: every one was on a branch nobody could run.
These doubles reproduce exactly the rule each branch exists for, no more:

* `FakeMsvcrt` — byte-range locks held per HANDLE (`msvcrt.locking`): a byte held through one open
  refuses every other open with EACCES, even in the same process — and every other handle's READ
  of that byte (`refuses_read` / `mandatory_reads`).
* `refuse_deleting_an_open_file` — `DeleteFileW` refuses a file any handle holds open (WinError
  32); POSIX unlinks it and lets the descriptors keep the inode.
* `refuse_readonly_unlink` — `DeleteFileW` refuses an entry carrying FILE_ATTRIBUTE_READONLY with
  `[WinError 5] Access is denied`, where POSIX consults only the parent directory. On Windows that
  attribute IS what `os.chmod(path, 0o444)` sets, so "lacks the owner-write bit" is its double.
* `windows_glob` — `glob.glob` keeps the pattern's literal prefix as written and joins every MATCHED
  component with `os.sep`, which is "\\" there: `C:\\...\\bench/model-probes\\p1\\runs\\...`. Code
  that splits such an answer on "/model-probes/" finds nothing to split.
* `windows_text_codec` — text I/O given no `encoding=` decodes with the ANSI code page (cp1252 on
  the runner), not UTF-8: `subprocess.run(text=True)` through `locale.getencoding()`, and
  `Path.read_text()`/`write_text()` through `io.text_encoding`. `non_utf8_child_env` is its
  stand-in for a child process, where the builtin `open()` is reached too.
* `windows_path_stat_ctime` — CPython (3.12+) fills a PATH stat's `st_ctime` with the creation
  time and an `fstat`'s with FILE_BASIC_INFO.ChangeTime, so for one unchanged file the two calls
  disagree about it (review 2026-09-22 round 2, GitHub Actions run 35804658308).

A test switches `os.name` to "nt" only around the call under test (pathlib picks its flavour from
it at construction time) and restores it before asserting.
"""
from __future__ import annotations

import errno
import glob
import io
import locale
import os
import re
import stat
import subprocess
import sys
import types


class FakeMsvcrt(types.ModuleType):
    """`msvcrt.locking`: a byte RANGE, taken at the descriptor's current position, held per handle.

    Beside who holds a file (`held`), it records WHICH bytes (`regions`), because on Windows the lock
    is mandatory for every other handle's READ of those bytes too (`ReadFile` fails with
    ERROR_LOCK_VIOLATION, i.e. EACCES), in this process as well: `refuses_read` answers that rule and
    `mandatory_reads` applies it to `open(...)` reads. `on_lock(fd)` runs while a lock is held, so a
    test can read the way a concurrent reader would (review 2026-09-22 round 2, run 35804658308)."""
    LK_UNLCK, LK_LOCK, LK_NBLCK = 0, 1, 2

    def __init__(self, on_lock=None):
        super().__init__("msvcrt")
        self.held: dict = {}
        self.regions: dict = {}
        self.on_lock = on_lock

    def locking(self, fd, mode, nbytes):
        info = os.fstat(fd)
        key = (info.st_dev, info.st_ino)
        offset = os.lseek(fd, 0, os.SEEK_CUR)
        if mode == self.LK_UNLCK:
            if self.held.get(key) == fd:
                if self.regions.get(key, (fd, offset, nbytes)) != (fd, offset, nbytes):
                    # UnlockFile names a region that must EXACTLY match a locked one.
                    raise PermissionError(errno.EACCES, "unlock of a region that is not locked")
                del self.held[key]
                self.regions.pop(key, None)
            return
        if self.held.get(key, fd) != fd:
            raise OSError(errno.EACCES, "Permission denied")
        self.held[key] = fd
        self.regions[key] = (fd, offset, nbytes)
        if self.on_lock is not None:
            self.on_lock(fd)

    def refuses_read(self, fd, start: int, stop: int) -> bool:
        """Would a read of bytes [start, stop) through `fd` fail here, as it does on Windows?"""
        info = os.fstat(fd)
        held = self.regions.get((info.st_dev, info.st_ino))
        if held is None or held[0] == fd:
            return False
        _holder, offset, nbytes = held
        return start < offset + nbytes and offset < stop

    def mandatory_reads(self, monkeypatch) -> list:
        """Make `open(..., "rb")` reads obey `refuses_read`. Returns the refused paths."""
        import builtins

        real_open = builtins.open
        refused: list = []
        double = self

        class _Reader:
            def __init__(self, handle, name):
                self._handle, self._name = handle, name

            def read(self, n=-1):
                start = self._handle.tell()
                stop = (os.fstat(self._handle.fileno()).st_size
                        if n is None or n < 0 else start + n)
                if double.refuses_read(self._handle.fileno(), start, stop):
                    refused.append(self._name)
                    raise PermissionError(errno.EACCES, "Permission denied (emulated lock "
                                          "violation)", self._name)
                return self._handle.read(n)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return self._handle.__exit__(*exc)

            def __getattr__(self, name):
                return getattr(self._handle, name)

        def _open(file, mode="r", *args, **kwargs):
            handle = real_open(file, mode, *args, **kwargs)
            return _Reader(handle, file) if mode == "rb" else handle

        monkeypatch.setattr(builtins, "open", _open)
        return refused


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


def windows_text_codec(monkeypatch, codec: str = "cp1252") -> None:
    """Make text I/O given no `encoding=` decode the way a Windows runner does: with its ANSI code
    page (cp1252 there), not UTF-8.

    `subprocess` resolves `text=True` through `locale.getencoding()` and `Path.read_text()` /
    `write_text()` through `io.text_encoding`, so both seams are re-pointed. The builtin `open()`
    resolves the locale in C and is NOT covered. A process in UTF-8 mode never asks the locale, so
    the double first proves it fires -- a child's UTF-8 `é` must come back as cp1252 mojibake -- and
    SKIPS where it cannot, rather than let a test pass without the Windows codec in play."""
    import pytest

    real_text_encoding = io.text_encoding

    def _text_encoding(encoding, stacklevel=2):
        return codec if encoding is None else real_text_encoding(encoding, stacklevel + 1)

    monkeypatch.setattr(locale, "getencoding", lambda: codec)
    monkeypatch.setattr(io, "text_encoding", _text_encoding)
    probe = subprocess.run(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xc3\\xa9')"],
        capture_output=True, text=True, timeout=60)
    if probe.stdout != b"\xc3\xa9".decode(codec):
        pytest.skip(f"cannot emulate the Windows {codec} text codec in this interpreter "
                    f"(UTF-8 mode: {sys.flags.utf8_mode}); got {probe.stdout!r}")


def windows_path_rendering(monkeypatch, module) -> type:
    """Make `module.Path` RENDER the way a `WindowsPath` does, and nothing else.

    `str()` of a path uses the host separator, so on Windows a relative path a module formats into a
    message or a spec reads `sub\\x`. This subclass renders `str()` with "\\" while `__fspath__` keeps
    the POSIX form (so every stat/open still resolves here) and `as_posix()` answers "/" -- which is
    what `WindowsPath.as_posix()` does there. Code that formats `str(path)` shows the Windows
    spelling; code that asks for `as_posix()` does not. Returns the class."""
    real_path = module.Path

    class _WindowsRenderedPath(type(real_path())):
        def __str__(self):
            return super().__str__().replace("/", "\\")

        def __fspath__(self):
            return super().__str__()

        def as_posix(self):
            # What `WindowsPath.as_posix()` answers: "/" separators. On POSIX `super().__str__()`
            # already is that; on a REAL Windows host it is the native "\\" spelling, which this
            # double used to hand straight back -- so the test built on it failed there against the
            # very `as_posix()` line it pins (CI run 35804658308, review 2026-09-22 round 2).
            return super().__str__().replace(os.sep, "/")

    monkeypatch.setattr(module, "Path", _WindowsRenderedPath)
    return _WindowsRenderedPath


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


# Every named `os.stat_result` field past the ten positional ones that a platform may carry: the
# double below rebuilds a result and must hand back EXACTLY what the filesystem said in every other
# field, or a test built on it proves the wrong thing.
_STAT_EXTRA_FIELDS = (
    "st_atime", "st_mtime", "st_ctime", "st_atime_ns", "st_mtime_ns", "st_ctime_ns",
    "st_blksize", "st_blocks", "st_rdev", "st_flags", "st_gen", "st_birthtime", "st_birthtime_ns",
    "st_file_attributes", "st_reparse_tag", "st_fstype")


def windows_path_stat_ctime(monkeypatch) -> list:
    """Make a PATH stat disagree with an `fstat` about `st_ctime`, the way CPython (3.12+) does on
    Windows.

    `os.stat`/`os.lstat` go through `win32_xstat`, which copies the file's CREATION time into
    `st_ctime` ("ctime is only deprecated from 3.12, so we copy birthtime across"), while `os.fstat`
    goes through `_Py_fstat_noraise` and reports FILE_BASIC_INFO.ChangeTime. For any file written after
    it was created the two differ, so a `file_identity` taken through one never equals one taken
    through the other -- measured on the Windows CI leg (run 35804658308) as every launch through a
    `task_file` answering 422 `task_source_changed`. Every other field is filled from the same handle
    information by both calls (the leg's own logs show path and descriptor identities agreeing on
    `(st_dev, st_ino)`), so this double moves ctime alone: every PATH stat's ctime is shifted one
    second back, `os.fstat` (and `os.stat` of an int descriptor, which IS an fstat) stays real.
    Returns the paths it answered for, so a test can prove it fired."""
    real_stat, real_lstat = os.stat, os.lstat
    answered: list = []

    def _creation_stamped(info):
        extra = {name: getattr(info, name) for name in _STAT_EXTRA_FIELDS if hasattr(info, name)}
        extra["st_ctime_ns"] = info.st_ctime_ns - 1_000_000_000
        extra["st_ctime"] = extra["st_ctime_ns"] / 1e9
        head = list(tuple(info)[:10])
        head[9] = int(extra["st_ctime"])
        return os.stat_result(head, extra)

    def _stat(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        if isinstance(path, int):
            return info
        answered.append(path)
        return _creation_stamped(info)

    def _lstat(path, *args, **kwargs):
        answered.append(path)
        return _creation_stamped(real_lstat(path, *args, **kwargs))

    monkeypatch.setattr(os, "stat", _stat)
    monkeypatch.setattr(os, "lstat", _lstat)
    return answered


def refuse_deleting_an_open_file(monkeypatch) -> list:
    """Make `os.unlink`/`os.remove` refuse a file this process holds OPEN, the way Windows does.

    `DeleteFileW` fails with ERROR_SHARING_VIOLATION (WinError 32) while any handle to the file was
    opened without FILE_SHARE_DELETE -- and every `open()`/`os.open()` CPython makes on Windows is
    such a handle -- where POSIX removes the name and lets the open descriptors keep the inode. The
    open handles are read off `/proc/self/fd`, so the double sees exactly the descriptors this
    process holds (and skips where there is no such table). Returns the refused paths, so a test can
    prove it fired (review 2026-09-22 round 2, run 35804658308)."""
    import pytest

    if not os.path.isdir("/proc/self/fd"):
        pytest.skip("no /proc/self/fd to read this process's open descriptors from")
    real_unlink = os.unlink
    refused: list = []

    def _held_open(path, dir_fd) -> bool:
        try:
            info = os.lstat(path, dir_fd=dir_fd) if dir_fd is not None else os.lstat(path)
        except OSError:
            return False
        for name in os.listdir("/proc/self/fd"):
            try:
                held = os.fstat(int(name))
            except (OSError, ValueError):
                continue
            if stat.S_ISREG(held.st_mode) and (held.st_dev, held.st_ino) == (
                    info.st_dev, info.st_ino):
                return True
        return False

    def _unlink(path, *, dir_fd=None):
        if _held_open(path, dir_fd):
            refused.append(path)
            exc = PermissionError(errno.EACCES, "The process cannot access the file because it is "
                                  "being used by another process (emulated)", path)
            exc.winerror = 32
            raise exc
        if dir_fd is not None:
            return real_unlink(path, dir_fd=dir_fd)
        return real_unlink(path)

    monkeypatch.setattr(os, "unlink", _unlink)
    monkeypatch.setattr(os, "remove", _unlink)
    return refused
