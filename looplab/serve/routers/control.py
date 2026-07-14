"""Control-plane routes: append control intents (/control) and spawn/resume/reset/start engine
processes. Handler bodies are verbatim moves from `serve/server.py::make_app` (BACKLOG §4)."""
from __future__ import annotations

import json
import math
import os
import secrets
import stat
import time
from pathlib import Path
from typing import Optional

import anyio
import orjson
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from looplab.serve import engine_proc as _engine_proc
from looplab.core.atomicio import atomic_write_bytes, atomic_write_text
from looplab.core.config import Settings
from looplab.events.eventstore import EventStore, EventStoreConcurrencyError, write_jsonl_atomic
from looplab.events.replay import fold
from looplab.events.types import EV_APPROVAL_GRANTED, EV_RESUME_REQUESTED, EV_SPEC_APPROVED
from looplab.serve.appstate import _RESERVED_RUN_IDS
from looplab.serve.engine_proc import (
    _claim_and_spawn_resume, _clear_run_launching, _engine_alive, _engine_liveness,
    _fresh_resume_launch_pending, _fresh_run_launch_pending, _mark_run_launching,
    _resolve_task_file, _run_lifecycle_lock)
from looplab.serve.launch import (
    idempotency_key_digest,
    launch_request_digest,
    preflight_response,
    preflight_start,
    safe_run_dir,
    validate_idempotency_key,
)
from looplab.serve.protocol import (
    CONTROL_EVENTS, EXPECTED_RUN_GENERATION_FIELD, GENESIS_CHAT_SEQ_BASE)
from looplab.serve.run_commands import normalize_control
from looplab.serve.settings_store import _ALLOWED_FIELDS, _SECRET_FIELDS


def _spawn_engine(*args, **kwargs):
    """Late-bound compatibility seam for patches on either this router or engine_proc."""
    return _engine_proc._spawn_engine(*args, **kwargs)


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
    file_settings: dict = {}
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
            task_spec, file_settings, _out = load_document(Path(task_file))
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
        # A unified task file's settings outrank UI/env defaults in the CLI. Treat its backend as an
        # explicit choice too, so the display-only Genesis hint cannot promise llm while the child
        # would actually consume backend=toy from that file.
        selected = {**(ui_settings or {}), **(file_settings or {})}
        return "backend" not in getattr(Settings(**selected), "model_fields_set", set())
    except ValueError:  # pydantic ValidationError ⊂ ValueError — bad saved/env settings fail later,
        return False    # in the spawned engine's own Settings(); don't inject on top of them


