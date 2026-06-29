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
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

import anyio
import orjson

from .atomicio import atomic_write_text
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


# Workstream C → agentic boss: the chat action-router LLM emits a _Plan — a short conversational reply
# plus an ORDERED list of _Action steps. Each _Action maps to a control {type, data} the UI applies
# (boss mode auto-applies them in order, then reopens/resumes the run ONCE if any step needs the
# engine). An empty actions list = pure conversation (only the reply is shown).
from pydantic import BaseModel  # noqa: E402


class _Action(BaseModel):
    action: str = "advise"   # advise|confirm|ablate|fork|promote|hint|note|strategy|budget|deep_research|inject|import|approve|ratify|pause|resume|stop
    node_id: Optional[int] = None
    text: str = ""           # hint text / note text / free rationale
    operator: str = "improve"
    params: dict = {}
    policy: str = ""
    fidelity: str = ""
    source_run: str = ""     # import: the SIBLING run to seed an experiment from
    source_node: Optional[int] = None   # import: which experiment (node id) of that sibling run
    nodes: int = 0           # budget: add this many experiment nodes to the run's node budget
    rationale: str = ""      # one-line why for THIS step (shown on its applied row)


class _Plan(BaseModel):
    reply: str = ""               # conversational narration to the human (always shown)
    actions: list[_Action] = []   # ordered steps to apply; empty list = pure advice


class _GenesisSpec(BaseModel):
    """The BOSS's proposed plan for a brand-new run, bootstrapped from a one-line goal (there is no run
    yet, so this is the pre-run counterpart of the per-message _Plan). The UI shows it as an editable spec card and
    only launches on confirm — creating a run spends real tokens, so we propose-then-go, never silently
    auto-start. The task is EITHER an inline `task` object (e.g. {"kind":"mlebench_real","competition":
    "..."}) OR a path to an existing catalogue `task_file`; `settings` carries only the engine overrides
    the goal implies (model, node budget, policy…), the rest fall back to the UI defaults."""
    run_id: str = ""          # invented kebab-case run name (we slugify + de-dup server-side)
    task: dict = {}           # inline task JSON (kind + params) when authoring a fresh task
    task_file: str = ""       # OR a path from the catalogue (preferred when one matches)
    settings: dict = {}       # engine overrides only (llm_model, max_nodes, n_seeds, policy, …)
    rationale: str = ""       # one-line why-this-plan
    reply: str = ""           # conversational message to show in the genesis chat
    # Adaptation checklist: concrete steps the operator must take to make their target ready (chiefly
    # for kind="repo" — expose a metric, pin deps, choose the edit surface, protect the grader…). The
    # UI renders these as a to-do list under the spec card. Empty for a ready-to-run catalogue task.
    setup_steps: list[str] = []


def _action_to_control(c: "_Action", st) -> Optional[dict]:
    """Map ONE classified boss action to a control {type, data, label}. None => no actionable verb
    (a pure-advice step, skipped). `label` is the human-readable line the UI's applied-row shows."""
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
        # The boss authors the COMPLETE current directive each turn (it has the full chat + run
        # context), so a boss hint REPLACES the standing one rather than piling up — the researcher/
        # agent/strategist then read a single, current directive instead of a contradictory stack.
        return {"type": "hint", "data": {"text": c.text, "replace": True},
                "label": f"Set directive: {c.text[:60]}"}
    if a in ("note", "annotate") and nid is not None and c.text:
        return {"type": "annotation", "data": {"node_id": nid, "text": c.text}, "label": f"Note on #{nid}: {c.text[:50]}"}
    if a in ("budget", "extend_budget"):
        n = int(c.nodes)
        if n <= 0:                       # a budget verb only ADDS room — ignore zero/negative (no-op)
            return None
        n = min(n, 1000)                 # cap a hallucinated huge delta so the LLM can't trigger a runaway
        return {"type": "budget_extend", "data": {"add_nodes": n},
                "label": f"Extend the run budget by {n} node(s)"}
    if a == "strategy" and (c.policy or c.fidelity):
        strat = {k: v for k, v in (("policy", c.policy), ("fidelity", c.fidelity)) if v}
        pretty = " ".join(f"{k}={v}" for k, v in strat.items())   # "policy=ucb fidelity=low", not a dict repr
        return {"type": "set_strategy", "data": {"strategy": strat}, "label": f"Switch strategy → {pretty}"}
    if a == "deep_research":
        return {"type": "deep_research", "data": {}, "label": "Run a deep-research step now"}
    if a == "inject":
        idea = {"operator": c.operator or "improve", "params": c.params or {}, "rationale": c.rationale or c.text or ""}
        pp = " ".join(f"{k}={v}" for k, v in (idea["params"] or {}).items())   # "lr=0.1 depth=3", not a dict repr
        return {"type": "inject_node", "data": {"idea": idea, "parent_id": nid, "code": None},
                "label": f"Add experiment: {idea['operator']}" + (f" ({pp})" if pp else "")}
    if a == "import" and c.source_run and c.source_node is not None:
        # Seed an experiment FROM a sibling run. The source idea/code/metric are resolved from disk at
        # apply time (in /control), which then bakes `origin` provenance into the inject_node event —
        # so this rides the existing manual-injection pipeline, no new event type.
        return {"type": "inject_node",
                "data": {"idea": {"operator": c.operator or "improve", "params": c.params or {},
                                  "rationale": c.rationale or c.text or ""},
                         "parent_id": nid, "code": None,
                         "source_run": c.source_run, "source_node": int(c.source_node)},
                "label": f"Import #{c.source_node} from run {c.source_run}"}
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


def _plan_to_actions(plan: "_Plan", st) -> list[dict]:
    """Map a boss plan to the ORDERED list of control actions, dropping pure-advice steps. Each
    carries its own `rationale` so the UI's applied-row can explain that step."""
    out: list[dict] = []
    for step in plan.actions:
        ctrl = _action_to_control(step, st)
        if ctrl is not None:
            ctrl["rationale"] = step.rationale or ""
            out.append(ctrl)
    return out


