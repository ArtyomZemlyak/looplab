"""Attempt receipts for mutable per-node observability sidecars.

Events are append-only and already carry a node lifecycle generation.  Files under
``runs/<run>/nodes/node_<id>`` are different: a reset deliberately reuses that directory, so a
reader needs one small receipt before it can claim that a metric series belongs to the current
attempt.
"""
from __future__ import annotations

import errno
import json
import os
import stat
import time
from pathlib import Path
from typing import Optional

from looplab.core.atomicio import atomic_write_text, same_file_entry
from looplab.core.pathsafe import is_reparse


METRICS_ATTEMPT_FILE = ".looplab-metrics-attempt.json"
#: The receipt is a ~50-byte JSON object; this is its whole allowance. A larger file is not one.
METRICS_ATTEMPT_RECEIPT_MAX_BYTES = 8 * 1024

# THE FLAG SET for reading a file a candidate process can write, in ONE place (review 2026-09-22,
# SRV2-03) — the same set `core/trace_files.py::open_private_trace_file` holds for trace sidecars.
# `O_NOFOLLOW` refuses a final-component symlink (ELOOP) and `O_NONBLOCK` lets a FIFO open at once so
# the `fstat` below can refuse it; without it `open()` on a FIFO waits for a writer that never comes.
_UNTRUSTED_READ_FLAGS = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                         | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0))
_UNTRUSTED_READ_CHUNK = 1024 * 1024


def read_bounded_regular_file(path: str | os.PathLike, limit: int, *,
                              tail: bool = False) -> Optional[bytes]:
    """At most `limit` bytes of ONE regular file a candidate may have written, or ``None``.

    Review 2026-09-22, SRV2-03. The node workdir is the candidate's own cwd, and three POLLED readers
    opened names in it with blocking, link-following, unbounded opens (the node-log tails, the
    attempt receipt below, the stage manifest). `os.mkfifo('setup.log')` pinned a request thread
    forever — forty of them hang every sync route — and a symlink published whatever it pointed at,
    another run's log included. Modelled on `core/trace_files.py::open_private_trace_file`, minus
    its trace-specific refusal of hard-link aliases (a hard link reaches nothing its maker could not
    already read and copy into its own log, and a Docker-tier link cannot leave the mount):

      * `lstat` first — a link or reparse point, a directory, a FIFO or a device is refused before
        any open; this is also the whole symlink rule where `O_NOFOLLOW` does not exist (Windows);
      * the `_UNTRUSTED_READ_FLAGS` open, then `fstat`: still a regular file, and the SAME entry the
        `lstat` saw (`same_file_entry`), so a swap between the two cannot hand over another file;
      * a bounded read of `limit` bytes — from the END when `tail`, the way a log panel reads.

    ``None`` for absent, not regular, a link, swapped or unreadable: every caller's answer to each
    of those is "no evidence yet", and none of them may raise into a route that polls. What this
    does NOT cover is a DIRECTORY component swapped for a link between the caller's check and the
    open — `node_workdir` below refuses a linked node directory, and the rest needs an `openat` walk.
    """
    if type(limit) is not int or limit < 0:
        raise ValueError("limit must be a non-negative integer")
    try:
        with open_untrusted_regular(path) as fh:
            if tail:
                size = os.fstat(fh.fileno()).st_size
                if size > limit:
                    fh.seek(size - limit)
            chunks, remaining = [], limit
            while remaining > 0:
                chunk = fh.read(min(remaining, _UNTRUSTED_READ_CHUNK))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)
    except OSError:
        return None


