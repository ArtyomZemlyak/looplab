"""Node metrics adapters (UI observability).

Read a node's logged metric SERIES from whatever the training/eval code wrote — so the UI can plot
ALL metrics online (loss, every recall@k, grad norms, lr, …), not just the run's objective. Pluggable
by design: the TensorBoard adapter is the base (PyTorch-Lightning et al. write event files); add CSV /
MLflow / JSONL adapters later behind the same `read(node_dir) -> {tag: [points]}` shape.

Best-effort everywhere: a mid-write log, a missing optional dependency, or a corrupt file yields an
empty result, never an exception — the UI must never break because a training log isn't ready yet.
"""
from __future__ import annotations

import glob
import os
from typing import Protocol


class MetricsAdapter(Protocol):
    name: str
    def read(self, node_dir: str) -> dict[str, list[dict]]: ...


class TensorBoardAdapter:
    """Read scalar series from TensorBoard event files anywhere under the node workdir (frameworks
    write them under their own logdir, e.g. `models/<name>/version_N/`). One series per scalar tag."""
    name = "tensorboard"

    def read(self, node_dir: str) -> dict[str, list[dict]]:
        try:
            from tensorboard.backend.event_processing.event_accumulator import EventAccumulator
        except Exception:  # noqa: BLE001 - tensorboard optional; no data if absent
            return {}
        out: dict[str, list[dict]] = {}
        seen: set[str] = set()
        try:
            evs = glob.glob(os.path.join(node_dir, "**", "events.out.tfevents.*"), recursive=True)
        except OSError:
            evs = []
        for ev in evs:
            d = os.path.dirname(ev)
            if d in seen:
                continue
            seen.add(d)
            try:
                ea = EventAccumulator(d, size_guidance={"scalars": 100_000})
                ea.Reload()
                for tag in ea.Tags().get("scalars", []):
                    pts = [{"step": int(s.step), "value": float(s.value), "wall_time": float(s.wall_time)}
                           for s in ea.Scalars(tag)]
                    out.setdefault(tag, []).extend(pts)
            except Exception:  # noqa: BLE001 - skip an unreadable/half-written run dir
                continue
        for tag in out:
            out[tag].sort(key=lambda p: p["step"])
        return out


_ADAPTERS: list[MetricsAdapter] = [TensorBoardAdapter()]


def read_node_metrics(node_dir: str) -> dict[str, list[dict]]:
    """Merge every adapter's scalar series for one node. Returns {tag: [{step, value, wall_time}, …]},
    each series sorted by step. Empty dict when nothing is logged yet (or on any error)."""
    merged: dict[str, list[dict]] = {}
    for a in _ADAPTERS:
        try:
            for tag, series in a.read(node_dir).items():
                merged.setdefault(tag, []).extend(series)
        except Exception:  # noqa: BLE001 - one adapter must never break the others / the request
            continue
    for tag in merged:
        merged[tag].sort(key=lambda p: p["step"])
    return merged


def metric_tags(node_dir: str) -> list[str]:
    """Just the available metric tag names for a node (cheap listing for the UI)."""
    return sorted(read_node_metrics(node_dir).keys())
