"""Node metrics adapters (UI observability).

Read a node's logged metric SERIES from whatever the training/eval code wrote — so the UI can plot
ALL metrics online (loss, every recall@k, grad norms, lr, …), not just the run's objective. Pluggable
by design: the TensorBoard adapter is the base (PyTorch-Lightning et al. write event files); Hugging Face
`trainer_state.json` and `*.metrics.jsonl` adapters sit beside it behind the same
`read(node_dir) -> {tag: [points]}` shape.

Best-effort everywhere: a mid-write log, a missing optional dependency, or a corrupt file yields an
empty result, never an exception — the UI must never break because a training log isn't ready yet.
"""
from __future__ import annotations

import os
import re
import stat
from typing import Protocol

from looplab.core.atomicio import file_identity
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


# ---------------------------------------------------------------- file-based adapters (JSONL, HF)
#
# UI review 2026-10-14 §7: a Hugging Face `Trainer` writes its curves to `trainer_state.json`
# (`log_history`), and many training scripts append one JSON object per logging step to a
# `*.metrics.jsonl`. Neither reached the Metrics view, which read TensorBoard only — so a run whose
# trainer logged loss/lr/grad-norm every step showed no curve at all. Same safety rules as the
# TensorBoard walk (no link followed, bounded walk, hidden dirs skipped, regular files only), plus a
# byte cap per file and a parse cache keyed by (path, size, mtime), because this is read on a poll.

_TRAINER_STATE_NAME = "trainer_state.json"
_JSONL_SUFFIX = ".metrics.jsonl"
_TRAINER_STATE_MAX_BYTES = 16 * 1024 * 1024
_JSONL_MAX_BYTES = 32 * 1024 * 1024
_MAX_POINTS_PER_TAG = 100_000
#: Keys of a `log_history` row that are bookkeeping, not curves.
_TRAINER_SKIP_KEYS = frozenset({"step", "epoch", "total_flos"})
_PARSE_CACHE: dict[tuple, dict] = {}
_PARSE_CACHE_MAX = 64


def _named_regular_files(node_dir: str, match) -> list[tuple[str, os.stat_result]]:
    """Regular, unlinked files under `node_dir` whose NAME satisfies `match`, with their lstat.

    The `_event_files` walk rules, for adapters that read a single named file per directory."""
    try:
        if is_reparse(os.lstat(node_dir)):
            return []
        root = os.path.realpath(node_dir)
    except (OSError, ValueError):
        return []
    found: list[tuple[str, os.stat_result]] = []
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
        for name in sorted(filenames):
            if not match(name):
                continue
            path = os.path.join(dirpath, name)
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode) and not is_reparse(info):
                found.append((path, info))
    return found


def _finite(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and value == value and value not in (float("inf"), float("-inf")))


def _cached_parse(path: str, info: os.stat_result, max_bytes: int, parse) -> dict:
    if info.st_size > max_bytes:
        return {}
    # `file_identity` (dev, ino, size, mtime_ns, …): a REPLACED file with the same size and mtime is
    # still a miss, which a hand-rolled (size, mtime) pair would serve stale.
    key = (path, file_identity(info))
    hit = _PARSE_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return {}
            with os.fdopen(fd, "rb", closefd=False) as fh:
                raw = fh.read(max_bytes + 1)
        finally:
            os.close(fd)
    except OSError:
        return {}
    if len(raw) > max_bytes:
        return {}
    try:
        result = parse(raw, info.st_mtime)
    except Exception:  # noqa: BLE001 - a half-written or foreign file is "no data yet"
        result = {}
    if len(_PARSE_CACHE) >= _PARSE_CACHE_MAX:
        _PARSE_CACHE.pop(next(iter(_PARSE_CACHE)))
    _PARSE_CACHE[key] = result
    return result