def open_untrusted_regular(path: str | os.PathLike):
    """`open(path, "rb")` for ONE regular file a candidate may have written — the rule
    `read_bounded_regular_file` above reads by, for a reader that needs the FILE (positioned reads,
    its `fstat`) rather than a bounded byte string: `lstat` refuses a link, a directory, a FIFO or a
    device before any open; the `_UNTRUSTED_READ_FLAGS` open cannot follow a link or BLOCK (a FIFO
    answers at once); `fstat` must show the same regular entry the `lstat` saw.

    RAISES `OSError` for each refusal — absent, not regular, a link, swapped — and never blocks, so
    a caller that wrote `with open(path, "rb")` keeps its own `except OSError` answer for all of them.
    Found 2026-09-26 (critic, driven): `engine/activation.py` and `engine/eval_log_plan.py` opened
    the eval's `*.log` files and its activation manifest with blocking, link-following opens, and a
    FIFO named `x.log` stopped the event loop."""
    before = os.lstat(path)
    if is_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise OSError(errno.EINVAL, "not a regular file", str(path))
    fd = os.open(path, _UNTRUSTED_READ_FLAGS)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or same_file_entry(opened) != same_file_entry(before):
            raise OSError(errno.EINVAL, "not the regular file that was checked", str(path))
    except BaseException:
        os.close(fd)
        raise
    # OUTSIDE the `try`: once `fdopen` has the descriptor, a failure inside it closes it itself, and a
    # second `os.close` here could close a number another thread was just handed (critic 2026-09-26).
    return os.fdopen(fd, "rb")


# `_UNTRUSTED_READ_FLAGS` minus `O_NOFOLLOW`: for a reader whose caller owns containment.
_FOLLOWING_READ_FLAGS = (os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
                         | getattr(os, "O_BINARY", 0))


def read_bounded_regular_target(path: str | os.PathLike, limit: int, *,
                                refuse_larger: bool = False) -> Optional[bytes]:
    """At most `limit` bytes of the regular file `path` NAMES — a link FOLLOWED — or ``None``.

    `read_bounded_regular_file`'s rule without its refusal of a link, for a reader whose caller owns
    containment (`runtime/command_eval.py::_confined` resolves links and refuses one that leaves the
    workdir) and whose inputs may legitimately be links INSIDE it: a candidate linking its
    `submission.csv` to `outputs/submission.csv`, a repo whose `config.yaml` links a base config.
    What it keeps is the part the critic drove (2026-09-26): a FIFO or a device is refused before
    any open — the old size gate passed a FIFO as 0 bytes and `read_text` then waited for a writer
    that never came — the open never blocks, and the entry opened must be the one checked.
    `refuse_larger` answers ``None`` for a file over `limit` instead of its first `limit` bytes.
    ``None`` for absent, not regular, swapped, over-size (when asked) or unreadable."""
    if type(limit) is not int or limit < 0:
        raise ValueError("limit must be a non-negative integer")
    try:
        st = os.stat(path)
        if not stat.S_ISREG(st.st_mode) or (refuse_larger and st.st_size > limit):
            return None
        fd = os.open(path, _FOLLOWING_READ_FLAGS)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or same_file_entry(opened) != same_file_entry(st):
                raise OSError(errno.EINVAL, "not the regular file that was checked", str(path))
        except BaseException:
            os.close(fd)
            raise
        with os.fdopen(fd, "rb") as fh:
            data = fh.read(limit + 1 if refuse_larger else limit)
        return None if refuse_larger and len(data) > limit else data
    except OSError:
        return None


