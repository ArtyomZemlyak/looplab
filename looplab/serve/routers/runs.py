"""Run read-model routes: the runs list, per-run state + SSE stream, node detail/logs/metrics,
traces, provenance, artifacts, config and cost. Handler bodies are verbatim moves from
`serve/server.py::make_app` (BACKLOG §4); captured locals now live on `srv` (AppState)."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from looplab.core.atomicio import atomic_write_text
from looplab.core.config import Settings
from looplab.events.eventstore import EventStore, iter_jsonl
from looplab.events.replay import fold
from looplab.events.types import EV_TRUST_GATE_CHANGED
# Per-theme rollup for the cross-run map: {theme: {count, best_metric}}. Now lives in `digest` so the
# Researcher's working-set digest and this UI endpoint share one definition.
from looplab.events.digest import theme_rollup as _theme_rollup
from looplab.serve.artifacts import (
    _ART_MAX_BYTES, _LOG_TAIL_MAX, _artifact_roots, _list_artifact_files)
from looplab.serve.engine_proc import _engine_alive
from looplab.serve.protocol import POLL_SECONDS, SSE_DONE, SSE_STATE

# Snapshot-derived OPERATOR stage names, memoized per run DIRECTORY + snapshot VERSION: keyed on
# (run dir, task.snapshot.json mtime_ns, size), so the normalize+validate work runs once per snapshot
# version per server process instead of on every node_logs poll (the extra stat syscall per call is
# negligible next to the parse it saves). The stat-derived key makes a rewritten/replaced snapshot
# SELF-INVALIDATE — the memo must not outlive the run: a DELETE + same-id relaunch, or a CLI re-entry
# of the run dir rewriting task.snapshot.json, changes the pipeline this panel must render. Keyed by
# the absolute run dir, not the bare run_id — distinct run roots (tests, multi-root deploys) reuse
# ids like "demo" with different snapshots. Values are TUPLES, not lists: a caller that forgets to
# copy before mutating raises instead of silently corrupting the cache. The DEVELOPER-manifest
# fallback in node_logs deliberately stays per-poll: looplab_stages.json is written mid-node by the
# Developer's STAGES phase, so it can appear between polls and differs node to node.
_OP_STAGE_NAMES: dict[tuple[str, int, int], tuple] = {}


def _operator_stage_names(rd: Path) -> tuple:
    """Names of the OPERATOR-declared `cmd.stages` pipeline from the run's verbatim
    task.snapshot.json, vetted the way the ENGINE consumes them — () when the task declares none
    (single-command eval) or the declared list doesn't survive validation. Memoized on the snapshot's
    stat identity (see `_OP_STAGE_NAMES` above); returns an immutable tuple — callers list() it
    before mutating."""
    snap_path = rd / "task.snapshot.json"
    try:
        stt = snap_path.stat()
    except OSError:
        return ()   # engine still starting (snapshot not written yet) — don't memoize a transient miss
    key = (str(rd), stt.st_mtime_ns, stt.st_size)
    cached = _OP_STAGE_NAMES.get(key)
    if cached is not None:
        return cached
    names: tuple = ()
    try:
        from looplab.adapters.tasks import normalize_task
        from looplab.runtime.command_eval import validate_stages
        snap = json.loads(snap_path.read_text("utf-8"))
        es = (normalize_task(snap) if isinstance(snap, dict) else {}).get("eval") or {}
        if es.get("stages"):
            # ENGINE PARITY (Engine._resolve_stages): the engine re-runs the shared validator over
            # the snapshot's stages (an old/hand-edited snapshot bypasses pydantic) and on ANY error
            # falls back to the Developer-manifest + protected-`score` path — mirror it exactly, or
            # this panel would render phantom stage bands for a pipeline the engine never ran AND
            # miss the manifest stages it actually did run.
            clean, err = validate_stages(es["stages"])
            if err is None:
                names = tuple(str(s["name"]) for s in clean)
    except Exception:  # noqa: BLE001 - no/foreign/kind-less snapshot -> fall back to the manifest
        names = ()
    # Prune this run dir's entries for OLDER snapshot versions before inserting, so the dict stays
    # bounded per run (a rewrite would otherwise leave one dead entry behind per version).
    for stale in [k for k in _OP_STAGE_NAMES if k[0] == key[0]]:
        del _OP_STAGE_NAMES[stale]
    _OP_STAGE_NAMES[key] = names
    return names


def build_router(srv) -> APIRouter:
    router = APIRouter()
    _run_dir, _phase = srv.run_dir, srv.phase
    _state_payload = srv.state_payload

    # ------------------------------------------------------------------ runs list
    _summary_cache = srv.summary_cache   # run_id -> (size, mtime, summary); skips re-folding

    @router.get("/api/runs")
    def list_runs():
        out = []
        root = srv.root
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
                st = srv.state(rd)
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
        pdata = srv.projects.load()
        assignments, labels = pdata["assignments"], pdata.get("labels", {})
        st_assign = pdata.get("supertask_assignments", {})
        # engine_running is a live fact (lock probe), so it stays OUT of the mtime-keyed summary cache —
        # a zombie's events.jsonl is unchanged, yet its liveness flips the instant its engine dies.
        return [{**s, "project_id": assignments.get(s["run_id"]),
                 "label": labels.get(s["run_id"]),
                 "supertask_id": st_assign.get(s["run_id"]),
                 "engine_running": _engine_alive(root / s["run_id"])} for s in out]

    # Late-bind the runs list for the cross-run scope reports (`_scope_run_ids`), breaking the
    # route-calls-route dependency between this router and `routers/reports.py`.
    srv.list_runs_fn = list_runs

    # ------------------------------------------------------------------ state + time-travel
    @router.get("/api/runs/{run_id}/state")
    def get_state(run_id: str, seq: Optional[int] = None):
        return _state_payload(_run_dir(run_id), seq)

    @router.get("/api/runs/{run_id}/events")
    async def stream_events(run_id: str, request: Request):
        rd = _run_dir(run_id)

        async def gen():
            last_sent = -2
            last_alive = None
            last_beat = time.monotonic()
            # A quiet/"thinking" run (a long LLM call or eval) advances no seq and flips no liveness,
            # so without this the stream goes byte-silent. Behind jupyter-server-proxy (tornado) and
            # any nginx hop, an idle read-timeout then tears the connection down → the client reconnects
            # → full re-fold → drops again: a reconnect sawtooth that freezes the live UI. Emit an SSE
            # comment every KEEPALIVE seconds of silence so the proxy's idle timer never fires; a failed
            # keepalive write also surfaces a proxy-side disconnect promptly (X-Accel-Buffering is an
            # nginx-only hint that tornado ignores, so the regular small write is what actually flows).
            KEEPALIVE = 15.0
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
                    last_beat = time.monotonic()
                    yield (f"id: {payload['seq']}\n"
                           f"event: {SSE_STATE}\n"
                           f"data: {json.dumps(payload)}\n\n")
                    if payload["state"].get("finished"):   # reuse the threaded fold; don't re-fold
                        # End this stream — but the client deliberately does NOT close on `done`; it
                        # lets the closed connection trigger its reconnect, so a reopen (fork / branch
                        # / add-experiment) is picked up within a couple seconds. (Holding the stream
                        # open instead would never terminate, which hangs the TestClient SSE test.)
                        yield f"event: {SSE_DONE}\ndata: {{}}\n\n"
                        break
                elif time.monotonic() - last_beat >= KEEPALIVE:
                    last_beat = time.monotonic()
                    yield ": keepalive\n\n"     # ignored by EventSource; resets the proxy read-timer
                await anyio.sleep(POLL_SECONDS)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------------ node detail
    @router.get("/api/runs/{run_id}/nodes/{nid}")
    def node_detail(run_id: str, nid: int, seq: Optional[int] = None):
        rd = _run_dir(run_id)
        # Historical Inspector/Report must use the same prefix fold as the visible DAG.  Falling back
        # to the live fold here leaked later code, annotations and confirmations into old snapshots.
        st = fold(srv.events(rd, seq)) if seq is not None else srv.state(rd)
        n = st.nodes.get(nid)
        if n is None:
            # A node still BUILDING has no node_created yet (not in st.nodes), but its create_node
            # sub-spans (propose/implement generations + tool calls) already flush to spans.jsonl tagged
            # with this node_id AS THEY COMPLETE. Serve a minimal in-progress detail carrying that live
            # trace instead of 404ing — otherwise the Trace tab can't fill in until the whole build ends
            # (the exact "nothing, then everything at once" the operator hit).
            if seq is not None:
                raise HTTPException(404, "no such node at requested sequence")
            trace = _node_trace(rd, nid)
            building = bool(st.building and st.building.get("node_id") == nid)
            if building or trace.get("nodes"):
                b = st.building or {}
                return {"id": nid, "status": "building",
                        "operator": b.get("operator"), "parent_ids": b.get("parent_ids", []),
                        "idea": None, "code": "", "annotations": [], "trace": trace}
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
        # spans.jsonl is a current sidecar rather than an event-versioned projection. Never label its
        # future contents as historical; the UI explains that trace is unavailable in History.
        out["trace"] = _node_trace(rd, nid) if seq is None else {"nodes": []}
        if seq is not None:
            out["historical_seq"] = seq
        return out

    def _node_dir(rd: Path, nid: int) -> Path:
        return rd / "nodes" / f"node_{nid}"

    @router.get("/api/runs/{run_id}/nodes/{nid}/logs")
    def node_logs(run_id: str, nid: int, tail: int = 200_000):
        """Live training/eval logs for a node — the streamed stdout/stderr of its eval + setup
        subprocesses. `tail` caps bytes returned (from the end) so the UI can poll cheaply. Empty
        strings when a log doesn't exist yet.

        A SINGLE-command eval tees to `eval.log`; a MULTI-STAGE eval (data_prep → train → score) tees
        each stage to its OWN `<stage>.log` (command_eval `_log(f"{name}.log")`) and never writes
        `eval.log`. So `eval` carries ONLY eval.log's tail (empty for a multi-stage node — deliberately
        NO fallback duplication into it), and the per-stage tails come back as the ordered `stages`
        map, which the UI's log panel renders per stage."""
        rd = _run_dir(run_id)
        nd = _node_dir(rd, nid)
        # Clamp the client-controlled tail so a hostile/large value can't force an unbounded read; and
        # seek to the tail instead of read_bytes() so we never pull a multi-GB training log into RAM.
        n = min(max(0, tail), _LOG_TAIL_MAX)
        def _tail(name: str) -> str:
            p = nd / name
            try:
                size = p.stat().st_size
                with open(p, "rb") as f:
                    if size > n:
                        f.seek(size - n)
                    b = f.read()
            except OSError:
                return ""
            return b.decode("utf-8", "replace")
        # Per-stage logs — a multi-stage eval tees each stage to `<name>.log`. Bound the set to the
        # node's DECLARED stages, in pipeline order. NOT a bare `*.log` glob: that would surface any
        # stray log the training code writes to its cwd (a framework's own debug.log) as a phantom
        # stage band, and let an unbounded file count inflate the response. Each `<name>.log` is
        # tailed independently (a missing/racing one just yields "").
        # OPERATOR-declared stages first: when the task's `cmd` carries VALID `stages`, those ARE the
        # canonical pipeline (Engine._resolve_stages ignores the Developer's manifest) and no `score`
        # stage is appended — the LAST operator stage prints the metric. Their names come from the
        # run's verbatim task.snapshot.json (normalize_task maps composable `cmd` → `eval`,
        # validate_stages vets the list the same way the engine does), memoized on the snapshot's
        # stat identity (a rewrite self-invalidates) — so live logs surface for operator cmd.stages
        # pipelines too, the very mode the per-stage logs fix targeted (mega-review D7). list() the
        # cached tuple: the fallback below appends "score", which must never leak into the cache.
        stage_names: list[str] = list(_operator_stage_names(rd))
        if not stage_names:
            # Single-command cmd (or an INVALID operator stage list — engine parity: it too ignores
            # a bad list and takes this path): the Developer's `looplab_stages.json` manifest
            # declares the PRECEDING stages, and the engine appends the protected, operator-owned
            # `score` stage after them. Re-read EVERY poll, never memoized — the manifest is written
            # mid-node by the STAGES phase, so it can appear between polls and differs per node.
            try:
                man = json.loads((nd / "looplab_stages.json").read_text("utf-8"))
                stage_names = [str(s["name"]) for s in (man.get("stages") or []) if s.get("name")]
            except (OSError, ValueError, TypeError):
                pass
            if "score" not in stage_names:  # the engine appends a protected `score` stage post-manifest
                stage_names.append("score")
        stages = {name: body for name in stage_names if (body := _tail(f"{name}.log"))}
        return {"eval": _tail("eval.log"), "stages": stages, "setup": _tail("setup.log"),
                "run_setup": (rd / "run_setup.log").read_text("utf-8", "replace")
                             if (rd / "run_setup.log").exists() else ""}

    @router.get("/api/runs/{run_id}/nodes/{nid}/metrics")
    def node_metrics(run_id: str, nid: int):
        """Online metric SERIES a node's training logged — every scalar (loss, each recall@k, grad
        norms, lr, …), not just the objective — read via the pluggable metrics adapters (TensorBoard
        today). Shape: {"metrics": {tag: [{step, value, wall_time}, …]}}. Empty until logs appear."""
        from looplab.serve.metrics_adapters import read_node_metrics
        try:
            m = read_node_metrics(str(_node_dir(_run_dir(run_id), nid)))
        except Exception:  # noqa: BLE001 - observability must never 500
            m = {}
        return {"metrics": m}

    def _node_trace(rd: Path, nid: int) -> dict:
        try:
            from looplab.events.traceview import build_trace_view, load_spans
            st = srv.state(rd)
            # light=True: the tree carries structure + tokens + timing but NOT the prompts/outputs —
            # the UI fetches a single observation's full I/O lazily via /spans/{sid} when expanded
            # (Langfuse-style), so a heavily-repaired node's trace stays small and nothing is lost.
            tv = build_trace_view(st, load_spans(rd / "spans.jsonl"), light=True)
            return {"nodes": tv.get("nodes", {}).get(str(nid), []),
                    "rollup": tv.get("rollups", {}).get(str(nid), {}),
                    "summary": tv.get("summary", {})}
        except Exception:  # noqa: BLE001
            return {"nodes": [], "rollup": {}, "summary": {}}

    @router.get("/api/runs/{run_id}/spans/{sid}")
    def span_io(run_id: str, sid: str):
        """Full (uncapped) input/output/reasoning for ONE observation — fetched on demand when the
        user expands a generation/tool in the trace tree. The tree endpoints stay light (no I/O) so a
        long run's trace is browser-safe; the complete text lives here and in spans.jsonl. No info lost."""
        rd = _run_dir(run_id)
        try:
            for s in iter_jsonl(rd / "spans.jsonl"):
                if s.get("span_id") == sid:
                    return {"span_id": sid, "name": s.get("name"), "kind": s.get("kind"),
                            "attributes": s.get("attributes") or {}, "events": s.get("events") or [],
                            "duration_s": s.get("duration_s"), "status": s.get("status")}
        except Exception:  # noqa: BLE001
            pass
        return {"span_id": sid, "attributes": {}, "events": []}

    @router.get("/api/runs/{run_id}/trace/tail")
    def trace_tail(run_id: str, limit: int = 30):
        """LIVE 'what is the agent doing right now' feed: the most recent generation (LLM thinking/
        output) + tool (name + args) observations, newest last. Powers the Dock's live-trace disclosure
        so a user can watch the agent reason/act during a coarse 'Thinking…'/'Planning…' status instead
        of only seeing the label. Reads just the TAIL of spans.jsonl (bounded regardless of run length);
        text is capped here, the full I/O is at /spans/{sid}."""
        rd = _run_dir(run_id)
        limit = max(1, min(int(limit or 30), 100))
        # Read BACKWARD from EOF until we have `limit` complete lines (or hit a hard ceiling), instead of
        # a fixed 256KB window: a single span line can be 100KB+ (a repo-Developer generation carries the
        # whole prompt+output on it), so a fixed window could land ENTIRELY inside one line and return an
        # empty feed exactly during the heavy generations a user most wants to watch. Still bounded so a
        # multi-MB spans.jsonl is never re-parsed in full every poll.
        recent: list[dict] = []
        _CHUNK = 262144
        _MAX_TAIL = 8 * 1024 * 1024
        try:
            import os
            p = rd / "spans.jsonl"
            sz = os.path.getsize(p)
            with open(p, "rb") as f:
                start = sz
                blob = b""
                while start > 0 and (sz - start) < _MAX_TAIL:
                    step = min(_CHUNK, start)
                    start -= step
                    f.seek(start)
                    blob = f.read(step) + blob
                    if blob.count(b"\n") > limit:    # enough complete lines past the (partial) first
                        break
            lines = blob.splitlines()
            if start > 0 and lines:
                lines = lines[1:]                    # drop the partial first line (didn't reach BOF)
            for line in lines:
                try:
                    s = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if s.get("kind") not in ("generation", "tool"):
                    continue
                a = s.get("attributes") or {}
                it = {"span_id": s.get("span_id"), "kind": s.get("kind"), "node_id": a.get("node_id"),
                      "start": s.get("start"), "duration_s": s.get("duration_s"),
                      "status": s.get("status")}
                if s.get("kind") == "generation":
                    it["model"] = a.get("model")
                    txt = a.get("thinking") or a.get("output") or ""
                    it["text"] = txt[:500] if isinstance(txt, str) else ""
                else:
                    it["tool"] = a.get("tool")
                    inp = a.get("input")
                    if isinstance(inp, dict):
                        it["arg"] = str(inp.get("path") or inp.get("pattern") or inp.get("query")
                                        or inp.get("command") or inp.get("root") or "")[:160]
                    it["output"] = str(a.get("output") or "")[:200]
                recent.append(it)
        except OSError:
            pass
        return {"tail": recent[-limit:]}

    @router.get("/api/runs/{run_id}/nodes/{nid}/conversation")
    def node_conversation(run_id: str, nid: int):
        """The node's trace as a LINEAR, de-duplicated conversation: the system+user request shown
        once per sub-loop, then each generation's delta (reasoning + text + tool calls) interleaved
        with the tool executions — so the agent's train of thought reads without the raw tree's
        per-turn re-send of the whole message history. Caps I/O for the browser (full text in the
        raw span tree / spans.jsonl)."""
        rd = _run_dir(run_id)
        try:
            from looplab.events.traceview import build_conversation, load_spans
            return build_conversation(srv.state(rd), load_spans(rd / "spans.jsonl"), nid)
        except Exception:  # noqa: BLE001
            return {"run_id": run_id, "node_id": str(nid), "stages": []}

    @router.get("/api/runs/{run_id}/log")
    def event_log(run_id: str, since: int = -1):
        """Raw event envelopes (for the activity feed + event/span explorer). `since` = exclusive
        seq lower bound."""
        rd = _run_dir(run_id)
        return [o for o in iter_jsonl(rd / "events.jsonl") if o.get("seq", -1) > since]

    @router.get("/api/runs/{run_id}/artifacts")
    def artifacts(run_id: str):
        """List every file the run produced, grouped by root: the run directory (events/snapshots, the
        per-node eval workdirs under nodes/<id>/, operator subdirs) PLUS — for a RepoTask — the host
        repo / reference / data paths the task declared, so outputs a training command wrote straight
        into the actual repo (not under runs/) are reachable too. Each file carries size + mtime + a
        cheap is_text guess; /artifact serves the content."""
        rd = _run_dir(run_id)
        out = []
        for r in _artifact_roots(rd):
            files, truncated = _list_artifact_files(r["base"])
            out.append({"id": r["id"], "label": r["label"], "path": str(r["base"]),
                        "is_run_dir": r["id"] == "run", "truncated": truncated,
                        "n_files": len(files), "files": files})
        return {"run_id": run_id, "roots": out}

    @router.get("/api/runs/{run_id}/artifact")
    def artifact(run_id: str, root: str, path: str):
        """Serve ONE artifact's content for inline viewing. `root` must be one of the ids returned by
        /artifacts; `path` is resolved within that root and traversal-guarded (a browser can never read
        outside the declared roots). Text is returned UTF-8 (errors replaced) capped at 2 MB; binary or
        oversize files return is_text=false / truncated=true with no inline content."""
        rd = _run_dir(run_id)
        base = next((r["base"] for r in _artifact_roots(rd) if r["id"] == root), None)
        if base is None:
            raise HTTPException(404, "no such artifact root")
        target = (base / path).resolve()
        if target != base and base not in target.parents:     # path-traversal guard
            raise HTTPException(404, "no such artifact")
        if not target.is_file():
            raise HTTPException(404, "no such artifact")
        size = target.stat().st_size
        with open(target, "rb") as f:
            head = f.read(_ART_MAX_BYTES + 1)
        body = head[:_ART_MAX_BYTES]
        if b"\x00" in body:                                    # a NUL anywhere in the read → binary
            return {"root": root, "path": path, "size": size, "is_text": False,
                    "truncated": False, "content": None}
        return {"root": root, "path": path, "size": size, "is_text": True,
                "truncated": size > _ART_MAX_BYTES,
                "content": body.decode("utf-8", errors="replace")}

    @router.get("/api/runs/{run_id}/trace")
    def trace(run_id: str):
        rd = _run_dir(run_id)
        from looplab.events.traceview import build_trace_view, load_spans
        try:
            # light=True: strip prompt/output text — the run-level timeline needs only structure +
            # timing + token usage; a heavy run's full I/O is ~50 MB and crashes the browser.
            return build_trace_view(srv.state(rd), load_spans(rd / "spans.jsonl"), light=True)
        except Exception:  # noqa: BLE001 — a malformed/foreign spans.jsonl must degrade, not 500
            return {"run_id": run_id, "task_id": "", "nodes": {}, "unscoped": [],
                    "summary": {"spans": 0, "errors": 0, "total_eval_seconds": 0}}

    @router.get("/api/runs/{run_id}/trace/by_trace/{trace_id}")
    def trace_by_trace(run_id: str, trace_id: str):
        """Spans of ONE operation's trace (by trace_id) as a tree, WITH capped I/O — powers the
        per-event trace expansion: a strategy_decision / hypothesis_merged event carries its own
        operation's trace_id (the engine wraps each op in a named new_trace span and appends the event
        inside, so eventstore stamps it), and the UI shows only THAT trace here, not the node's whole
        Researcher+Developer trace."""
        rd = _run_dir(run_id)
        from looplab.events.traceview import load_spans, _tree, _cap_span_io
        try:
            spans = [_cap_span_io(s) for s in load_spans(rd / "spans.jsonl")
                     if s.get("trace_id") == trace_id]
            return {"spans": _tree(spans), "count": len(spans)}
        except Exception:  # noqa: BLE001 — malformed spans must degrade, not 500
            return {"spans": [], "count": 0}

    @router.get("/api/runs/{run_id}/prov")
    def prov(run_id: str):
        """W3C-PROV-style provenance of the search DAG: each node's solution is an entity
        generated by an experiment activity (its operator), derived from its parent nodes. Lets
        the lineage be queried as a knowledge-graph ('which change improved metric M the most')."""
        st = srv.state(_run_dir(run_id))
        agent = f"agent:looplab/{st.config_hash or 'run'}"
        ent, act, wgb, used, wdf, waw = {}, {}, {}, {}, [], {}
        for n in st.nodes.values():
            e, a = f"sol:{n.id}", f"exp:{n.id}"
            ent[e] = {"prov:label": f"solution node {n.id}",
                      "ll:metric": n.robust_metric,
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

    @router.get("/api/runs/{run_id}/config")
    def run_config(run_id: str):
        rd = _run_dir(run_id)
        snap = rd / "config.snapshot.json"
        if snap.exists():
            return json.loads(snap.read_text(encoding="utf-8"))   # already secret-masked by `run`
        return Settings().masked_snapshot()

    @router.put("/api/runs/{run_id}/config")
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
        # Use the SHARED single-source sets (settings_store) rather than a hardcoded {"llm_api_key"} —
        # a future SecretStr field is then masked here automatically instead of leaking into
        # config.snapshot.json in plaintext (the whole point of the _SECRET_FIELDS abstraction).
        from looplab.serve.settings_store import _ALLOWED_FIELDS, _SECRET_FIELDS
        allowed, secret = _ALLOWED_FIELDS, _SECRET_FIELDS
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
        # A profile switch would silently no-op on resume: the snapshot already carries every field
        # of the OLD profile's expansion as explicit values, so `_apply_profile` in the resumed
        # engine would skip the new bundle entirely. Refuse rather than pretend.
        if "profile" in changed:
            raise HTTPException(422, "profile can't be changed per-run after launch — the snapshot "
                                     "already contains the expanded profile; set the individual "
                                     "fields (trust_gate, confirm_top_k, …) instead")
        atomic_write_text(snap, json.dumps(updated, indent=2))
        # trust_gate is enforced by the FOLD (which reads it from the event log, not the snapshot),
        # so a snapshot edit alone would leave the Trust panel claiming an enforcement that never
        # engages. Record the change as an event: every fold — live UI, resume, reset — applies it.
        if "trust_gate" in changed:
            try:
                EventStore(rd / "events.jsonl").append(
                    EV_TRUST_GATE_CHANGED, {"trust_gate": updated["trust_gate"],
                                           "source": "config_edit"})
            except Exception as e:  # noqa: BLE001
                raise HTTPException(500, f"snapshot updated but trust_gate event append failed: {e}")
        return {"ok": True, "config": updated, "changed": sorted(changed),
                "engine_running": _engine_alive(rd)}

    @router.get("/api/runs/{run_id}/cost")
    def run_cost(run_id: str):
        st = srv.state(_run_dir(run_id))
        return st.llm_cost or {"cost": 0.0, "calls": 0, "total_tokens": 0}

    @router.get("/api/runs/{run_id}/agents_md")
    def agents_md(run_id: str):
        rd = _run_dir(run_id)
        f = rd / "AGENTS.md"
        return PlainTextResponse(f.read_text(encoding="utf-8") if f.exists() else "")

    return router
