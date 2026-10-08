"""Run a declared evaluation TRACK over settled nodes' preserved artifacts (doc 73 §1.4, stage 1).

WHY (2026-10-06). The operator's questions about a finished search were evaluations it never ran:
the champions at @200 where the search scored @20, the same checkpoints on drift weeks w00..w07.
They were answered by hand-run scoring jobs whose numbers then had no way back into the record, and
the run could not be ranked by any of them.

WHAT. The task declares `eval.tracks.<name>` (`adapters/repo_task.py::TrackSpec`): an argv over a
node's workdir. `looplab evaluate-track RUN TRACK --nodes 3,5 | all [--apply]` runs it for each named
node and records what it printed through the SAME row an operator import writes
(`extra_metrics_imported`, `maintenance/import_metrics.py`): beside the node's live metrics, never
over them, marked reconstructed key by key, with `source: "track <name>"`.

THE PRECONDITION, and why it is strict. A track measures the node's ARTIFACTS, so it must be sure
the workdir holds what the node's evaluation produced: the node is evaluated in its CURRENT
lifecycle, and the workdir's `.looplab-manifest` stamp equals the digest of the node's code and
lifecycle (`engine/evaluate.py::workdir_manifest_digest`) — the stamp the engine writes after the
files are on disk. A reset, a rebuild or a missing stamp is REFUSED for that node, never measured.
A dry run (the default) runs nothing; `--apply` runs the tracks while holding the run's
`engine.lock` (the backfill's `offline_run`), so no engine rebuilds a workdir under them.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.maintenance.import_metrics import MetricsInputRefusal


def read_track(run_dir: Path, track: str) -> dict:
    """The declared `eval.tracks.<track>` from the run's task snapshot. Raises MetricsInputRefusal."""
    try:
        data = json.loads((Path(run_dir) / "task.snapshot.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MetricsInputRefusal(f"cannot read task.snapshot.json: {exc}") from None
    # The snapshot's own block, under either spelling the task schema accepts (`cmd`, alias
    # `eval`) — read raw rather than through `adapters/task_schema.py`, which this package does not
    # reach (`tests/test_package_layering.py`).
    spec = (data.get("eval") or data.get("cmd")) if isinstance(data, dict) else None
    tracks = (spec or {}).get("tracks") if isinstance(spec, dict) else None
    if not isinstance(tracks, dict) or track not in tracks:
        named = ", ".join(sorted(tracks)) if isinstance(tracks, dict) and tracks else "none"
        raise MetricsInputRefusal(f"the task declares no eval.tracks.{track} (declared: {named})")
    entry = tracks[track]
    if not isinstance(entry, dict) or not isinstance(entry.get("command"), list) or not entry["command"]:
        raise MetricsInputRefusal(f"eval.tracks.{track} has no command")
    return entry


# The track's two rules are the LIVE engine's (`engine/track_lane.py`), re-exported here under the
# names this module always had: one reading of a track's output and one refusal, offline or live.
from looplab.engine.track_lane import parse_track_output, track_refusal  # noqa: E402,F401


def evaluate_track(run_dir: Path, track: str, nodes: str, *, apply: bool) -> str:
    """Plan (and with `apply`, run and record) `track` over `nodes` (`"all"` or `"3,5"`)."""
    from looplab.engine.artifact_sync import (append_tool_log, mask_tool_text, passthrough_env,
                                              render_argv, sync_cwd)
    from looplab.events.types import EV_EXTRA_METRICS_IMPORTED
    from looplab.maintenance.backfill_applied_params import offline_run
    from looplab.runtime.sandbox import run_argv
    run_dir = Path(run_dir)
    spec = read_track(run_dir, track)
    with offline_run(run_dir, hold=apply) as refusal:
        if refusal:
            return f"{run_dir}: skipped — {refusal}"
        store = EventStore(str(run_dir / "events.jsonl"))
        state = fold(store.read_all())
        if nodes.strip().lower() == "all":
            ids = sorted(n.id for n in state.evaluated_nodes())
        else:
            try:
                ids = sorted({int(x) for x in nodes.split(",") if x.strip()})
            except ValueError:
                raise MetricsInputRefusal(
                    f"--nodes must be 'all' or a comma list of ids, not {nodes!r}") from None
        lines, recorded = [], 0
        for nid in ids:
            node = state.nodes.get(nid)
            why = "no such node" if node is None else track_refusal(run_dir, node)
            if why:
                lines.append(f"  node {nid}: refused — {why}")
                continue
            workdir = run_dir / "nodes" / f"node_{nid}"
            argv = render_argv(spec["command"], {
                "workdir": str(workdir), "run_dir": str(run_dir), "run_id": run_dir.name,
                "node_id": nid, "generation": node.attempt})
            if not apply:
                lines.append(f"  node {nid}: would run {' '.join(argv)}")
                continue
            started = time.monotonic()
            try:
                # The declared credentials by NAME, and the working directory they imply, as the
                # copy-out (`engine/artifact_sync.py::sync_cwd`).
                env = passthrough_env(spec)
                rc, out, err, timed = run_argv(argv, sync_cwd(workdir, run_dir, spec),
                                               float(spec.get("timeout") or 3600.0), env=env)
                # MASKED at the write boundary (round 3): the passthrough values and every known
                # secret, never the tool's raw bytes (`artifact_sync.py::append_tool_log`).
                append_tool_log(run_dir / f"track_{track}.log", argv, out, err, env)
            except (OSError, ValueError) as exc:
                lines.append(f"  node {nid}: could not run — {type(exc).__name__}: "
                             f"{mask_tool_text(str(exc), env)}")
                continue
            seconds = round(time.monotonic() - started, 1)
            values = parse_track_output(out, keys=spec.get("keys"), prefix=str(spec.get("key_prefix") or ""))
            if rc != 0 or timed or not values:
                tail = mask_tool_text((err or "").strip(), env).splitlines()[-1:] or [""]
                lines.append(f"  node {nid}: failed (exit {rc}{', timed out' if timed else ''}, "
                             f"{len(values)} value(s), {seconds}s) {tail[0][:200]}")
                continue
            live = set(node.extra_metrics or {})
            fresh = {k: v for k, v in values.items() if k not in live}
            kept = sorted(set(values) & live)
            if fresh:
                store.append(EV_EXTRA_METRICS_IMPORTED, {
                    "node_id": nid, "generation": node.attempt, "extra_metrics": fresh,
                    "source": f"track {track}"[:400], "imported_at": round(time.time(), 3)})
                recorded += 1
            lines.append(f"  node {nid}: {', '.join(f'{k}={v:.6g}' for k, v in sorted(values.items()))}"
                         f" ({seconds}s)" + (f"; already carried, kept: {', '.join(kept)}" if kept else ""))
    head = (f"{run_dir}: track {track!r} — recorded on {recorded} node(s)" if apply
            else f"{run_dir}: track {track!r} — dry run, nothing executed (--apply runs it)")
    return "\n".join([head, *lines])


def request_live_track(run_dir: Path, track: str, nodes: str) -> str:
    """Queue `track` over `nodes` for the run's LIVE engine (`engine/track_lane.py`): one
    `track_requested` control intent, validated as the server's intake validates it (the track is
    declared, the ids exist). Holds no lock: a control intent is the one thing a CLI may append
    beside a running engine (invariant #1)."""
    from looplab.events.types import EV_TRACK_REQUESTED
    run_dir = Path(run_dir)
    read_track(run_dir, track)
    store = EventStore(str(run_dir / "events.jsonl"))
    state = fold(store.read_all())
    if nodes.strip().lower() == "all":
        node_ids = "all"
    else:
        try:
            node_ids = sorted({int(x) for x in nodes.split(",") if x.strip()})
        except ValueError:
            raise MetricsInputRefusal(
                f"--nodes must be 'all' or a comma list of ids, not {nodes!r}") from None
        missing = [n for n in node_ids if n not in state.nodes]
        if missing or not node_ids:
            raise MetricsInputRefusal(f"no node(s) {missing or nodes!r} in this run")
    store.append(EV_TRACK_REQUESTED, {"track": track, "node_ids": node_ids})
    position = len(state.track_requests) - state.tracks_done
    return (f"{run_dir}: track {track!r} queued for the live engine"
            + (f" behind {position} earlier request(s)" if position else "")
            + " — its numbers land as `extra_metrics_imported` rows (source: track "
            + f"{track}); a stopped run serves it on its next resume")

