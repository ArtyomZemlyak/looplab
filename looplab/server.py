"""Live UI server (opt-in `[ui]` extra) — a SEPARATE read/control process, never imported by the
engine or the offline test suite (ADR-18: the engine is a process, not a server). It tails each
run's append-only `events.jsonl`, folds it with `replay.fold`, streams the current state to the
browser over SSE, serves the built React assets, and turns UI actions into APPENDED control
events (`EventStore.append`, the same files-as-truth primitive as `LoopLab approve`). It also
spawns/resumes engine runs as subprocesses so a browser can drive a live run end-to-end.

Reuses the canonical projections: `replay.fold`, `eventstore.iter_jsonl`, `traceview.build_trace_view`,
`Settings.masked_snapshot`. No new source of truth lives here.

Run it via `LoopLab ui --run-root runs/` (the CLI lazily imports this so the core stays zero-dep).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import anyio
import orjson

from .config import Settings
from .eventstore import EventStore, iter_jsonl
from .models import Event
from .projects import ProjectError, ProjectStore
from .replay import fold
from .tasks import make_llm_client

# These imports require the [ui] extra; importing this module without it raises a clear error.
try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse,
                                   StreamingResponse)
    from fastapi.staticfiles import StaticFiles
except ModuleNotFoundError as e:  # pragma: no cover - exercised only without the extra
    raise ModuleNotFoundError(
        "The LoopLab UI server needs the [ui] extra: pip install 'looplab[ui]' "
        "(fastapi + uvicorn)."
    ) from e


# Control events the UI is allowed to append (intent). The engine writes the domain effect.
CONTROL_EVENTS = {
    "run_abort", "pause", "resume", "node_abort", "budget_extend", "hint",
    "force_confirm", "force_ablate", "fork", "annotation", "promote",
    "approval_granted", "spec_approved", "inject_node", "run_reopened",
    "set_strategy",   # A7: operator pins/overrides the Strategist's choice (HITL parity)
    "deep_research",  # P2: operator asks the engine to run the Deep-Research stage now
}

POLL_SECONDS = 0.4   # SSE tail cadence — fast enough to feel live, light on the disk


# Workstream C: the chat action-router LLM fills this; the server maps it to a control {type, data}
# the UI confirms before executing. `advise` = no action (fall back to a grounded chat reply).
from pydantic import BaseModel  # noqa: E402


class _Command(BaseModel):
    action: str = "advise"   # advise|confirm|ablate|fork|promote|hint|strategy|deep_research|inject|approve|ratify|pause|resume|stop
    node_id: Optional[int] = None
    text: str = ""           # hint text / note / free rationale
    operator: str = "improve"
    params: dict = {}
    policy: str = ""
    fidelity: str = ""
    rationale: str = ""


def _command_to_action(c: "_Command", st) -> Optional[dict]:
    """Map the LLM's classified command to a control {type, data, label}. None => no actionable verb
    (treat as advisory chat). `label` is the human-readable confirmation the UI shows."""
    a, nid = c.action, c.node_id
    if a == "confirm" and nid is not None:
        return {"type": "force_confirm", "data": {"node_id": nid}, "label": f"Confirm #{nid} (multi-seed robustness)"}
    if a == "ablate" and nid is not None:
        return {"type": "force_ablate", "data": {"node_id": nid}, "label": f"Ablate #{nid} (sensitivity probe)"}
    if a == "fork" and nid is not None:
        return {"type": "fork", "data": {"from_node_id": nid}, "label": f"Fork an improve-branch from #{nid}"}
    if a == "promote" and nid is not None:
        return {"type": "promote", "data": {"node_id": nid, "alias": "champion"}, "label": f"Promote #{nid} to champion"}
    if a == "hint" and c.text:
        return {"type": "hint", "data": {"text": c.text}, "label": f"Send hint: {c.text[:60]}"}
    if a == "strategy" and (c.policy or c.fidelity):
        strat = {k: v for k, v in (("policy", c.policy), ("fidelity", c.fidelity)) if v}
        return {"type": "set_strategy", "data": {"strategy": strat}, "label": f"Switch strategy → {strat}"}
    if a == "deep_research":
        return {"type": "deep_research", "data": {}, "label": "Run a deep-research step now"}
    if a == "inject":
        idea = {"operator": c.operator or "improve", "params": c.params or {}, "rationale": c.rationale or c.text or ""}
        return {"type": "inject_node", "data": {"idea": idea, "parent_id": nid, "code": None},
                "label": f"Add experiment: {idea['operator']} {idea['params'] or ''}".strip()}
    if a == "approve":
        node = nid if nid is not None else st.best_node_id
        if node is None:                      # no champion yet -> not an actionable approve
            return None
        return {"type": "approval_granted", "data": {"node_id": node}, "label": f"Approve #{node}"}
    if a == "ratify":
        return {"type": "spec_approved", "data": {}, "label": "Ratify the eval spec"}
    if a in ("pause", "resume", "stop"):
        t = {"pause": "pause", "resume": "resume", "stop": "run_abort"}[a]
        return {"type": t, "data": ({"reason": "ui"} if a == "stop" else {}), "label": a.capitalize() + " the run"}
    return None


def _ui_dist() -> Path:
    """Built React assets. Override with LOOPLAB_UI_DIST; default <repo>/ui/dist."""
    env = os.environ.get("LOOPLAB_UI_DIST")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[1] / "ui" / "dist"


# Per-theme rollup for the cross-run map: {theme: {count, best_metric}}. Now lives in `digest` so the
# Researcher's working-set digest and this UI endpoint share one definition.
from .digest import theme_rollup as _theme_rollup


def make_app(run_root: str | os.PathLike) -> "FastAPI":
    root = Path(run_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="LoopLab UI", version="0.1.0")
    # CORS allow-list (review C3): the production UI is served SAME-ORIGIN from /dist (needs no
    # CORS), so the only legitimate cross-origin caller is the Vite dev server. Restricting to
    # localhost dev origins (instead of "*") stops any other web page the operator has open from
    # driving this unauthenticated control-plane cross-origin (CSRF). Override with LOOPLAB_UI_CORS
    # (comma-separated) if the dev server runs elsewhere.
    cors = os.environ.get("LOOPLAB_UI_CORS")
    origins = ([o.strip() for o in cors.split(",") if o.strip()] if cors else
               ["http://localhost:5173", "http://127.0.0.1:5173"])
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"],
                       allow_headers=["*"])
    # G1 server auth (review C3): when LOOPLAB_UI_TOKEN is set, require a matching X-LoopLab-Token
    # header on every MUTATING /api/* request (GET/HEAD/OPTIONS + SSE stay open for read/stream). The
    # SPA is served the token in a same-origin <meta> tag (see _index_response), so a cross-origin
    # page can't read it. Unset (default local single-user) -> no auth, behaviour unchanged.
    ui_token = os.environ.get("LOOPLAB_UI_TOKEN")
    if ui_token:
        @app.middleware("http")
        async def _require_token(request: "Request", call_next):
            if (request.method in ("POST", "PUT", "PATCH", "DELETE")
                    and request.url.path.startswith("/api/")
                    and request.headers.get("X-LoopLab-Token") != ui_token):
                return JSONResponse({"detail": "unauthorized (missing/invalid UI token)"},
                                    status_code=401)
            return await call_next(request)
    projects = ProjectStore(root / "projects.json")   # ClearML-style run organization (UI-only)

    # ------------------------------------------------------------------ helpers
    def _run_dir(run_id: str) -> Path:
        rd = (root / run_id).resolve()
        if root != rd and root not in rd.parents:   # path-traversal guard
            raise HTTPException(404, "no such run")
        if not (rd / "events.jsonl").exists():
            raise HTTPException(404, "no such run")
        return rd

    def _events(rd: Path, upto_seq: Optional[int] = None) -> list[Event]:
        evs = [Event(**o) for o in iter_jsonl(rd / "events.jsonl")]
        if upto_seq is not None:
            evs = [e for e in evs if e.seq <= upto_seq]
        return evs

    def _state_payload(rd: Path, upto_seq: Optional[int] = None) -> dict:
        evs = _events(rd, upto_seq)
        st = fold(evs)
        last_seq = evs[-1].seq if evs else -1
        # Trim heavy per-node payloads from the live state (code/files/stdout/error) — they are
        # fetched on demand via /nodes/{id}. Keeps SSE ticks small even for code-writing runs.
        d = st.model_dump(mode="json")
        better = (lambda a, b: a < b) if st.direction == "min" else (lambda a, b: a > b)
        for n in d.get("nodes", {}).values():
            n.pop("code", None); n.pop("files", None)
            n["stdout_tail"] = (n.get("stdout_tail") or "")[:160]
            n["error"] = (n.get("error") or "")[:160]
            # Intra-node sweep: a node can carry many trials — replace the full array with a compact
            # summary for the live state (card badge + spark + explode-hull header). The full trials
            # ride along the on-demand /nodes/{id} detail endpoint, like code/files do.
            trials = n.pop("trials", None) or []
            if trials:
                vals = [t.get("metric") for t in trials if t.get("metric") is not None]
                best = None
                for m in vals:
                    if best is None or better(m, best):
                        best = m
                ok = sum(1 for t in trials if t.get("metric") is not None and not t.get("error"))
                n["trials_summary"] = {
                    "count": len(trials), "best": best, "ok": ok, "failed": len(trials) - ok,
                    "series": vals[:64],   # cap the inline sparkline series
                }
        d["phase"] = _phase(st)
        return {"state": d, "seq": last_seq, "max_seq": last_seq}

    def _phase(st) -> str:
        if st.finished:
            return "finished"
        if st.paused:
            return "paused"
        if st.awaiting_approval:
            return "approval"
        if st.spec_approval_requested and not st.spec_confirmed:
            return "spec_approval"
        if st.proposed_spec is not None and not st.spec_confirmed:
            return "onboarding"
        if not st.nodes and st.data_profile is None and st.run_id:
            return "grounding"
        return "search"

    # ------------------------------------------------------------------ runs list
    _summary_cache: dict[str, tuple] = {}   # run_id -> (size, mtime, summary); skips re-folding
    @app.get("/api/runs")
    def list_runs():
        out = []
        for rd in sorted(root.iterdir()) if root.exists() else []:
            log = rd / "events.jsonl"
            if not log.exists():
                continue
            try:
                stt = log.stat()
                sig = (stt.st_size, stt.st_mtime)
                cached = _summary_cache.get(rd.name)
                if cached and cached[:2] == sig:    # unchanged log -> reuse (finished runs never re-fold)
                    out.append(cached[2])
                    continue
                st = fold(_events(rd))
                best = st.best()
                summary = {
                    "run_id": rd.name, "task_id": st.task_id, "goal": st.goal,
                    "direction": st.direction, "finished": st.finished,
                    "phase": _phase(st), "nodes": len(st.nodes),
                    "best_metric": (best.metric if best else None),
                    "best_confirmed": (best.confirmed_mean if best else None),
                    "stop_reason": st.stop_reason,
                    "themes": _theme_rollup(st),
                    "mtime": stt.st_mtime,    # last activity (events.jsonl mtime) — time sort + "updated"
                    "created": stt.st_ctime,  # run creation time (events.jsonl ctime) — "started" date
                }
                _summary_cache[rd.name] = (*sig, summary)
                out.append(summary)
            except Exception:  # noqa: BLE001 - a half-written run shouldn't break the list
                continue
        # Overlay project membership (kept OUT of the summary cache — assignments change
        # independently of the event log, so a finished/cached run can still be re-filed).
        pdata = projects.load()
        assignments, labels = pdata["assignments"], pdata.get("labels", {})
        return [{**s, "project_id": assignments.get(s["run_id"]),
                 "label": labels.get(s["run_id"])} for s in out]

    # ------------------------------------------------------------------ projects (ClearML-style)
    @app.get("/api/projects")
    def list_projects():
        return projects.load()

    @app.post("/api/projects")
    async def create_project(request: Request):
        body = await request.json()
        try:
            p = projects.create(body.get("name", ""), body.get("parent_id"))
        except ProjectError as e:
            raise HTTPException(400, str(e))
        return p.model_dump()

    @app.patch("/api/projects/{pid}")
    async def patch_project(pid: str, request: Request):
        body = await request.json()
        try:
            if "name" in body and body["name"] is not None:
                projects.rename(pid, body["name"])
            if "parent_id" in body:
                projects.reparent(pid, body["parent_id"])
        except ProjectError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.delete("/api/projects/{pid}")
    def delete_project(pid: str):
        try:
            projects.delete(pid)
        except ProjectError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.post("/api/runs/{run_id}/project")
    async def assign_run(run_id: str, request: Request):
        _run_dir(run_id)   # 404 guard: only real runs can be filed
        body = await request.json()
        try:
            projects.assign(run_id, body.get("project_id"))
        except ProjectError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.patch("/api/runs/{run_id}")
    async def rename_run(run_id: str, request: Request):
        """Set/clear a run's UI display label. Non-destructive: the run dir id is unchanged."""
        _run_dir(run_id)   # 404 guard
        body = await request.json()
        projects.set_label(run_id, body.get("label"))
        return {"ok": True}

    @app.delete("/api/runs/{run_id}")
    def delete_run(run_id: str):
        """Permanently remove a run's directory and forget its UI metadata. Refuses to delete a
        run that is still live (not finished) so we never yank the dir out from under the engine."""
        import shutil
        rd = _run_dir(run_id)
        st = fold(_events(rd))
        if not st.finished:
            raise HTTPException(409, "run is still live — pause/stop it before deleting")
        # Don't report success on a partial delete (e.g. a Windows open handle on a node dir) —
        # that would leave a ghost run the UI thinks is gone. Only forget it if the dir is actually
        # removed; otherwise surface the failure.
        shutil.rmtree(rd, ignore_errors=True)
        if rd.exists():
            raise HTTPException(500, "run dir could not be fully removed (a file may be in use); "
                                     "retry after the engine process exits")
        projects.forget(run_id)
        _summary_cache.pop(run_id, None)
        return {"ok": True}

    # ------------------------------------------------------------------ state + time-travel
    @app.get("/api/runs/{run_id}/state")
    def get_state(run_id: str, seq: Optional[int] = None):
        return _state_payload(_run_dir(run_id), seq)

    @app.get("/api/runs/{run_id}/events")
    async def stream_events(run_id: str, request: Request):
        rd = _run_dir(run_id)

        async def gen():
            last_sent = -2
            # Initial snapshot so a fresh/reconnecting client is immediately correct.
            while True:
                if await request.is_disconnected():
                    break
                payload = await anyio.to_thread.run_sync(_state_payload, rd)
                if payload["seq"] != last_sent:
                    last_sent = payload["seq"]
                    yield (f"id: {payload['seq']}\n"
                           f"event: state\n"
                           f"data: {json.dumps(payload)}\n\n")
                    if payload["state"].get("finished"):   # reuse the threaded fold; don't re-fold
                        # End this stream — but the client deliberately does NOT close on `done`; it
                        # lets the closed connection trigger its reconnect, so a reopen (fork / branch
                        # / add-experiment) is picked up within a couple seconds. (Holding the stream
                        # open instead would never terminate, which hangs the TestClient SSE test.)
                        yield "event: done\ndata: {}\n\n"
                        break
                await anyio.sleep(POLL_SECONDS)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------------ node detail
    @app.get("/api/runs/{run_id}/nodes/{nid}")
    def node_detail(run_id: str, nid: int):
        rd = _run_dir(run_id)
        st = fold(_events(rd))
        n = st.nodes.get(nid)
        if n is None:
            raise HTTPException(404, "no such node")
        out = n.model_dump(mode="json")
        out["annotations"] = st.annotations.get(nid, [])
        out["confirm_seeds_detail"] = st.confirm_seed_results.get(nid, {})
        # parent diff (vs the first parent's solution.py) — files-as-truth lineage
        if n.parent_ids:
            p = st.nodes.get(n.parent_ids[0])
            if p is not None:
                out["parent_code"] = p.code
                out["parent_id_diffed"] = p.id
        # per-node execution/agent trace from the trace projection
        out["trace"] = _node_trace(rd, nid)
        return out

    def _node_trace(rd: Path, nid: int) -> dict:
        try:
            from .traceview import build_trace_view, load_spans
            st = fold(_events(rd))
            tv = build_trace_view(st, load_spans(rd / "spans.jsonl"))
            return {"nodes": tv.get("nodes", {}).get(str(nid), []),
                    "summary": tv.get("summary", {})}
        except Exception:  # noqa: BLE001
            return {"nodes": [], "summary": {}}

    @app.get("/api/runs/{run_id}/log")
    def event_log(run_id: str, since: int = -1):
        """Raw event envelopes (for the activity feed + event/span explorer). `since` = exclusive
        seq lower bound."""
        rd = _run_dir(run_id)
        return [o for o in iter_jsonl(rd / "events.jsonl") if o.get("seq", -1) > since]

    @app.get("/api/runs/{run_id}/trace")
    def trace(run_id: str):
        rd = _run_dir(run_id)
        from .traceview import build_trace_view, load_spans
        return build_trace_view(fold(_events(rd)), load_spans(rd / "spans.jsonl"))

    @app.get("/api/runs/{run_id}/prov")
    def prov(run_id: str):
        """W3C-PROV-style provenance of the search DAG: each node's solution is an entity
        generated by an experiment activity (its operator), derived from its parent nodes. Lets
        the lineage be queried as a knowledge-graph ('which change improved metric M the most')."""
        st = fold(_events(_run_dir(run_id)))
        agent = f"agent:looplab/{st.config_hash or 'run'}"
        ent, act, wgb, used, wdf, waw = {}, {}, {}, {}, [], {}
        for n in st.nodes.values():
            e, a = f"sol:{n.id}", f"exp:{n.id}"
            ent[e] = {"prov:label": f"solution node {n.id}",
                      "ll:metric": n.confirmed_mean if n.confirmed_mean is not None else n.metric,
                      "ll:status": n.status, "ll:operator": n.operator, "ll:feasible": n.feasible,
                      "ll:is_best": n.id == st.best_node_id}
            act[a] = {"prov:label": f"{n.operator} experiment", "ll:params": n.idea.params,
                      "ll:rationale": n.idea.rationale}
            wgb[f"wgb:{n.id}"] = {"prov:entity": e, "prov:activity": a}
            waw[f"waw:{n.id}"] = {"prov:activity": a, "prov:agent": agent}
            for p in n.parent_ids:
                used[f"used:{n.id}-{p}"] = {"prov:activity": a, "prov:entity": f"sol:{p}"}
                wdf.append({"prov:generatedEntity": e, "prov:usedEntity": f"sol:{p}"})
        return {"prefix": {"prov": "http://www.w3.org/ns/prov#", "ll": "urn:looplab:"},
                "entity": ent, "activity": act, "agent": {agent: {"prov:type": "prov:SoftwareAgent"}},
                "wasGeneratedBy": wgb, "used": used,
                "wasAssociatedWith": waw,
                "wasDerivedFrom": {f"wdf:{i}": d for i, d in enumerate(wdf)}}

    @app.get("/api/runs/{run_id}/config")
    def run_config(run_id: str):
        rd = _run_dir(run_id)
        snap = rd / "config.snapshot.json"
        if snap.exists():
            return json.loads(snap.read_text(encoding="utf-8"))   # already secret-masked by `run`
        return Settings().masked_snapshot()

    @app.get("/api/runs/{run_id}/cost")
    def run_cost(run_id: str):
        st = fold(_events(_run_dir(run_id)))
        return st.llm_cost or {"cost": 0.0, "calls": 0, "total_tokens": 0}

    @app.get("/api/runs/{run_id}/agents_md")
    def agents_md(run_id: str):
        rd = _run_dir(run_id)
        f = rd / "AGENTS.md"
        return PlainTextResponse(f.read_text(encoding="utf-8") if f.exists() else "")

    # ------------------------------------------------------------------ control
    @app.post("/api/runs/{run_id}/control")
    async def control(run_id: str, request: Request):
        rd = _run_dir(run_id)
        body = await request.json()
        etype = body.get("type")
        if etype not in CONTROL_EVENTS:
            raise HTTPException(400, f"unknown control event: {etype!r}")
        data = body.get("data") or {}
        # Fresh EventStore per write (single-writer discipline): it rescans last seq before append.
        ev = EventStore(rd / "events.jsonl").append(etype, data)
        return {"ok": True, "seq": ev.seq, "type": etype}

    # ------------------------------------------------------------------ spawn / resume
    @app.post("/api/runs/{run_id}/resume")
    def resume_run(run_id: str):
        rd = _run_dir(run_id)
        # Prefer the UI's recorded task_file; fall back to the verbatim task.snapshot.json that
        # `run` now writes into every run dir, so even a CLI-started run can be resumed/continued.
        task_file: Optional[str] = None
        meta = rd / "ui_meta.json"
        if meta.exists():
            task_file = json.loads(meta.read_text(encoding="utf-8")).get("task_file")
        if not task_file and (rd / "task.snapshot.json").exists():
            task_file = str(rd / "task.snapshot.json")
        if not task_file:
            raise HTTPException(400, "run is not resumable — no task.snapshot.json or ui_meta.json "
                                     "(it predates self-describing runs; start it via the UI to enable resume)")
        _spawn_engine(["resume", str(rd), "--task-file", str(task_file)])
        return {"ok": True}

    @app.post("/api/runs/{run_id}/reset")
    def reset_run(run_id: str):
        """round-7 "Replay": reset a run IN PLACE — archive its event log + spans and re-spawn a fresh
        run on the same run-id. The UI only offers Replay on a FINISHED run (no live engine is writing
        here), so archiving + re-spawning is race-free and needs no engine coordination. The prior log
        is renamed (not deleted) so the history is recoverable."""
        rd = _run_dir(run_id)
        task_file: Optional[str] = None
        meta = rd / "ui_meta.json"
        if meta.exists():
            task_file = json.loads(meta.read_text(encoding="utf-8")).get("task_file")
        if not task_file and (rd / "task.snapshot.json").exists():
            task_file = str(rd / "task.snapshot.json")
        if not task_file:
            raise HTTPException(400, "run is not resettable — no task.snapshot.json or ui_meta.json")
        stamp = int(time.time())
        for name in ("events.jsonl", "spans.jsonl", "readmodel.sqlite"):
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
                env = _settings_env({k: v for k, v in cfg.items()
                                     if k in _ALLOWED_FIELDS and k not in _SECRET_FIELDS and v is not None})
            except (OSError, json.JSONDecodeError, ValueError):
                env = None
        _spawn_engine(["run", str(task_file), "--out", str(rd)], env=env)
        return {"ok": True}

    @app.post("/api/start")
    async def start_run(request: Request):
        body = await request.json()
        task_file = body.get("task_file")
        run_id = body.get("run_id")
        if not task_file or not run_id:
            raise HTTPException(400, "task_file and run_id are required")
        if not Path(task_file).exists():
            raise HTTPException(400, f"task file not found: {task_file}")
        rd = (root / run_id).resolve()
        if root not in rd.parents and rd != root:
            raise HTTPException(400, "bad run_id")
        if (rd / "events.jsonl").exists():
            raise HTTPException(409, f"run {run_id!r} already exists — pick another id")
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "ui_meta.json").write_text(json.dumps({"task_file": str(task_file)}), encoding="utf-8")
        # Per-run settings = the saved UI defaults overlaid with whatever the launch dialog set.
        # Everything reaches the engine as LOOPLAB_* env on the spawned process, so ANY Settings
        # field is configurable from the UI without growing the CLI surface (Settings() reads env).
        settings = {**_load_ui_settings(), **(body.get("settings") or {})}
        env = _settings_env(settings)
        _spawn_engine(["run", str(task_file), "--out", str(rd)], env=env)
        return {"ok": True, "run_id": run_id}

    def _spawn_engine(cli_args: list[str], env: Optional[dict] = None) -> None:
        cmd = [sys.executable, "-m", "looplab.cli", *cli_args]
        kw: dict = {"cwd": str(Path(__file__).resolve().parents[1])}
        if env:
            kw["env"] = {**os.environ, **env}
        if os.name == "nt":
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # detached, survives request
        else:
            kw["start_new_session"] = True
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)

    # ------------------------------------------------------------------ settings (UI defaults)
    # The engine has no settings server (ADR-18); these are UI-chosen DEFAULTS for new runs,
    # persisted at <run-root>/ui_settings.json and applied to a spawned run as LOOPLAB_* env.
    _ui_settings_path = root / "ui_settings.json"
    _SECRET_FIELDS = {"llm_api_key"}
    _ALLOWED_FIELDS = set(Settings.model_fields)

    def _load_ui_settings() -> dict:
        try:
            d = json.loads(_ui_settings_path.read_text(encoding="utf-8"))
            return {k: v for k, v in d.items() if k in _ALLOWED_FIELDS and k not in _SECRET_FIELDS}
        except (OSError, json.JSONDecodeError):
            return {}

    def _resolved_settings() -> dict:
        """Engine defaults (Settings(): defaults+env) overlaid with the saved UI overrides — i.e.
        exactly what a new run gets if the launch dialog changes nothing. Secret masked."""
        base = Settings().masked_snapshot()
        base.update(_load_ui_settings())
        base.pop("llm_api_key", None)
        return base

    def _settings_env(settings: dict) -> dict:
        """Render UI settings into LOOPLAB_* env strings pydantic-settings can parse back."""
        env = {}
        for k, v in settings.items():
            if k not in _ALLOWED_FIELDS or k in _SECRET_FIELDS or v is None:
                continue
            if isinstance(v, bool):
                s = "true" if v else "false"
            elif isinstance(v, (list, dict)):
                s = json.dumps(v)            # pydantic reads complex env values as JSON
            else:
                s = str(v)
            env[f"LOOPLAB_{k.upper()}"] = s
        return env

    @app.get("/api/settings")
    def get_settings():
        defaults = Settings().model_dump()
        defaults.pop("llm_api_key", None)
        return {"settings": _resolved_settings(), "overrides": _load_ui_settings(), "defaults": defaults}

    @app.put("/api/settings")
    async def put_settings(request: Request):
        body = await request.json()
        incoming = body.get("settings", body) or {}
        # Keep only known, non-secret fields whose value differs from the engine default — the file
        # stays a small, readable diff rather than a full mirror of every Settings field.
        base = Settings().model_dump()
        overrides = {}
        for k, v in incoming.items():
            if k in _ALLOWED_FIELDS and k not in _SECRET_FIELDS and v is not None and base.get(k) != v:
                overrides[k] = v
        tmp = _ui_settings_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(overrides, indent=2), encoding="utf-8")
        os.replace(tmp, _ui_settings_path)
        return {"ok": True, "settings": _resolved_settings(), "overrides": overrides}

    # ------------------------------------------------------------------ task catalogue
    @app.get("/api/tasks")
    def list_tasks():
        """Discover runnable task JSON files (the `examples/` catalogue by default, plus any in the
        run-root) so the launch dialog can offer a pick-list instead of a raw path."""
        repo = Path(__file__).resolve().parents[1]
        dirs = [repo / "examples", root]
        env_dir = os.environ.get("LOOPLAB_TASKS_DIR")
        if env_dir:
            dirs.insert(0, Path(env_dir))
        seen, out = set(), []
        for d in dirs:
            if not d.exists():
                continue
            for p in sorted(d.glob("*.json")):
                rp = str(p.resolve())
                if rp in seen:
                    continue
                seen.add(rp)
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(data, dict) or not ("goal" in data or "id" in data):
                    continue
                out.append({"path": rp, "name": p.name,
                            "kind": data.get("kind", "quadratic"),
                            "id": data.get("id"), "goal": data.get("goal", ""),
                            "direction": data.get("direction")})
        return {"tasks": out}

    # ------------------------------------------------------------------ pre-research a topic
    @app.post("/api/research")
    async def research(request: Request):
        """Best-effort LLM brief for a research topic, to prime a run. Optionally saved as a
        knowledge note (markdown) so the agentic-retrieval Researcher can read it (ADR-16).
        Degrades cleanly when no model endpoint is reachable."""
        body = await request.json()
        topic = (body.get("topic") or "").strip()
        if not topic:
            raise HTTPException(400, "topic is required")
        s = Settings(**{k: v for k, v in _load_ui_settings().items()
                        if k in {"llm_model", "llm_base_url", "llm_temperature", "llm_api_key"}})
        try:
            from .tasks import make_llm_client
            client = make_llm_client(s)
            msgs = [
                {"role": "system", "content": "You are a senior ML research advisor. Given a problem "
                 "topic, write a concise markdown brief: key approaches to try, likely hyperparameters "
                 "and sensible ranges, common pitfalls, and 2-3 concrete first experiments. Be specific "
                 "and terse."},
                {"role": "user", "content": f"Research topic for an autonomous ML run:\n\n{topic}"},
            ]
            text = client.complete_text(msgs)
        except Exception as e:  # noqa: BLE001 - offline / no model -> soft fail
            return {"ok": False, "error": str(e), "model": s.llm_model, "base_url": s.llm_base_url}
        saved = None
        if body.get("save"):
            kd = _load_ui_settings().get("knowledge_dir") or Settings().knowledge_dir
            if kd:
                d = Path(kd); d.mkdir(parents=True, exist_ok=True)
                slug = "".join(c if c.isalnum() else "-" for c in topic.lower())[:48].strip("-") or "topic"
                fp = d / f"research-{slug}.md"
                fp.write_text(f"# Research brief: {topic}\n\n{text}\n", encoding="utf-8")
                saved = str(fp)
        return {"ok": True, "text": text, "model": s.llm_model, "saved": saved}

    # ------------------------------------------------------------------ LLM (chat / suggest / health)
    def _llm_settings() -> "Settings":
        """Settings for the UI-side LLM calls (chat/suggest/health/research): engine defaults +
        env overlaid with the saved UI LLM overrides, so the UI talks to the SAME model a run does."""
        ui = {k: v for k, v in _load_ui_settings().items()
              if k in {"llm_model", "llm_base_url", "llm_temperature"}}
        return Settings(**ui)

    def _node_context(st, nid: Optional[int], full: "Path") -> str:
        """A compact textual brief of the run (+ one focused experiment) to ground an LLM chat:
        goal, direction, best-so-far, and — when a node is selected — its idea/metric/code/error."""
        best = st.best()
        lines = [f"Run goal: {st.goal or st.task_id}", f"Optimization direction: {st.direction}",
                 f"Nodes so far: {len(st.nodes)} ({len(st.evaluated_nodes())} evaluated)."]
        if best is not None:
            lines.append(f"Best node #{best.id}: metric={best.metric} "
                         f"params={best.idea.params} operator={best.operator}")
        if nid is not None and nid in st.nodes:
            n = st.nodes[nid]
            lines += ["", f"--- Focused experiment: node #{n.id} ---",
                      f"operator={n.operator} status={n.status} metric={n.metric} "
                      f"feasible={n.feasible}",
                      f"params={n.idea.params}", f"rationale: {n.idea.rationale}"]
            if n.error:
                lines.append(f"error ({n.error_reason}): {n.error[:400]}")
            if n.code:
                lines.append("solution.py:\n```python\n" + n.code[:2400] + "\n```")
        return "\n".join(lines)

    @app.post("/api/runs/{run_id}/chat")
    async def chat(run_id: str, request: Request):
        """Advisory chat grounded on a run (and optionally one experiment node). Read-only — it
        never appends events; it's a thinking aid. The UI keeps the history and posts the full
        message list each turn. Soft-fails offline so the panel degrades cleanly."""
        rd = _run_dir(run_id)
        body = await request.json()
        msgs = body.get("messages") or []
        nid = body.get("node_id")
        st = fold(_events(rd))
        sys_prompt = (
            "You are an ML research collaborator embedded in an autonomous experiment loop, chatting "
            "with the human running it. Talk like a sharp, friendly colleague at a whiteboard — warm "
            "and conversational, not a formal report. Use the human's language. Open with a direct "
            "answer to what they asked, then your reasoning; ask a clarifying question back when it "
            "would help. Keep it concise but human: contractions are fine, and it's okay to say what "
            "you'd be curious to try and why.\n"
            "Format with Markdown so it's easy to read: short paragraphs, **bold** for the key point, "
            "bullet lists for options, and ```python fenced blocks for any code or params. When you "
            "actually recommend an experiment, name the operator (improve/draft/debug), give the exact "
            "params, and a one-line why — but don't force every reply into that shape; sometimes the "
            "right answer is just an explanation or a question.\n\n"
            "Here is the run you're discussing:\n" + _node_context(st, nid, rd))
        try:
            client = make_llm_client(_llm_settings())
            text = client.complete_text([{"role": "system", "content": sys_prompt}, *msgs])
        except Exception as e:  # noqa: BLE001 - offline / no model -> soft fail
            return JSONResponse({"ok": False, "error": str(e)}, status_code=200)
        # Return the LLM I/O so the chat row can expand into a langfuse-style trace. We include the
        # user's latest message + the system prompt (which carries the run/node context) so the trace
        # honestly shows the actual input, but omit the REST of the echoed conversation — the client
        # already holds it, and re-sending the whole history would grow the payload O(n²) over a long
        # chat (the single latest user turn is O(1)). `text` unchanged.
        user_msg = next((m.get("content", "") for m in reversed(msgs) if m.get("role") == "user"), "")
        return {"ok": True, "text": text,
                "trace": {"model": getattr(client, "model", None),
                          "system": sys_prompt, "user": user_msg, "completion": text}}

    @app.post("/api/runs/{run_id}/suggest")
    async def suggest(run_id: str, request: Request):
        """Turn the chat discussion (or a free-form instruction) into a CONCRETE experiment idea
        (operator + params + rationale) the UI can drop straight into the inject-node dialog.
        Uses structured output so the result is a ready-to-run Idea. Soft-fails offline."""
        rd = _run_dir(run_id)
        body = await request.json()
        nid = body.get("node_id")
        instruction = (body.get("instruction") or "").strip()
        history = body.get("messages") or []
        st = fold(_events(rd))
        from .models import Idea
        from .parse import parse_structured
        convo = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in history)
        prompt = ("Propose ONE next experiment as a structured Idea (operator one of "
                  "draft/improve/debug/merge; numeric params; a short rationale). Base it on the "
                  "run context and this discussion.\n\n" + _node_context(st, nid, rd)
                  + (f"\n\nDiscussion so far:\n{convo}" if convo else "")
                  + (f"\n\nInstruction: {instruction}" if instruction else ""))
        s = _llm_settings()
        try:
            client = make_llm_client(s)
        except Exception as e:  # noqa: BLE001 - offline / no model
            return JSONResponse({"ok": False, "error": str(e)}, status_code=200)
        try:
            idea = parse_structured(client, [{"role": "user", "content": prompt}], Idea, s.llm_parser)
            return {"ok": True, "idea": idea.model_dump(mode="json"), "parsed": True}
        except Exception:  # noqa: BLE001 - small models fumble strict tool-call output; fall back to
            # a free-text suggestion the operator can finish editing in the inject dialog. Never the
            # difference between "got a starting point" and "got nothing".
            try:
                text = client.complete_text([
                    {"role": "system", "content": "Reply with a one-line experiment suggestion: the "
                     "operator (improve/draft/debug), suggested params, and why — plain text."},
                    {"role": "user", "content": prompt}])
                return {"ok": True, "parsed": False,
                        "idea": {"operator": "improve", "params": {}, "rationale": text.strip()[:600]}}
            except Exception as e:  # noqa: BLE001
                return JSONResponse({"ok": False, "error": str(e)}, status_code=200)

    @app.post("/api/runs/{run_id}/command")
    async def command(run_id: str, request: Request):
        """Action-router (Workstream C): turn a free-text instruction into EITHER a concrete control
        action the UI confirms-then-executes, or a grounded advisory reply. Read-only itself — it
        never appends events; the UI calls /control after the human confirms. Soft-fails offline."""
        rd = _run_dir(run_id)
        body = await request.json()
        msgs = body.get("messages") or []
        nid = body.get("node_id")
        instruction = (body.get("instruction") or "").strip()
        st = fold(_events(rd))
        s = _llm_settings()
        try:
            client = make_llm_client(s)
        except Exception as e:  # noqa: BLE001 - offline / no model
            return JSONResponse({"ok": False, "error": str(e)}, status_code=200)
        sys_prompt = (
            "Translate the human's instruction about an automated ML experiment run into ONE action. "
            "If they are merely asking a question or discussing, use action='advise'. Otherwise pick "
            "the action and fill its fields. Actions: confirm(node_id), ablate(node_id), fork(node_id), "
            "promote(node_id), hint(text), strategy(policy,fidelity), deep_research, "
            "inject(operator,params,rationale[,node_id=parent]), approve(node_id), ratify, pause, "
            "resume, stop. Use the node in context when no id is given.\n\n" + _node_context(st, nid, rd))
        convo = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in msgs)
        user = (f"Instruction: {instruction}" if instruction else "") + (f"\n\nDiscussion:\n{convo}" if convo else "")
        from .parse import parse_structured
        # The LLM calls are blocking + network-bound; run them off the event loop (anyio worker
        # thread) so a slow model doesn't stall SSE tails / other endpoints for every UI client.
        try:
            c = await anyio.to_thread.run_sync(
                lambda: parse_structured(client, [{"role": "system", "content": sys_prompt},
                                                  {"role": "user", "content": user}], _Command, s.llm_parser))
            act = _command_to_action(c, st)
            if act is not None:
                act["rationale"] = c.rationale or ""
                return {"ok": True, "action": act}
        except Exception:  # noqa: BLE001 - parse fumble -> fall through to advisory reply
            pass
        try:
            advise_sys = ("You are an ML research collaborator embedded in an experiment loop. Answer "
                          "concisely, grounded on the run.\n" + _node_context(st, nid, rd))
            advise_msgs = msgs + ([{"role": "user", "content": instruction}] if instruction else [])
            text = await anyio.to_thread.run_sync(lambda: client.complete_text(
                [{"role": "system", "content": advise_sys}, *advise_msgs]))
            # Carry the LLM I/O back so the chat row can expand into a langfuse-style trace card. We
            # include the user's instruction + system prompt so the trace shows the real input, but
            # omit the rest of the echoed conversation (the client holds it) to avoid O(n²) growth.
            user_msg = instruction or next(
                (m.get("content", "") for m in reversed(msgs) if m.get("role") == "user"), "")
            return {"ok": True, "reply": text,
                    "trace": {"model": getattr(client, "model", None),
                              "system": advise_sys, "user": user_msg, "completion": text}}
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=200)

    @app.post("/api/runs/{run_id}/report_refresh")
    async def report_refresh(run_id: str):
        """Force a high-quality regeneration of the agent-authored run report NOW. Generates inline
        (like /chat) and appends a `report_generated` event directly, so it works whether or not the
        engine loop is alive — appends are lock-guarded, same as control events. Soft-fails offline:
        the deterministic report keeps rendering and no event is written."""
        rd = _run_dir(run_id)
        st = fold(_events(rd))
        s = _llm_settings()
        try:
            from .report import generate_report
            client = make_llm_client(s)
            # Offload the (blocking, network-bound) synthesis to a worker thread so a slow model
            # call doesn't freeze the event loop / SSE tails for every other connected UI client.
            content = await anyio.to_thread.run_sync(
                lambda: generate_report(st, client, parser=s.llm_parser, trigger="manual"))
        except Exception as e:  # noqa: BLE001 — offline / no model -> soft fail, no event
            return JSONResponse({"ok": False, "error": str(e)}, status_code=200)
        ev = EventStore(rd / "events.jsonl").append(
            "report_generated", {"content": content, "at_node": content.get("at_node"),
                                 "trigger": "manual"})
        return {"ok": True, "seq": ev.seq, "content": content}

    @app.get("/api/llm/health")
    def llm_health():
        """Liveness self-test for the configured LLM endpoint (the UI equivalent of `LoopLab
        smoke`): pings the model with a one-word prompt. Never raises — returns reachability so
        the UI can warn before a run launches against a dead endpoint."""
        s = _llm_settings()
        info = {"model": s.llm_model, "base_url": s.llm_base_url}
        try:
            client = make_llm_client(s)
            txt = client.complete_text([{"role": "user", "content": "Reply with one word: ready"}])
            return {"ok": True, "text": (txt or "").strip()[:80], **info}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e), **info}

    # ------------------------------------------------------------------ GPU monitor
    @app.get("/api/gpu")
    def gpu():
        try:
            q = ("--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,"
                 "power.draw")
            out = subprocess.run(["nvidia-smi", q, "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=4)
            if out.returncode != 0 or not out.stdout.strip():
                return {"available": False}
            gpus = []
            for line in out.stdout.strip().splitlines():
                p = [c.strip() for c in line.split(",")]
                if len(p) >= 6:
                    gpus.append({"name": p[0], "util": _f(p[1]), "mem_used": _f(p[2]),
                                 "mem_total": _f(p[3]), "temp": _f(p[4]), "power": _f(p[5])})
            return {"available": True, "gpus": gpus}
        except Exception:  # noqa: BLE001 - no GPU / no nvidia-smi -> soft fail
            return {"available": False}

    # ------------------------------------------------------------------ authoring (files-as-truth)
    def _author_dir(kind: str) -> Optional[Path]:
        s = Settings()
        m = {"prompts": s.prompt_dir, "skills": s.skills_dir, "knowledge": s.knowledge_dir}
        d = m.get(kind)
        return Path(d) if d else None

    @app.get("/api/{kind}")
    def list_author(kind: str):
        if kind not in ("prompts", "skills", "knowledge"):
            raise HTTPException(404, "unknown kind")
        d = _author_dir(kind)
        if d is None or not d.exists():
            return {"dir": (str(d) if d else None), "files": []}
        files = [{"name": p.name, "text": p.read_text(encoding="utf-8", errors="replace")}
                 for p in sorted(d.glob("*.md"))]
        return {"dir": str(d), "files": files}

    @app.put("/api/{kind}/{name}")
    async def write_author(kind: str, name: str, request: Request):
        if kind not in ("prompts", "skills", "knowledge"):
            raise HTTPException(404, "unknown kind")
        d = _author_dir(kind)
        if d is None:
            raise HTTPException(400, f"no {kind} dir configured (set LOOPLAB_{kind.upper()}_DIR)")
        d.mkdir(parents=True, exist_ok=True)
        target = (d / name).resolve()
        if d.resolve() not in target.parents:    # path-traversal guard
            raise HTTPException(400, "bad name")
        body = await request.body()
        target.write_text(body.decode("utf-8"), encoding="utf-8")  # engine hot-reloads on next run
        return {"ok": True, "name": name}

    @app.get("/api/memory")
    def memory():
        s = Settings()
        if not s.memory_dir:
            return {"dir": None, "cases": []}
        md = Path(s.memory_dir)
        cases: list[dict] = []
        for f in sorted(md.glob("*.jsonl")) if md.exists() else []:
            cases.extend(iter_jsonl(f))
        return {"dir": str(md), "cases": cases}

    # ------------------------------------------------------------------ static React app
    dist = _ui_dist()

    def _index_response():
        # G1: inject the UI token into the served page (same-origin <meta>) when auth is on, so the
        # SPA can echo it on mutating requests. Without a token, serve the file unchanged.
        html_path = dist / "index.html"
        if ui_token:
            html = html_path.read_text(encoding="utf-8")
            meta = f'<meta name="ll-token" content="{ui_token}">'
            return HTMLResponse(html.replace("</head>", meta + "</head>", 1))
        return FileResponse(str(html_path))

    if dist.exists():
        app.mount("/assets", StaticFiles(directory=str(dist / "assets")), name="assets")

        @app.get("/")
        def index():
            return _index_response()

        @app.get("/{path:path}")
        def spa(path: str):
            # SPA fallback for client-side routes; never shadow /api. Resolve-guard the path so a
            # traversal (`/..%2f..%2fwin.ini`) can't read a file outside the built assets dir — the
            # other file routes guard, this one used to serve any readable file (review C3).
            base = dist.resolve()
            target = (dist / path).resolve()
            if (target == base or base in target.parents) and target.is_file():
                return FileResponse(str(target))
            return _index_response()
    else:
        @app.get("/")
        def index_placeholder():
            return JSONResponse({
                "looplab_ui": "backend up; the React app is not built yet",
                "build": "cd ui && npm ci --strict-ssl=false && npm run build",
                "api": ["/api/runs", "/api/runs/{id}/state", "/api/runs/{id}/events (SSE)"],
            })

    return app


def _f(s: str) -> Optional[float]:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def serve(run_root: str | os.PathLike, host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn
    uvicorn.run(make_app(run_root), host=host, port=port, log_level="info")
