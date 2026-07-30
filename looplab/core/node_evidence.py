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
    node = state.nodes.get(nid)
    if node is not None:
        attempt = getattr(node, "attempt", 0)
        return attempt if type(attempt) is int and attempt >= 0 else 0
    marker = state.buildings.get(nid)
    if marker is None and state.building and state.building.get("node_id") == nid:
        marker = state.building
    raw = marker.get("generation") if isinstance(marker, dict) else None
    return raw if type(raw) is int and raw >= 0 else (0 if marker is not None else None)
