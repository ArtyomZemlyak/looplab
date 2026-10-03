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

import os
import stat
from pathlib import Path

from fastapi import HTTPException

from looplab.core.atomicio import file_identity
from looplab.core.headroom import headroom
from looplab.core.models import task_scale_metric
from looplab.core.pathsafe import is_reparse
from looplab.core.run_discovery import has_run_marker, is_run_dir
from looplab.core.run_deletion import (RUN_DELETION_FENCE_PREFIX, RunDeletionStorageError,
                                       load_run_deletion_fence, run_deletion_fence_path,
                                       run_deletion_snapshot_token)
from looplab.engine.champion_caveats import champion_metric_caveats, mislead_gap
from looplab.engine.comparability import record_of
from looplab.events.trajectory import running_best
from looplab.engine.finalize import incomplete_finalize_scope
from looplab.events.digest import concept_rollup as _concept_rollup, theme_rollup as _theme_rollup
from looplab.events.eventstore import integrity_wire
from looplab.events.replay import fold
from looplab.serve.deletion_transaction import (
    DELETE_IDENTITY_PREFIX, DELETE_QUARANTINE_PREFIX, DELETE_RECEIPT_PREFIX)
from looplab.serve.run_commands import run_generation_token
from looplab.serve.run_result_summary import run_result_summary
from looplab.serve.external_attention import external_mode, optional_identity


def _listed_fence_holds(rd, fence_names: set) -> bool:
    """Does `rd` carry a deletion fence, given the fence names its root's listing holds?

    A BATCH answer, for a caller that has just listed the root itself: no name in the listing means
    no load at all (the common case — a fence exists only while a delete is in flight), and a run
    whose own fence name is listed gets `load_run_deletion_fence`, the authority, which raises
    `RunDeletionStorageError` on a fence it cannot read. A fence published after the listing is
    seen by the next list; every per-run route still answers it through `AppState.run_dir`.
    """
    if not fence_names:
        return False
    if run_deletion_fence_path(rd).name.lower() not in fence_names:
        return False
    return load_run_deletion_fence(rd) is not None


def _is_unreadable_log(exc: HTTPException) -> bool:
    """Is this `AppState.events`' own refusal for a log that exists but cannot be read? (The slug is
    `serve/http.py::REFUSALS`' row; `tests/test_refusal_vocabulary.py` drives this branch.)"""
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    return detail.get("code") == "event_log_unreadable"


def _is_regular_log_entry(log) -> bool:
    """The entry itself (`lstat`) is a plain regular file — the shape `AppState.run_dir` admits."""
    try:
        entry = log.lstat()
    except OSError:
        return False
    return stat.S_ISREG(entry.st_mode) and not is_reparse(entry)


def _unreadable_log_row(rd, stt) -> dict:
    """The run-list row for a run whose `events.jsonl` exists but cannot be read (SRV2-04).

    The SAME keys as a folded row (a test pins the two key sets equal), each at the value that
    claims nothing: no task, no generation, no nodes, no metric. What it does say is on the receipt:
    `source_integrity` is `unreadable`, the direction `log_integrity` itself takes for a file it
    cannot scan. The two stat-derived fields are real — the entry is there and was stat'ed.
    """
    return {
        "run_id": rd.name, "task_id": None, "goal": None, "run_uid": None,
        "generation": None, "deletion_generation": None, "seq": -1,
        "direction": None, "finished": False, "phase": None, "external_harness": None,
        "finalization_incomplete": False, "nodes": 0,
        "source_integrity": integrity_wire({"complete": False, "unreadable": True}),
        "best_metric": None, "best_confirmed": None, "best_metric_caveats": [],
        "mislead_gap": None, "trajectory": None, "best_metric_comparability": None,
        "headroom": None, "objective_key": None,
        "result_summary": None,
        "stop_reason": None, "resume_pending": False, "seeded_from": [], "themes": {},
        "concepts": {}, "mtime": stt.st_mtime, "created": stt.st_ctime,
    }


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
    entries = sorted(root.iterdir()) if root.exists() else []
    # The deletion fences present in THIS listing (review 2026-09-22, SRV2-02). A fence lives in the
    # run ROOT, and `load_run_deletion_fence` warms its lookup by `scandir`-ing that directory first
    # (`core/fence.py::_warm_directory_lookup`) — right for one lookup, but called once per run here
    # it re-read the root listing N times: O(N²) in directory entries, 2.33 s for a warm list over
    # 2,000 runs. The listing the loop walks already names every fence file, so only a run whose own
    # fence name appears in it gets the authoritative load — which still decides, and still fails
    # closed on a fence it cannot read.
    fence_names = {entry.name.lower() for entry in entries
                   if entry.name.lower().startswith(RUN_DELETION_FENCE_PREFIX)}
    for rd in entries:
        if only is not None and rd.name not in only:
            continue
        row = _run_row(srv, rd, fence_names, cache_key=rd.name)
        if row is not None:
            out.append(row)
    return out


