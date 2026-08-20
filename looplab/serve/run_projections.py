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

from looplab.core.atomicio import file_identity
from looplab.core.run_deletion import (RUN_DELETION_FENCE_PREFIX, RunDeletionStorageError,
                                       load_run_deletion_fence, run_deletion_snapshot_token)
from looplab.engine.champion_caveats import champion_metric_caveats
from looplab.engine.comparability import record_of
from looplab.engine.finalize import incomplete_finalize_scope
from looplab.events.digest import concept_rollup as _concept_rollup, theme_rollup as _theme_rollup
from looplab.events.replay import fold
from looplab.serve.deletion_transaction import (
    DELETE_IDENTITY_PREFIX, DELETE_QUARANTINE_PREFIX, DELETE_RECEIPT_PREFIX)
from looplab.serve.run_commands import run_generation_token


def run_summaries(srv, only=None) -> list:
    """The mtime-cached per-run fold summaries, WITHOUT the live-fact overlay.

    Split out so scope reports can read run membership without the `_alive` lock probe and its
    best-effort resume re-spawn — a report GET must not mutate the workspace.

    `only` bounds the work to a set of run ids, skipping every other run BEFORE its fold. The cost
    this exists for is real: a caller that needs a handful of runs' rollups (the Memory panel's
    concept shelf joins on the `run_id` its rows cite) otherwise folds the WHOLE workspace on the
    request thread — every run on a cold cache, and every LIVE run on each open, since the cache is
    keyed on `file_identity` and a live log's identity changes on every append. `None` means every
    run, which is what the run list and the report scope want; an EMPTY set means none, and is a
    real answer rather than "unfiltered" — a caller whose rows cite no run wants no fold at all.
    """
    out = []
    root = srv.root
    for rd in sorted(root.iterdir()) if root.exists() else []:
        if only is not None and rd.name not in only:
            continue
        # IMPORTED from the writers rather than respelled: a hand-copied prefix does not fail when
        # a writer's changes, it silently stops recognizing that writer's files, and here that means
        # a deletion service file is scanned as a RUN — one `lstat`/`fold` per entry against
        # something that was never a run directory. The identity sidecar was already missing.
        if rd.name.lower().startswith((
                RUN_DELETION_FENCE_PREFIX, DELETE_RECEIPT_PREFIX,
                DELETE_QUARANTINE_PREFIX, DELETE_IDENTITY_PREFIX)):
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
            # `file_identity`, not a hand-rolled tuple: this one used to be that definition minus
            # BOTH `st_dev` and the Windows `st_file_attributes`, with no stated reason — the same
            # omission already fixed in `appstate.state_payload` and in the attention feed (doc 25
            # SC-11). The reachable wrong outcome here is STALE SAME-ID data, not cross-run bleed
            # (the cache is keyed by `rd.name`, so two runs cannot collide): a run whose
            # events.jsonl is replaced by a file on a DIFFERENT DEVICE that happens to match on
            # (ino, ctime_ns, size, mtime_ns) reads as unchanged, and the dashboard keeps serving the
            # previous generation's summary. Not exotic — geesefs/s3fs synthesize inode numbers from
            # the path, so a restored or rsynced run dir on a FUSE/S3 mount collides by construction.
            sig = file_identity(stt)
            cached = srv.summary_cache.get(rd.name)
            # Stored as (signature, summary): the flattened `(*sig, summary)` made the tuple WIDTH
            # load-bearing, so widening the signature by one field silently turned `cached[4]` into
            # a stat number the dashboard would have served as a run summary.
            # An unchanged log is reused as-is (a finished run never re-folds).
            if cached is not None and cached[0] == sig:
                out.append(cached[1])
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
                # Whether the fold above saw the WHOLE log. Every other field in this row is derived
                # from `events` and none of them can express "this is 1.2 % of the run": the shipped
                # corpus has a row reading `phase: search, finished: false, nodes: 2,
                # best_metric: 0.8077` for `rubertlite-dense-retrieval`, whose own `budget` event says
                # `nodes: 81` and whose log holds 1,624 records. Cached WITH the fold (same
                # `file_identity` key), so it costs one scan per changed log, not one per poll.
                "source_integrity": srv.log_integrity(rd),
                "best_metric": (best.metric if best else None),
                "best_confirmed": (best.confirmed_mean if best else None),
                # WHAT KIND OF NUMBER the two fields above are, when the run recorded a caveat about
                # it and selected on it anyway. Every OTHER field the row carries about the champion
                # is the number itself, so a client reading this row could not derive it: `violations`,
                # `metric_provenance` and `reward_hacks` all stop at the run-state payload. Two rungs
                # put a caveated number here — `metric_salvage: "select"` (which mints no violation
                # row, so the node is feasible and competes) and `trust_gate: "audit"`, the DEFAULT
                # (which enforces nothing, so a hard reward-hack/leakage signal excludes nothing).
                # The rule is `engine/champion_caveats.py` and it is spelled as calls to the same
                # predicates the fold and the cross-run writers use, never as a re-reading of the rows
                # here — see that module for why this is NOT `memory.unreliable_metric_ids` (which is
                # empty on a champion by construction). Additive with a reader-side default: `[]` on
                # a legacy client's absent key, and an EMPTY list means the run recorded no caveat,
                # never that a detector ran (`reward_hack_detect` is off by default). Cached WITH the
                # fold, so it costs one derivation per changed log rather than one per poll.
                "best_metric_caveats": champion_metric_caveats(st),
                # WHAT THIS NUMBER MAY BE RANKED AGAINST (`engine/comparability.py`). The row's own
                # `task_id` + `direction` is what every cross-run surface currently partitions on,
                # and `ui/src/crossRunRank.js` says in its own words why that is not enough: "a
                # shared task_id is an operational lookup key … two runs of `repo_task` may have
                # optimized recall@100 against different corpora". This is the field that lets it
                # SAY so instead of caveating in prose — one server-side value, not a fix in each of
                # the thirteen browser surfaces that read this row.
                #
                # `None` for every run written before 2026-08-20 and for every task that declares
                # neither `eval.inputs` nor a `comparison_contract`. `None` is UNKNOWN at every
                # reader and is NEVER "the same as mine" — an absent key that defaulted to equal
                # would certify the whole corpus as mutually comparable, which is the false
                # statement this exists to stop. Read off the CHAMPION's own folded
                # `metric_provenance`, because the number this row publishes is that node's.
                "best_metric_comparability": record_of(best) if best is not None else None,
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
                # The run's CONCEPT membership, keyed by whole id. `themes` cannot stand in for it:
                # it is axis-truncated AND legacy-theme-backfilled, so it answers "how do I group this
                # run" and never "which concepts is this run evidence for". This rollup is the join key
                # the cross-run concept surfaces need — the memory shelf attributes an old lesson to a
                # concept through its `run_id`, and it may only do so when the run really is tagged.
                # Empty dict = untagged, and every reader must show that as untagged rather than absent.
                "concepts": _concept_rollup(st),
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
            srv.summary_cache[rd.name] = (sig, summary)
            out.append(summary)
        except Exception:  # noqa: BLE001 - a half-written run shouldn't break the list
            continue
    return out


def run_membership(srv) -> list:
    """Only the columns `reports._scope_run_ids` joins on. Side-effect free by construction.

    The membership-only projection of the same list: run_id -> task/project/supertask, with NO
    engine-liveness lock probe and NO durable-resume reconciler. Scope reports need only those
    columns, and calling the full handler for them made a report READ probe every run's lock and
    potentially SPAWN an engine process."""
    pdata = srv.projects.load()
    assignments = pdata["assignments"]
    st_assign = pdata.get("supertask_assignments", {})
    return [{"run_id": s["run_id"], "task_id": s.get("task_id"),
             "project_id": assignments.get(s["run_id"]),
             "supertask_id": st_assign.get(s["run_id"])}
            for s in run_summaries(srv)]
