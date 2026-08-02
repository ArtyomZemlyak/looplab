"""One spelling of the filesystem-safety primitives the run-path validators all need.

These three rules were re-derived independently across the tree (doc 25 SC-03): `_is_reparse` in
seven modules, the Windows reserved-name set in four, and the case/Unicode identity rule in three.
Every copy carried its own version of the reasoning, and the copies had already drifted — one
`_is_reparse` omits the symlink half, so the same input answers differently depending on which
module happens to ask. A hardening fix therefore had to be found and applied N times, and a missed
copy is silent.

The rules live in `core` because they are filesystem facts, not policy: `serve` maps them to HTTP
status codes, the engine maps them to refusals, and `core` itself needs them for the reset/deletion
markers. Nothing here raises or logs — each caller keeps its own error vocabulary.
"""
from __future__ import annotations

import os
import stat
import sys
import unicodedata

# Windows exposes a reparse point (symlink, junction, mount point, and various filter drivers)
# through a file attribute rather than a mode bit, and `stat` may not define the constant on a
# non-Windows build — hence the `getattr` default rather than a bare `stat.FILE_ATTRIBUTE_*`.
REPARSE_POINT = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))

# Reserved DOS device names. Windows resolves these regardless of extension or directory, so a run
# named `CON` is a device handle, not a directory — creating, reading or deleting it does something
# other than what the caller asked. Case-insensitive: compare against `name.upper()`.
WINDOWS_RESERVED = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})


def is_reparse(info: os.stat_result) -> bool:
    """True when this entry redirects elsewhere instead of being the thing it names.

    BOTH halves are load-bearing and a copy that keeps only one is wrong on that platform: POSIX
    symlinks show up in `st_mode` and never set an attribute, while Windows junctions and mount
    points set `FILE_ATTRIBUTE_REPARSE_POINT` and are NOT `S_ISLNK`. Callers use this to refuse
    service-owned paths that could redirect a privileged write outside the run directory, so
    answering False on the platform you did not think about is the failure mode.

    Takes an `lstat`/`fstat` result, never a `stat` one — `stat` follows the link and reports the
    target, which is exactly the question this is trying not to ask.
    """
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & REPARSE_POINT)


def filesystem_identity(name: str) -> str:
    """Fold a path component the way the host filesystem would when deciding "same file".

    Two names that a case-insensitive or normalization-insensitive volume treats as ONE file must
    fold to one identity here, or two callers each take "their own" lock on what is physically the
    same run and both proceed. Over-folding is safe (a case-sensitive volume is merely
    over-serialized); under-folding is a correctness bug, so both special cases fold and every other
    platform is left exact.

    * Windows — `normcase` applies the platform's own case folding (and separator normalization).
    * macOS — default APFS/HFS+ is case-insensitive AND normalization-insensitive, so `é` typed as
      one codepoint and as `e` + combining accent are the same file. NFD + casefold covers both.
    """
    if os.name == "nt":
        return os.path.normcase(name)
    if sys.platform == "darwin":
        return unicodedata.normalize("NFD", name).casefold()
    return name


__all__ = ["REPARSE_POINT", "WINDOWS_RESERVED", "filesystem_identity", "is_reparse"]