def _run_row(srv, rd, fence_names: set, *, cache_key: str):
    """One run's list row, or None when `rd` is not a run the list shows — the body `run_summaries`
    walked inline, shared since 2026-09-29 with `campaign_runs` (doc 70 70.1). `fence_names` are the
    deletion fences in `rd`'s PARENT listing (a fence lives beside its run); `cache_key` keys
    `srv.summary_cache`, the run id for a root run and `<folder>/<run>` for a campaign run, which
    no run id can spell (a run id is one path segment)."""
    # IMPORTED from the writers rather than respelled: a hand-copied prefix does not fail when
    # a writer's changes, it silently stops recognizing that writer's files, and here that means
    # a deletion service file is scanned as a RUN — one `lstat`/`fold` per entry against
    # something that was never a run directory. The identity sidecar was already missing.
    if rd.name.lower().startswith((
            RUN_DELETION_FENCE_PREFIX, DELETE_RECEIPT_PREFIX,
            DELETE_QUARANTINE_PREFIX, DELETE_IDENTITY_PREFIX)):
        return None
    try:
        entry = rd.lstat()
        attributes = int(getattr(entry, "st_file_attributes", 0) or 0)
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        if (not stat.S_ISDIR(entry.st_mode) or stat.S_ISLNK(entry.st_mode)
                or bool(attributes & reparse_flag)
                or _listed_fence_holds(rd, fence_names)):
            return None
    except (OSError, RunDeletionStorageError):
        return None
    log = rd / "events.jsonl"
    try:
        if not log.exists():
            return None
    except OSError:
        # `exists()` RAISES for anything but "absent" (EACCES on an unsearchable directory, EIO):
        # it sat outside the per-run `try`, so one such directory 500'd the whole list. Whether
        # it is a run at all cannot be told — `run_dir` 404s it — so it is not listed.
        return None
    try:
        stt = log.stat()
        # `file_identity`, not a hand-rolled tuple: this one used to be that definition minus
        # BOTH `st_dev` and the Windows `st_file_attributes`, with no stated reason — the same
        # omission already fixed in `appstate.state_payload` and in the attention feed (doc 25
        # SC-11). The reachable wrong outcome here is STALE SAME-ID data, not cross-run bleed
        # (the cache is keyed by the run's own `cache_key`, so two runs cannot collide): a run whose
        # events.jsonl is replaced by a file on a DIFFERENT DEVICE that happens to match on
        # (ino, ctime_ns, size, mtime_ns) reads as unchanged, and the dashboard keeps serving the
        # previous generation's summary. Not exotic — geesefs/s3fs synthesize inode numbers from
        # the path, so a restored or rsynced run dir on a FUSE/S3 mount collides by construction.
        # The launch mode is in the config snapshot, not the log. A repaired/replaced
        # snapshot must invalidate this field without requiring a new event.
        try:
            config_identity = optional_identity(rd / "config.snapshot.json")
        except OSError:
            config_identity = ("unreadable",)
        sig = (file_identity(stt), config_identity)
        cached = srv.summary_cache.get(cache_key)
        # Stored as (signature, summary): the flattened `(*sig, summary)` made the tuple WIDTH
        # load-bearing, so widening the signature by one field silently turned `cached[4]` into
        # a stat number the dashboard would have served as a run summary.
        # An unchanged log is reused as-is (a finished run never re-folds).
        if cached is not None and cached[0] == sig:
            return cached[1]
        try:
            events = srv.events(rd)
        except HTTPException as exc:
            if not _is_unreadable_log(exc) or not _is_regular_log_entry(log):
                # A log that is not a plain regular FILE (a directory, a link) is not a run
                # `run_dir` would open — it 404s one — so it stays unlisted, as before.
                raise
            # PRESENT, AND SAYING SO (review 2026-09-22, SRV2-04). The log exists but cannot be
            # read, and the generic handler below used to swallow that: the run VANISHED from
            # the list while every per-run GET answered a bare 500. It is listed as a stub whose
            # receipt says `unreadable` — the one the UI already words as "could not be read at
            # all" — and it is NOT cached, so the next list retries the read.
            return _unreadable_log_row(rd, stt)
        st = fold(events)
        first_ts = events[0].ts if events else 0.0
        finalize_incomplete = (
            incomplete_finalize_scope(events) is not None or st.finalization_pending())
        best = st.best()
        trajectory = running_best(st)
        generation = run_generation_token(events)
        summary = {
            "run_id": rd.name, "task_id": st.task_id, "goal": st.goal,
            # The incarnation, so the concept shelf can inherit a memory row's concepts from
            # the run that WROTE it rather than from whatever now bears its name (doc 52 row 4).
            "run_uid": st.run_uid,
            # A run id is reusable after reset/delete.  Portfolio consumers must include the
            # durable event-log generation in their resource identity or an in-flight detail
            # read for generation A can be joined to generation B's unchanged run id.
            "generation": generation,
            "deletion_generation": run_deletion_snapshot_token(log, generation),
            "seq": events[-1].seq if events else -1,
            "direction": st.direction, "finished": st.finished,
            "external_harness": external_mode(rd),
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
            # HOW MUCH of that number the intended protocol supports (doc 52 row 22): the
            # Protocol Validity pair — the champion beside the node the same selector crowns
            # among those the record says nothing against, and their gap in the run's direction
            # (`engine/champion_caveats.py::mislead_gap`). `None` without a champion;
            # a clean run reads `gap: 0` with `excluded: 0`. Additive; a legacy client ignores it.
            "mislead_gap": mislead_gap(st),
            # THE RUN'S METRIC TRAJECTORY as change points (doc 52 row 26): the running best per
            # evaluated experiment, `[index, best, node_id]` at every improvement plus the final
            # index, bounded by `events/trajectory.py::TRAJECTORY_CAP`. This is the series the
            # cross-run overlay (`ui/src/crossRunRank.js::trajectoryOverlay`) was waiting for: it
            # rides on the row rather than costing one state fold per run on the request thread,
            # and it is cached WITH the fold, so a poll pays nothing for it. `None` when no
            # feasible measured node exists. Additive; a legacy client ignores it.
            "trajectory": trajectory,
            "result_summary": run_result_summary(st, trajectory),
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
            # HOW FAR THE CHAMPION CLOSED THE TASK'S DECLARED GAP (doc 67 67.14): its gain over
            # the task's baseline and, with a target, the share of baseline -> target it covers
            # (`core/headroom.py::headroom`) — the one number that reads the same across TASKS.
            # `None` for every task that declared no `reference_score`, and it is never a zero.
            # Additive; a legacy client ignores it.
            # On the TASK's scale always (`core/models.py::task_scale_metric`): the declared
            # baseline and target are the task metric's, and under an operator retarget the
            # champion's `robust_metric` is the objective's (critic 2026-09-27, driven: 150 % of
            # a gap the task metric had closed by half).
            "headroom": headroom(task_scale_metric(best, st.objective_key)
                                 if best is not None else None,
                                 st.reference_score, st.direction),
            # WHICH METRIC `best_metric` and `trajectory` are (doc 68 68.2): the declared extra
            # metric an operator retarget made the objective, or None — the task's own. The
            # cross-run table partitions on it (`ui/src/crossRunRank.js::partitionKey`) and
            # `best_metric_caveats` says `retargeted_objective`. Additive.
            "objective_key": st.objective_key,
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
        srv.summary_cache[cache_key] = (sig, summary)
        return summary
    except Exception:  # noqa: BLE001 - a half-written run shouldn't break the list
        return None


# THE CAMPAIGN FOLDERS (doc 70 70.1): `looplab run --out runs/<campaign>/<seed>` is as ordinary as
# `runs/<run>`, and the run list above sees only the root's children. Its runs are listed HERE, apart
# from `run_summaries`, and never inside it: every per-run route addresses a run by ONE path segment
# through `AppState.run_dir`'s direct-child rule, so a nested run has no id a route can open yet, and
# a row in `/api/runs` is a row every caller (the compare view, the portfolio, the assistant) opens.
# Bounded work: at most this many folders and, per folder, this many runs are folded; what a bound
# cut is COUNTED on the payload, never silently dropped.
CAMPAIGN_FOLDERS_CAP = 200
CAMPAIGN_RUNS_CAP = 500
# …and at most this many ENTRIES of any one listing — the root's, each folder's — are looked at,
# with no `stat` at all for an entry that is not a directory (critic 2026-09-29, LOW-2: a
# 200 000-file `data/` beside the campaigns cost every run-list mount 3 s of `lstat`, and the caps
# above bound only what was FOLDED). A listing the bound cut says so (`listing_cut`).
CAMPAIGN_ENTRIES_CAP = 100_000


def _directory_children(directory) -> tuple[list, bool]:
    """The non-hidden subdirectories of `directory`, sorted by name — a link is not one, and no
    entry is stat-ed for this (`DirEntry.is_dir(follow_symlinks=False)` reads the listing's own type
    where the platform gives one) — and whether `CAMPAIGN_ENTRIES_CAP` cut the listing short."""
    children: list = []
    cut = False
    with os.scandir(directory) as listing:
        for seen, entry in enumerate(listing):
            if seen >= CAMPAIGN_ENTRIES_CAP:
                cut = True
                break
            try:
                if not entry.name.startswith(".") and entry.is_dir(follow_symlinks=False):
                    children.append(Path(entry.path))
            except OSError:
                continue
    return sorted(children), cut


def campaign_folder(entry) -> bool:
    """Is this root child a CAMPAIGN folder — a plain directory that is not a run itself?

    The rule `core/run_discovery.py` walks by, one level down: a directory holding a run's log or any
    of its other markers is a run (or one in setup) and is never descended — its children are node
    workspaces a candidate can write, which must never read as runs (`AppState.run_dir`'s reason for
    the direct-child rule). Hidden, reserved and service entries are the root's own stores; a link or
    reparse point is not followed, as the run list follows none."""
    from looplab.serve.appstate import (_DELETE_SERVICE_PREFIXES, _LIFECYCLE_LOCK_PREFIX,
                                        _RESERVED_RUN_IDS, _RESET_RECEIPT_PREFIX,
                                        _TRACE_CLEAR_RECEIPT_PREFIX)
    name = entry.name
    lowered = name.lower()
    if (name.startswith(".") or lowered in _RESERVED_RUN_IDS
            or lowered.startswith((_LIFECYCLE_LOCK_PREFIX, _TRACE_CLEAR_RECEIPT_PREFIX,
                                   _RESET_RECEIPT_PREFIX, *_DELETE_SERVICE_PREFIXES))):
        return False
    try:
        info = entry.lstat()
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or is_reparse(info):
        return False
    return not is_run_dir(entry) and not has_run_marker(entry)


def campaign_runs(srv) -> dict:
    """The runs one level inside each campaign folder, grouped by folder — the same rows as the run
    list (`_run_row`, the same fold cache), each keyed `<folder>/<run>` in that cache.

    READ-ONLY by construction: nothing here spawns, reconciles or writes, and a row carries no id a
    per-run route accepts. `run_root` is the folder itself, the argument `looplab ui --run-root` takes
    to serve it as a root where its runs ARE addressable. A folder holding no run is not listed.

    A ROOT THAT IS ITSELF A RUN lists nothing (critic 2026-09-29, LOW-1, driven: `looplab ui
    --run-root runs/demo` listed `nodes/` as a campaign and folded `nodes/node_0/events.jsonl`, a
    file the candidate writes): its children are its own stores and node workspaces, never runs.

    WHAT A BOUND CUT IS COUNTED AS IT IS (LOW-3): `runs_skipped` counts only directories past the cap
    that carry a run's marker — never a file or a plain directory, which were never rows — and
    `folders_skipped` the campaign-folder candidates past the folder cap, NOT DESCENDED, so some may
    hold no run at all (said so by the UI's "not examined")."""
    root = srv.root
    empty = {"folders": [], "folders_skipped": 0, "listing_cut": False}
    if not root.exists() or is_run_dir(root) or has_run_marker(root):
        return empty
    try:
        entries, root_cut = _directory_children(root)
    except OSError:
        return empty
    groups: list = []
    folders_skipped = 0
    for folder in entries:
        if not campaign_folder(folder):
            continue
        if len(groups) >= CAMPAIGN_FOLDERS_CAP:
            folders_skipped += 1
            continue
        try:
            children, cut = _directory_children(folder)
        except OSError:
            continue
        # The deletion fences a run's row is checked against are FILES beside it, so they come from
        # a name-only scan of the same listing (`_listed_fence_holds`), bounded like the listing.
        try:
            with os.scandir(folder) as listing:
                fence_names = {entry.name.lower() for _i, entry in zip(
                    range(CAMPAIGN_ENTRIES_CAP), listing)
                    if entry.name.lower().startswith(RUN_DELETION_FENCE_PREFIX)}
        except OSError:
            continue
        runs: list = []
        runs_skipped = 0
        for rd in children:
            if len(runs) >= CAMPAIGN_RUNS_CAP:
                if is_run_dir(rd) or has_run_marker(rd):
                    runs_skipped += 1
                continue
            row = _run_row(srv, rd, fence_names, cache_key=f"{folder.name}/{rd.name}")
            if row is not None:
                runs.append(row)
        if runs:
            groups.append({"folder": folder.name, "run_root": str(folder), "runs": runs,
                           "runs_skipped": runs_skipped, "listing_cut": cut})
    return {"folders": groups, "folders_skipped": folders_skipped, "listing_cut": root_cut}


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
