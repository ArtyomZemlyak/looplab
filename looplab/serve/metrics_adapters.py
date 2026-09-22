"""Node metrics adapters (UI observability).

Read a node's logged metric SERIES from whatever the training/eval code wrote — so the UI can plot
ALL metrics online (loss, every recall@k, grad norms, lr, …), not just the run's objective. Pluggable
by design: the TensorBoard adapter is the base (PyTorch-Lightning et al. write event files); add CSV /
MLflow / JSONL adapters later behind the same `read(node_dir) -> {tag: [points]}` shape.

Best-effort everywhere: a mid-write log, a missing optional dependency, or a corrupt file yields an
empty result, never an exception — the UI must never break because a training log isn't ready yet.
"""
from __future__ import annotations

import os
import re
import stat
from typing import Protocol

from looplab.core.pathsafe import is_reparse

# A logging framework that RE-RUNS training in the SAME node workdir (an inline-repair retrain, or any
# re-score) writes a NEW sibling run dir and leaves the old one on disk — PyTorch-Lightning names them
# `version_0`, `version_1`, … (the `v_num` in its logs). Reading EVERY version and merging their scalars
# interleaves the stale and fresh curves (both start at step 0), so after a retrain the plot never looks
# updated. Collapse `version_N` siblings to the NEWEST run (highest N, mtime tie-break) per parent. Kept
# deliberately narrow to Lightning's `version_N` — a broader `run_N` would risk collapsing the distinct
# runs of an intra-node sweep, which are NOT re-runs of one model.
_VERSION_RE = re.compile(r"^version[_-]?(\d+)$", re.IGNORECASE)

#: How many directory entries ONE metrics read may walk. The node workdir is candidate-writable and a
#: 4 s poll reads it: a framework's logdir is tens of entries, while a runaway (or hostile) tree
#: would otherwise turn every poll into a full walk of it.
_EVENT_WALK_ENTRY_CAP = 20_000
_EVENT_FILE_PREFIX = "events.out.tfevents."


def _only_regular_event_entries(directory: str) -> bool:
    """May `EventAccumulator` be handed `directory`? It opens EVERY name containing `tfevents` in a
    directory it is given (TensorBoard's own `io_wrapper.IsSummaryEventsFile`), by path and
    blocking — so one FIFO or link among them is the same hang or leak as a bad file of our own."""
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if "tfevents" not in entry.name:
                    continue
                info = entry.stat(follow_symlinks=False)
                if is_reparse(info) or not stat.S_ISREG(info.st_mode):
                    return False
    except OSError:
        return False
    return True


