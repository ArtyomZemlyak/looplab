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
from .replay import fold

# These imports require the [ui] extra; importing this module without it raises a clear error.
try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse,
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
    "approval_granted", "spec_approved",
}

POLL_SECONDS = 0.4   # SSE tail cadence — fast enough to feel live, light on the disk


def _ui_dist() -> Path:
    """Built React assets. Override with LOOPLAB_UI_DIST; default <repo>/ui/dist."""
    env = os.environ.get("LOOPLAB_UI_DIST")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[1] / "ui" / "dist"


def make_app(run_root: str | os.PathLike) -> "FastAPI":
    root = Path(run_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="LoopLab UI", version="0.1.0")
    # Local dev tool: the Vite dev server (5173) hits the API on another port.
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                       allow_headers=["*"])

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
        for n in d.get("nodes", {}).values():
            n.pop("code", None); n.pop("files", None)
            n["stdout_tail"] = (n.get("stdout_tail") or "")[:160]
            n["error"] = (n.get("error") or "")[:160]
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
                }
                _summary_cache[rd.name] = (*sig, summary)
                out.append(summary)
            except Exception:  # noqa: BLE001 - a half-written run shouldn't break the list
                continue
        return out

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
        meta = rd / "ui_meta.json"
        if not meta.exists():
            raise HTTPException(400, "run has no ui_meta.json (start it via the UI to enable resume)")
        task_file = json.loads(meta.read_text(encoding="utf-8")).get("task_file")
        if not task_file:
            raise HTTPException(400, "ui_meta.json missing task_file")
        _spawn_engine(["resume", str(rd), "--task-file", str(task_file)])
        return {"ok": True}

    @app.post("/api/start")
    async def start_run(request: Request):
        body = await request.json()
        task_file = body.get("task_file")
        run_id = body.get("run_id")
        if not task_file or not run_id:
            raise HTTPException(400, "task_file and run_id are required")
        rd = (root / run_id).resolve()
        if root not in rd.parents and rd != root:
            raise HTTPException(400, "bad run_id")
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "ui_meta.json").write_text(json.dumps({"task_file": str(task_file)}), encoding="utf-8")
        extra = []
        for k in ("backend", "developer_backend", "model", "max_nodes"):
            if body.get(k) is not None:
                extra += [f"--{k.replace('_', '-')}", str(body[k])]
        if body.get("require_approval"):
            extra += ["--require-approval"]
        _spawn_engine(["run", str(task_file), "--out", str(rd), *extra])
        return {"ok": True, "run_id": run_id}

    def _spawn_engine(cli_args: list[str]) -> None:
        cmd = [sys.executable, "-m", "looplab.cli", *cli_args]
        kw: dict = {"cwd": str(Path(__file__).resolve().parents[1])}
        if os.name == "nt":
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # detached, survives request
        else:
            kw["start_new_session"] = True
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)

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
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=str(dist / "assets")), name="assets")

        @app.get("/")
        def index():
            return FileResponse(str(dist / "index.html"))

        @app.get("/{path:path}")
        def spa(path: str):
            # SPA fallback for client-side routes; never shadow /api.
            f = (dist / path)
            if f.is_file():
                return FileResponse(str(f))
            return FileResponse(str(dist / "index.html"))
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
