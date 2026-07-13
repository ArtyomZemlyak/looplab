"""Control-plane routes: append control intents (/control) and spawn/resume/reset/start engine
processes. Handler bodies are verbatim moves from `serve/server.py::make_app` (BACKLOG §4)."""
from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from typing import Optional

import anyio
import orjson
from fastapi import APIRouter, HTTPException, Request, Response

from looplab.core.atomicio import best_effort_fsync
from looplab.core.config import Settings
from looplab.events.eventstore import EventStore, write_jsonl_atomic
from looplab.serve.appstate import _RESERVED_RUN_IDS
from looplab.serve.engine_proc import _engine_alive, _spawn_engine
from looplab.serve.protocol import GENESIS_CHAT_SEQ_BASE
from looplab.serve.run_commands import normalize_control, task_file_for
from looplab.serve.settings_store import _ALLOWED_FIELDS, _SECRET_FIELDS


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
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "control body must be valid JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "control body must be a JSON object")
        with srv.commands.sequence(rd):
            rd = srv.commands.validate_paths(rd)
            srv.commands.reject_if_active(rd, "append a legacy control event")
            etype = body.get("type")
            data = normalize_control(srv, rd, etype, body.get("data"))
            # Fresh EventStore per write (single-writer discipline): it rescans last seq before append.
            ev = EventStore(rd / "events.jsonl").append(etype, data)
        return {"ok": True, "seq": ev.seq, "type": etype}

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
        return srv.commands.submit(
            rd, request.headers.get("Idempotency-Key", ""), body.get("type"), body.get("data"))

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
        return srv.commands.resolve_active_claims(
            _run_dir(run_id), str(body.get("confirmation") or ""))

    # ------------------------------------------------------------------ spawn / resume

    @router.post("/api/runs/{run_id}/resume")
    def resume_run(run_id: str):
        rd = _run_dir(run_id)
        with srv.commands.sequence(rd):
            rd = srv.commands.validate_paths(rd)
            # This endpoint is a stop-aware driver recovery seam: unlike a legacy `/control resume`
            # event it appends no mutation itself, and the CLI preserves an incomplete terminal scope.
            srv.commands.reject_if_active(
                rd, "resume through the legacy endpoint", allow_incomplete_finalize=True)
            # Decide + Popen + spawn lease atomically with command workers. The lease covers the
            # unavoidable Popen→engine.lock window after this route releases the sequencer.
            if _engine_alive(rd):
                return {"ok": True, "already_running": True}
            if srv.commands.spawn_inflight(rd):
                return {"ok": True, "already_starting": True}
            task_file = task_file_for(rd)
            if not task_file:
                raise HTTPException(400, "run is not resumable — no task.snapshot.json or ui_meta.json "
                                         "(it predates self-describing runs; start it via the UI to enable resume)")
            srv.commands.begin_external_spawn(rd, "legacy-resume")
            spawned = False
            try:
                pid = _spawn_engine(["resume", str(rd), "--task-file", str(task_file)], run_dir=rd)
                spawned = True
                srv.commands.record_external_spawn(rd, "legacy-resume", pid)
            except BaseException:
                # If Popen returned, retain the preclaim even when persisting its PID failed: the
                # detached child may be live/cold and a retry must not launch a duplicate.
                if not spawned:
                    srv.commands.cancel_external_spawn(rd, "legacy-resume")
                raise
            return {"ok": True}

    @router.post("/api/runs/{run_id}/reset")
    def reset_run(run_id: str):
        """round-7 "Replay": reset a run IN PLACE — archive its event log + spans + node workspaces and
        re-spawn a fresh run on the same run-id. The prior artifacts are RENAMED (not deleted) so the
        history is recoverable."""
        rd = _run_dir(run_id)
        with srv.commands.destructive_guard(rd, "reset run") as rd:
            # Re-check liveness/state INSIDE the command sequencer, after all pending workers are
            # excluded. This closes the check→archive race with a command spawning an engine.
            if _engine_alive(rd) or not srv.state(rd).finished:
                raise HTTPException(
                    409, "run is still active — stop it first (Replay resets a finished run)")
            task_file = task_file_for(rd)
            if not task_file:
                raise HTTPException(400, "run is not resettable — no task.snapshot.json or ui_meta.json")
            # A prior process has no in-memory ledger/activity context for its durable usage outbox.
            # Reconcile it only now, after liveness/state validation and while the destructive
            # sequencer is still held, so no late generation-A evidence can cross the reset boundary.
            flush_durable_costs = getattr(srv, "flush_durable_run_costs", None)
            if not callable(flush_durable_costs):
                raise HTTPException(503, "cannot reset run: durable run-cost recovery is unavailable")
            try:
                durable_costs_flushed = flush_durable_costs(rd)
            except Exception as exc:  # noqa: BLE001 - reset must fail closed on unknown evidence
                raise HTTPException(
                    503, "cannot reset run: durable run-cost recovery failed") from exc
            if durable_costs_flushed is not True:
                raise HTTPException(
                    409, "cannot reset run: run-cost evidence is pending, busy, malformed, or conflicting")

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
                            409,
                            "cannot reset run: run-cost outbox is a symlink or reparse point or is not a directory",
                        )
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
            stamp = int(time.time() * 1000)
            moved = []

            def _rollback_archives() -> list[str]:
                failed = []
                for original, archived in reversed(moved):
                    try:
                        if os.path.lexists(archived) and not os.path.lexists(original):
                            archived.rename(original)
                    except OSError:
                        failed.append(original.name)
                return failed

            # Command idempotency records deliberately survive an in-place reset. A lost response
            # replaying the same key must resolve to the old terminal record, never re-apply that
            # budget/fork/finalize intent to the new generation occupying this run id.
            for name in ("events.jsonl", ".llm-usage-outbox", "spans.jsonl", "readmodel.sqlite",
                         "nodes", "chat.jsonl"):
                p = rd / name
                if os.path.lexists(p):
                    if name == ".llm-usage-outbox":
                        try:
                            _outbox_archiveable(p)
                        except HTTPException as exc:
                            rollback_failed = _rollback_archives()
                            detail = f"{exc.detail}; no engine was started"
                            if rollback_failed:
                                detail += f"; rollback also failed for {', '.join(rollback_failed)}"
                            raise HTTPException(409, detail) from exc
                    archived = rd / f"{name}.reset-{stamp}"
                    try:
                        p.rename(archived)
                        moved.append((p, archived))
                    except OSError as exc:
                        rollback_failed = _rollback_archives()
                        detail = f"reset archive failed at {name!r}; no engine was started"
                        if rollback_failed:
                            detail += f"; rollback also failed for {', '.join(rollback_failed)}"
                        raise HTTPException(500, detail) from exc
            env: Optional[dict] = None
            snap = rd / "config.snapshot.json"
            if snap.exists():
                try:
                    cfg = json.loads(snap.read_text(encoding="utf-8"))
                    env = srv.settings.settings_env({k: v for k, v in cfg.items()
                                                     if k in _ALLOWED_FIELDS and k not in _SECRET_FIELDS
                                                     and v is not None})
                except (OSError, json.JSONDecodeError, ValueError):
                    env = None
            spawned = False
            try:
                # The pre-spawn lease is part of the reset transaction: the run artifacts are already
                # archived at this point, so even a lease-write failure must restore them before the
                # request exits.  Keeping this inside the rollback guard also covers an ambiguous
                # begin that committed its claim and then raised.
                srv.commands.begin_external_spawn(rd, "reset")
                pid = _spawn_engine(["run", str(task_file), "--out", str(rd)], env=env, run_dir=rd)
                spawned = True
                srv.commands.record_external_spawn(rd, "reset", pid)
            except BaseException:
                if not spawned:
                    try:
                        srv.commands.cancel_external_spawn(rd, "reset")
                    finally:
                        _rollback_archives()
                # After Popen, rolling archives back would mutate the filesystem underneath a
                # possibly-live new engine. Keep both the archive layout and preclaim fail-closed.
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
        return srv.commands.resolve_spawn_claim(rd, str(body.get("confirmation") or ""))

    @router.post("/api/start")
    async def start_run(request: Request):
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "start body must be valid JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "start body must be a JSON object")
        run_id = body.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise HTTPException(400, "run_id is required")
        reserved_devices = {"CON", "PRN", "AUX", "NUL",
                            *(f"COM{i}" for i in range(1, 10)),
                            *(f"LPT{i}" for i in range(1, 10))}
        if (len(run_id) > 255 or run_id != run_id.strip() or run_id.endswith((".", " "))
                or ":" in run_id or any(ord(ch) < 32 for ch in run_id)
                or run_id.split(".", 1)[0].upper() in reserved_devices):
            raise HTTPException(400, "bad run_id (unsafe or filesystem-ambiguous name)")
        requested_rd = root / run_id
        rd = requested_rd.resolve()
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
        inline_task = isinstance(task, dict) and bool(task)
        # Inline task (the genesis flow authors one): a COMPOSABLE spec needs no `kind` — the capability
        # fields (repo/dataset/cmd/kaggle) infer it. VALIDATE it the same way the engine will
        # (validate_task → normalize + model_validate) BEFORE materializing anything — so a bad spec
        # (unknown kind, mlebench_real with an unknown/empty competition, a missing required field, an
        # uninferrable kind-less dict) fails HERE with a 400 instead of spawning a detached engine that
        # dies (DEVNULL'd) before writing any events, leaving a phantom never-started run.
        if inline_task:
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
            task_file = str(rd / "task.input.json")
        if not isinstance(task_file, str) or not task_file:
            raise HTTPException(400, "task_file or task is required")
        # Inline input is intentionally not materialized until the per-run start reservation below.
        # An external task, however, must already exist before we reserve the run id.
        if not inline_task and not Path(task_file).exists():
            raise HTTPException(400, f"task file not found: {task_file}")
        # Carry the GENESIS conversation (the chat-first creation flow, where the boss planned this run)
        # into the run's saved chat, so that planning becomes the OPENING history of the run's chat.jsonl
        # instead of vanishing the moment the run launches. Stamp each turn with the creation time (which
        # is < the engine's run_started ts) so it sorts at the TOP of the run's chat feed, and a
        # chat-range seq so the Dock renders it as a conversation turn (not an engine event).
        seed_chat = body.get("chat")
        # Per-run settings = the saved UI defaults overlaid with whatever the launch dialog set.
        # Everything reaches the engine as LOOPLAB_* env on the spawned process, so ANY Settings
        # field is configurable from the UI without growing the CLI surface (Settings() reads env).
        # Bind the saved defaults ONCE: the merge and the backend predicate below must see the SAME
        # store read (and a second disk read per launch bought nothing).
        ui = srv.settings.load_ui_settings()
        launch_settings = body.get("settings") or {}
        if not isinstance(launch_settings, dict):
            raise HTTPException(400, "settings must be a JSON object")
        settings = {**ui, **launch_settings}
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

        # Atomic run-id reservation. The first request installs a durable spawn lease BEFORE it
        # materializes files or calls Popen; a concurrent request can therefore never pass the same
        # preflight and launch a second engine during the Popen→engine.lock window. The early
        # events.jsonl check above is only a fast path — the ownership decision is authoritative here,
        # under the cross-process per-run sequencer.
        with srv.commands.sequence(rd):
            current_rd = requested_rd.resolve()
            if requested_rd.is_symlink() or current_rd != rd or current_rd.parent != root:
                raise HTTPException(409, "run path changed while start was being prepared")
            if (rd / "events.jsonl").exists():
                raise HTTPException(409, f"run {run_id!r} already exists — pick another id")
            if _engine_alive(rd):
                raise HTTPException(409, f"run {run_id!r} already has an engine starting")
            if srv.commands.spawn_inflight(rd):
                raise HTTPException(409, f"run {run_id!r} start is already in progress")

            srv.commands.begin_external_spawn(rd, "start")
            spawned = False
            try:
                rd.mkdir(parents=True, exist_ok=True)
                if inline_task:
                    Path(task_file).write_text(json.dumps(task, indent=2), encoding="utf-8")
                (rd / "ui_meta.json").write_text(
                    json.dumps({"task_file": str(task_file)}), encoding="utf-8")
                if isinstance(seed_chat, list) and seed_chat:
                    t0 = time.time()
                    with open(rd / "chat.jsonl", "ab") as f:
                        for i, m in enumerate(seed_chat):
                            if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
                                continue
                            f.write(orjson.dumps({
                                "role": m["role"], "content": str(m.get("content", "")),
                                "ts": t0 + i * 1e-3, "seq": GENESIS_CHAT_SEQ_BASE + i,
                                "genesis": True}) + b"\n")
                        f.flush()
                        # FUSE/S3 fsync may raise — don't fail the launch.
                        best_effort_fsync(f.fileno())
                pid = _spawn_engine(["run", str(task_file), "--out", str(rd)], env=env, run_dir=rd)
                spawned = True
                srv.commands.record_external_spawn(rd, "start", pid)
            except BaseException:
                # Once Popen returned, retain the pre-spawn lease even if persisting its PID failed:
                # the child may be live, and releasing ownership would permit a duplicate launch.
                if not spawned:
                    srv.commands.cancel_external_spawn(rd, "start")
                raise
        return {"ok": True, "run_id": run_id}

    return router