def _client_tokens(client) -> Optional[dict]:
    """Best-effort token usage for ONE chat request. `make_llm_client` mints a fresh client per
    request, so its accountant totals already SUM every sub-call this turn made (the boss tool-loop
    can fire several). Shape matches the UI's `callTok` reader ({prompt, completion, total}). None
    when the client/model doesn't report usage (older local servers) — the UI just omits the badge."""
    acc = getattr(client, "accountant", None)
    if acc is not None and getattr(acc, "total_tokens", 0):
        return {"prompt": acc.prompt_tokens, "completion": acc.completion_tokens,
                "total": acc.total_tokens, "calls": acc.calls}
    u = getattr(client, "_last_usage", None) or {}
    if u:
        return {"prompt": u.get("prompt_tokens", 0), "completion": u.get("completion_tokens", 0),
                "total": u.get("total_tokens", 0), "calls": 1}
    return None


def _ui_dist() -> Path:
    """Built React assets. Override with LOOPLAB_UI_DIST; default <repo>/ui/dist.

    Single source of truth shared with `uibuild` (the on-launch auto-builder)."""
    from .uibuild import ui_dist_dir
    return ui_dist_dir()


# Per-theme rollup for the cross-run map: {theme: {count, best_metric}}. Now lives in `digest` so the
# Researcher's working-set digest and this UI endpoint share one definition.
from .digest import theme_rollup as _theme_rollup

# run-root subdirectories that are NOT runs and must never be used as a run_id (would collide with the
# cross-run scope-report store at <run-root>/reports/).
_RESERVED_RUN_IDS = {"reports"}


