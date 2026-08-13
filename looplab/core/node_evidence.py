"""Attempt receipts for mutable per-node observability sidecars.

Events are append-only and already carry a node lifecycle generation.  Files under
``runs/<run>/nodes/node_<id>`` are different: a reset deliberately reuses that directory, so a
reader needs one small receipt before it can claim that a metric series belongs to the current
attempt.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from looplab.core.atomicio import atomic_write_text


METRICS_ATTEMPT_FILE = ".looplab-metrics-attempt.json"


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
    """
    try:
        raw = json.loads((Path(node_dir) / METRICS_ATTEMPT_FILE).read_text("utf-8"))
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
