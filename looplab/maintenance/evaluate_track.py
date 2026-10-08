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
files are on disk. A reset, a rebuild, a linked workdir or a missing stamp is REFUSED for that node,
never measured. A dry run (the default) runs nothing; `--apply` runs the tracks while holding the
run's `engine.lock` (the backfill's `offline_run`), so no engine rebuilds a workdir under them.

ONE STEP, TWO LANES (review 2026-10-08). The per-node step — the argv over the ABSOLUTE run
directory, the run, the log written apart from it, the refusal re-asked after the command, the
reading of its output — is the live lane's (`engine/track_lane.py::run_track_on_node`), and so are
the declaration's validation (`track_spec`) and the refusal (`track_refusal`); this module keeps
only what is offline's: the snapshot read, the lock, and the report.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from looplab.events.eventstore import EventStore
from looplab.events.replay import fold, plan_extra_metrics_import
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
    entry = track_spec(spec, track)          # the live lane's reading of the same declaration
    if entry is None:
        raise MetricsInputRefusal(f"eval.tracks.{track} has no command")
    return entry


# The track's rules are the LIVE engine's (`engine/track_lane.py`), re-exported here under the names
# this module always had: one reading of a track's output and one refusal, offline or live.
from looplab.engine.track_lane import (MAX_TRACK_NODE_IDS, parse_track_output,  # noqa: E402,F401
                                       run_track_on_node, track_argv, track_refusal, track_spec)


def parse_nodes(nodes: str):
    """`--nodes`: `"all"`, or the sorted distinct ids of a comma list. Raises MetricsInputRefusal."""
    if nodes.strip().lower() == "all":
        return "all"
    try:
        ids = sorted({int(x) for x in nodes.split(",") if x.strip()})
    except ValueError:
        raise MetricsInputRefusal(
            f"--nodes must be 'all' or a comma list of ids, not {nodes!r}") from None
    if not ids or any(i < 0 for i in ids):
        raise MetricsInputRefusal(f"--nodes must name at least one node id >= 0, not {nodes!r}")
    return ids


def evaluate_track(run_dir: Path, track: str, nodes: str, *, apply: bool) -> str:
    """Plan (and with `apply`, run and record) `track` over `nodes` (`"all"` or `"3,5"`)."""
    from looplab.engine.artifact_sync import passthrough_env
    from looplab.events.types import EV_EXTRA_METRICS_IMPORTED
    from looplab.maintenance.backfill_applied_params import offline_run
    # ABSOLUTE (review 2026-10-08): `{workdir}` rendered relative to a command run FROM that workdir
    # resolved twice. The report still names the directory as the operator typed it.
    named, run_dir = Path(run_dir), Path(run_dir).absolute()
    spec = read_track(run_dir, track)
    wanted = parse_nodes(nodes)
    with offline_run(run_dir, hold=apply) as refusal:
        if refusal:
            return f"{named}: skipped — {refusal}"
        store = EventStore(str(run_dir / "events.jsonl"))
        state = fold(store.read_all())
        ids = sorted(n.id for n in state.evaluated_nodes()) if wanted == "all" else wanted
        # The declared credentials by NAME (`artifact_sync.py::passthrough_env`).
        env = passthrough_env(spec)
        lines, recorded = [], 0
        for nid in ids:
            node = state.nodes.get(nid)
            why = track_refusal(run_dir, node)
            if why:
                lines.append(f"  node {nid}: refused — {why}")
                continue
            if not apply:
                lines.append(f"  node {nid}: would run "
                             f"{' '.join(track_argv(spec, run_dir, nid, node.attempt))}")
                continue
            run = run_track_on_node(spec, track, run_dir, nid, node.attempt, env=env,
                                    declared=env, check=lambda: track_refusal(run_dir, node))
            if run.outcome == "refused":
                lines.append(f"  node {nid}: refused — {run.detail}")
                continue
            if run.outcome != "ok":
                lines.append(f"  node {nid}: failed ({run.detail}) [{run.seconds}s]")
                continue
            # THE FOLD'S OWN RULE (`core/models.py::plan_extra_metrics_import`): only the keys it
            # will keep are written, and the report says which it did not.
            fresh, kept, dropped = plan_extra_metrics_import(node.extra_metrics, run.values)
            if fresh:
                store.append(EV_EXTRA_METRICS_IMPORTED, {
                    "node_id": nid, "generation": node.attempt, "extra_metrics": fresh,
                    "source": f"track {track}"[:400], "imported_at": round(time.time(), 3)})
                recorded += 1
            lines.append(
                f"  node {nid}: {', '.join(f'{k}={v:.6g}' for k, v in sorted(run.values.items()))}"
                f" ({run.seconds}s)" + (f"; already carried, kept: {', '.join(kept)}" if kept else "")
                + (f"; past the 256-key bound, not recorded: {', '.join(dropped[:8])}"
                   if dropped else ""))
    head = (f"{named}: track {track!r} — recorded on {recorded} node(s)" if apply
            else f"{named}: track {track!r} — dry run, nothing executed (--apply runs it)")
    return "\n".join([head, *lines])


def request_live_track(run_dir: Path, track: str, nodes: str) -> str:
    """Queue `track` over `nodes` for the run's LIVE engine (`engine/track_lane.py`): one
    `track_requested` control intent, validated as the server's intake validates it (the track is
    declared, the ids exist, at most `MAX_TRACK_NODE_IDS` of them). Holds no lock: a control intent
    is the one thing a CLI may append beside a running engine (invariant #1)."""
    from looplab.events.types import EV_TRACK_REQUESTED
    run_dir = Path(run_dir)
    read_track(run_dir, track)
    node_ids = parse_nodes(nodes)
    store = EventStore(str(run_dir / "events.jsonl"))
    state = fold(store.read_all())
    if node_ids != "all":
        if len(node_ids) > MAX_TRACK_NODE_IDS:
            raise MetricsInputRefusal(f"--nodes names {len(node_ids)} ids; one request may name at "
                                      f"most {MAX_TRACK_NODE_IDS}")
        missing = [n for n in node_ids if n not in state.nodes]
        if missing:
            raise MetricsInputRefusal(f"no node(s) {missing[:8]} in this run")
    store.append(EV_TRACK_REQUESTED, {"track": track, "node_ids": node_ids})
    position = len(state.track_requests) - state.tracks_done
    return (f"{run_dir}: track {track!r} queued for the live engine"
            + (f" behind {position} earlier request(s)" if position else "")
            + " — its numbers land as `extra_metrics_imported` rows (source: track "
            + f"{track}); a stopped run serves it on its next resume")