def _engine_alive(rd: Path) -> bool:
    """True iff a LIVE engine process currently drives this run. The engine holds an exclusive OS lock on
    <run_dir>/engine.lock for its whole lifetime (cli._engine_singleton) and the OS frees it on exit —
    even on crash — so this is a race-free, staleness-free liveness signal: a non-blocking acquire that
    FAILS means a process holds it (alive); one that SUCCEEDS means none does (a finished run, or a
    ZOMBIE whose engine died without emitting run_finished — the bug this distinguishes from "thinking").

    Probe-and-release: we never hold the lock past this call, and close the handle in `finally` so even a
    mid-probe error can't leak a lock that would block a real resume. Best-effort — any error → False."""
    lock = rd / "engine.lock"
    if not lock.exists():
        return False                     # no engine has ever locked this dir (or it predates the lock)
    try:
        f = open(lock, "a+")
    except OSError:
        return False
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return True              # byte held by a live engine
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)   # we got it → no engine; release at once
            return False
        else:
            import fcntl
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            return False
    except OSError:
        return False                     # platform without file locking → can't tell → assume not alive
    finally:
        f.close()


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
        # Liveness: is a real engine process driving this run RIGHT NOW? (lock probe, not the event log).
        # A run with finished=False but engine_running=False is a ZOMBIE — the UI uses this to stop
        # showing a perpetual "thinking" strip and to resume on the next engine-needing chat action.
        d["engine_running"] = _engine_alive(rd)
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
                    # Cross-run lineage: distinct sibling run_ids this run SEEDED experiments from
                    # (via `import`). Drives the MapView's "derived-from" edges. Empty for most runs.
                    "seeded_from": sorted({n.origin["run_id"] for n in st.nodes.values()
                                           if isinstance(n.origin, dict) and n.origin.get("run_id")}),
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
        st_assign = pdata.get("supertask_assignments", {})
        # engine_running is a live fact (lock probe), so it stays OUT of the mtime-keyed summary cache —
        # a zombie's events.jsonl is unchanged, yet its liveness flips the instant its engine dies.
        return [{**s, "project_id": assignments.get(s["run_id"]),
                 "label": labels.get(s["run_id"]),
                 "supertask_id": st_assign.get(s["run_id"]),
                 "engine_running": _engine_alive(root / s["run_id"])} for s in out]

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

    # ------------------------------------------------------------------ super-tasks (flat axis)
    @app.get("/api/supertasks")
    def list_supertasks():
        data = projects.load()
        return {"supertasks": data["supertasks"], "assignments": data["supertask_assignments"]}

    @app.post("/api/supertasks")
    async def create_supertask(request: Request):
        body = await request.json()
        try:
            st = projects.create_supertask(body.get("name", ""), body.get("task_id"))
        except ProjectError as e:
            raise HTTPException(400, str(e))
        return st

    @app.patch("/api/supertasks/{sid}")
    async def patch_supertask(sid: str, request: Request):
        body = await request.json()
        try:
            projects.rename_supertask(sid, body.get("name", ""))
        except ProjectError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.delete("/api/supertasks/{sid}")
    def delete_supertask(sid: str):
        try:
            projects.delete_supertask(sid)
        except ProjectError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.post("/api/runs/{run_id}/supertask")
    async def assign_supertask(run_id: str, request: Request):
        _run_dir(run_id)   # 404 guard: only real runs can be filed
        body = await request.json()
        try:
            projects.assign_supertask(run_id, body.get("supertask_id"))
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
            last_alive = None
            # engine_running is a post-fold liveness probe with no seq of its own: a run that dies
            # AFTER its last event (a zombie) never advances seq, so also re-emit when liveness flips,
            # else the stalled/zombie UI never updates over a live stream.
            # Initial snapshot so a fresh/reconnecting client is immediately correct.
            while True:
                if await request.is_disconnected():
                    break
                payload = await anyio.to_thread.run_sync(_state_payload, rd)
                alive = payload["state"].get("engine_running")
                if payload["seq"] != last_sent or alive != last_alive:
                    last_sent = payload["seq"]
                    last_alive = alive
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
        try:
            return build_trace_view(fold(_events(rd)), load_spans(rd / "spans.jsonl"))
        except Exception:  # noqa: BLE001 — a malformed/foreign spans.jsonl must degrade, not 500
            return {"run_id": run_id, "task_id": "", "nodes": {}, "unscoped": [],
                    "summary": {"spans": 0, "errors": 0, "total_eval_seconds": 0}}

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

    @app.put("/api/runs/{run_id}/config")
    async def put_run_config(run_id: str, request: Request):
        """Per-run settings edit: rewrite THIS run's config.snapshot.json so a later RESUME re-enters
        the loop with the new values. Resume reads the snapshot via `Settings(**data)` (cli.py) — it
        does NOT read the UI's global new-run defaults — so editing the snapshot is the only way to
        change a specific run's settings (e.g. raise `timeout`, enable timeout repair) before continuing it.

        Writing the snapshot is SAFE even while the engine is live: the only other writer is `run` at
        startup (under the singleton lock, before the loop), and a running engine never re-reads the
        snapshot — so a concurrent PUT can't corrupt or race it. A live engine just keeps its in-memory
        settings until it's stopped & resumed; we return `engine_running` so the UI can say "applies on
        the next restart" (and offer a pause→resume to apply now). Only known, non-secret fields are
        applied (masked llm_api_key + any unknown keys preserved); the merged config is validated through
        `Settings()` so a bad value (e.g. n_seeds<1, timeout<=0, bad enum) is rejected 422 — with the
        offending field surfaced — instead of poisoning the next resume."""
        from pydantic import ValidationError
        rd = _run_dir(run_id)
        snap = rd / "config.snapshot.json"
        if not snap.exists():
            raise HTTPException(404, "run has no config.snapshot.json (it predates self-describing runs)")
        body = await request.json()
        incoming = body.get("settings", body) or {}
        allowed, secret = set(Settings.model_fields), {"llm_api_key"}
        current = json.loads(snap.read_text(encoding="utf-8"))
        updated = dict(current)
        changed = {}
        for k, v in incoming.items():        # apply only known, non-secret, actually-set fields
            if k in allowed and k not in secret and v is not None and current.get(k) != v:
                updated[k] = v
                changed[k] = v
        # Validate the MERGED config before persisting. Only KNOWN, non-secret fields are passed to
        # Settings (the masked secret re-reads from env/default at resume; any unknown/forward-compat
        # keys are preserved on disk but not validated). A ValidationError is reported per-field so the
        # UI can tell the user EXACTLY what's wrong (the original bug: an opaque "422").
        try:
            Settings(**{k: v for k, v in updated.items() if k in allowed and k not in secret})
        except ValidationError as e:
            fields = "; ".join(f"{'.'.join(str(x) for x in err['loc']) or '?'}: {err['msg']}"
                               for err in e.errors())
            raise HTTPException(422, f"invalid settings — {fields}")
        except Exception as e:               # noqa: BLE001 - any other coercion error
            raise HTTPException(422, f"invalid settings: {e}")
        atomic_write_text(snap, json.dumps(updated, indent=2))
        return {"ok": True, "config": updated, "changed": sorted(changed),
                "engine_running": _engine_alive(rd)}

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
        # Cross-run import: an inject seeded from a sibling run. Resolve the source experiment from disk
        # NOW and bake its code + `origin` provenance into the inject_node, so the engine reproduces it
        # faithfully and the lineage is recorded. (_run_dir guards path traversal on the sibling id.)
        if etype == "inject_node" and data.get("source_run") and data.get("source_node") is not None:
            sr = str(data.pop("source_run"))
            sn = int(data.pop("source_node"))
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
    @app.post("/api/runs/{run_id}/resume")
    def resume_run(run_id: str):
        rd = _run_dir(run_id)
        # Don't spawn a second engine when one is already alive: the engine-singleton lock would make it
        # no-op anyway, but skipping the detached Popen keeps the signal honest (and avoids a phantom
        # process flash). The UI's auto-resume already gates on engine_running; this is the backstop.
        if _engine_alive(rd):
            return {"ok": True, "already_running": True}
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
        task_file: Optional[str] = None
        meta = rd / "ui_meta.json"
        if meta.exists():
            task_file = json.loads(meta.read_text(encoding="utf-8")).get("task_file")
        if not task_file and (rd / "task.snapshot.json").exists():
            task_file = str(rd / "task.snapshot.json")
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
                env = _settings_env({k: v for k, v in cfg.items()
                                     if k in _ALLOWED_FIELDS and k not in _SECRET_FIELDS and v is not None})
            except (OSError, json.JSONDecodeError, ValueError):
                env = None
        _spawn_engine(["run", str(task_file), "--out", str(rd)], env=env)
        return {"ok": True}

    @app.post("/api/start")
    async def start_run(request: Request):
        body = await request.json()
        run_id = body.get("run_id")
        if not run_id:
            raise HTTPException(400, "run_id is required")
        if run_id in _RESERVED_RUN_IDS:        # don't let a run clobber the cross-run report store dir
            raise HTTPException(400, f"run_id {run_id!r} is reserved")
        rd = (root / run_id).resolve()
        if root not in rd.parents and rd != root:
            raise HTTPException(400, "bad run_id")
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
            from .tasks import kinds, validate_task
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
                        "ts": t0 + i * 1e-3, "seq": int(1e15) + i, "genesis": True}) + b"\n")
                f.flush()
                os.fsync(f.fileno())
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

    # --- Secret store (B3/ADR-11): secrets (the LLM API key) are NEVER written to ui_settings.json
    # or a run's config.snapshot.json (which only ever record `***`). They live in a separate,
    # owner-only file and are applied to this server process's env + every spawned engine via env —
    # so the value transits as a credential, not as a persisted, reportable setting. The HTTP API
    # only ever echoes the masked `***`, never the value.
    _secrets_path = root / "secrets.json"
    # Derive the secret -> env-var map from the SAME _SECRET_FIELDS set the rest of the server uses to
    # strip secrets, via the standard env_prefix convention (the LOOPLAB_{KEY} rule _settings_env also
    # uses). A NEW SecretStr field is then covered by editing ONE place (_SECRET_FIELDS), not three.
    _secret_prefix = Settings.model_config.get("env_prefix", "LOOPLAB_")
    _SECRET_ENV = {k: f"{_secret_prefix}{k.upper()}" for k in _SECRET_FIELDS}   # UI key -> LOOPLAB_* env

    def _load_secrets() -> dict:
        try:
            d = json.loads(_secrets_path.read_text(encoding="utf-8"))
            return {k: v for k, v in d.items() if k in _SECRET_ENV and isinstance(v, str) and v}
        except (OSError, json.JSONDecodeError):
            return {}

    def _store_secret(key: str, value: str) -> None:
        d = _load_secrets()
        if value:
            d[key] = value
        else:
            d.pop(key, None)
        # Write through a temp file that is owner-only FROM CREATION (mkstemp creates 0600), then
        # atomically rename. This closes the window where atomic_write_text + a later chmod would leave
        # the plaintext key world-readable at the default umask between the rename and the chmod.
        fd, tmp = tempfile.mkstemp(dir=str(_secrets_path.parent), prefix=".secrets-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(d))
            os.replace(tmp, _secrets_path)    # the 0600 mode rides along from the temp inode
        finally:
            try:
                os.unlink(tmp)                # no-op once the rename consumed it; cleans up on write error
            except OSError:
                pass
        try:                                  # belt-and-suspenders (no-op on Windows)
            os.chmod(_secrets_path, 0o600)
        except OSError:
            pass
        env_name = _SECRET_ENV[key]           # live-apply: in-process LLM + future spawns see it now
        if value:
            os.environ[env_name] = value
        else:
            os.environ.pop(env_name, None)

    # Prime this process's env from the stored secrets at startup, WITHOUT clobbering a value the
    # operator exported explicitly (an env var / .env wins over the saved store).
    for _k, _v in _load_secrets().items():
        os.environ.setdefault(_SECRET_ENV[_k], _v)

    def _resolved_settings(s: Optional["Settings"] = None) -> dict:
        """Engine defaults (Settings(): defaults+env) overlaid with the saved UI overrides — i.e.
        exactly what a new run gets if the launch dialog changes nothing. Secret masked. Pass an
        already-built Settings to avoid constructing one (and re-reading .env from disk) a 2nd time."""
        base = (s or Settings()).masked_snapshot()
        base.update(_load_ui_settings())
        # Keep llm_api_key but ONLY as the mask masked_snapshot already applied ("***" when set, else
        # None) — the UI needs the set/unset state to render the secret field; the value never leaks.
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
        s = Settings()                        # build once — each Settings() now also reads .env off disk
        defaults = s.model_dump()
        defaults.pop("llm_api_key", None)
        return {"settings": _resolved_settings(s), "overrides": _load_ui_settings(), "defaults": defaults}

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

    @app.put("/api/settings/secret")
    async def put_secret(request: Request):
        """Store (or clear) a secret credential securely. The value is written owner-only to
        secrets.json (never ui_settings.json / a run snapshot) and applied to the server + spawned
        engines as env. The response only reports whether a value is now set — never the value."""
        body = await request.json()
        key = body.get("key")
        if key not in _SECRET_ENV:
            raise HTTPException(400, f"unknown secret {key!r} (known: {sorted(_SECRET_ENV)})")
        value = body.get("value")
        if value is not None and not isinstance(value, str):
            raise HTTPException(400, "value must be a string (or null to clear)")
        _store_secret(key, (value or "").strip())
        return {"ok": True, "key": key, "set": bool((value or "").strip())}

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

    # ------------------------------------------------------------------ genesis (pre-run BOSS)
    def _normalize_genesis(spec: "_GenesisSpec", draft: dict) -> dict:
        """Turn the boss's raw proposal into a launch-ready, editable card: slugify + de-dup the run_id
        against existing run dirs, keep only known non-secret setting overrides, and invent a name when
        the model didn't give one."""
        def _slug(s):
            return re.sub(r"(^-|-$)", "", re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()))[:40]

        task = spec.task if isinstance(spec.task, dict) else {}
        task_file = spec.task_file or ""
        base = (_slug(spec.run_id) or _slug(task.get("competition")) or _slug(task.get("kind"))
                or _slug(Path(task_file).stem if task_file else "") or "run")
        run_id, n = base, 2
        # A name is "taken" only when it holds a REAL run (events.jsonl) — matches /api/start's 409 — so
        # a leftover empty dir (e.g. a validation-failed materialization) doesn't force a -2 suffix.
        while (root / run_id / "events.jsonl").exists():
            run_id, n = f"{base}-{n}", n + 1
        settings = {k: v for k, v in (spec.settings or {}).items()
                    if k in _ALLOWED_FIELDS and k not in _SECRET_FIELDS and v is not None}
        steps = [str(s).strip() for s in (spec.setup_steps or []) if str(s).strip()][:12]
        return {"run_id": run_id, "task": task, "task_file": task_file,
                "settings": settings, "rationale": spec.rationale or "", "setup_steps": steps}

    @app.post("/api/genesis")
    async def genesis(request: Request):
        """Pre-run BOSS: turn a one-line goal into an editable run spec (name + task + key settings).
        No run exists yet, so this grounds on the task catalogue + registered kinds + the current default
        settings and PROPOSES a plan the UI shows as an editable card — we launch on confirm via
        /api/start, never here (creating a run spends real tokens). Refinement turns pass the prior
        `draft` so the boss edits it in place. Degrades cleanly when no model is reachable."""
        from .tasks import kinds
        body = await request.json()
        msgs = body.get("messages") or []
        instruction = (body.get("instruction") or "").strip()
        draft = body.get("draft") or {}
        catalogue = list_tasks().get("tasks", [])
        cat_lines = "\n".join(
            f"- {t['name']} (kind={t['kind']}, path={t['path']}): {str(t.get('goal', ''))[:90]}"
            for t in catalogue[:40]) or "(none)"
        defaults = _resolved_settings()
        key_defaults = {k: defaults.get(k) for k in
                        ("llm_model", "llm_base_url", "llm_temperature", "max_nodes", "n_seeds", "policy")}
        sys_prompt = (
            "You are the BOSS that bootstraps a NEW autonomous-ML run from the user's goal. Decide the "
            "whole plan and return it as ONE structured spec:\n"
            "- run_id: a short, memorable kebab-case name you invent (e.g. 'nomad-minimax', "
            "'titanic-baseline'). NEVER ask the user for it.\n"
            "- the TASK: if an existing catalogue entry clearly matches, set task_file to its path. "
            "Otherwise AUTHOR an inline `task` object. For a Kaggle / MLE-bench competition use "
            '{"kind":"mlebench_real","competition":"<id>"} with the FULL slug exactly as on Kaggle — '
            "e.g. 'nomad2018-predict-transparent-conductors' (NOT the short 'nomad2018'), "
            "'spooky-author-identification'.\n"
            "- REPO task (the agent optimizes an EXISTING code repo on this machine): author "
            '{"kind":"repo","goal":"<what to optimize>","direction":"max"|"min",'
            '"editable_path":"<absolute path to the repo the agent may edit>",'
            '"edit_surface":["**/*.py"],'
            '"eval":{"command":["python","train.py"],"cwd":".",'
            '"metric":{"kind":"stdout_json","key":"<the key the command prints>"},'
            '"setup":["pip","install","-r","requirements.txt"],"timeout":1800}}. '
            "The `eval.command` is the OPERATOR's trusted way to RUN and score the repo (argv, no "
            "shell); it must print the metric the loop reads (e.g. a final JSON line "
            '{"metric": 0.93}). If the user states HOW the repo is run but NOT how it is scored, set '
            '"onboard": true with "onboard_command" = that run command and ask in `reply` how the '
            "metric is emitted. Copy any path / command / metric-key the user gives VERBATIM; never "
            "invent a path you weren't given (leave editable_path empty and ask instead).\n"
            "- setup_steps: WHENEVER the task is a repo, return a concrete checklist of what the user "
            "must do to make the repo LoopLab-ready, e.g.: 'Expose a metric — print one JSON line "
            '{"metric": <score>} at the end of the eval command\'; \'Pin dependencies in '
            "requirements.txt so setup can install them'; 'Set edit_surface to only the files the "
            "agent should change (e.g. src/model/**.py)'; 'Protect the eval/grader/answer files so "
            "the agent can't overwrite them'; 'Add a cheap smoke profile (few steps) so the search is "
            "fast'. One actionable line each; [] for a ready-to-run catalogue task.\n"
            "- settings: ONLY the overrides the goal implies. CRITICAL: if the user mentions ANY model "
            "name — even mid-sentence ('on minimax/minimax-m3', 'with deepseek') — copy it VERBATIM into "
            "settings.llm_model; when it is an OpenRouter-style 'vendor/model' id (contains '/') also set "
            "settings.llm_base_url='https://openrouter.ai/api/v1'. Map phrasing like '100 nodes' → "
            "max_nodes, 'N seeds' → n_seeds. Leave everything else to the defaults.\n"
            "- reply: a short, friendly message stating the plan in one or two sentences.\n"
            "- rationale: one terse line on why.\n"
            "When the goal is too vague to choose a task, still invent a sensible run_id, leave task / "
            "task_file empty, and ask ONE clarifying question in `reply`.\n\n"
            f"Registered task kinds: {kinds()}\n"
            f"Default settings (override only what matters): {json.dumps(key_defaults)}\n"
            f"Task catalogue:\n{cat_lines}\n")
        prior = _prior_learnings_index()
        if prior:
            sys_prompt += ("\nPrior cross-run learnings (portfolio reports from earlier runs — use them "
                           "to pick a better task / model / settings, and reference them in your reply "
                           "when relevant):\n" + prior + "\n")
        if draft:
            sys_prompt += ("\nThe user is refining this current draft — edit it in place, keeping the "
                           "fields they didn't ask to change:\n" + json.dumps(draft)[:1200])
        convo = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in msgs)
        user = (f"Goal: {instruction}" if instruction else "") + (f"\n\nConversation:\n{convo}" if convo else "")
        from .parse import parse_structured
        try:
            client = make_llm_client(_llm_settings(None))
            spec = await anyio.to_thread.run_sync(
                lambda: parse_structured(client, [{"role": "system", "content": sys_prompt},
                                                  {"role": "user", "content": user}],
                                         _GenesisSpec, defaults.get("llm_parser", "tool_call")))
        except Exception as e:  # noqa: BLE001 - offline / no model -> soft fail with a usable message
            return {"ok": False, "error": str(e),
                    "spec": {"run_id": "", "task": {}, "task_file": "", "settings": {}, "rationale": ""},
                    "reply": f"Couldn't reach the model to plan this ({e}). You can still use the manual form."}
        return {"ok": True, "spec": _normalize_genesis(spec, draft),
                "reply": spec.reply or "Here's a plan — tweak the card and launch."}

    # ------------------------------------------------------------------ LLM (chat / suggest / health)
    def _llm_settings(rd: Optional[Path] = None) -> "Settings":
        """Settings for the UI-side LLM calls (chat/command/suggest/report). ONE source of truth per
        run: when the run has a `config.snapshot.json`, its llm_model/base_url/temperature WIN — so
        chat (and the action-router) speak with the SAME model the run was launched with, which keeps
        the conversation reproducible and the trace honest even if the UI server's own env points at a
        different model. Falls back to the UI's saved LLM overrides + env when there's no snapshot (or
        for a run-less call). The api_key is NEVER read from the snapshot (it's masked there) — it
        always comes from the server env."""
        over = {k: v for k, v in _load_ui_settings().items()
                if k in {"llm_model", "llm_base_url", "llm_temperature"}}
        if rd is not None:
            try:
                cfg = json.loads((rd / "config.snapshot.json").read_text(encoding="utf-8"))
                for k in ("llm_model", "llm_base_url", "llm_temperature"):
                    if cfg.get(k) is not None:
                        over[k] = cfg[k]
            except (OSError, json.JSONDecodeError, ValueError):
                pass   # no/!readable snapshot -> keep the UI/env defaults
        return Settings(**over)

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

    def _boss_context(st, nid: Optional[int], full: "Path") -> str:
        """Richer grounding for the BOSS (action-router + advisory chat): the node brief PLUS the
        experiments digest (top / weakest / failures / themes — the working set) PLUS the latest
        agent-authored report. So the boss decides WITH context (what's been tried, what's winning,
        what failed) instead of just the single best node — and can still reach for the run-tools
        when even this isn't enough."""
        from .digest import experiments_digest
        parts = [_node_context(st, nid, full)]
        dg = experiments_digest(st)
        if dg:
            parts.append(dg)
        # st.report is the _ReportOut dump (headline/verdict/champion_summary + lists) — NOT a 'content'
        # string — so stitch the high-signal fields into a readable brief. (A legacy/plain-string
        # report, or a {'content': ...} shape, is used as-is.)
        rep = getattr(st, "report", None)
        rtext = ""
        if isinstance(rep, str):
            rtext = rep
        elif isinstance(rep, dict):
            inner = rep.get("content")
            if isinstance(inner, str):
                rtext = inner                                  # legacy plain-string content
            else:
                src = inner if isinstance(inner, dict) else rep   # nested dict, or the _ReportOut dump
                segs = [str(src[k]) for k in ("headline", "verdict", "champion_summary") if src.get(k)]
                for k in ("what_worked", "what_didnt", "next_directions", "caveats"):
                    v = src.get(k)
                    if v:                                      # a malformed report may store a str/non-list
                        items = v if isinstance(v, (list, tuple)) else [v]
                        segs.append(f"{k.replace('_', ' ')}: " + "; ".join(str(x) for x in items))
                rtext = "\n".join(segs)
        if rtext:
            parts.append("\nLatest run report (agent-authored):\n" + rtext[:1800])
        return "\n".join(parts)

    # ---- persisted chat transcript (the human↔boss conversation, saved WITH the run) --------------
    # The /chat and /command endpoints below are stateless thinking aids — the UI holds the history.
    # That history used to live ONLY in React state, so it vanished whenever the Dock remounted (a
    # Search↔Report toggle, the finish-auto-land, reopening the run, or a reload). This sidecar makes
    # the transcript durable: one JSON turn per line in `chat.jsonl`, loaded on mount + appended per
    # turn. It is the UI server's OWN file (the engine never touches it), kept separate from
    # events.jsonl so it never folds into engine state and a `reset` can archive it independently.
    @app.get("/api/runs/{run_id}/chat-log")
    def chat_log(run_id: str):
        """The saved chat turns for this run, in order ({role:'user'|'assistant'|'action', …})."""
        rd = _run_dir(run_id)
        return list(iter_jsonl(rd / "chat.jsonl"))

    @app.post("/api/runs/{run_id}/chat-log")
    async def chat_log_append(run_id: str, request: Request):
        """Append ONE chat turn (the verbatim feed entry: role/content/trace or role/action/status)
        so it survives a remount/reload. Single writer (this server) + a synchronous fsync'd append
        per request serialize within the process, so no cross-process lock is needed here."""
        rd = _run_dir(run_id)
        turn = await request.json()
        if not isinstance(turn, dict):
            raise HTTPException(400, "chat turn must be a JSON object")
        path = rd / "chat.jsonl"
        with open(path, "ab") as f:
            f.write(orjson.dumps(turn) + b"\n")
            f.flush()
            os.fsync(f.fileno())
        return {"ok": True}

    @app.post("/api/runs/{run_id}/chat-compact")
    async def chat_compact(run_id: str, request: Request):
        """Summarize a stretch of older chat turns into ONE tight recap, so the boss's working memory
        stops growing turn-over-turn (the human↔boss history is re-sent in full each message). The UI
        sends the turns to fold; we return a recap string (+ its token cost) which the UI appends as a
        durable `summary` turn and then sends to the boss IN PLACE OF those turns. Read-only + soft-fail
        offline — compaction is opt-in, so a missing model just leaves the chat uncompacted."""
        rd = _run_dir(run_id)
        body = await request.json()
        msgs = body.get("messages") or []
        convo = "\n".join(f"{m.get('role')}: {m.get('content', '')}"
                          for m in msgs if str(m.get("content", "")).strip())
        if not convo.strip():
            return {"ok": True, "summary": "", "tokens": None}
        try:
            client = make_llm_client(_llm_settings(rd))
            sys_prompt = (
                "You are compacting a conversation between a human and the BOSS of an autonomous ML "
                "experiment run. Rewrite it as a TIGHT recap that becomes the boss's memory of these "
                "turns, so they can be dropped from the live context. PRESERVE, in order of priority: "
                "decisions made, actions already applied (and their outcome), open questions, and any "
                "agreed next steps or constraints the human set. Drop pleasantries and resolved "
                "tangents. One compact paragraph, no preamble, written as notes-to-self.")
            summary = await anyio.to_thread.run_sync(lambda: client.complete_text(
                [{"role": "system", "content": sys_prompt}, {"role": "user", "content": convo}]))
        except Exception as e:  # noqa: BLE001 - offline / no model -> soft fail (chat stays uncompacted)
            return JSONResponse({"ok": False, "error": str(e)}, status_code=200)
        return {"ok": True, "summary": (summary or "").strip(), "tokens": _client_tokens(client)}

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
            "Here is the run you're discussing:\n" + _boss_context(st, nid, rd))
        try:
            client = make_llm_client(_llm_settings(rd))
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
                          "system": sys_prompt, "user": user_msg, "completion": text,
                          "tokens": _client_tokens(client)}}

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
        s = _llm_settings(rd)
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
        s = _llm_settings(rd)
        try:
            client = make_llm_client(s)
        except Exception as e:  # noqa: BLE001 - offline / no model
            return JSONResponse({"ok": False, "error": str(e)}, status_code=200)
        sys_prompt = (
            "You are the BOSS of an autonomous ML experiment run. Turn the human's chat message into a "
            "PLAN: a short conversational `reply` plus an ORDERED list of `actions` to apply right now. "
            "You are a real agent — take AS MANY actions as the request needs (zero, one, or several), "
            "and the run will apply them in order, then reopen+resume itself if any step needs the "
            "engine. Bias toward ACTING on what they want, not just talking back.\n"
            "- Empty `actions` (advice only) ONLY for a pure question or chit-chat that asks for nothing "
            "to change. Otherwise put real steps in `actions`.\n"
            "- Compose steps freely. E.g. 'you have 10 more nodes, try some neural nets' →\n"
            "    [budget(nodes=10), hint(text='try small neural nets: an MLP and a 1-D CNN baseline, "
            "tune width/depth/lr'), inject(operator='draft', params={...}, rationale='MLP baseline'), "
            "inject(operator='draft', params={...}, rationale='CNN baseline')].\n"
            "- Verbs: budget(nodes=N) raises the run's node budget by N (REQUIRED before asking for more "
            "experiments on a finished/near-budget run, else there's no room to run them); "
            "hint(text=the COMPLETE current standing directive distilled into specific techniques/"
            "features/params to try or avoid — it REPLACES the previous directive the researcher "
            "follows, so restate anything earlier that still applies; the researcher and strategist "
            "both read it, so phrase exploration asks plainly, e.g. 'try several distinct neural "
            "architectures'); inject(operator one of draft/improve/debug/merge, params, rationale) for ONE "
            "concrete experiment — emit several inject steps for several experiments; deep_research to "
            "read the literature first; note(node_id, text) to annotate a node; confirm(node_id), "
            "ablate(node_id), fork(node_id), promote(node_id); strategy(policy,fidelity) pins the "
            "search policy/fidelity and OVERRIDES the autonomous strategist for the rest of the run "
            "— pre-set it to match the request: an exploratory policy (evolutionary/asha) when the "
            "user wants to TRY MANY distinct approaches (so the search doesn't just greedily refine "
            "the current best), or greedy to exploit a clear leader; "
            "import(source_run, source_node) to SEED a winning experiment from a SIBLING run of this "
            "task into this run (use list_sibling_runs / read_sibling_experiment first to find one — the "
            "imported node records where it came from); "
            "approve(node_id), ratify, pause, resume, stop. Use the node in context when no id is given. "
            "Give each step a one-line `rationale`.\n\n" + _boss_context(st, nid, rd))
        convo = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in msgs)
        user = (f"Instruction: {instruction}" if instruction else "") + (f"\n\nDiscussion:\n{convo}" if convo else "")
        from .parse import parse_structured

        def _route_with_tools() -> Optional["_Plan"]:
            """Boss decision grounded by the run-introspection tools: it MAY read experiments / data
            before choosing, then emits ONE _Plan (reply + ordered actions). None when the model can't
            drive the tool loop (then the caller falls back to a plain single-call route)."""
            from .agent import CompositeTools, drive_tool_loop
            from .run_tools import RunTools, SiblingRunTools
            providers = [RunTools(), SiblingRunTools(rd.parent, rd.name)]
            try:                                  # DataTools needs the task; add it when we can load it
                from .run_tools import DataTools
                from .tasks import load_task
                snap = rd / "task.snapshot.json"
                if snap.exists():
                    providers.append(DataTools(load_task(snap)))
            except Exception:  # noqa: BLE001 - tools are best-effort; RunTools alone is fine
                pass
            tools = providers[0] if len(providers) == 1 else CompositeTools(providers)
            tools.bind_state(st, st.nodes.get(nid) if nid is not None else None)
            emit_spec = {"type": "function", "function": {
                "name": "emit", "description": "Emit the plan: a reply plus the ordered actions to "
                "apply now (empty actions = advice only).",
                "parameters": _Plan.model_json_schema()}}
            tool_sys = sys_prompt + ("\n\nThe context above (digest + report) usually has what you "
                "need — prefer to `emit` your plan directly. ONLY call a read-only tool first "
                "(read_experiment for a node's code/trials, find_analogous, data_schema/data_profile) "
                "when you SPECIFICALLY need a detail it doesn't show. Call `emit` exactly once.")
            box: dict = {}
            def _fin(args):
                try:
                    box["c"] = _Plan(**{k: v for k, v in (args or {}).items()
                                        if k in _Plan.model_fields})
                except Exception:  # noqa: BLE001 - junk emit -> treat as advise (empty plan)
                    box["c"] = _Plan()
                return box["c"]
            # max_turns kept TIGHT: the rich static context (digest + report) means the model usually
            # emits on turn 1; the tool turns are only for when it genuinely needs to look deeper. A
            # small cap bounds latency on slow/flaky hosted models (each turn is a network round-trip).
            drive_tool_loop(client, tools, [{"role": "system", "content": tool_sys},
                                            {"role": "user", "content": user}],
                            emit_spec, max_turns=3, finalize=_fin, fallback=lambda _m: box.get("c"))
            # loop exhausted without an emit (model kept calling tools) -> default to advise rather
            # than None, so we don't burn extra round-trips on the single-call route for that case.
            return box.get("c") or _Plan()

        # The LLM calls are blocking + network-bound; run them off the event loop (anyio worker
        # thread) so a slow model doesn't stall SSE tails / other endpoints for every UI client.
        plan = None
        try:
            plan = await anyio.to_thread.run_sync(_route_with_tools)
        except Exception:  # noqa: BLE001 - model can't tool-call / loop error -> single-call route
            plan = None
        if plan is None:
            try:
                plan = await anyio.to_thread.run_sync(
                    lambda: parse_structured(client, [{"role": "system", "content": sys_prompt},
                                                      {"role": "user", "content": user}], _Plan, s.llm_parser))
            except Exception:  # noqa: BLE001 - parse fumble -> fall through to advisory reply
                plan = None
        if plan is not None:
            actions = _plan_to_actions(plan, st)
            tok = _client_tokens(client)         # whole-turn token cost (incl. any tool-loop sub-calls)
            if actions:
                # An agentic plan: the ordered actions the UI applies in sequence, plus the boss's
                # narration. `reply` (if any) is shown as the chat message above the applied rows.
                return {"ok": True, "actions": actions, "reply": plan.reply or "", "tokens": tok}
            if plan.reply:                       # the boss chose to only talk back — show its reply
                return {"ok": True, "reply": plan.reply, "tokens": tok}
        try:
            advise_sys = ("You are an ML research collaborator embedded in an experiment loop. Answer "
                          "concisely, grounded on the run.\n" + _boss_context(st, nid, rd))
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
                              "system": advise_sys, "user": user_msg, "completion": text,
                              "tokens": _client_tokens(client)}}
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
        s = _llm_settings(rd)
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

    # ------------------------------------------------------------------ cross-run aggregate reports
    # On-demand portfolio reports over a SET of runs (a project folder, a task, or a super-task) — ONE
    # generator, three scope axes. Persisted under <run-root>/reports/ with a run-set fingerprint so the
    # UI can flag staleness; an agent reads every run in the set (per-run reports + drill) and synthesizes.
    _reports_dir = root / "reports"

    def _scope_label(scope_type: str, scope_id: str) -> str:
        data = projects.load()
        if scope_type == "project":
            p = next((x for x in data["projects"] if x["id"] == scope_id), None)
            return f"project “{p['name']}”" if p else f"project {scope_id}"
        if scope_type == "supertask":
            s = next((x for x in data["supertasks"] if x["id"] == scope_id), None)
            return f"super-task “{s['name']}”" if s else f"super-task {scope_id}"
        return f"task {scope_id}"

    def _scope_run_ids(scope_type: str, scope_id: str) -> list:
        """The runs a scope covers. project = the folder AND everything nested under it; task = same
        task_id; supertask = assigned to that super-task."""
        summaries = list_runs()
        if scope_type == "task":
            return [s["run_id"] for s in summaries if s.get("task_id") == scope_id]
        if scope_type == "supertask":
            return [s["run_id"] for s in summaries if s.get("supertask_id") == scope_id]
        if scope_type == "project":
            scopeset = {scope_id} | projects.descendants(scope_id)
            return [s["run_id"] for s in summaries if s.get("project_id") in scopeset]
        return []

    def _run_brief(run_id: str, labels: dict) -> dict:
        rd = _run_dir(run_id)
        st = fold(_events(rd))
        best = st.best()
        cfg = {}
        snap = rd / "config.snapshot.json"
        if snap.exists():
            try:
                cfg = json.loads(snap.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cfg = {}
        return {"run_id": run_id, "label": labels.get(run_id), "task_id": st.task_id,
                "goal": st.goal, "direction": st.direction,
                "model": cfg.get("llm_model"), "policy": cfg.get("policy"),
                "best_metric": (best.metric if best else None), "phase": _phase(st),
                "nodes": len(st.nodes),
                "report": st.report if isinstance(st.report, dict) else None}

    def _scope_drill(run_id: str, node_id: int) -> str:
        """Deep access for the report agent: read one experiment of one run via RunTools."""
        try:
            from .run_tools import RunTools
            st = fold(_events(_run_dir(run_id)))
            rt = RunTools()
            rt.bind_state(st, None)
            return rt.execute("read_experiment", {"node_id": node_id})
        except Exception as e:  # noqa: BLE001 - deep access is best-effort
            return f"(drill failed: {e})"

    def _scope_sig(run_ids: list) -> list:
        """Cheap fingerprint of the run set (ids + each log's size/mtime) to detect staleness — a new
        run added to the scope, or an existing one that kept evolving, changes the sig."""
        sig = []
        for rid in sorted(run_ids):
            try:
                stt = (root / rid / "events.jsonl").stat()
                sig.append([rid, stt.st_size, int(stt.st_mtime)])
            except OSError:
                sig.append([rid, 0, 0])
        return sig

    def _scope_report_path(scope_type: str, scope_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", f"{scope_type}-{scope_id}")[:120]
        return _reports_dir / (safe + ".json")

    def _prior_learnings_index() -> str:
        """Compact index of stored cross-run reports (scope label + headline + a couple of next-
        directions) so the genesis boss can bootstrap a new run informed by prior portfolios."""
        if not _reports_dir.exists():
            return ""
        lines = []
        for p in sorted(_reports_dir.glob("*.json")):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            c = rec.get("content") or {}
            lbl = (rec.get("scope") or {}).get("label") or p.stem
            line = f"- {lbl}: {c.get('headline') or ''}"
            nd = c.get("next_directions") or []
            if nd:
                line += " | next: " + "; ".join(str(x) for x in nd[:2])
            lines.append(line)
        return "\n".join(lines[:20])

    @app.get("/api/scope-report/{scope_type}/{scope_id}")
    def get_scope_report(scope_type: str, scope_id: str):
        if scope_type not in ("project", "task", "supertask"):
            raise HTTPException(400, "bad scope type")
        cur_ids = _scope_run_ids(scope_type, scope_id)
        p = _scope_report_path(scope_type, scope_id)
        if not p.exists():
            return {"exists": False, "run_count": len(cur_ids),
                    "label": _scope_label(scope_type, scope_id)}
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"exists": False, "run_count": len(cur_ids),
                    "label": _scope_label(scope_type, scope_id)}
        added = sorted(set(cur_ids) - set(rec.get("run_ids", [])))
        stale = _scope_sig(cur_ids) != rec.get("sig")
        return {"exists": True, **rec, "stale": stale,
                "current_run_count": len(cur_ids), "added": added}

    @app.post("/api/scope-report/{scope_type}/{scope_id}/generate")
    async def generate_scope_report_ep(scope_type: str, scope_id: str):
        """Generate (or regenerate) the cross-run report for a scope. On-demand only — the agent reads
        every run in the set (their per-run reports, configs, metrics) and synthesizes, drilling into
        any run when needed. Degrades to a metrics rollup offline."""
        if scope_type not in ("project", "task", "supertask"):
            raise HTTPException(400, "bad scope type")
        run_ids = _scope_run_ids(scope_type, scope_id)
        if not run_ids:
            raise HTTPException(400, "no runs in this scope")
        labels = projects.load().get("labels", {})
        briefs = []
        for rid in run_ids:
            try:
                briefs.append(_run_brief(rid, labels))
            except Exception:  # noqa: BLE001 - a half-written run shouldn't block the report
                continue
        scope = {"type": scope_type, "id": scope_id, "label": _scope_label(scope_type, scope_id)}
        from .scope_report import generate_scope_report as _gen
        s = _llm_settings(None)
        try:
            client = make_llm_client(s)
            content = await anyio.to_thread.run_sync(
                lambda: _gen(scope, briefs, client, parser=s.llm_parser, drill=_scope_drill))
        except Exception:  # noqa: BLE001 - offline -> deterministic rollup still persists
            content = _gen(scope, briefs, None)
        # Record coverage + staleness over the runs that ACTUALLY contributed a brief — a run skipped
        # above (corrupt log) wasn't read, so it must not count toward "over N runs" or the freshness sig.
        brief_ids = [b["run_id"] for b in briefs]
        rec = {"scope": scope, "generated_at": int(time.time() * 1000), "run_ids": brief_ids,
               "sig": _scope_sig(brief_ids), "model": s.llm_model, "content": content}
        _reports_dir.mkdir(parents=True, exist_ok=True)
        dst = _scope_report_path(scope_type, scope_id)
        tmp = dst.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rec, indent=2), encoding="utf-8")
        os.replace(tmp, dst)
        return {"ok": True, **rec, "stale": False, "added": []}

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
                "build": "looplab build-ui   (or: cd ui && npm ci && npm run build)",
                "note": "`looplab ui` auto-builds the bundle when Node/npm are on PATH.",
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
