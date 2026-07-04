"""Cross-run aggregate report routes. On-demand portfolio reports over a SET of runs (a project
folder, a task, or a super-task) — ONE generator, three scope axes. Persisted under
<run-root>/reports/ with a run-set fingerprint so the UI can flag staleness; an agent reads every
run in the set (per-run reports + drill) and synthesizes. Bodies are verbatim moves from
`serve/server.py::make_app` (BACKLOG §4)."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException

from looplab.core.atomicio import atomic_write_text
from looplab.events.replay import fold


def _prior_learnings_index(reports_dir: Path) -> str:
    """Compact index of stored cross-run reports (scope label + headline + a couple of next-
    directions) so the genesis boss can bootstrap a new run informed by prior portfolios."""
    if not reports_dir.exists():
        return ""
    lines = []
    for p in sorted(reports_dir.glob("*.json")):
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


def build_router(srv) -> APIRouter:
    router = APIRouter()
    _run_dir, _events, _phase, root = srv.run_dir, srv.events, srv.phase, srv.root
    projects = srv.projects
    _reports_dir = srv.reports_dir

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
        summaries = srv.list_runs_fn()
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
            from looplab.tools.run_tools import RunTools
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

    @router.get("/api/scope-report/{scope_type}/{scope_id}")
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

    @router.post("/api/scope-report/{scope_type}/{scope_id}/generate")
    async def generate_scope_report_ep(scope_type: str, scope_id: str):
        """Generate (or regenerate) the cross-run report for a scope. On-demand only — the agent reads
        every run in the set (their per-run reports, configs, metrics) and synthesizes, drilling into
        any run when needed. Degrades to a metrics rollup offline. Runs as a BACKGROUND JOB: reading +
        synthesizing over many runs can outlast a UI proxy's gateway timeout, so a slow synthesis hands
        back a job_id the UI polls (a fast/offline one still returns inline within the wait — no 504)."""
        if scope_type not in ("project", "task", "supertask"):
            raise HTTPException(400, "bad scope type")
        run_ids = _scope_run_ids(scope_type, scope_id)
        if not run_ids:
            raise HTTPException(400, "no runs in this scope")

        def _compute() -> dict:
            labels = projects.load().get("labels", {})
            briefs = []
            for rid in run_ids:
                try:
                    briefs.append(_run_brief(rid, labels))
                except Exception:  # noqa: BLE001 - a half-written run shouldn't block the report
                    continue
            scope = {"type": scope_type, "id": scope_id, "label": _scope_label(scope_type, scope_id)}
            from looplab.serve.scope_report import generate_scope_report as _gen
            s = srv.llm_settings(None)
            try:
                client = srv.make_llm_client(s)
                # Config-driven agent loop limits (default UNLIMITED) from jovial-kalam — a slow
                # reasoning model can't be cut off before it emits; combined with the background-job
                # wrapper below so a long synthesis returns {status:running} rather than 504ing.
                content = _gen(scope, briefs, client, parser=s.llm_parser, drill=_scope_drill,
                               max_turns=getattr(s, "agent_max_turns", 0),
                               time_budget_s=getattr(s, "agent_time_budget_s", 0.0))
            except Exception:  # noqa: BLE001 - offline -> deterministic rollup still persists
                content = _gen(scope, briefs, None)
            # Record coverage + staleness over the runs that ACTUALLY contributed a brief — a run skipped
            # above (corrupt log) wasn't read, so it must not count toward "over N runs" or the sig.
            brief_ids = [b["run_id"] for b in briefs]
            rec = {"scope": scope, "generated_at": int(time.time() * 1000), "run_ids": brief_ids,
                   "sig": _scope_sig(brief_ids), "model": s.llm_model, "content": content}
            _reports_dir.mkdir(parents=True, exist_ok=True)
            dst = _scope_report_path(scope_type, scope_id)
            atomic_write_text(dst, json.dumps(rec, indent=2))   # unique temp + best-effort fsync (FUSE)
            return {"ok": True, **rec, "stale": False, "added": []}

        return await srv.jobs.run_as_job(_compute)

    return router
