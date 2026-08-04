"""The run-list projections `routers/runs.py` used to hand to `AppState` as attributes.

`build_router` closed over `srv` and a local `_run_summaries`, then assigned the results onto the
AppState bag (`srv.list_runs_fn`, `srv.list_runs_membership_fn`) so `routers/reports.py` could read
them back — an implicit protocol that existed only after the right `build_router` calls had run, with
no type or registry guarding it (doc 25 SR-12).

They live here instead: plain functions of `srv`, exposed as `AppState.run_summaries()` /
`AppState.run_membership()`. `serve/` may import anything, and this module imports no router, so the
graph stays acyclic — the routers call these, not the other way round.

The per-run fold cache stays on AppState (`srv.summary_cache`), where the reset/delete paths already
invalidate it.
"""
from __future__ import annotations

import stat

from looplab.core.run_deletion import (RunDeletionStorageError, load_run_deletion_fence,
                                       run_deletion_snapshot_token)
from looplab.engine.finalize import incomplete_finalize_scope
from looplab.events.digest import theme_rollup as _theme_rollup
from looplab.events.replay import fold
from looplab.serve.run_commands import run_generation_token


def run_summaries(srv) -> list:
    """The mtime-cached per-run fold summaries, WITHOUT the live-fact overlay.

    Split out so scope reports can read run membership without the `_alive` lock probe and its
    best-effort resume re-spawn — a report GET must not mutate the workspace."""
    out = []
    root = srv.root
    for rd in sorted(root.iterdir()) if root.exists() else []:
        if rd.name.lower().startswith((
                ".looplab-delete-fence-", ".looplab-delete-receipt-",
                ".looplab-delete-quarantine-")):
            continue
        try:
            entry = rd.lstat()
            attributes = int(getattr(entry, "st_file_attributes", 0) or 0)
            reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
            if (not stat.S_ISDIR(entry.st_mode) or stat.S_ISLNK(entry.st_mode)
                    or bool(attributes & reparse_flag)
                    or load_run_deletion_fence(rd) is not None):
                continue
        except (OSError, RunDeletionStorageError):
            continue
        log = rd / "events.jsonl"
        if not log.exists():
            continue
        try:
            stt = log.stat()
            sig = (stt.st_ino, stt.st_ctime_ns, stt.st_size, stt.st_mtime_ns)
            cached = srv.summary_cache.get(rd.name)
            if cached and cached[:4] == sig:    # unchanged log -> reuse (finished runs never re-fold)
                out.append(cached[4])
                continue
            events = srv.events(rd)
            st = fold(events)
            first_ts = events[0].ts if events else 0.0
            finalize_incomplete = (
                incomplete_finalize_scope(events) is not None or st.finalization_pending())
            best = st.best()
            generation = run_generation_token(events)
            summary = {
                "run_id": rd.name, "task_id": st.task_id, "goal": st.goal,
                # A run id is reusable after reset/delete.  Portfolio consumers must include the
                # durable event-log generation in their resource identity or an in-flight detail
                # read for generation A can be joined to generation B's unchanged run id.
                "generation": generation,
                "deletion_generation": run_deletion_snapshot_token(log, generation),
                "seq": events[-1].seq if events else -1,
                "direction": st.direction, "finished": st.finished,
                "phase": srv.phase(st, finalize_incomplete=finalize_incomplete),
                "finalization_incomplete": finalize_incomplete, "nodes": len(st.nodes),
                "best_metric": (best.metric if best else None),
                "best_confirmed": (best.confirmed_mean if best else None),
                "stop_reason": st.stop_reason,
                # Cached with the fold so liveness polling can cheaply decide whether the
                # durable-resume reconciler is needed. Without this bit every dashboard poll
                # re-read and re-folded every stopped/finished run, defeating the summary cache.
                "resume_pending": st.resume_pending(),
                # Cross-run lineage: distinct sibling run_ids this run SEEDED experiments from
                # (via `import`). Drives the MapView's "derived-from" edges. Empty for most runs.
                "seeded_from": sorted({n.origin["run_id"] for n in st.nodes.values()
                                       if isinstance(n.origin, dict) and n.origin.get("run_id")}),
                "themes": _theme_rollup(st),
                "mtime": stt.st_mtime,    # last activity (events.jsonl mtime) — time sort + "updated"
                # The run's true START, from the log itself. `st_ctime` is NOT creation time on
                # POSIX — it is the inode-CHANGE time, which every append to events.jsonl
                # advances, so on Linux this tracked `mtime` and the RunList's
                # "started <date>" tooltip showed the last-update date. The FIRST event's `ts`
                # is the wall clock the run actually began at (`setup_started` when the task has
                # a setup phase, else `run_started`). Fall back to the stat only when the log
                # carries no usable timestamp (an empty or hand-edited recoverable prefix),
                # where a wrong-but-close date beats none.
                "created": (first_ts if first_ts > 0 else stt.st_ctime),  # "started" date
            }
            srv.summary_cache[rd.name] = (*sig, summary)
            out.append(summary)
        except Exception:  # noqa: BLE001 - a half-written run shouldn't break the list
            continue
    return out


def run_membership(srv) -> list:
    """Only the columns `reports._scope_run_ids` joins on. Side-effect free by construction."""
    pdata = srv.projects.load()
    assignments = pdata["assignments"]
    st_assign = pdata.get("supertask_assignments", {})
    return [{"run_id": s["run_id"], "task_id": s.get("task_id"),
             "project_id": assignments.get(s["run_id"]),
             "supertask_id": st_assign.get(s["run_id"])}
            for s in run_summaries(srv)]