def build_router(srv) -> APIRouter:
    router = APIRouter()
    _run_dir, root = srv.run_dir, srv.root

    def _known_engine_liveness(rd: Path, operation: str) -> bool:
        """Return a real lock verdict; unknown ownership cannot authorize a mutation/Popen."""
        liveness = _engine_liveness(rd)
        if liveness is None:
            raise HTTPException(409, {
                "code": "engine_liveness_unknown",
                "message": f"Cannot {operation} because engine ownership is unknown.",
                "remediation": (
                    "Inspect engine.lock and storage locking, then retry only after liveness "
                    "is verifiable."),
                "retryable": True,
            })
        return liveness

    # ------------------------------------------------------------------ control
    @router.post("/api/runs/{run_id}/control")
    async def control(run_id: str, request: Request):
        rd = _run_dir(run_id)
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "control body must be valid JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "control body must be a JSON object")

        def _append_control() -> dict:
            # Offloaded to a worker thread: ``sequence`` takes the cross-process flock (blocking up to
            # ``lock_acquire_timeout``) and the append does disk I/O — holding that on the ASGI event
            # loop freezes every concurrent SSE/poll in the worker. Same offload the start/preflight
            # handlers already use.
            with srv.commands.sequence(rd):
                local_rd = srv.commands.validate_paths(rd)
                srv.commands.reject_if_active(local_rd, "append a legacy control event")
                etype = body.get("type")
                if etype not in CONTROL_EVENTS:
                    raise HTTPException(400, f"unknown control event: {etype!r}")
                # Approval decisions are valid only for the exact gate the normalizer folded. If the
                # caller omitted an explicit CAS, bind the append to that pre-normalization tail so a
                # replacement approval request cannot be accepted by this legacy endpoint.
                gated_baseline = None
                if etype in {EV_APPROVAL_GRANTED, EV_SPEC_APPROVED}:
                    events = srv.events(local_rd)
                    gated_baseline = events[-1].seq if events else -1
                # One shared normalizer owns strict payload validation plus node-attempt and parent
                # generation CAS. Pass the raw data intact so attempt>0 tokens are never erased here.
                data = normalize_control(srv, local_rd, etype, body.get("data"))
                _known_engine_liveness(local_rd, "append a control event")
                expected = body.get("expected_seq")
                if expected is None and gated_baseline is not None:
                    expected = gated_baseline
                if expected is not None:
                    if isinstance(expected, bool):
                        raise HTTPException(400, "expected_seq must be an integer")
                    try:
                        expected = int(expected)
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise HTTPException(400, "expected_seq must be an integer") from exc
                try:
                    ev = EventStore(local_rd / "events.jsonl").append(
                        etype, data, expected_last_seq=expected)
                except EventStoreConcurrencyError as exc:
                    raise HTTPException(409, str(exc)) from exc
            return {"ok": True, "seq": ev.seq, "type": etype}

        return await anyio.to_thread.run_sync(_append_control)

    # ------------------------------------------------------------------ authoritative command lifecycle
    def _command_response_headers(response: Response) -> None:
        # These records transition asynchronously. A browser/proxy cache of ``accepted`` would freeze
        # polling forever, and token-scoped deployments must never share one owner's record response.
        response.headers["Cache-Control"] = "no-store"
        response.headers["Vary"] = "X-LoopLab-Token, Authorization"

    @router.post("/api/runs/{run_id}/commands")
    async def submit_command(run_id: str, request: Request, response: Response):
        _command_response_headers(response)
        rd = _run_dir(run_id)
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "command body must be valid JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "command body must be a JSON object")
        idem = request.headers.get("Idempotency-Key", "")
        # submit() takes the run flock and folds the log — offload so it never blocks the event loop.
        return await anyio.to_thread.run_sync(lambda: srv.commands.submit(
            rd, idem, body.get("type"), body.get("data"),
            expected_generation=body.get(EXPECTED_RUN_GENERATION_FIELD)))

    @router.get("/api/runs/{run_id}/commands/{command_id}")
    def get_command(run_id: str, command_id: str, response: Response):
        _command_response_headers(response)
        return srv.commands.get(_run_dir(run_id), command_id)

    @router.post("/api/runs/{run_id}/commands/{command_id}/retry")
    def retry_command(run_id: str, command_id: str, response: Response):
        _command_response_headers(response)
        return srv.commands.retry(_run_dir(run_id), command_id)

    @router.post("/api/runs/{run_id}/resolve-activity-claims")
    async def resolve_activity_claims(run_id: str, request: Request, response: Response):
        """Guarded operator recovery for an ownership claim that cannot be proven dead."""
        _command_response_headers(response)
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "resolve-activity-claims body must be valid JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "resolve-activity-claims body must be a JSON object")
        rd = _run_dir(run_id)
        confirmation = str(body.get("confirmation") or "")
        return await anyio.to_thread.run_sync(
            lambda: srv.commands.resolve_active_claims(rd, confirmation))

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
        # The command sequencer excludes authoritative command workers while the lifecycle lock
        # serializes this durable handoff with reset/delete and the resume reconciler.
        with srv.commands.sequence(rd):
            rd = srv.commands.validate_paths(rd)
            srv.commands.reject_if_active(
                rd, "resume through the legacy endpoint", allow_incomplete_finalize=True)
            task_file = _task_file_for(rd)
            if not task_file:
                raise HTTPException(
                    400, "run is not resumable — no task.snapshot.json or ui_meta.json "
                         "(it predates self-describing runs; start it via the UI to enable resume)")
            with _run_lifecycle_lock(rd):
                known_alive = _known_engine_liveness(rd, "resume the run")
                # Durable before every liveness branch: a current owner in its final tail, or a
                # detached child that dies before engine.lock, leaves a recoverable intent.
                mode = _append_resume_request(rd)
            cli_args = (
                ["finalize", str(rd), "--task-file", str(task_file)]
                if mode == "finalize"
                else ["resume", str(rd), "--task-file", str(task_file)])
            # Preserve the historical monkeypatch seam while the production verdict remains the
            # exact tri-state result captured under the lifecycle fence.
            was_alive = known_alive or _engine_alive(rd)
            # Mirror the launch into the command service's pre-lock lease so command-aware callers
            # also fail closed during Popen→engine.lock. The event-log claim stays authoritative.
            srv.commands.begin_external_spawn(rd, "legacy-resume")
            popen_returned = False

            def _record_spawn(pid: Optional[int]) -> None:
                nonlocal popen_returned
                # Mark the Popen boundary before persisting the PID. If persistence fails, the child
                # may be live and the PID-less preclaim must remain as duplicate-spawn quarantine.
                popen_returned = True
                srv.commands.record_external_spawn(rd, "legacy-resume", pid)

            try:
                spawned = _claim_and_spawn_resume(
                    rd, cli_args, cancel_event=srv.resume_cancel, wait_on_alive=True,
                    spawn_engine=_spawn_engine, on_spawn=_record_spawn)
            except BaseException:
                if not popen_returned:
                    srv.commands.cancel_external_spawn(rd, "legacy-resume")
                raise
            if not spawned:
                # A live owner/post-exit waiter is fenced by the durable resume claim instead.
                srv.commands.cancel_external_spawn(rd, "legacy-resume")
            if was_alive and not spawned:
                return {"ok": True, "already_running": True, "resume_after_exit": True}
            return {"ok": True, "launch_pending": not spawned}

    @router.post("/api/runs/{run_id}/reset")
    def reset_run(run_id: str):
        """round-7 "Replay": reset a run IN PLACE — archive its event log + spans + node workspaces and
        re-spawn a fresh run on the same run-id. The prior artifacts are RENAMED (not deleted) so the
        history is recoverable."""
        rd = _run_dir(run_id)
        # The command sequencer protects command-aware work/current spawn leases; the lifecycle
        # lock additionally serializes durable resume reconciliation and CLI-compatible launch
        # markers. Keep this lock order (command → lifecycle) everywhere to avoid inversion.
        with srv.commands.destructive_guard(rd, "reset run") as rd:
            with _run_lifecycle_lock(rd):
                known_alive = _known_engine_liveness(rd, "reset the run")
                if (known_alive or _engine_alive(rd) or _fresh_resume_launch_pending(rd)
                        or _fresh_run_launch_pending(rd)
                        or not srv.state(rd).finished):
                    raise HTTPException(
                        409, "run is still active or launching — stop it first "
                             "(Replay resets a finished run)")
                task_file = _task_file_for(rd)
                if not task_file:
                    raise HTTPException(
                        400, "run is not resettable — no task.snapshot.json or ui_meta.json")

                flush_durable_costs = getattr(srv, "flush_durable_run_costs", None)
                if not callable(flush_durable_costs):
                    raise HTTPException(
                        503, "cannot reset run: durable run-cost recovery is unavailable")
                try:
                    durable_costs_flushed = flush_durable_costs(rd)
                except Exception as exc:  # noqa: BLE001 - fail closed on unknown evidence
                    raise HTTPException(
                        503, "cannot reset run: durable run-cost recovery failed") from exc
                if durable_costs_flushed is not True:
                    raise HTTPException(
                        409, "cannot reset run: run-cost evidence is pending, busy, "
                             "malformed, or conflicting")

                def _outbox_archiveable(path: Path) -> bool:
                    """Validate the entry itself; only true absence or a real directory is safe."""
                    try:
                        entry = path.lstat()
                    except FileNotFoundError:
                        return False
                    except OSError as exc:
                        raise HTTPException(
                            409, "cannot reset run: run-cost outbox metadata is inaccessible") from exc
                    try:
                        if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
                            raise HTTPException(
                                409, "cannot reset run: run-cost outbox is a symlink or "
                                     "reparse point or is not a directory")
                        is_junction = getattr(path, "is_junction", None)
                        if callable(is_junction) and is_junction():
                            raise HTTPException(
                                409, "cannot reset run: run-cost outbox is a junction/reparse point")
                    except HTTPException:
                        raise
                    except OSError as exc:
                        raise HTTPException(
                            409, "cannot reset run: run-cost outbox type is inaccessible") from exc
                    return True

                outbox_path = rd / ".llm-usage-outbox"
                _outbox_archiveable(outbox_path)
                # Archive auxiliaries first and the event source of truth last. Command/start records
                # deliberately survive so lost-response idempotency never re-applies to generation B.
                names = (
                    ".llm-usage-outbox", "spans.jsonl", "spans.index.jsonl",
                    "readmodel.sqlite-wal", "readmodel.sqlite-shm", "readmodel.sqlite",
                    "nodes", "chat.jsonl", "events.jsonl",
                )

                def _present(path: Path) -> bool:
                    return os.path.lexists(path)

                def _archive_temp(archived: Path) -> Path:
                    return archived.with_name(f"{archived.name.upper()}.tmp")

                stamp = int(time.time() * 1000)
                while any(
                        _present(candidate)
                        for name in names
                        for candidate in (
                            rd / f"{name}.reset-{stamp}",
                            _archive_temp(rd / f"{name}.reset-{stamp}"))):
                    stamp += 1
                moved: list[tuple[Path, Path]] = []

                def _rollback_archives() -> list[str]:
                    failures: list[str] = []
                    restored: list[tuple[Path, Path]] = []
                    for original, archived in reversed(moved):
                        try:
                            if _present(archived):
                                archived.replace(original)
                            elif not _present(original):
                                failures.append(original.name)
                            if _present(original) and not _present(archived):
                                restored.append((original, archived))
                        except OSError:
                            failures.append(original.name)
                    # A Windows/network layer may publish an implementation-owned, case-variant
                    # shadow after replace returns. Stamp collision checks prove this exact name did
                    # not pre-exist, so remove only the temp derived from entries this transaction
                    # successfully restored; never glob or touch an older approved archive.
                    deadline = time.monotonic() + 0.1
                    while restored and time.monotonic() < deadline:
                        for _original, archived in restored:
                            temp = _archive_temp(archived)
                            if _present(temp):
                                try:
                                    temp.unlink()  # files/symlinks only; directories fail closed below
                                except OSError:
                                    pass
                        time.sleep(0.01)
                    for original, archived in restored:
                        if _present(_archive_temp(archived)):
                            failures.append(f"{original.name}.tmp")
                    return failures

                try:
                    for name in names:
                        source = rd / name
                        if not _present(source):
                            continue
                        if name == ".llm-usage-outbox":
                            _outbox_archiveable(source)
                        archived = rd / f"{name}.reset-{stamp}"
                        source.rename(archived)
                        moved.append((source, archived))
                except (OSError, HTTPException) as exc:
                    rollback_failures = _rollback_archives()
                    detail = f"could not archive run for Replay; no engine was started: {exc}"
                    if rollback_failures:
                        detail += f"; rollback also failed for {rollback_failures}"
                    raise HTTPException(500, detail) from exc

                # The identity signature already prevents reuse across Replay.  Evict explicitly too:
                # a light trace for a large run can occupy hundreds of MB and must not linger until the
                # replacement engine has written enough state for the next trace request.
                srv.invalidate_trace_view(rd)

                env: Optional[dict] = None
                snap = rd / "config.snapshot.json"
                if snap.exists():
                    try:
                        cfg = json.loads(snap.read_text(encoding="utf-8"))
                        if isinstance(cfg, dict):
                            env = srv.settings.settings_env({
                                key: value for key, value in cfg.items()
                                if key in _ALLOWED_FIELDS and key not in _SECRET_FIELDS
                                and value is not None
                            })
                    except (OSError, json.JSONDecodeError, ValueError):
                        env = None

                spawned = False
                popen_attempted = False
                try:
                    # Stamp both pre-lock launch fences before Popen while both serializers are held.
                    _mark_run_launching(rd)
                    srv.commands.begin_external_spawn(rd, "reset")
                    popen_attempted = True
                    pid = _spawn_engine(
                        ["run", str(task_file), "--out", str(rd)], env=env, run_dir=rd)
                    spawned = True
                    srv.commands.record_external_spawn(rd, "reset", pid)
                except BaseException as exc:
                    if not spawned:
                        _clear_run_launching(rd)
                        try:
                            srv.commands.cancel_external_spawn(rd, "reset")
                        finally:
                            rollback_failures = _rollback_archives()
                        if rollback_failures:
                            raise HTTPException(
                                500, "could not launch Replay; rollback also failed for "
                                     f"{rollback_failures}")
                        if popen_attempted and isinstance(exc, Exception):
                            raise HTTPException(
                                500, f"could not launch Replay: {exc}") from exc
                    # After Popen, never roll archives back beneath a possibly-live child. The
                    # preclaim/marker intentionally remain fail-closed if PID persistence failed.
                    raise
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
        with srv.commands.destructive_guard(rd, "clear node trace") as rd:
            # Re-check inside the command sequencer: a pending worker must not Popen between the
            # liveness probe and the whole-file atomic rewrite.
            known_alive = _known_engine_liveness(rd, "clear the node trace")
            if known_alive or _engine_alive(rd):
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
                # The persisted index stores byte offsets into spans.jsonl, so any rewrite invalidates
                # both its in-memory and on-disk representations. Rebuild lazily on the next trace read.
                from looplab.events.span_index import invalidate
                invalidate(sp)
                (rd / "spans.index.jsonl").unlink(missing_ok=True)
                srv.invalidate_trace_view(rd)
            return {"ok": True, "removed": removed, "kept": len(kept)}

    def _start_public(record: dict) -> dict:
        status = str(record.get("status") or "uncertain")
        # ``accepted`` proves only that Popen returned and its ownership evidence was persisted.  The
        # child is positively started only once its exact PID generation, engine lock, or run_started
        # event is observed.  Likewise, never advertise retry while a paid effect may have escaped.
        started = status in {"executing", "succeeded"}
        paid_effect_unknown = bool(record.get("paid_effect_unknown"))
        can_retry = status in {"not_started", "failed"} and not paid_effect_unknown
        result = {
            "ok": status in {"accepted", "executing", "succeeded"},
            "run_id": str(record.get("run_id") or ""),
            "start_id": str(record.get("id") or ""),
            "status": status,
            "started": started,
            "can_retry": can_retry,
            "paid_effect_unknown": paid_effect_unknown,
        }
        if record.get("validation_token"):
            result["validation_token"] = str(record["validation_token"])
        if record.get("error_code"):
            result["error"] = {"code": str(record["error_code"])}
        return result

    def _start_meta_id(rd: Path) -> str:
        path = rd / "ui_meta.json"
        if path.is_symlink():
            return ""
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return ""
        return str(value.get("start_id") or "") if isinstance(value, dict) else ""

    def _has_first_run_started(rd: Path) -> bool:
        """Whether the first identity event is a durable, correlated ``run_started``.

        Current engines durably emit ``setup_started``/``setup_step`` immediately before their
        identity anchor; older valid engines emitted ``run_started`` at sequence zero.  Accept both
        layouts, but fail closed on a torn line, a malformed/unsupported envelope, a sequence gap,
        an unrelated pre-identity event, or a run id that does not name this exact directory.  A
        merely parseable ``{"type": "run_started"}`` is not process evidence.
        """
        path = rd / "events.jsonl"
        if path.is_symlink():
            return False
        try:
            with path.open("rb") as stream:
                expected_seq = 0
                total_bytes = 0
                for _ in range(4096):
                    raw = stream.readline(1_048_577)
                    if not raw:
                        return False
                    total_bytes += len(raw)
                    if (len(raw) > 1_048_576 or total_bytes > 4_194_304
                            or not raw.endswith(b"\n") or not raw.strip()):
                        return False
                    event = orjson.loads(raw)
                    if not isinstance(event, dict):
                        return False
                    version = event.get("v")
                    seq = event.get("seq")
                    ts = event.get("ts")
                    event_type = event.get("type")
                    data = event.get("data")
                    if (type(version) is not int or version != 1
                            or type(seq) is not int or seq != expected_seq
                            or isinstance(ts, bool) or not isinstance(ts, (int, float))
                            or not math.isfinite(ts) or ts <= 0
                            or not isinstance(event_type, str)
                            or not isinstance(data, dict)):
                        return False
                    expected_seq += 1
                    if event_type == "run_started":
                        run_id = data.get("run_id")
                        return isinstance(run_id, str) and run_id == rd.name
                    if event_type not in {"setup_started", "setup_step"}:
                        return False
                return False
        except (OSError, ValueError, TypeError, orjson.JSONDecodeError):
            return False

    def _reconcile_start(rd: Path, record: dict) -> tuple[dict, dict]:
        """Fold durable run/claim evidence into one observational startup state.

        Callers hold ``commands.sequence(rd)``. This function may retire an observed/dead spawn
        claim through the command service, but never creates a directory, lease, event, or process.
        """
        updated = dict(record)
        start_id = str(updated.get("id") or "")
        meta_matches = _start_meta_id(rd) == start_id
        liveness = _engine_liveness(rd)

        def transition(**changes) -> None:
            # Stable polling must be observational: publish a new timestamp only for an actual state
            # transition, not on every GET of the same evidence.
            if any(updated.get(key) != value for key, value in changes.items()):
                updated.update(changes)
                updated["updated_at"] = time.time()

        if meta_matches and _has_first_run_started(rd):
            transition(status="succeeded", phase="event_observed", paid_effect_unknown=False,
                       error_code=None)
        elif meta_matches and (liveness is True
                               or (liveness is False and _engine_alive(rd))):
            transition(status="executing", phase="engine_observed", paid_effect_unknown=False,
                       error_code=None)
        elif str(updated.get("phase") or "") in {
                "popen_pending", "popen_returned", "engine_observed"}:
            evidence = srv.commands.observe_external_spawn(rd, f"start:{start_id}")
            # A start_id in ui_meta is the durable correlation between this sidecar and this run
            # directory.  An engine lock without it may belong to a manually replaced incarnation.
            if meta_matches and evidence in {"live", "pending_known"}:
                transition(status="executing", paid_effect_unknown=False, error_code=None)
            elif not meta_matches or evidence in {"uncertain", "mismatched"}:
                transition(status="uncertain", paid_effect_unknown=True,
                           error_code="start_uncertain")
            else:
                # Popen may already have crossed the provider boundary before dying. A new explicit
                # launch is possible only after review/revalidation; never call it automatically.
                transition(status="failed", phase="failed_after_spawn",
                           paid_effect_unknown=True, error_code="start_failed_after_spawn")
        elif str(updated.get("phase") or "") in {"reserved", "materialized"}:
            evidence = srv.commands.observe_external_spawn(rd, f"start:{start_id}")
            if evidence in {"absent", "dead_or_cleared"}:
                transition(status="not_started", paid_effect_unknown=False, error_code=None)
            else:
                transition(status="uncertain", paid_effect_unknown=True,
                           error_code="start_uncertain")
        if updated != record:
            srv.commands.save_start_record(rd, updated)
        return updated, _start_public(updated)

    def _inspect_keyed_start(rd: Path, key_digest: str, request_digest: str):
        record = srv.commands.load_start_record(rd)
        if record is None:
            return None, None, False
        same_key = secrets.compare_digest(
            str(record.get("idempotency_key_digest") or ""), key_digest)
        if same_key and not secrets.compare_digest(
                str(record.get("request_digest") or ""), request_digest):
            raise HTTPException(409, {
                "code": "idempotency_key_reused",
                "message": "this idempotency key belongs to a different launch request",
                "field_errors": {"idempotency_key": "generate a new key for the edited proposal"},
            })
        reconciled, public = _reconcile_start(rd, record)
        return reconciled, public, same_key

    def _raise_existing_start(public: dict, *, same_key: bool) -> None:
        status = str(public.get("status") or "uncertain")
        if not same_key:
            raise HTTPException(409, {
                "code": "run_id_conflict",
                "message": "this run name is already owned by another startup",
                "start_id": public.get("start_id"),
                "field_errors": {"run_id": "choose another run name"},
                "remediation": "Use the card that owns the existing startup, or choose another name.",
            })
        if same_key and status in {"accepted", "executing", "succeeded"}:
            return
        if status == "uncertain":
            raise HTTPException(409, {
                "code": "start_uncertain",
                "message": "the earlier startup may have crossed Popen; observe it before retrying",
                "start_id": public.get("start_id"),
                "remediation": "Use the startup status endpoint; do not submit another launch.",
            })
        if same_key:
            raise HTTPException(409, {
                "code": "start_not_completed",
                "message": "this startup did not establish a run",
                "start_id": public.get("start_id"),
                "remediation": "Review provider/error evidence, then validate again before a new launch.",
            })

    @router.post("/api/start/{run_id}/resolve-claim")
    async def resolve_start_claim(run_id: str, request: Request, response: Response):
        """Operator recovery for a crash-window claim whose child identity cannot be proven."""
        _command_response_headers(response)
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "resolve-claim body must be valid JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "resolve-claim body must be a JSON object")
        rd = (root / run_id).resolve()
        if rd == root or rd.parent != root or rd.name.lower() in _RESERVED_RUN_IDS:
            raise HTTPException(400, "bad run_id")
        confirmation = str(body.get("confirmation") or "")
        return await anyio.to_thread.run_sync(
            lambda: srv.commands.resolve_spawn_claim(rd, confirmation))

    @router.get("/api/start/{run_id}/status")
    def start_status(run_id: str, request: Request, response: Response,
                     idempotency_key: str | None = None):
        """Observe one exact durable startup. GET never launches or resumes an engine."""
        response.headers["Cache-Control"] = "no-store"
        response.headers["Vary"] = "X-LoopLab-Token, Authorization, Idempotency-Key"
        raw_header_key = request.headers.get("Idempotency-Key")
        header_key = (validate_idempotency_key(raw_header_key)
                      if raw_header_key is not None else None)
        query_key = (validate_idempotency_key(idempotency_key)
                     if idempotency_key is not None else None)
        if (header_key is not None and query_key is not None
                and not secrets.compare_digest(
                    idempotency_key_digest(header_key), idempotency_key_digest(query_key))):
            raise HTTPException(400, {
                "code": "idempotency_key_mismatch",
                "message": "Idempotency-Key header and query parameter disagree",
                "field_errors": {"idempotency_key": "send one exact startup key"},
            })
        key = header_key if header_key is not None else query_key
        if key is None:
            raise HTTPException(400, {
                "code": "invalid_idempotency_key",
                "message": "Idempotency-Key header is required",
                "field_errors": {"idempotency_key": "send the startup observation key"},
            })
        rd = safe_run_dir(root, run_id, check_conflict=False)
        digest = idempotency_key_digest(key)
        with srv.commands.sequence(rd):
            record = srv.commands.load_start_record(rd)
            if record is None or not secrets.compare_digest(
                    str(record.get("idempotency_key_digest") or ""), digest):
                raise HTTPException(404, {
                    "code": "start_not_found",
                    "message": "no startup is recorded for this run name and idempotency key",
                })
            _record, public = _reconcile_start(rd, record)
        return public

    @router.post("/api/start/preflight")
    async def start_preflight(request: Request):
        """Validate and resolve a launch without writing, reserving a name, or starting an engine."""
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, {
                "code": "invalid_launch_request",
                "message": "start body must be valid JSON",
                "field_errors": {},
            }) from exc
        return preflight_response(await anyio.to_thread.run_sync(lambda: preflight_start(srv, body)))

    @router.post("/api/start")
    async def start_run(request: Request):
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, {
                "code": "invalid_launch_request", "message": "start body must be valid JSON",
                "field_errors": {},
            }) from exc
        if not isinstance(body, dict):
            raise HTTPException(400, {
                "code": "invalid_launch_request", "message": "start body must be a JSON object",
                "field_errors": {},
            })

        key = validate_idempotency_key(body.get("idempotency_key"))
        key_digest = idempotency_key_digest(key) if key else ""
        request_digest = launch_request_digest(body) if key else ""
        rd = safe_run_dir(root, body.get("run_id"), check_conflict=False)

        # Lost-response replay is resolved before rereading mutable sources/defaults or rejecting the
        # now-owned run name. The request digest contains effects, never the raw idempotency key.
        if key:
            with srv.commands.sequence(rd):
                record, public, same_key = _inspect_keyed_start(rd, key_digest, request_digest)
                if record is not None:
                    if same_key and public["status"] in {"accepted", "executing", "succeeded"}:
                        return JSONResponse(public)
                    if same_key or public["status"] not in {"not_started", "failed"}:
                        _raise_existing_start(public, same_key=same_key)

        plan = await anyio.to_thread.run_sync(lambda: preflight_start(srv, body))
        submitted_token = body.get("validation_token") or ""
        if key and not submitted_token:
            raise HTTPException(409, {
                "code": "launch_validation_required",
                "message": "validate this exact launch proposal before starting it",
                "field_errors": {"validation_token": "run the free preflight first"},
            })
        if submitted_token and submitted_token != plan.validation_token:
            raise HTTPException(409, {
                "code": "launch_validation_stale",
                "message": "the launch draft changed after it was validated",
                "field_errors": {"validation_token": "validate the current draft again"},
            })

        run_id = plan.run_id
        requested_rd = root / run_id
        task_file = rd / "task.input.json"
        # The canonical unified file carries every resolved setting. Keep the process environment to
        # actual deviations from this server's Settings baseline so profile/default provenance and
        # legacy non-generative launches are not turned into explicit overrides accidentally.
        base_settings = Settings().model_dump(mode="json")
        base_settings.pop("llm_api_key", None)
        env = srv.settings.settings_env({
            setting: value for setting, value in plan.effective_settings.items()
            if base_settings.get(setting, object()) != value
        })

        start_result = None
        with srv.commands.sequence(rd):
            if key:
                existing, public, same_key = _inspect_keyed_start(
                    rd, key_digest, request_digest)
                if existing is not None:
                    if same_key and public["status"] in {"accepted", "executing", "succeeded"}:
                        return JSONResponse(public)
                    if same_key or public["status"] not in {"not_started", "failed"}:
                        _raise_existing_start(public, same_key=same_key)

            current_rd = requested_rd.resolve()
            if requested_rd.is_symlink() or current_rd != rd or current_rd.parent != root:
                raise HTTPException(409, {
                    "code": "run_path_changed",
                    "message": "run path changed while start was being prepared",
                    "field_errors": {"run_id": "choose a stable run name"},
                })
            current_token = plan.current_token(srv)
            if current_token != plan.validation_token:
                raise HTTPException(409, {
                    "code": "launch_validation_changed",
                    "message": "task, settings, run name, chat, or a referenced path changed before launch",
                    "field_errors": {},
                    "remediation": "Run preflight again and review the updated launch preview.",
                })
            if (rd / "events.jsonl").exists():
                raise HTTPException(409, {
                    "code": "run_id_conflict", "message": f"run {run_id!r} already exists",
                    "field_errors": {"run_id": "choose another run name"},
                })
            known_alive = _known_engine_liveness(rd, "start the run")
            if known_alive or _engine_alive(rd):
                raise HTTPException(409, {
                    "code": "external_start_in_progress" if key else "start_in_progress",
                    "message": f"run {run_id!r} already has an engine starting",
                })
            if srv.commands.spawn_inflight(rd):
                raise HTTPException(409, {
                    "code": "external_start_uncertain" if key else "start_uncertain",
                    "message": f"run {run_id!r} already has an unresolved startup",
                    "remediation": "Observe or explicitly resolve the spawn claim; do not retry.",
                })

            start_id = f"start_{secrets.token_hex(16)}" if key else ""
            created_at = time.time()
            record = None
            if key:
                record = {
                    "version": 1, "id": start_id, "run_id": run_id,
                    "idempotency_key_digest": key_digest, "request_digest": request_digest,
                    "validation_token": plan.validation_token,
                    "status": "preparing", "phase": "reserved",
                    "paid_effect_unknown": False,
                    "created_at": created_at, "updated_at": created_at,
                }
                srv.commands.save_start_record(rd, record)

            owner = f"start:{start_id}" if key else "start"
            lease_started = False
            popen_boundary_entered = False
            try:
                rd.mkdir(parents=True, exist_ok=True)
                atomic_write_text(task_file, json.dumps(plan.canonical_document, indent=2))
                meta = {"task_file": str(task_file)}
                if plan.source_task_file:
                    meta["source_task_file"] = plan.source_task_file
                if key:
                    meta["start_id"] = start_id
                atomic_write_text(rd / "ui_meta.json", json.dumps(meta, indent=2))

                chat_path = rd / "chat.jsonl"
                if plan.seed_chat:
                    chat_bytes = b"".join(orjson.dumps({
                        "role": turn["role"], "content": turn["content"],
                        "ts": created_at + i * 1e-3, "seq": GENESIS_CHAT_SEQ_BASE + i,
                        "genesis": True,
                    }) + b"\n" for i, turn in enumerate(plan.seed_chat))
                    atomic_write_bytes(chat_path, chat_bytes)
                elif chat_path.exists():
                    atomic_write_bytes(chat_path, b"")
                if record is not None:
                    record.update(phase="materialized", updated_at=time.time())
                    srv.commands.save_start_record(rd, record)

                srv.commands.begin_external_spawn(rd, owner)
                lease_started = True
                if record is not None:
                    # After this durable phase, crash-before-call and crash-after-Popen are
                    # indistinguishable. The PID-less claim therefore remains fail-closed.
                    record.update(status="executing", phase="popen_pending",
                                  paid_effect_unknown=True, updated_at=time.time())
                    srv.commands.save_start_record(rd, record)
                # From this assignment onward, an exception cannot prove whether the helper failed
                # before or after the OS accepted Popen. Retain the claim and report uncertainty.
                popen_boundary_entered = True
                pid = _spawn_engine(["run", str(task_file), "--out", str(rd)], env=env, run_dir=rd)
                srv.commands.record_external_spawn(rd, owner, pid)
                if record is not None:
                    record.update(status="accepted", phase="popen_returned",
                                  paid_effect_unknown=False, updated_at=time.time())
                    srv.commands.save_start_record(rd, record)
                    # Fold immediately available positive evidence into the response: a known-live
                    # PID becomes executing and a durable run_started becomes succeeded. PID-less or
                    # uncorrelated evidence becomes uncertain, so clients never navigate on Popen alone.
                    record, start_result = _reconcile_start(rd, record)
            except BaseException as exc:
                # Clear ownership only while we still know the Popen boundary was never entered.
                if lease_started and not popen_boundary_entered:
                    srv.commands.cancel_external_spawn(rd, owner)
                if record is not None:
                    detail = getattr(exc, "detail", None)
                    code = (str(detail.get("code"))
                            if isinstance(detail, dict) and detail.get("code")
                            else "spawn_failed" if record.get("phase") == "popen_pending"
                            else "start_materialization_failed")
                    record.update(
                        status="uncertain" if popen_boundary_entered else "failed",
                        phase=("failed_after_spawn" if popen_boundary_entered
                               else "failed_before_spawn"),
                        error_code=code, paid_effect_unknown=popen_boundary_entered,
                        updated_at=time.time(),
                    )
                    try:
                        srv.commands.save_start_record(rd, record)
                    except Exception:  # noqa: BLE001 - preserve original error + the spawn claim
                        pass
                raise

        if start_result is not None:
            return start_result
        return {"ok": True, "run_id": run_id, "validation_token": plan.validation_token}

    return router
