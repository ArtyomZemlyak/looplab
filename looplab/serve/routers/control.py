"""Control-plane routes: append control intents (/control) and spawn/resume/reset/start engine
processes. Handler bodies are verbatim moves from `serve/server.py::make_app` (BACKLOG §4)."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import anyio
import orjson
from fastapi import APIRouter, HTTPException, Request

from looplab.core.atomicio import best_effort_fsync
from looplab.core.config import Settings
from looplab.events.eventstore import EventStore, EventStoreConcurrencyError, write_jsonl_atomic
from looplab.events.replay import fold
from looplab.events.types import (
    EV_APPROVAL_GRANTED, EV_FORCE_ABLATE, EV_FORCE_CONFIRM, EV_FORK, EV_INJECT_NODE,
    EV_NODE_ABORT, EV_NODE_RESET, EV_PROMOTE, EV_RESUME_REQUESTED,
)
from looplab.serve.appstate import _RESERVED_RUN_IDS
from looplab.serve.engine_proc import (
    _claim_and_spawn_resume, _engine_alive, _fresh_resume_launch_pending,
    _resolve_task_file, _run_lifecycle_lock, _spawn_engine)
from looplab.serve.protocol import CONTROL_EVENTS, GENESIS_CHAT_SEQ_BASE
from looplab.serve.settings_store import _ALLOWED_FIELDS, _SECRET_FIELDS


# Node-scoped operator intents are compare-and-set operations. The generation is the version the
# operator actually inspected; accepting an id-only delayed click after node_reset would apply it to
# different code/results that merely reused the same node id (an ABA race).
_LIFECYCLE_CONTROL_TARGETS = {
    EV_NODE_ABORT: "node_id",
    EV_NODE_RESET: "node_id",
    EV_APPROVAL_GRANTED: "node_id",
    EV_FORCE_CONFIRM: "node_id",
    EV_FORCE_ABLATE: "node_id",
    EV_FORK: "from_node_id",
    EV_PROMOTE: "node_id",
}
_ABSENT = object()


def _strict_nonnegative_int(raw, field: str) -> int:
    if isinstance(raw, bool) or (isinstance(raw, float) and not raw.is_integer()):
        raise HTTPException(400, f"{field} must be an integer")
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(400, f"{field} must be an integer")
    if value < 0:
        raise HTTPException(400, f"{field} must be non-negative")
    return value


def _defaults_backend_llm(task_spec: Optional[dict], task_file: Optional[str],
                          settings: dict, ui_settings: dict) -> bool:
    """True when a launch should default `backend="llm"`: the task normalizes to a GENERATIVE kind
    (the agent writes/edits code) and nobody chose a backend. CLI parity (mega-review P10):
    `looplab run --goal` already defaults backend=llm for these kinds (cli.py's `backend_chosen`
    rule), but Settings.backend defaults to "toy" — a repo/dataset run launched over HTTP without
    this got NoOpRepoDeveloper and every node silently re-evaluated the unchanged baseline (no
    error, just a flat run). Shared by /api/start (authoritative — the one funnel every launch goes
    through) and the genesis card (display-only, so the operator can see/override it pre-launch).
    "Chosen" = a `backend` key already in the merged launch/card `settings`, or one the deployment
    set — a UI-saved value, LOOPLAB_BACKEND env, or a `.env` line all land in
    `Settings(**ui).model_fields_set`, the same test cli.py's `backend_chosen` uses (and
    `_spawn_engine` overlays our env ON TOP of os.environ, so injecting would clobber it). Only that
    surface-specific "chosen" detection lives here; the kind→backend rule itself is
    `engine/genesis.py::default_backend`, shared with cli.py's genesis defaulting."""
    if "backend" in settings:
        return False
    if not (isinstance(task_spec, dict) and task_spec):
        if not task_file:
            return False
        # A catalogue/snapshot launch: the task lives only in the file — read it with the SAME
        # loader the spawned engine uses (cli.py `run` → appconfig.load_document): it handles a
        # YAML catalogue entry, a unified config's `task:` block, and a BOM'd JSON, all of which a
        # raw json.loads mis-reads — so this default can never disagree with the task the engine
        # actually parses out of the very same file (read parity).
        try:
            from looplab.core.appconfig import load_document
            task_spec, _file_settings, _out = load_document(Path(task_file))
        except (OSError, ValueError):
            return False                # unreadable/foreign task file → no default; fails downstream
        if not (isinstance(task_spec, dict) and task_spec):
            return False
    from looplab.adapters.tasks import normalize_task
    from looplab.engine.genesis import default_backend
    # Best-effort, NARROW: only the task normalization may soft-fail here — an unnormalizable spec
    # is validate_task's 400 (or the engine's own startup error), never this default's concern.
    try:
        kind = normalize_task(dict(task_spec)).get("kind")
    except (KeyError, TypeError, ValueError):
        return False
    # `chosen=False` probe first: a non-generative kind can never default, so skip the Settings
    # construction (env + saved-UI validation) entirely for it.
    if default_backend(kind, chosen=False) != "llm":
        return False
    try:
        return "backend" not in getattr(Settings(**(ui_settings or {})), "model_fields_set", set())
    except ValueError:  # pydantic ValidationError ⊂ ValueError — bad saved/env settings fail later,
        return False    # in the spawned engine's own Settings(); don't inject on top of them


