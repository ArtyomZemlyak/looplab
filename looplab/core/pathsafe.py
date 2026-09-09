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
from pathlib import Path, PurePath
from typing import NamedTuple

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


# --- the whole run-child rule, not just its micro-helpers (doc 25 SC-03) ------------------------
#
# SC-03 found canonical run-path validation implemented at least six ways. The first pass shared the
# LEAVES above; the six full validators kept their own copies of the composition, and they had
# already drifted in ways no reader could see from one site:
#
#   * `appstate.run_dir` and `reset_route.durable_reset_run` re-spelled `is_reparse` INLINE
#     (`S_ISLNK(...) or attributes & FILE_ATTRIBUTE_REPARSE_POINT`) rather than calling the shared
#     helper this module exists to be — so the one hardening the helper receives would miss them.
#   * `run_commands.run_generation_if_present` omitted the Windows JUNCTION probe its three
#     siblings make, so a junction passed there and was refused everywhere else.
#   * `reset_route` compared `normcase(abspath(requested))` against the resolved path, which is
#     `filesystem_identity` minus the macOS half — an NFC-typed name for an NFD-stored directory
#     compared unequal and 404'd a run that exists.
#
# What is NOT shared is each caller's error vocabulary, which is the reason the copies survived: the
# same physical defect is a 404 for the read path, a 400/409 pair at launch, a 410 mid-deletion and a
# `ScopeSourceCorruptError` for the report scanner. So this returns a VERDICT and phrases nothing —
# the same shape `events/trust_gate.py::apply_trust_gate` uses for the same reason. Nothing here
# raises, logs, or knows what HTTP is.

#: Name defects: the id could never be a run, whatever is on disk. Callers that answer a
#: malformed id differently from a missing one (launch: 400 vs 409) branch on membership here.
RUN_CHILD_NAME_DEFECTS = ("absent", "not_a_plain_name", "filesystem_ambiguous")
#: Every verdict a validation can carry, name defects included. `None` is the only acceptance.
RUN_CHILD_DEFECTS = (*RUN_CHILD_NAME_DEFECTS, "missing", "unreadable", "indirect",
                     "not_a_directory", "outside_root")


class RunChild(NamedTuple):
    """The verdict. ``defect is None`` is the ONLY acceptance, and ``path`` is set only then.

    A tuple rather than a raised exception because the callers disagree about what the defect
    MEANS to their protocol, and a shared exception type would have to be caught and re-mapped at
    every one of them anyway — at which point the copies come back as except-blocks.
    """

    path: "Path | None"
    defect: "str | None"


def run_child_name_defect(name: object, *, strict: bool = False) -> "str | None":
    """Why *name* cannot be a run directory's name, or ``None`` if nothing rules it out.

    Two tiers, because the six callers genuinely admit two different sets and collapsing them would
    change which runs are reachable:

    * the DEFAULT tier is the lexical rule every one of them already enforces — a single plain path
      component, never ``.``/``..``, never a separator, never a NUL. This is what the read paths
      (`appstate.run_dir`, `reset_route`) use, so a directory the CLI created out of band with an
      unusual-but-legal name stays openable.
    * ``strict`` adds the FILESYSTEM-AMBIGUITY rule that the paths which CREATE or DESTROY a run
      already spell (`launch.safe_run_dir`, `deletion_service`): length, surrounding or trailing
      whitespace, a trailing dot, a drive/stream colon, control characters, and the reserved DOS
      device names. Those are refusals about what the operator may bring into existence, and being
      stricter there than at read time is deliberate — the reverse (creating a name the reader
      cannot address) is the bug.
    """
    if not isinstance(name, str) or not name:
        return "absent"
    if (name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name
            or PurePath(name).name != name):
        return "not_a_plain_name"
    if strict and (len(name) > 255 or name != name.strip() or name.endswith((".", " "))
                   or ":" in name
                   or any(ord(ch) < 32 or ord(ch) == 127 for ch in name)
                   or name.split(".", 1)[0].upper() in WINDOWS_RESERVED):
        return "filesystem_ambiguous"
    return None


def validate_run_child(root, child, *, must_exist: bool = True,
                       strict_name: bool = False) -> RunChild:
    """Is *child* a canonical DIRECT CHILD directory of *root*? Returns a `RunChild` verdict.

    *root* must already be the canonical run root; it is resolved here anyway so a caller that
    forgets cannot make every containment comparison vacuous. *child* is either a name (validated by
    `run_child_name_defect`) or an absolute `Path` the caller already holds — the two shapes the six
    validators take, and the `Path` form skips the NAME tier because that id was checked when it was
    first turned into a path.

    ``must_exist=False`` stops after the LEXICAL and CONTAINMENT rules and inspects no directory
    entry. That is not a weaker version of the same question, it is a different one: `launch` is
    about a run that does not exist yet, and what an EXISTING entry there means (a symlink, a file,
    an occupied directory) is its own conflict policy with its own three refusals. Merging those
    into this predicate would have made one of them unreachable.

    With ``must_exist=True`` the entry itself is judged, and every clause is one a copy had lost:

    * `is_reparse` on the ``lstat`` (never a ``stat``: that follows the link and reports the target,
      which is the question this is trying not to ask) PLUS the Windows junction probe, because a
      junction is neither a symlink nor always a reparse point through Python's stat.
    * ``S_ISDIR`` — a run is a directory, and a FILE with an events.jsonl-shaped sibling is not one.
    * `filesystem_identity` between what was asked for and what it resolved to, which is how an
      indirection ANYWHERE in the component is caught on a volume that folds case or normalization.
    * the resolved parent is the root and the resolved path is not the root itself. Accepting a
      DESCENDANT is the defect this rule exists for: `run1/nodes/n3_ws` is sandbox-WRITABLE, so any
      events.jsonl a candidate wrote there would otherwise be addressable as a "run" and folded.
    """
    try:
        root = Path(root).resolve()
    except OSError:
        return RunChild(None, "unreadable")

    if isinstance(child, PurePath) and Path(child).is_absolute():
        requested = Path(child)
    else:
        defect = run_child_name_defect(child, strict=strict_name)
        if defect is not None:
            return RunChild(None, defect)
        requested = root / str(child)

    if not must_exist:
        try:
            resolved = requested.resolve()
        except OSError:
            return RunChild(None, "unreadable")
        if resolved == root or resolved.parent != root:
            return RunChild(None, "outside_root")
        return RunChild(resolved, None)

    try:
        entry = requested.lstat()
        # `is_junction` exists only on 3.12+; on older interpreters the reparse attribute above is
        # what carries this case, which is why the probe is optional rather than assumed.
        probe = getattr(requested, "is_junction", None)
        junction = bool(callable(probe) and probe())
        canonical = requested.resolve(strict=True)
    except FileNotFoundError:
        return RunChild(None, "missing")
    except OSError:
        return RunChild(None, "unreadable")

    if is_reparse(entry) or junction:
        return RunChild(None, "indirect")
    if not stat.S_ISDIR(entry.st_mode):
        return RunChild(None, "not_a_directory")
    if filesystem_identity(str(canonical)) != filesystem_identity(str(requested)):
        return RunChild(None, "indirect")
    if canonical == root or canonical.parent != root:
        return RunChild(None, "outside_root")
    return RunChild(canonical, None)


__all__ = ["REPARSE_POINT", "RUN_CHILD_DEFECTS", "RUN_CHILD_NAME_DEFECTS", "RunChild",
           "WINDOWS_RESERVED", "filesystem_identity", "is_reparse", "run_child_name_defect",
           "validate_run_child"]
