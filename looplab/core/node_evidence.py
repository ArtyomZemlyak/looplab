"""Attempt receipts for mutable per-node observability sidecars.

Events are append-only and already carry a node lifecycle generation.  Files under
``runs/<run>/nodes/node_<id>`` are different: a reset deliberately reuses that directory, so a
reader needs one small receipt before it can claim that a metric series belongs to the current
attempt.
"""
from __future__ import annotations

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
        before = os.lstat(path)
    except OSError:
        return None
    if is_reparse(before) or not stat.S_ISREG(before.st_mode):
        return None
    try:
        fd = os.open(path, _UNTRUSTED_READ_FLAGS)
    except OSError:
        return None
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or same_file_entry(opened) != same_file_entry(before):
            return None
        if tail and opened.st_size > limit:
            os.lseek(fd, opened.st_size - limit, os.SEEK_SET)
        chunks, remaining = [], limit
        while remaining > 0:
            chunk = os.read(fd, min(remaining, _UNTRUSTED_READ_CHUNK))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


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

    `serve/routers/runs.py` needs the attempt from its metadata-keyed payload cache — four fresh
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