def build_router(srv) -> APIRouter:
    router = APIRouter()
    _run_dir, root = srv.run_dir, srv.root

    # ------------------------------------------------------------------ control
    @router.post("/api/runs/{run_id}/control")
    async def control(run_id: str, request: Request):
        rd = _run_dir(run_id)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "control body must be an object")
        etype = body.get("type")
        if etype not in CONTROL_EVENTS:
            raise HTTPException(400, f"unknown control event: {etype!r}")
        raw_data = body.get("data")
        if raw_data is not None and not isinstance(raw_data, dict):
            raise HTTPException(400, "control data must be an object")
        data = dict(raw_data or {})
        # node_reset: re-run an existing node in place — validate the target + stage so a typo can't
        # append a no-op reset (the fold would silently ignore an unknown node_id).
        if etype == EV_NODE_RESET:
            # propose|implement are the lifecycle stages; anything else is an eval-PIPELINE stage name
            # to restart from (train / eval / data_prep / …) — those are per-node, so accept any sane
            # non-empty name rather than a fixed allow-list.
            stage = str(data.get("from_stage", "eval")).strip()
            if not stage or len(stage) > 64:
                raise HTTPException(400, "from_stage must be a non-empty stage name")
            data["from_stage"] = stage

        target_key = _LIFECYCLE_CONTROL_TARGETS.get(etype)
        if target_key is not None:
            # Bind the decision to the exact lifecycle the UI/agent inspected. The generation must be
            # carried by the producer, not filled from current state on receipt: filling it here would
            # turn a delayed generation-0 click into a valid generation-1 action.
            st = srv.state(rd)
            raw_nid = data.get(target_key)
            if etype == EV_APPROVAL_GRANTED and raw_nid is None:
                best = st.best()
                if best is None:
                    raise HTTPException(400, "run has no evaluated node to approve")
                nid = best.id
            else:
                nid = _strict_nonnegative_int(raw_nid, target_key)
            node = st.nodes.get(nid)
            if node is None:
                raise HTTPException(404, f"no node #{nid} in this run")
            if node.tombstoned:
                raise HTTPException(409, f"node #{nid} is tombstoned and cannot be controlled")
            if nid in st.aborted_nodes and etype not in (EV_NODE_ABORT, EV_NODE_RESET):
                raise HTTPException(409, f"node #{nid} is aborted; reset it before {etype}")
            raw_generation = data.get("generation", _ABSENT)
            if raw_generation is _ABSENT:
                # Backward-compatible for generation-0 clients/logs. After a reset ambiguity is
                # destructive, so fail closed and make the caller refresh the node snapshot.
                if node.attempt != 0:
                    raise HTTPException(
                        409, f"stale {etype}: generation is required (current generation is "
                             f"{node.attempt})")
                generation = 0
            else:
                generation = _strict_nonnegative_int(raw_generation, "generation")
            if generation != node.attempt:
                raise HTTPException(
                    409, f"stale {etype}: node #{nid} is generation {node.attempt}, not {generation}")
            data[target_key] = nid
            data["generation"] = generation

        if etype == EV_INJECT_NODE and (data.get("parent_id") is not None
                                        or data.get("parent_ids") is not None
                                        or "parent_generations" in data):
            # Manual inject/merge actions also carry the parent snapshot they were designed against.
            # Validate it now; node_created repeats the check in replay to close reset-during-build.
            raw_snapshot = data.get("parent_generations", _ABSENT)
            if raw_snapshot is not _ABSENT and not isinstance(raw_snapshot, dict):
                raise HTTPException(400, "parent_generations must be an object")
            if ("parent_ids" in data and data.get("parent_ids") is not None
                    and not isinstance(data.get("parent_ids"), list)):
                raise HTTPException(400, "parent_ids must be an array")
            if isinstance(data.get("parent_ids"), list):
                parents = [_strict_nonnegative_int(pid, "parent_id")
                           for pid in data["parent_ids"]]
                data["parent_ids"] = parents
            elif data.get("parent_id") is not None:
                parents = [_strict_nonnegative_int(data.get("parent_id"), "parent_id")]
                data["parent_id"] = parents[0]
            else:
                parents = []
            if len(set(parents)) != len(parents):
                raise HTTPException(400, "parent ids must be unique")
            st = srv.state(rd)
            if raw_snapshot is _ABSENT:
                # Old generation-0 web clients remain valid. Once any parent was reset, however, an
                # id-only delayed inject is ambiguous and must refresh instead of rebinding silently.
                missing = [pid for pid in parents if pid not in st.nodes]
                if missing:
                    raise HTTPException(404, f"no node #{missing[0]} in this run")
                if any(st.nodes[pid].attempt != 0 for pid in parents):
                    raise HTTPException(409, "parent generation is required after node reset")
                raw_snapshot = {str(pid): 0 for pid in parents}
            if len(raw_snapshot) != len(parents):
                raise HTTPException(400, "parent generation snapshot does not match parents")
            normalized_snapshot: dict[str, int] = {}
            for pid in parents:
                node = st.nodes.get(pid)
                if node is None:
                    raise HTTPException(404, f"no node #{pid} in this run")
                if node.tombstoned:
                    raise HTTPException(409, f"parent #{pid} is tombstoned")
                if pid in st.aborted_nodes:
                    raise HTTPException(409, f"parent #{pid} is aborted")
                raw_generation = raw_snapshot.get(str(pid), raw_snapshot.get(pid, _ABSENT))
                if raw_generation is _ABSENT:
                    raise HTTPException(400, f"missing generation for parent #{pid}")
                generation = _strict_nonnegative_int(raw_generation, "parent generation")
                if generation != node.attempt:
                    raise HTTPException(
                        409, f"stale parent #{pid}: current generation is {node.attempt}")
                normalized_snapshot[str(pid)] = generation
            data["parent_generations"] = normalized_snapshot
        # Cross-run import: an inject seeded from a sibling run. Resolve the source experiment from disk
        # NOW and bake its code + `origin` provenance into the inject_node, so the engine reproduces it
        # faithfully and the lineage is recorded. (_run_dir guards path traversal on the sibling id.)
        if etype == EV_INJECT_NODE and data.get("source_run") and data.get("source_node") is not None:
            sr = str(data.pop("source_run"))
            try:
                sn = int(data.pop("source_node"))
            except (TypeError, ValueError):
                raise HTTPException(400, "source_node must be an integer")
            sst = srv.state(_run_dir(sr))            # 404 if the sibling run doesn't exist
            snode = sst.nodes.get(sn)
            if snode is None:
                raise HTTPException(404, f"no experiment #{sn} in run {sr}")
            if snode.tombstoned:
                raise HTTPException(409, f"source experiment #{sn} in run {sr} is tombstoned")
            if sn in sst.aborted_nodes:
                raise HTTPException(409, f"source experiment #{sn} in run {sr} is aborted")
            sidea = snode.idea.model_dump(mode="json")
            note = f"imported from run {sr} #{sn}"
            base = (sidea.get("rationale") or "").strip()
            sidea["rationale"] = f"{base} | {note}" if base else note
            data["idea"] = sidea
            data["code"] = snode.code or None           # None => engine re-implements the idea
            # Carry the sibling's FULL solution, not just solution.py: multi-file (repo/agent) nodes
            # keep their helper modules + accepted deletions, so the reproduction actually runs. Safe
            # to replay `deleted` because a sibling shares this task's pristine repo base (same task_id).
            data["files"] = dict(snode.files)
            data["deleted"] = list(snode.deleted)
            data["origin"] = {"run_id": sr, "node_id": sn, "metric": snode.robust_metric}
        # P1-12 optional optimistic concurrency: a client may pass `expected_seq` (the log tail it based
        # this intent on). The append then lands ONLY if nothing else was written since — else 409, so a
        # control raised on a STALE view (an approval for a best that just changed, a reset of a node
        # that just advanced) is rejected instead of applied blind. Omitted => today's unconditional append.
        expected = body.get("expected_seq")
        if expected is not None:
            try:
                expected = int(expected)
            except (TypeError, ValueError):
                raise HTTPException(400, "expected_seq must be an integer")
        # Fresh EventStore per write (single-writer discipline): it rescans last seq before append.
        try:
            ev = EventStore(rd / "events.jsonl").append(etype, data, expected_last_seq=expected)
        except EventStoreConcurrencyError as e:
            raise HTTPException(409, str(e))
        return {"ok": True, "seq": ev.seq, "type": etype}

    # ------------------------------------------------------------------ spawn / resume
    def _task_file_for(rd: Path) -> Optional[str]:
        # The resolved immutable snapshot is authoritative. The shared helper tolerates malformed
        # legacy ui_meta and only accepts its task_file when no snapshot exists and the target exists.
        return _resolve_task_file(rd)

    def _append_resume_request(rd: Path) -> str:
        """Classify and durably append one handoff against the exact folded tail."""
        store = EventStore(rd / "events.jsonl")
        for _attempt in range(8):
            events = store.read_all()
            state = fold(events)
            last_seq = events[-1].seq if events else -1
            last_stop = state.last_stop_request_seq
            last_finish = state.last_finish_seq
            mode = ("finalize" if state.stop_requested and last_stop > last_finish else "resume")
            try:
                store.append(EV_RESUME_REQUESTED, {"mode": mode}, expected_last_seq=last_seq)
                return mode
            except EventStoreConcurrencyError:
                continue
        raise HTTPException(409, "run state changed repeatedly; retry resume")

    @router.post("/api/runs/{run_id}/resume")
    def resume_run(run_id: str):
        rd = _run_dir(run_id)
        # Serialize task resolution + intent append against reset/delete. Once the append lands, the
        # fresh-pending fence protects the short interval before `_claim_and_spawn_resume` acquires
        # this lifecycle lock itself; there is no unprotected delete -> ghost-directory recreation.
        with _run_lifecycle_lock(rd):
            task_file = _task_file_for(rd)
            if not task_file:
                raise HTTPException(
                    400, "run is not resumable - no task.snapshot.json or ui_meta.json "
                         "(it predates self-describing runs; start it via the UI to enable resume)")
            # Durable before every liveness branch: if the current owner is already past
            # run_finished, or a detached spawn dies before taking the lock, recovery retains it.
            mode = _append_resume_request(rd)
        cli_args = (["finalize", str(rd), "--task-file", str(task_file)] if mode == "finalize" else
                    ["resume", str(rd), "--task-file", str(task_file)])
        # Claim does its own liveness checks under the lifecycle fence. `wait_on_alive` closes both
        # the obvious live-owner path and the dead-probe -> claim live flip; cancellation is shared
        # with the ASGI shutdown handler so no resume child appears after the reaper's snapshot.
        was_alive = _engine_alive(rd)
        spawned = _claim_and_spawn_resume(
            rd, cli_args, cancel_event=srv.resume_cancel, wait_on_alive=True)
        if was_alive and not spawned:
            return {"ok": True, "already_running": True, "resume_after_exit": True}
        return {"ok": True, "launch_pending": not spawned}

    @router.post("/api/runs/{run_id}/reset")
    def reset_run(run_id: str):
        """round-7 "Replay": reset a run IN PLACE — archive its event log + spans + node workspaces and
        re-spawn a fresh run on the same run-id. The prior artifacts are RENAMED (not deleted) so the
        history is recoverable."""
        rd = _run_dir(run_id)
        # Never reset an ACTIVE or LAUNCHING run. The engine is the sole event-log writer: OS liveness
        # covers its full post-finish tail, while the lifecycle + fresh-claim fence covers the earlier
        # request/claim/Popen interval before a detached child has acquired engine.lock.
        with _run_lifecycle_lock(rd):
            if (_engine_alive(rd) or _fresh_resume_launch_pending(rd)
                    or not srv.state(rd).finished):
                raise HTTPException(
                    409, "run is still active or launching — stop it first "
                         "(Replay resets a finished run)")
            task_file = _task_file_for(rd)
            if not task_file:
                raise HTTPException(400, "run is not resettable — no task.snapshot.json or ui_meta.json")
            stamp = int(time.time() * 1000)  # ms granularity, plus collision check below
            # Archive auxiliaries first and the source-of-truth event log LAST. A rename is a
            # transaction: on any failure restore every completed move and do not start a writer on
            # a mixed old/new directory. The old best-effort `except: pass` could leave events.jsonl
            # in place, partially archive nodes, then launch a second writer into the old history.
            names = ("spans.jsonl", "readmodel.sqlite-wal", "readmodel.sqlite-shm",
                     "readmodel.sqlite", "nodes", "chat.jsonl", "events.jsonl")
            def _present(path: Path) -> bool:
                return path.exists() or path.is_symlink()

            def _archive_temp(archived: Path) -> Path:
                return archived.with_name(f"{archived.name.upper()}.tmp")

            while any(
                    _present(candidate)
                    for name in names
                    for candidate in (
                        rd / f"{name}.reset-{stamp}",
                        _archive_temp(rd / f"{name}.reset-{stamp}"))):
                stamp += 1
            moved: list[tuple[Path, Path]] = []

            def _rollback_reset_archive() -> list[str]:
                failures: list[str] = []
                restored: list[tuple[Path, Path]] = []
                for src, archived in reversed(moved):
                    try:
                        if _present(archived):
                            # The lifecycle fence guarantees no legitimate newer writer can recreate
                            # `src` during this transaction. Atomic replace also bypasses the forward
                            # Path.rename fault surface and tolerates an already-created destination.
                            # Only this transaction's exact archive is restored; older approved
                            # `.reset-*` archives are never enumerated or deleted.
                            archived.replace(src)
                        elif not _present(src):
                            failures.append(src.name)
                        if _present(src) and not _present(archived):
                            restored.append((src, archived))
                    except OSError:
                        failures.append(src.name)
                # On Windows the filesystem may publish this case-variant shadow just after replace
                # returns, then reap it asynchronously. Stamp collision checks prove it did not exist
                # before this transaction. Poll a short bounded window and remove only candidates
                # derived from successfully restored entries in `moved`; never glob suffix-less
                # approved archives or recursively remove anything.
                deadline = time.monotonic() + 0.1
                while restored and time.monotonic() < deadline:
                    for _src, archived in restored:
                        temp = _archive_temp(archived)
                        if _present(temp):
                            try:
                                temp.unlink()  # file/symlink only; directories fail closed below
                            except OSError:
                                pass
                    time.sleep(0.01)
                for src, archived in restored:
                    if _present(_archive_temp(archived)):
                        failures.append(f"{src.name}.tmp")
                return failures

            try:
                for name in names:
                    src = rd / name
                    if _present(src):
                        archived = rd / f"{name}.reset-{stamp}"
                        src.rename(archived)
                        moved.append((src, archived))
            except OSError as exc:
                rollback_failures = _rollback_reset_archive()
                suffix = (f"; rollback also failed for {rollback_failures}"
                          if rollback_failures else "")
                raise HTTPException(
                    500, f"could not archive run for Replay: {exc}{suffix}") from exc
            try:
                # Reuse the run's resolved settings (minus secrets); the API key remains inherited.
                # Keep preparation inside the transaction too: a malformed/non-object snapshot or
                # environment conversion failure must restore the archived run just like Popen does.
                env: Optional[dict] = None
                snap = rd / "config.snapshot.json"
                if snap.exists():
                    try:
                        cfg = json.loads(snap.read_text(encoding="utf-8"))
                        if isinstance(cfg, dict):
                            env = srv.settings.settings_env({
                                k: v for k, v in cfg.items()
                                if k in _ALLOWED_FIELDS and k not in _SECRET_FIELDS and v is not None
                            })
                    except (OSError, json.JSONDecodeError, ValueError):
                        env = None
                _spawn_engine(["run", str(task_file), "--out", str(rd)], env=env, run_dir=rd)
            except Exception as exc:  # noqa: BLE001 - failed Popen must restore the old runnable run
                rollback_failures = _rollback_reset_archive()
                suffix = (f"; rollback also failed for {rollback_failures}"
                          if rollback_failures else "")
                raise HTTPException(
                    500, f"could not launch Replay: {exc}{suffix}") from exc
        return {"ok": True}

    @router.post("/api/runs/{run_id}/nodes/{nid}/clear_trace")
    def clear_node_trace(run_id: str, nid: int):
        """Erase ONE node's spans from spans.jsonl — the "clear this node's trace" button. spans.jsonl
        is append-only, so after a node_reset the rebuild would otherwise STACK its fresh bands on top
        of the old attempt's (build_conversation shows every trace tagged with the node). This removes
        the node's spans so only the next build's trace remains. REFUSED while the engine is live — it
        is the sole writer of spans.jsonl and rewriting the file under it would race/corrupt the trace;
        stop the run first. Non-destructive to the event log (events.jsonl, the source of truth, is
        untouched) — only the diagnostics trace is dropped."""
        rd = _run_dir(run_id)
        if _engine_alive(rd):
            raise HTTPException(409, "run is live — stop it first (the engine is writing spans.jsonl)")
        sp = rd / "spans.jsonl"
        if not sp.exists():
            return {"ok": True, "removed": 0, "kept": 0}
        from looplab.events.eventstore import iter_jsonl
        # A span belongs to the node when its node_id (stamped on EVERY span in the node's traces via
        # tracing._node_ctx) matches — str-compared, since old logs may carry it as a string. iter_jsonl
        # tolerates a torn final line (dropped); every surviving span is re-serialized compactly.
        kept, removed = [], 0
        for o in iter_jsonl(sp):
            if str((o.get("attributes") or {}).get("node_id")) == str(nid):
                removed += 1
            else:
                kept.append(o)
        if removed:
            # Atomic temp+rename so a crash mid-write can't truncate spans.jsonl to a partial trace.
            write_jsonl_atomic(sp, kept)
        return {"ok": True, "removed": removed, "kept": len(kept)}

    @router.post("/api/start")
    async def start_run(request: Request):
        body = await request.json()
        run_id = body.get("run_id")
        if not run_id:
            raise HTTPException(400, "run_id is required")
        rd = (root / run_id).resolve()
        # A run id must resolve to a DIRECT child of the runs root: this rejects "." (which would make
        # the runs root itself a run — an invisible ghost engine writing events.jsonl at the root),
        # nested "a/b" (invisible to the run list), and traversal. Check the RESERVED names against the
        # resolved directory name, so "./reports" can't sneak into the cross-run report store.
        if rd.parent != root or rd == root:
            raise HTTPException(400, "bad run_id (must be a plain name, not a path)")
        # Case-INSENSITIVE (arch-review §5 P2): on a case-insensitive FS (Windows/macOS default) an
        # `ASSISTANT` run dir aliases the reserved `assistant` service store, so compare lowercased.
        if rd.name.lower() in _RESERVED_RUN_IDS:   # don't let a run clobber the report/assistant stores
            raise HTTPException(400, f"run_id {rd.name!r} is reserved")
        if (rd / "events.jsonl").exists():
            raise HTTPException(409, f"run {run_id!r} already exists — pick another id")
        task_file = body.get("task_file")
        task = body.get("task")
        # Inline task (the genesis flow authors one): a COMPOSABLE spec needs no `kind` — the capability
        # fields (repo/dataset/cmd/kaggle) infer it. VALIDATE it the same way the engine will
        # (validate_task → normalize + model_validate) BEFORE materializing anything — so a bad spec
        # (unknown kind, mlebench_real with an unknown/empty competition, a missing required field, an
        # uninferrable kind-less dict) fails HERE with a 400 instead of spawning a detached engine that
        # dies (DEVNULL'd) before writing any events, leaving a phantom never-started run.
        if isinstance(task, dict) and task:
            from looplab.adapters.tasks import validate_task
            # A COMPOSABLE task carries no `kind` — validate_task normalizes (inferring the kind from
            # repo/dataset/cmd/kaggle) and validates in ONE guarded call, so every malformed spelling
            # (a string `cmd`, an unknown kind, an uninferrable task) is a 400 with the validator's
            # message — never an unhandled 500 from a pre-check outside the try (mega-review fix; a
            # separate normalize_task pre-check also normalized the same dict twice).
            try:
                adapter = await anyio.to_thread.run_sync(lambda: validate_task(task))
            except HTTPException:
                raise
            except Exception as e:  # noqa: BLE001 - normalization/validation failed (e.g. bad competition)
                raise HTTPException(400, f"invalid task: {e}")
            kind = getattr(adapter, "kind", "")
            # Repo task: the editable repo must actually exist on THIS machine at submit time (the
            # model can't check that — a snapshot loads on any host). A relative/missing path would
            # otherwise only surface as a warning, then fail deep in materialize. Reject it now.
            if kind == "repo":
                ep = getattr(adapter, "editable_path", "") or ""
                if ep and not Path(ep).exists():
                    raise HTTPException(400, f"editable_path does not exist: {ep!r} — point it at the "
                                             "repo to edit (an ABSOLUTE path, e.g. /home/jovyan/data/…).")
            rd.mkdir(parents=True, exist_ok=True)
            task_file = str(rd / "task.input.json")
            Path(task_file).write_text(json.dumps(task, indent=2), encoding="utf-8")
        if not task_file:
            raise HTTPException(400, "task_file or task is required")
        if not Path(task_file).exists():
            raise HTTPException(400, f"task file not found: {task_file}")
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "ui_meta.json").write_text(json.dumps({"task_file": str(task_file)}), encoding="utf-8")
        # Carry the GENESIS conversation (the chat-first creation flow, where the boss planned this run)
        # into the run's saved chat, so that planning becomes the OPENING history of the run's chat.jsonl
        # instead of vanishing the moment the run launches. Stamp each turn with the creation time (which
        # is < the engine's run_started ts) so it sorts at the TOP of the run's chat feed, and a
        # chat-range seq so the Dock renders it as a conversation turn (not an engine event).
        seed_chat = body.get("chat")
        if isinstance(seed_chat, list) and seed_chat:
            t0 = time.time()
            with open(rd / "chat.jsonl", "ab") as f:
                for i, m in enumerate(seed_chat):
                    if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
                        continue
                    f.write(orjson.dumps({
                        "role": m["role"], "content": str(m.get("content", "")),
                        "ts": t0 + i * 1e-3, "seq": GENESIS_CHAT_SEQ_BASE + i, "genesis": True}) + b"\n")
                f.flush()
                best_effort_fsync(f.fileno())   # FUSE/S3 fsync may raise — don't fail the launch
        # Per-run settings = the saved UI defaults overlaid with whatever the launch dialog set.
        # Everything reaches the engine as LOOPLAB_* env on the spawned process, so ANY Settings
        # field is configurable from the UI without growing the CLI surface (Settings() reads env).
        # Bind the saved defaults ONCE: the merge and the backend predicate below must see the SAME
        # store read (and a second disk read per launch bought nothing).
        ui = srv.settings.load_ui_settings()
        settings = {**ui, **(body.get("settings") or {})}
        # F4 (CLI parity, mega-review P10): /api/start is the ONE launch funnel — genesis cards, the
        # assistant's propose_run cards, and direct API callers all land here — so the generative-kind
        # backend default is applied HERE, not per-card. Without it a repo/dataset task launched with
        # the default Settings.backend="toy" gets NoOpRepoDeveloper: every node silently re-evaluates
        # the unchanged baseline (no error, a flat run). cli.py's genesis path already defaults
        # backend=llm for GENERATIVE_KINDS; this closes the HTTP gap for ALL launches (the genesis
        # card's own injection is display-only sugar over this same rule). The predicate itself owns
        # the "backend already chosen" / non-dict-task guards — no caller-side pre-checks.
        if _defaults_backend_llm(task, task_file, settings, ui):
            settings["backend"] = "llm"
        # Drop fields that equal what the chosen profile resolves to anyway: the launch dialog
        # echoes back EVERY resolved field, and passing them all as explicit LOOPLAB_* env would
        # defeat `_apply_profile`'s "explicit key wins" check in the child — selecting a profile
        # in the dialog would be a complete no-op.
        try:
            prof_defaults = Settings(profile=settings.get("profile") or "default").model_dump()
            settings = {k: v for k, v in settings.items()
                        if k == "profile" or prof_defaults.get(k, object()) != v}
        except Exception:  # noqa: BLE001 — an invalid profile fails later, in Settings validation
            pass
        env = srv.settings.settings_env(settings)
        _spawn_engine(["run", str(task_file), "--out", str(rd)], env=env, run_dir=rd)
        return {"ok": True, "run_id": run_id}

    return router