def _point(series: dict, tag: str, step, value, wall_time: float) -> None:
    if not _finite(value) or not (isinstance(step, int) and not isinstance(step, bool)):
        return
    points = series.setdefault(tag, [])
    if len(points) < _MAX_POINTS_PER_TAG:
        points.append({"step": step, "value": float(value), "wall_time": float(wall_time)})


def _parse_trainer_state(raw: bytes, mtime: float) -> dict:
    import json
    doc = json.loads(raw.decode("utf-8"))
    history = doc.get("log_history") if isinstance(doc, dict) else None
    out: dict[str, list[dict]] = {}
    if not isinstance(history, list):
        return out
    for row in history:
        if not isinstance(row, dict):
            continue
        step = row.get("step")
        for key, value in row.items():
            if key not in _TRAINER_SKIP_KEYS and isinstance(key, str) and len(key) <= 120:
                # `log_history` rows carry no clock; the file's mtime is the latest moment any of
                # them can have been written, which is what the attempt fence compares against.
                _point(out, f"trainer/{key}", step, value, mtime)
    return out


def _parse_metrics_jsonl(raw: bytes, mtime: float, stem: str) -> dict:
    import json
    out: dict[str, list[dict]] = {}
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue                           # a torn last line while the trainer appends
        if not isinstance(row, dict):
            continue
        step = row.get("step", row.get("global_step"))
        clock = next((row[k] for k in ("wall_time", "time", "timestamp") if _finite(row.get(k))),
                     mtime)
        for key, value in row.items():
            if key in ("step", "global_step", "wall_time", "time", "timestamp", "epoch"):
                continue
            if isinstance(key, str) and len(key) <= 120:
                _point(out, f"{stem}/{key}", step, value, float(clock))
    return out


def _newest_trainer_state(files) -> dict:
    """`Trainer` writes one `trainer_state.json` into the output dir AND into every
    `checkpoint-N/`, each holding the WHOLE history so far — merging them would plot every point
    several times. Only the file with the largest logged step is read (newest mtime on a tie), the
    same "newest re-run wins" rule the TensorBoard adapter applies to Lightning's `version_N`."""
    best = None
    for path, info in files:
        parsed = _cached_parse(path, info, _TRAINER_STATE_MAX_BYTES, _parse_trainer_state)
        last = max((p["step"] for pts in parsed.values() for p in pts), default=-1)
        rank = (last, info.st_mtime)
        if parsed and (best is None or rank > best[0]):
            best = (rank, parsed)
    return {tag: list(points) for tag, points in (best[1] if best else {}).items()}


def _jsonl_series(files) -> dict:
    """`*.metrics.jsonl`: one JSON object per logging step, a `step` (or `global_step`) and numeric
    fields. Tags are `<file stem>/<field>` so two files never interleave."""
    out: dict[str, list[dict]] = {}
    for path, info in files:
        stem = os.path.basename(path)[:-len(_JSONL_SUFFIX)] or "metrics"
        parsed = _cached_parse(path, info, _JSONL_MAX_BYTES,
                               lambda raw, mtime, stem=stem: _parse_metrics_jsonl(raw, mtime, stem))
        for tag, points in parsed.items():
            out.setdefault(tag, []).extend(points)
    return out


class FileMetricsAdapter:
    """Hugging Face `trainer_state.json` and `*.metrics.jsonl` curves, from ONE bounded walk of
    the node dir (this runs on the Metrics poll beside the TensorBoard walk)."""
    name = "metric_files"

    def read(self, node_dir: str) -> dict[str, list[dict]]:
        files = _named_regular_files(
            node_dir, lambda n: n == _TRAINER_STATE_NAME or n.endswith(_JSONL_SUFFIX))
        out = _newest_trainer_state([f for f in files if os.path.basename(f[0]) == _TRAINER_STATE_NAME])
        for tag, points in _jsonl_series([f for f in files if f[0].endswith(_JSONL_SUFFIX)]).items():
            out.setdefault(tag, []).extend(points)
        return out


_ADAPTERS: list[MetricsAdapter] = [TensorBoardAdapter(), FileMetricsAdapter()]


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
