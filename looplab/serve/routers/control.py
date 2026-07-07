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
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.events.types import EV_INJECT_NODE, EV_NODE_RESET
from looplab.serve.appstate import _RESERVED_RUN_IDS
from looplab.serve.engine_proc import _engine_alive, _spawn_engine
from looplab.serve.protocol import CONTROL_EVENTS, GENESIS_CHAT_SEQ_BASE
from looplab.serve.settings_store import _ALLOWED_FIELDS, _SECRET_FIELDS


def build_router(srv) -> APIRouter:
    router = APIRouter()
    _run_dir, _events, root = srv.run_dir, srv.events, srv.root

    # ------------------------------------------------------------------ control
    @router.post("/api/runs/{run_id}/control")
    async def control(run_id: str, request: Request):
        rd = _run_dir(run_id)
        body = await request.json()
        etype = body.get("type")
        if etype not in CONTROL_EVENTS:
            raise HTTPException(400, f"unknown control event: {etype!r}")
        data = body.get("data") or {}
        # node_reset: re-run an existing node in place — validate the target + stage so a typo can't
        # append a no-op reset (the fold would silently ignore an unknown node_id).
        if etype == EV_NODE_RESET:
            try:
                nid = int(data.get("node_id"))
            except (TypeError, ValueError):
                raise HTTPException(400, "node_id must be an integer")
            # propose|implement are the lifecycle stages; anything else is an eval-PIPELINE stage name
            # to restart from (train / eval / data_prep / …) — those are per-node, so accept any sane
            # non-empty name rather than a fixed allow-list.
            stage = str(data.get("from_stage", "eval")).strip()
            if not stage or len(stage) > 64:
                raise HTTPException(400, "from_stage must be a non-empty stage name")
            if nid not in fold(_events(rd)).nodes:
                raise HTTPException(404, f"no node #{nid} in this run")
            data = {"node_id": nid, "from_stage": stage}
        # Cross-run import: an inject seeded from a sibling run. Resolve the source experiment from disk
        # NOW and bake its code + `origin` provenance into the inject_node, so the engine reproduces it
        # faithfully and the lineage is recorded. (_run_dir guards path traversal on the sibling id.)
        if etype == EV_INJECT_NODE and data.get("source_run") and data.get("source_node") is not None:
            sr = str(data.pop("source_run"))
            try:
                sn = int(data.pop("source_node"))
            except (TypeError, ValueError):
                raise HTTPException(400, "source_node must be an integer")
            sst = fold(_events(_run_dir(sr)))            # 404 if the sibling run doesn't exist
            snode = sst.nodes.get(sn)
            if snode is None:
                raise HTTPException(404, f"no experiment #{sn} in run {sr}")
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
            data["origin"] = {"run_id": sr, "node_id": sn,
                              "metric": snode.confirmed_mean if snode.confirmed_mean is not None
                              else snode.metric}
        # Fresh EventStore per write (single-writer discipline): it rescans last seq before append.
        ev = EventStore(rd / "events.jsonl").append(etype, data)
        return {"ok": True, "seq": ev.seq, "type": etype}

    # ------------------------------------------------------------------ spawn / resume
    def _task_file_for(rd: Path) -> Optional[str]:
        # Prefer the UI's recorded task_file; fall back to the verbatim task.snapshot.json that
        # `run` now writes into every run dir, so even a CLI-started run can be resumed/continued.
        task_file: Optional[str] = None
        meta = rd / "ui_meta.json"
        if meta.exists():
            task_file = json.loads(meta.read_text(encoding="utf-8")).get("task_file")
        if not task_file and (rd / "task.snapshot.json").exists():
            task_file = str(rd / "task.snapshot.json")
        return task_file

    @router.post("/api/runs/{run_id}/resume")
    def resume_run(run_id: str):
        rd = _run_dir(run_id)
        # Don't spawn a second engine when one is already alive: the engine-singleton lock would make it
        # no-op anyway, but skipping the detached Popen keeps the signal honest (and avoids a phantom
        # process flash). The UI's auto-resume already gates on engine_running; this is the backstop.
        if _engine_alive(rd):
            return {"ok": True, "already_running": True}
        task_file = _task_file_for(rd)
        if not task_file:
            raise HTTPException(400, "run is not resumable — no task.snapshot.json or ui_meta.json "
                                     "(it predates self-describing runs; start it via the UI to enable resume)")
        _spawn_engine(["resume", str(rd), "--task-file", str(task_file)], run_dir=rd)
        return {"ok": True}

    @router.post("/api/runs/{run_id}/reset")
    def reset_run(run_id: str):
        """round-7 "Replay": reset a run IN PLACE — archive its event log + spans + node workspaces and
        re-spawn a fresh run on the same run-id. The prior artifacts are RENAMED (not deleted) so the
        history is recoverable."""
        rd = _run_dir(run_id)
        # Guard the invariant the UI relies on (it only offers Replay on a finished run): never reset an
        # ACTIVE run. A running engine is the SOLE writer of events.jsonl — archiving it out from under
        # one and spawning a second engine would corrupt the log. (A sub-second window remains right
        # after run_finished while the engine appends llm_cost + builds the readmodel; that's narrow and
        # a re-reset heals it. This guard is what makes a direct/stale API call safe.)
        # Also gate on the race-free liveness probe (the engine still holds engine.lock through its
        # sub-second post-finish tail: llm_cost append + readmodel build). fold().finished alone has a
        # window where st.finished is True but the engine is still the sole writer; _engine_alive closes
        # it, matching resume_run. Archiving events.jsonl out from under a live engine corrupts the log.
        if _engine_alive(rd) or not fold(_events(rd)).finished:
            raise HTTPException(409, "run is still active — stop it first (Replay resets a finished run)")
        task_file = _task_file_for(rd)
        if not task_file:
            raise HTTPException(400, "run is not resettable — no task.snapshot.json or ui_meta.json")
        stamp = int(time.time() * 1000)   # ms granularity so two resets in the same second don't collide
        # Archive the event log, spans, the read model, the node workspaces, AND the chat transcript.
        # The fresh run reuses node_<id> dirs with mkdir(exist_ok=True)/copytree(dirs_exist_ok=True),
        # so a leftover nodes/ would let stale files from the prior same-numbered node contaminate the
        # replay's eval; the prior chat.jsonl belongs to the old attempt, so the replay starts with a
        # clean conversation (the archived copy stays recoverable alongside events.jsonl.reset-*).
        for name in ("events.jsonl", "spans.jsonl", "readmodel.sqlite", "nodes", "chat.jsonl"):
            p = rd / name
            if p.exists():
                try:
                    p.rename(rd / f"{name}.reset-{stamp}")
                except OSError:
                    pass        # a Windows lock shouldn't block the replay; a fresh `run` recreates it
        # Reuse the run's OWN resolved settings (minus secrets) so the replay matches the original;
        # the API key still comes from the spawned process's inherited env.
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
        _spawn_engine(["run", str(task_file), "--out", str(rd)], env=env, run_dir=rd)
        return {"ok": True}

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
        if rd.name in _RESERVED_RUN_IDS:       # don't let a run clobber the report/assistant stores
            raise HTTPException(400, f"run_id {rd.name!r} is reserved")
        if (rd / "events.jsonl").exists():
            raise HTTPException(409, f"run {run_id!r} already exists — pick another id")
        task_file = body.get("task_file")
        task = body.get("task")
        # Inline task (the genesis flow authors one): require an explicit kind, then VALIDATE it the
        # same way the engine will (validate_task → model_validate) BEFORE materializing anything — so a
        # bad spec (unknown kind, mlebench_real with an unknown/empty competition, a missing required
        # field) fails HERE with a 400 instead of spawning a detached engine that dies (DEVNULL'd)
        # before writing any events, leaving a phantom never-started run.
        if isinstance(task, dict) and task:
            from looplab.adapters.tasks import kinds, validate_task
            kind = task.get("kind")
            if not kind:
                raise HTTPException(400, "inline task must declare a 'kind'")
            if kind not in kinds():
                raise HTTPException(400, f"unknown task kind: {kind!r} (known: {kinds()})")
            try:
                await anyio.to_thread.run_sync(lambda: validate_task(task))
            except HTTPException:
                raise
            except Exception as e:  # noqa: BLE001 - kind-specific validation failed (e.g. bad competition)
                raise HTTPException(400, f"invalid task: {e}")
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
        settings = {**srv.settings.load_ui_settings(), **(body.get("settings") or {})}
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