def _event_files(node_dir: str) -> list[str]:
    """The TensorBoard event files under `node_dir` that are safe to read, sorted.

    Review 2026-09-22, SRV2-03. This was `glob.glob(<node>/**/events.out.tfevents.*,
    recursive=True)` over a CANDIDATE-WRITABLE tree, and a recursive glob follows directory links:
    `a -> .` walked until the OS path limit on every poll, and `link -> ../other_run` served another
    run's curves as this node's — through the one-run review plane too. Now:

      * `os.walk(followlinks=False)` — a linked directory (a loop, a mounted dataset, another run) is
        never entered, and a node directory that is ITSELF a link yields nothing;
      * a realpath check on every directory walked — a subdirectory swapped for a link between the
        listing and the descent still cannot lead outside the node;
      * `_EVENT_WALK_ENTRY_CAP` entries, then stop;
      * hidden directories skipped, as `**` always did — also where a checkout's bulk lives (`.git`);
      * a directory is kept only when EVERY event-named entry in it is a regular, unlinked file.

    What remains is a race, not a plant: the accumulator re-opens by PATH, so a live process that
    swaps a checked file for a FIFO inside that window can still block one read.
    """
    try:
        if is_reparse(os.lstat(node_dir)):
            return []
        root = os.path.realpath(node_dir)
    except (OSError, ValueError):
        return []
    found: list[str] = []
    seen = 0
    for dirpath, dirnames, filenames in os.walk(node_dir, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        seen += len(dirnames) + len(filenames)
        if seen > _EVENT_WALK_ENTRY_CAP:
            break
        real = os.path.realpath(dirpath)
        if real != root and not real.startswith(root.rstrip(os.sep) + os.sep):
            dirnames[:] = []
            continue
        events = [name for name in filenames if name.startswith(_EVENT_FILE_PREFIX)]
        if events and _only_regular_event_entries(dirpath):
            found.extend(os.path.join(dirpath, name) for name in events)
    return sorted(found)


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
        # Discovery is `_event_files`: no directory link is followed, the walk is bounded, and only a
        # directory whose event entries are all regular files is ever handed to the accumulator
        # (review 2026-09-22, SRV2-03 — this was a link-following recursive glob).
        evs = _event_files(node_dir)
        # Pick which event dirs to actually read: keep every non-versioned dir (distinct purposes like
        # train/ vs val/ must all survive), but for version-style siblings under one parent keep ONLY the
        # newest run — so a repair-retrain's fresh curve replaces the stale one instead of interleaving.
        newest: dict[str, tuple] = {}   # parent-of-versions -> (rank, dir)
        keep: set[str] = set()
        for ev in evs:
            d = os.path.dirname(ev)
            mv = _VERSION_RE.match(os.path.basename(d))
            if mv:
                grp = os.path.dirname(d)
                try:
                    mt = os.lstat(ev).st_mtime          # never through a link
                except OSError:
                    mt = 0.0
                rank = (int(mv.group(1)), mt)
                if grp not in newest or rank > newest[grp][0]:
                    newest[grp] = (rank, d)
            else:
                keep.add(d)
        keep.update(d for _, d in newest.values())
        for d in sorted(keep):
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


def read_node_metrics(node_dir: str, *, since_wall_time: float | None = None) -> dict[str, list[dict]]:
    """Merge every adapter's scalar series for one node. Returns {tag: [{step, value, wall_time}, …]},
    each series sorted by step. When ``since_wall_time`` is supplied, points without a trustworthy
    current-attempt wall-time are excluded rather than letting a reset relabel old evidence. Empty
    dict when nothing is logged yet (or on any error)."""
    merged: dict[str, list[dict]] = {}
    for a in _ADAPTERS:
        try:
            for tag, series in a.read(node_dir).items():
                if since_wall_time is not None:
                    series = [
                        point for point in series
                        if isinstance(point, dict)
                        and isinstance(point.get("wall_time"), (int, float))
                        and not isinstance(point.get("wall_time"), bool)
                        and float(point["wall_time"]) >= since_wall_time
                    ]
                if series:
                    merged.setdefault(tag, []).extend(series)
        except Exception:  # noqa: BLE001 - one adapter must never break the others / the request
            continue
    for tag in merged:
        merged[tag].sort(key=lambda p: p["step"])
    return merged


def fenced_node_metrics(node_dir, current_attempt: int) -> dict[str, list[dict]]:
    """The node's metric series for THIS attempt only — the receipt fence, in one place.

    Both readers of a node's metric sidecar need the same three-way decision and must not drift,
    because a disagreement means one surface serves a reset node's superseded curves as if they
    were the current attempt's:

    * no receipt at all — legacy attempt-zero runs predate receipts and stay readable, but a LATER
      attempt without its exact marker is unknown, not old-but-fine, so it yields nothing;
    * the receipt names this attempt — read from its start wall-time, which drops reset-era points;
    * the receipt names another attempt — the on-disk series belongs to a lifecycle nobody asked
      about, so it yields nothing.

    Observability must never take down the request, so any read failure is an empty series. The
    routes keep what genuinely differs between them: the owner 409s on a concurrent reset, the
    reviewer returns an empty series because a read-only observer has no way to resolve an error.
    """
    from looplab.core.node_evidence import metrics_attempt_receipt

    receipt = metrics_attempt_receipt(node_dir)
    try:
        if receipt is None:
            return read_node_metrics(str(node_dir)) if current_attempt == 0 else {}
        if receipt[0] == current_attempt:
            return read_node_metrics(str(node_dir), since_wall_time=receipt[1])
        return {}
    except Exception:  # noqa: BLE001 - observability must never 500 / take down a review
        return {}