def normalized_newlines(text: str) -> str:
    """`text` with CRLF and lone CR read as LF — what a text-mode reader would have handed back.

    The half of `Path.read_text` that the two byte readers above do not do, for a caller that
    swapped one for the other and still reads the result as TEXT. Both of its callers made that
    swap in one commit, and the reuse closure lost the newline translation with it (critic
    2026-09-26, driven): its import scan (`engine/eval_stages.py::_stage_reachable_files`) is
    line-anchored `re.M`, which ends a line at LF only, so a module with lone-CR line ends — valid
    Python, `compile` runs it — read as ONE line and credited none of its imports, the
    missed-dependency direction. The tamper audit (`engine/audit.py::_audit_workdir_writes`)
    compares a text asset with this applied to BOTH sides. ONE spelling, so the two cannot drift."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


# THE FLAG SET for APPENDING to a log inside a directory a candidate can write — the stage logs the
# eval tee mirrors a child's output into (`runtime/sandbox.py::_tee_drain`). `O_NOFOLLOW` refuses a
# planted symlink and `O_NONBLOCK` makes a planted FIFO answer at once (ENXIO with no reader) instead
# of blocking the tee before the child's first byte; the `fstat` below refuses what still opens.
_UNTRUSTED_APPEND_FLAGS = (os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                           | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
                           | getattr(os, "O_BINARY", 0))


def open_untrusted_append(path: str | os.PathLike, *, encoding: str = "utf-8",
                          errors: str = "replace"):
    """`open(path, "a")` for a log the ENGINE appends to in a directory a candidate can write.

    Found 2026-09-26 (critic, driven): the eval tee opened `<workdir>/<stage>.log` with a blocking,
    link-following append. A stage that ran `os.mkfifo('train.log')` hung the next stage's tee before
    it started, and one that ran `ln -s ../../events.jsonl train.log` (a native child, which the read
    fence's audit hook never sees) had the next stage's stdout appended to the run's event log — whose
    every later append then failed, and the loop spun. So, the rule `open_untrusted_regular` reads by,
    for a writer:

      * `lstat` first, when the name exists: a link or reparse point, a directory, a FIFO or a device
        is refused, and so is a regular file with MORE THAN ONE LINK — the engine never hard-links a
        log, and a hard link is the one alias `O_NOFOLLOW` cannot see (appending through one writes
        into whatever it shares an inode with);
      * the `_UNTRUSTED_APPEND_FLAGS` open (created `0o666 & ~umask`, as `open(path, "a")` created it);
      * `fstat`: a regular file with one link, and the same entry the `lstat` saw when there was one.

    RAISES `OSError` for each refusal and never blocks; the tee's answer is to run without a live
    file, exactly as it does for any other unopenable log."""
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        before = None
    if before is not None and (is_reparse(before) or not stat.S_ISREG(before.st_mode)
                               or before.st_nlink != 1):
        raise OSError(errno.EINVAL, "not a regular single-link file", str(path))
    fd = os.open(path, _UNTRUSTED_APPEND_FLAGS, 0o666)
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or (before is not None and same_file_entry(opened) != same_file_entry(before))):
            raise OSError(errno.EINVAL, "not the regular single-link file that was checked", str(path))
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "a", encoding=encoding, errors=errors)


def node_workdir(run_dir: str | os.PathLike, nid: int) -> Optional[Path]:
    """`<run>/nodes/node_<nid>` when BOTH components are real directories, else ``None``.

    The reader above refuses a linked FILE; this refuses a linked DIRECTORY, which the candidate can
    reach just as well (review 2026-09-22, SRV2-03): `nodes/node_0 -> ../../other/nodes/node_0`
    turned this run's log panel into a window on another run's workdir, because the old containment
    check resolved the base before comparing against it. `run_dir` is the canonical directory
    `AppState.run_dir` returned; a missing node directory is ``None`` too — "no files yet".
    """
    base = Path(run_dir)
    node = base / "nodes" / f"node_{int(nid)}"
    for component in (base / "nodes", node):
        try:
            info = os.lstat(component)
        except OSError:
            return None
        if is_reparse(info) or not stat.S_ISDIR(info.st_mode):
            return None
    return node


def begin_metrics_attempt(node_dir: str | Path, attempt: int, *,
                          started_at: Optional[float] = None) -> None:
    """Atomically bind subsequent metric writes in ``node_dir`` to one node attempt."""
    if type(attempt) is not int or attempt < 0:
        raise ValueError("node attempt must be a non-negative integer")
    stamp = time.time() if started_at is None else float(started_at)
    atomic_write_text(
        Path(node_dir) / METRICS_ATTEMPT_FILE,
        json.dumps({"attempt": attempt, "started_at": stamp},
                   ensure_ascii=True, separators=(",", ":")) + "\n",
    )


def metrics_attempt_receipt(node_dir: str | Path) -> Optional[tuple[int, float]]:
    """Return ``(attempt, started_at)`` for a valid receipt, otherwise ``None``.

    The file is an observability accelerator, not durable run truth.  A missing/torn/hand-edited
    receipt therefore fails closed at the caller without making the run itself unavailable.

    Read through `read_bounded_regular_file` (review 2026-09-22, SRV2-03): it sits in the
    candidate-writable node workdir and is read by a 4 s metrics poll, so a FIFO planted under its
    name pinned that request thread forever. Bounded at `METRICS_ATTEMPT_RECEIPT_MAX_BYTES`; a
    larger file is not a receipt.
    """
    try:
        data = read_bounded_regular_file(Path(node_dir) / METRICS_ATTEMPT_FILE,
                                         METRICS_ATTEMPT_RECEIPT_MAX_BYTES + 1)
        if data is None or len(data) > METRICS_ATTEMPT_RECEIPT_MAX_BYTES:
            return None
        raw = json.loads(data.decode("utf-8"))
        attempt = raw.get("attempt")
        started_at = raw.get("started_at")
        if (type(attempt) is not int or attempt < 0
                or not isinstance(started_at, (int, float))
                or isinstance(started_at, bool)
                or float(started_at) < 0):
            return None
        return attempt, float(started_at)
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def node_attempt(state, nid: int) -> Optional[int]:
    """Current lifecycle generation for a folded node or its pre-create building marker.

    Lives here rather than in one router because BOTH readers of a node's metric sidecar need it to
    fence the receipt above: the owner route and the reviewer route must agree on which attempt the
    on-disk series is allowed to belong to, or a reset serves superseded evidence to whichever of
    them forgot. Duck-typed on `state` (a folded `RunState`) so `core` gains no new dependency.

    `None` means the node is neither folded nor building — there is no attempt to fence against."""
    return _node_attempt(state.nodes, state.buildings, state.building, nid,
                         read=lambda node: getattr(node, "attempt", 0))


def node_attempt_from_payload(payload_state: dict, nid: int) -> Optional[int]:
    """The SAME rule, over the SERIALIZED `/state` payload instead of a folded `RunState`.

    (Since review 2026-09-22, SRV2-05, the trace family reads `serve/appstate.py::TraceFacts`, which
    applies the typed `node_attempt` above; this spelling stays the rule for any reader holding only
    a payload, and `tests/test_trace_facts_and_sse_offload.py` holds the two to one answer.)

    `serve/routers/runs.py` needed the attempt from its metadata-keyed payload cache — four fresh
    folds every four seconds would defeat the indexed trace path — and had a second, untyped
    derivation for it: dict spelunking that re-guessed key types (`nodes.get(str(nid), nodes.get(nid))`),
    re-guessed the `buildings`/`building` marker shape, and depended on `_public_state_value` not
    scrubbing `generation`. Five routes fence a reset on this answer and there were two ways to
    compute it, so renaming a `RunState` field or changing the marker shape moved only one of them —
    and two routes would then disagree about the same reset, one 409ing while the other served the
    superseded attempt. One rule, two input shapes.
    """
    state = payload_state if isinstance(payload_state, dict) else {}
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    raw_buildings = state.get("buildings")
    if isinstance(raw_buildings, dict):
        buildings = raw_buildings
    elif isinstance(raw_buildings, list):
        buildings = {row.get("node_id"): row for row in raw_buildings if isinstance(row, dict)}
    else:
        buildings = {}
    building = state.get("building") if isinstance(state.get("building"), dict) else None
    return _node_attempt(
        _KeyEither(nodes), _KeyEither(buildings), building, nid,
        read=lambda node: node.get("attempt", 0) if isinstance(node, dict) else 0)


class _KeyEither:
    """A read-only mapping view that answers for either `nid` or `str(nid)`.

    The payload is JSON, so its integer node keys have become strings; the folded state's have not.
    Normalizing at the boundary keeps the shared rule below free of that difference instead of
    letting each caller re-guess it."""

    def __init__(self, mapping: dict):
        self._mapping = mapping or {}

    def get(self, key):
        if key in self._mapping:
            return self._mapping[key]
        return self._mapping.get(str(key))


def _node_attempt(nodes, buildings, building, nid: int, *, read) -> Optional[int]:
    """The one rule: a folded node's own attempt, else its pre-create building marker's generation."""
    node = nodes.get(nid)
    if node is not None:
        attempt = read(node)
        return attempt if type(attempt) is int and attempt >= 0 else 0
    marker = buildings.get(nid)
    if marker is None and building and building.get("node_id") == nid:
        marker = building
    raw = marker.get("generation") if isinstance(marker, dict) else None
    return raw if type(raw) is int and raw >= 0 else (0 if marker is not None else None)
