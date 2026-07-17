"""Run read-model routes: the runs list, per-run state + SSE stream, node detail/logs/metrics,
traces, provenance, artifacts, config and cost. Handler bodies are verbatim moves from
`serve/server.py::make_app` (BACKLOG §4); captured locals now live on `srv` (AppState)."""
from __future__ import annotations

from collections import OrderedDict
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

import anyio
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse, StreamingResponse

from looplab.core.atomicio import atomic_write_text
from looplab.core.config import (
    RUN_START_PINNED_FIELDS, Settings, run_start_pinned_settings)
from looplab.engine.finalize import incomplete_finalize_scope
from looplab.events.eventstore import EventStore, EventStoreConcurrencyError, iter_jsonl
from looplab.events.replay import fold
from looplab.events.traceview import TRACE_PROJECTION_SCHEMA, unavailable_projection
from looplab.events.types import EV_TRUST_GATE_CHANGED
# Per-theme rollup for the cross-run map: {theme: {count, best_metric}}. Now lives in `digest` so the
# Researcher's working-set digest and this UI endpoint share one definition.
from looplab.events.digest import theme_rollup as _theme_rollup
from looplab.serve.artifacts import (
    _ART_MAX_BYTES, _LOG_TAIL_MAX, ArtifactPolicyUnavailable,
    _artifact_exposure_policy, _artifact_file_identity, _artifact_roots,
    _list_artifact_files)
from looplab.serve.concept_frame import (
    MAX_LENS_BODY_BYTES as _CONCEPT_FRAME_MAX_LENS_BODY_BYTES,
    MAX_LENS_PROMPT_BYTES as _CONCEPT_FRAME_MAX_LENS_PROMPT_BYTES,
    MAX_LENS_PROMPT_CHARS as _CONCEPT_FRAME_MAX_LENS_PROMPT_CHARS,
    bounded_lens_label as _bounded_lens_label,
    build_core as _build_concept_core,
    core_lens_inputs as _concept_core_lens_inputs,
    folded_concepts as _folded_concepts,  # noqa: F401 - compatibility seam for pure callers/tests
    lens_request as _concept_lens_request,
    normalized_custom_lens_name as _normalized_custom_lens_name,
    project_frame as _project_concept_frame,
    TRUNCATION_CAP_REASONS as _TRUNCATION_CAP_REASONS,
)
from looplab.serve.engine_proc import _engine_liveness, reconcile_pending_resume
from looplab.serve.log_pages import (
    DEFAULT_BYTES, DEFAULT_ROWS, MAX_BYTES, MAX_ROWS, MIN_BYTES, EventLogPager)
from looplab.serve.protocol import (
    PHASE_FINALIZING, POLL_SECONDS, RUN_GENERATION_FIELD, SSE_DONE, SSE_STATE)
from looplab.serve.run_commands import run_generation_token

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


_CONCEPT_CORE_CACHE_MAX_ENTRIES = 16
_CONCEPT_CORE_CACHE_MAX_PREFIXES_PER_SOURCE = 4
_RUN_GENERATION_RE = re.compile(r"^[0-9a-f]{64}$")


def _concept_event_file_identity(path: Path) -> Optional[tuple]:
    """Identity of the exact on-disk byte version a ConceptFrame core was folded from."""
    try:
        status = path.stat()
    except OSError:
        return None
    return (
        str(path.absolute()),
        status.st_dev,
        status.st_ino,
        status.st_ctime_ns,
        status.st_mtime_ns,
        status.st_size,
    )


def _concept_relation_registry_identity(lens_pack: list[dict]) -> tuple[str, ...]:
    """Only the shipped relation vocabulary influences bounded core edge acceptance."""
    return tuple(sorted({relation for lens in lens_pack if isinstance(lens, dict)
                         for relation in (lens.get("rels") or []) if isinstance(relation, str)}))


class _ConceptCoreCache:
    """Small thread-safe LRU of bounded, lens-independent ConceptFrame cores.

    Keys retain generation and the relation-registry version in addition to file identity and requested
    prefix. Generation is not separately read on a hit (that would itself scan the log); instead the
    lookup matches the exact file identity + seq pair and the stored key records the generation proved
    by that folded prefix.
    """

    def __init__(self):
        self._entries: OrderedDict[tuple, dict] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, identity: tuple, requested_seq: Optional[int],
            relation_registry: tuple[str, ...] = (),
            run_id: Optional[str] = None) -> Optional[dict]:
        with self._lock:
            source = identity[0]
            # CODEX AGENT: an append, same-path replacement, or corruption changes at least one exact
            # stat field. Drop every old version for that path before lookup so generations and damaged
            # versus authoritative prefixes cannot coexist indefinitely in this bounded cache.
            for key in [key for key in self._entries
                        if key[0][0] == source and key[0] != identity]:
                del self._entries[key]
            for key in reversed(tuple(self._entries)):
                # CODEX AGENT: run_id is request identity echoed in the cached core. Two legal URL
                # aliases can resolve to one events file, so sharing their source identity must not
                # make one endpoint return the other alias in its versioned response.
                if (key[0] == identity and key[1] == requested_seq
                        and key[3] == relation_registry and key[4] == run_id):
                    core = self._entries[key]
                    self._entries.move_to_end(key)
                    return core
        return None

    def put(self, identity: tuple, requested_seq: Optional[int], core: dict,
            relation_registry: tuple[str, ...] = ()) -> None:
        key = (identity, requested_seq, core.get(RUN_GENERATION_FIELD), relation_registry,
               core.get("run_id"))
        with self._lock:
            self._entries[key] = core
            self._entries.move_to_end(key)
            source_keys = [existing for existing in self._entries
                           if existing[0][0] == identity[0]]
            while len(source_keys) > _CONCEPT_CORE_CACHE_MAX_PREFIXES_PER_SOURCE:
                del self._entries[source_keys.pop(0)]
            while len(self._entries) > _CONCEPT_CORE_CACHE_MAX_ENTRIES:
                self._entries.popitem(last=False)


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
    log_pages = EventLogPager()
    _run_dir, _phase = srv.run_dir, srv.phase

    def _trace_unavailable(**shape) -> dict:
        """Keep every trace read-failure envelope on one truthful, versioned contract."""
        return {"schema": TRACE_PROJECTION_SCHEMA, **shape,
                "projection": unavailable_projection()}
    _state_payload = srv.state_payload
    concept_core_cache = _ConceptCoreCache()

    def _materialize_concept_core(rd: Path, run_id: str, requested_seq: Optional[int],
                                  lens_pack: list[dict]) -> dict:
        """Fold one stable event-file snapshot and cache only its bounded ConceptFrame core."""
        path = rd / "events.jsonl"
        relation_registry = _concept_relation_registry_identity(lens_pack)

        def _from_snapshot(events, source_divergence):
            if not events:
                raise HTTPException(409, {
                    "code": "run_generation_unavailable",
                    "message": "The run has no durable generation identity.",
                    "remediation": "Wait for run_started, then refresh concepts.",
                })
            projected = (events if requested_seq is None
                         else [event for event in events if event.seq <= requested_seq])
            generation = run_generation_token(projected)
            max_seq = max((event.seq for event in events), default=-1)
            captured_seq = max((event.seq for event in projected), default=-1)
            return _build_concept_core(
                fold(projected), run_id=run_id, lens_pack=lens_pack,
                generation=generation, requested_seq=requested_seq,
                captured_seq=captured_seq, max_seq=max_seq,
                source_divergence=source_divergence)

        # A read raced by an append/replacement is still a coherent prefix, but it has no stat identity
        # we can safely reuse. Retry a few times for a cacheable stable view, then serve one uncached
        # coherent snapshot rather than delaying a busy live run indefinitely.
        # REVIEW(2026-07-16): on a LIVE run this cache is near-useless by construction — the key is the
        # exact stat identity (mtime_ns/size), so EVERY append invalidates it, and get() even evicts
        # all older versions for the path before lookup. The expensive miss path (full read + fold +
        # core build) therefore runs on every ConceptView refetch tick of an active run, which is
        # exactly when the endpoint is hottest; only finished runs ever hit. Pair it with the
        # ConceptView projectionKey note below: the client refetches per node-status flip while the
        # server recomputes per append — the two multiply. A generation+max_seq-keyed core (valid for
        # any longer prefix via incremental fold, like the SSE state cache) would serve live runs too.
        for _attempt in range(3):
            identity = _concept_event_file_identity(path)
            if identity is not None:
                cached = concept_core_cache.get(
                    identity, requested_seq, relation_registry, run_id=run_id)
                if (cached is not None
                        and _concept_event_file_identity(path) == identity):
                    return cached
            source = EventStore(path)
            events = source.read_all()
            after = _concept_event_file_identity(path)
            # CODEX AGENT: equality is required even when the first stat failed. Treating
            # ``None -> identity B`` as stable could cache bytes read from replaced generation A
            # under B's identity and poison every later hit until the file changed again.
            if after != identity:
                continue
            core = _from_snapshot(events, source.divergence)
            if identity is not None:
                concept_core_cache.put(after, requested_seq, core, relation_registry)
            return core

        source = EventStore(path)
        return _from_snapshot(source.read_all(), source.divergence)

    # ------------------------------------------------------------------ runs list
    # File identity is part of the signature: reset replaces events.jsonl and can preserve both
    # content metadata fields, so size+mtime alone can return generation A's summary for generation B.
    _summary_cache = srv.summary_cache   # run_id -> (ino, ctime_ns, size, mtime_ns, summary)

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
                sig = (stt.st_ino, stt.st_ctime_ns, stt.st_size, stt.st_mtime_ns)
                cached = _summary_cache.get(rd.name)
                if cached and cached[:4] == sig:    # unchanged log -> reuse (finished runs never re-fold)
                    out.append(cached[4])
                    continue
                events = srv.events(rd)
                st = fold(events)
                finalize_incomplete = (
                    incomplete_finalize_scope(events) is not None or st.finalization_pending())
                best = st.best()
                summary = {
                    "run_id": rd.name, "task_id": st.task_id, "goal": st.goal,
                    "direction": st.direction, "finished": st.finished,
                    "phase": _phase(st, finalize_incomplete=finalize_incomplete),
                    "finalization_incomplete": finalize_incomplete, "nodes": len(st.nodes),
                    "best_metric": (best.metric if best else None),
                    "best_confirmed": (best.confirmed_mean if best else None),
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
        def _alive(rd, resume_pending: bool):
            alive = _engine_liveness(rd)
            # P1-1 on-load reconcile: any not-alive run may carry an unserved durable resume. This
            # includes request-after-run_finished (an inject/reopen that hit the old finalization
            # tail); reconcile_pending_resume itself rejects ordinary finished runs with no pending
            # request. The detached re-spawn appears as running on the next refresh.
            if alive is False and resume_pending:
                try:
                    reconcile_pending_resume(rd, cancel_event=srv.resume_cancel)
                except Exception:  # noqa: BLE001 — recovery is best-effort; never break the run list
                    pass
            return alive
        return [{**s, "project_id": assignments.get(s["run_id"]),
                 "label": labels.get(s["run_id"]),
                 "supertask_id": st_assign.get(s["run_id"]),
                 "engine_running": _alive(
                     root / s["run_id"], bool(s.get("resume_pending")))} for s in out]

    # Late-bind the runs list for the cross-run scope reports (`_scope_run_ids`), breaking the
    # route-calls-route dependency between this router and `routers/reports.py`.
    srv.list_runs_fn = list_runs

    # ------------------------------------------------------------------ state + time-travel
    @router.get("/api/runs/{run_id}/state")
    def get_state(run_id: str, seq: Optional[int] = None):
        return _state_payload(_run_dir(run_id), seq)

    @router.get("/api/runs/{run_id}/concepts")
    def get_concepts(run_id: str, response: Response, lens: str = "is_a",
                     rels: Optional[str] = None, seq: Optional[int] = None):
        """Return one versioned, bounded, generation-bound ConceptFrame."""
        from looplab.search.concept_graph import default_lenses

        rd = _run_dir(run_id)
        lens_pack = default_lenses()
        canonical_lens, requested_spec, lens_registration = _concept_lens_request(
            lens, rels, lens_pack)
        if seq is not None and seq < -1:
            raise HTTPException(400, {
                "code": "concept_seq_invalid",
                "message": "Historical concept sequence must be -1 or greater.",
            })

        # CODEX AGENT: the exact event-prefix fold and every lens-independent bounded read model are
        # cached together; switching lenses now performs only this pure tree projection.
        core = _materialize_concept_core(rd, run_id, seq, lens_pack)
        frame = _project_concept_frame(
            core, requested_lens=canonical_lens, lens_pack=lens_pack,
            requested_spec=requested_spec, lens_registration=lens_registration)
        response.headers["Cache-Control"] = "no-store"
        return frame

    @router.post("/api/runs/{run_id}/concepts/lens")
    async def derive_concept_lens(run_id: str, request: Request, response: Response):
        """Mint a lens IN THE MOMENT from a natural-language request — the "create a lens" LLM tool the
        Concept view offers. A lens is a pure PROJECTION spec (a relation-subset + optional root): it
        writes NO events and grows NO edges, so this stays replay-clean — we derive the spec, immediately
        project the tree under it, and return both. Soft-fails ({ok:false, reason}) offline / when the
        model declines or picks nothing usable, so the UI falls back to a default lens rather than 500."""
        from looplab.search.concept_graph import default_lenses, derive_lens

        rd = _run_dir(run_id)
        raw_body = bytearray()
        async for chunk in request.stream():
            if len(raw_body) + len(chunk) > _CONCEPT_FRAME_MAX_LENS_BODY_BYTES:
                raise HTTPException(413, {
                    "code": "concept_lens_body_too_large",
                    "max_bytes": _CONCEPT_FRAME_MAX_LENS_BODY_BYTES,
                })
            raw_body.extend(chunk)
        try:
            body = json.loads(bytes(raw_body).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, TypeError) as exc:
            raise HTTPException(400, "request body must be valid JSON") from exc
        prompt_value = body.get("prompt") if isinstance(body, dict) else None
        prompt = prompt_value.strip() if isinstance(prompt_value, str) else ""
        if not prompt:
            raise HTTPException(400, "prompt is required")
        if (len(prompt) > _CONCEPT_FRAME_MAX_LENS_PROMPT_CHARS
                or len(prompt.encode("utf-8")) > _CONCEPT_FRAME_MAX_LENS_PROMPT_BYTES):
            raise HTTPException(413, {
                "code": "concept_lens_prompt_too_large",
                "max_chars": _CONCEPT_FRAME_MAX_LENS_PROMPT_CHARS,
                "max_bytes": _CONCEPT_FRAME_MAX_LENS_PROMPT_BYTES,
            })
        expected_generation = body.get("expected_generation") if isinstance(body, dict) else None
        if (not isinstance(expected_generation, str)
                or _RUN_GENERATION_RE.fullmatch(expected_generation) is None):
            raise HTTPException(400, {
                "code": "invalid_run_generation",
                "message": "expected_generation must be the exact generation from the Concepts response.",
                "remediation": "Refresh the run before creating another paid concept lens.",
            })

        lens_pack = default_lenses()
        core = _materialize_concept_core(rd, run_id, None, lens_pack)
        generation = core[RUN_GENERATION_FIELD]
        if not generation:
            raise HTTPException(409, {
                "code": "run_generation_unavailable",
                "message": "The run has no durable generation identity.",
            })
        if generation != expected_generation:
            raise HTTPException(409, {
                "code": "run_generation_changed",
                "expected_generation": expected_generation,
                "current_generation": generation,
                "message": "The run changed before paid lens creation began.",
                "remediation": "Reload the Concepts view and submit a new request intentionally.",
            })
        base_frame = _project_concept_frame(
            core, requested_lens="is_a", lens_pack=lens_pack)
        response.headers["Cache-Control"] = "no-store"
        # REVIEW(2026-07-16): the old all-or-nothing gate (`if not base_frame["complete"]`) permanently
        # disabled lens minting on real runs. `complete = not reasons` collapsed EVERY completeness
        # reason into one refusal, but most reasons derive from IMMUTABLE log contents or monotone caps
        # that can never clear in an append-only log: the engine's own co_occurs counts once tripped
        # invalid_edge (now clamped, not rejected — see concept_frame.py), and node_membership_cap/
        # concepts_per_node_cap/membership_cap/edge_cap only ever ratchet. Refusing before the model
        # call, forever, while the UI told the operator to "try naming a relation to group by" — a
        # prompt that could never succeed. The GET path deliberately serves partial frames with itemized
        # receipts; this gate now matches it: refuse ONLY on corruption-class reasons (torn/invalid
        # source), and mint against the bounded (partial) frame when the only reasons are truncation
        # caps — the SAME bounded frame the GET path serves and the UI already renders. Blocking reasons
        # ride back on the refusal so the UI can say WHY it is permanent instead of "rephrase". The cap
        # class is an EXPLICIT allow-list (TRUNCATION_CAP_REASONS), not an `endswith("_cap")` test, so a
        # corruption-adjacent reason that merely ends in "_cap" (rename_hop_cap) still blocks.
        blocking = [r for r in base_frame["completeness"]["reasons"]
                    if r not in _TRUNCATION_CAP_REASONS]
        if blocking:
            return {**base_frame, "ok": False, "reason": "concept_frame_partial",
                    "blocking_reasons": blocking}
        inputs = _concept_core_lens_inputs(core)
        # The LLM call (client build + one structured turn) runs off the event loop; both offline client
        # construction and any model failure fall through derive_lens' own best-effort None / this guard.
        def _mint():
            client = srv.make_llm_client(srv.llm_settings(rd))
            return derive_lens(prompt, inputs["edges"], client, concepts=inputs["concept_ids"])
        try:
            spec = await anyio.to_thread.run_sync(_mint)
        except Exception:  # noqa: BLE001 — offline / no model -> soft fail, UI keeps its current lens
            return {**base_frame, "ok": False, "reason": "no_model"}
        if not spec:
            return {**base_frame, "ok": False, "reason": "declined"}

        relations = list(dict.fromkeys(str(rel) for rel in (spec.get("rels") or [])))
        name = _normalized_custom_lens_name(spec.get("name"))
        shipped_names = {item.get("name") for item in lens_pack if isinstance(item, dict)}
        if not name or name in shipped_names:
            name = _normalized_custom_lens_name(
                "derived-" + (name or "-".join(relations) or "lens"))
        if not name:
            return {**base_frame, "ok": False, "reason": "invalid_spec"}
        try:
            canonical_name, validated_spec, registration = _concept_lens_request(
                name, ",".join(relations), lens_pack)
        except HTTPException:
            return {**base_frame, "ok": False, "reason": "invalid_spec"}
        validated_spec.update({
            "label": _bounded_lens_label(spec.get("label"), canonical_name),
            "provenance": "agent",
        })
        if spec.get("root") in set(inputs["concept_ids"]):
            validated_spec["root"] = spec["root"]
        frame = _project_concept_frame(
            core, requested_lens=canonical_name, lens_pack=lens_pack,
            requested_spec=validated_spec, lens_registration=registration)
        return {**frame, "ok": True, "spec": validated_spec}

    def _assert_historical_generation(rd: Path, expected: Optional[str]) -> str:
        if expected is None:
            raise HTTPException(400, {
                "code": "historical_generation_required",
                "message": "Historical node detail requires the exact run generation.",
                "remediation": "Use the generation returned by the historical /state response.",
            })
        with srv.commands.sequence(rd):
            rd = srv.commands.validate_paths(rd)
            current = srv.commands.run_generation(rd)
            if expected != current:
                raise HTTPException(409, {
                    "code": "run_generation_changed",
                    "expected_generation": expected,
                    "current_generation": current or None,
                    "message": "The run was reset or replaced before historical detail was read.",
                    "remediation": "Open the current generation or reload the exact historical view.",
                })
        return current

    @router.get("/api/runs/{run_id}/events")
    async def stream_events(run_id: str, request: Request):
        rd = _run_dir(run_id)

        async def gen():
            last_sent = -2
            last_alive = None
            last_generation = None
            last_event_count = None
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
                generation = payload.get(RUN_GENERATION_FIELD)
                event_count = payload.get("event_count")
                if (payload["seq"] != last_sent or alive != last_alive
                        or generation != last_generation or event_count != last_event_count):
                    last_sent = payload["seq"]
                    last_alive = alive
                    last_generation = generation
                    last_event_count = event_count
                    last_beat = time.monotonic()
                    yield (f"id: {payload['seq']}\n"
                           f"event: {SSE_STATE}\n"
                           f"data: {json.dumps(payload)}\n\n")
                    if (payload["state"].get("finished") and alive is False
                            and payload["state"].get("phase") != PHASE_FINALIZING):
                        # ``run_finished`` precedes the engine releasing its singleton while terminal
                        # reports/cost/lessons are still being flushed; an error finish with the stop
                        # intent preserved is also canonical finalization recovery, not terminal. Keep
                        # the stream open through
                        # that FINISHING window; emitting ``done`` on the event alone made the browser
                        # close/reconnect every 2.5s while the canonical lifecycle correctly remained
                        # non-terminal. Once the driver is gone, end this stream — the client then
                        # reconnect-polls so a later reopen is picked up without a manual reload.
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
    def node_detail(run_id: str, nid: int, seq: Optional[int] = None,
                    expected_generation: Optional[str] = None):
        rd = _run_dir(run_id)
        # Historical Inspector/Report must use the same prefix fold as the visible DAG.  Falling back
        # to the live fold here leaked later code, annotations and confirmations into old snapshots.
        historical_generation = (_assert_historical_generation(rd, expected_generation)
                                 if seq is not None else None)
        st = fold(srv.events(rd, seq)) if seq is not None else srv.state(rd)
        if seq is not None:
            # The expensive fold runs without the exclusive command sequencer. A reset may win while
            # it is assembled, but a mixed-generation payload is rejected before any field is returned.
            historical_generation = _assert_historical_generation(rd, expected_generation)
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
            out["historical_generation"] = historical_generation
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
                from looplab.runtime.command_eval import materialized_stages
                man = json.loads((nd / "looplab_stages.json").read_text("utf-8"))
                clean = materialized_stages(man)
                stage_names = [str(s["name"]) for s in clean] if clean else []
            except (OSError, ValueError, TypeError):
                pass
            if "score" not in stage_names:  # the engine appends a protected `score` stage post-manifest
                stage_names.append("score")
        # `tail` is a per-log convenience limit, but the HTTP response itself also needs one hard
        # envelope. Divide the existing byte cap across declared stages plus eval/setup/run setup, so
        # a valid many-stage pipeline cannot multiply a 5 MB query into an 80+ MB response.
        requested_n = min(max(0, tail), _LOG_TAIL_MAX)
        file_slots = max(1, len(stage_names) + 3)
        n = min(requested_n, _LOG_TAIL_MAX // file_slots)

        def _tail(name: str, base: Path = nd) -> str:
            try:
                root = base.resolve()
                p = (root / name).resolve()
                # Names are direct children, not paths. This second boundary also rejects a log
                # symlink that points outside the run/node directory.
                if p.parent != root:
                    return ""
                size = p.stat().st_size
                with open(p, "rb") as f:
                    if size > n:
                        f.seek(size - n)
                    b = f.read(n)
            except (OSError, ValueError):
                return ""
            return b.decode("utf-8", "replace")

        stages = {name: body for name in stage_names if (body := _tail(f"{name}.log"))}
        # run_setup.log lives in the RUN dir (shared setup), not the node dir. Tail-cap it like every
        # other log here (seek-to-end, byte-bounded) instead of read_text()-ing the whole file into RAM
        # on every poll — a verbose dependency-install log would otherwise defeat the tail cap.
        return {"eval": _tail("eval.log"), "stages": stages, "setup": _tail("setup.log"),
                "run_setup": _tail("run_setup.log", rd)}

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
            # light=True: the tree carries structure + tokens + timing but NOT prompts/outputs. The UI
            # fetches a bounded/redacted observation projection lazily via /spans/{sid} when expanded,
            # so a heavily-repaired node's trace stays small and its omission receipt remains explicit.
            # `srv.node_trace_view` builds over ONLY this node's spans via the light span INDEX (in-
            # memory, O(node) — not the whole-run tree), so a 1 GB, 4000-node run's node trace is ~ms.
            tv = srv.node_trace_view(rd, nid)
            return {"schema": tv.get("schema"),
                    "nodes": tv.get("nodes", {}).get(str(nid), []),
                    "rollup": tv.get("rollups", {}).get(str(nid), {}),
                    "summary": tv.get("summary", {}),
                    "projection": tv.get("projection", {})}
        except Exception:  # noqa: BLE001
            return _trace_unavailable(nodes=[], rollup={}, summary={})

    @router.get("/api/runs/{run_id}/nodes/{nid}/trace")
    def node_trace(run_id: str, nid: int):
        """The LIGHT trace tree for ONE node — the hot path for expanding a node's trace card. Reads
        only that node's spans via the index (O(node)), so the UI can fetch a node's trace lazily on
        expand instead of loading (and re-rendering) the whole-run timeline for a 4000-node run."""
        return _node_trace(_run_dir(run_id), nid)

    @router.get("/api/runs/{run_id}/spans/{sid}")
    def span_io(run_id: str, sid: str):
        """Bounded, redacted I/O projection for one observation; raw diagnostics stay in spans.jsonl."""
        rd = _run_dir(run_id)
        try:
            # Seek straight to the span's byte offset via the index instead of scanning the whole
            # (up to 1 GB) spans.jsonl for one span. Falls back to a scan if the index lacks it (a
            # span past the indexed tail, a foreign/torn file) so nothing is ever unreachable.
            from looplab.events.span_index import get_index
            from looplab.events.traceview import (
                TRACE_DETAIL_SPAN_CAP, _cap_span_io, _cap_str, _normalize_span)
            idx = get_index(rd / "spans.jsonl")
            s = idx.full_span(sid) if idx is not None else None
            indexed_span = s is not None
            if s is None:
                for cand in iter_jsonl(rd / "spans.jsonl"):
                    if isinstance(cand, dict) and cand.get("span_id") == sid:
                        s = _normalize_span(cand)
                        break
            if s is not None:
                a = s.get("attributes") or {}
                trace_spans = []
                trace_total = (idx.trace_span_count(s.get("trace_id"))
                               if idx is not None and indexed_span else None)
                # Delta-encoded generation: reconstruct its input from a bounded trace window, then
                # project it through the per-message/per-span caps below.
                # (tracing stores only the per-turn delta to keep spans.jsonl ~6x smaller) — so the
                # expanded observation shows the retained diagnostic input projection. No-op for
                # tools/old logs; capture-time filtering and response caps still apply.
                if s.get("kind") == "generation" and "input_carry" in a:
                    from looplab.events.traceview import hydrate_inputs
                    tid = s.get("trace_id")
                    trace_spans = (idx.full_spans_for_trace(
                        tid, TRACE_DETAIL_SPAN_CAP, anchor_sid=sid) if idx is not None else [s])
                    # `s` may be a just-appended span past the indexed tail (found via the scan fallback
                    # above but absent from the index snapshot) — include it so its own delta joins the
                    # chain and reconstruction resolves, instead of returning the raw delta until top-up.
                    if not any(c.get("span_id") == sid for c in trace_spans):
                        trace_spans = trace_spans + [s]
                    s = next((h for h in hydrate_inputs(trace_spans) if h.get("span_id") == sid), s)
                s = _cap_span_io(s)
                projection = dict(s.get("_projection") or {})
                # CODEX AGENT: Snapshot the selected span's own omission truth BEFORE folding in
                # trace cardinality. Hidden siblings make the aggregate response partial, but they
                # do not make this span's bounded input/output projection incomplete.
                detail_truncated = projection.get("truncated") is True
                projection["detail_truncated"] = detail_truncated
                visible = len(trace_spans) if trace_spans else 1
                if trace_total is None:
                    projection.update({
                        "trace_cardinality_unavailable": True,
                        "truncated": True,
                    })
                else:
                    trace_total = max(visible, trace_total)
                    omitted_trace_spans = max(0, trace_total - visible)
                    siblings_elided = omitted_trace_spans > 0
                    projection.update({
                        "trace_total_spans": trace_total,
                        "trace_visible_spans": visible,
                        "omitted_trace_spans": omitted_trace_spans,
                        "siblings_elided": siblings_elided,
                        "truncated": detail_truncated or siblings_elided,
                    })
                return {"schema": TRACE_PROJECTION_SCHEMA, "span_id": s.get("span_id"),
                        "name": s.get("name"), "kind": s.get("kind"),
                        "attributes": s.get("attributes") or {}, "events": s.get("events") or [],
                        "duration_s": s.get("duration_s"), "status": s.get("status"),
                        "projection": projection}
        except Exception:  # noqa: BLE001
            pass
        try:
            safe_sid = _cap_str(sid, 256)
        except Exception:  # noqa: BLE001
            safe_sid = ""
        return _trace_unavailable(span_id=safe_sid, attributes={}, events=[])

    @router.get("/api/runs/{run_id}/trace/tail")
    def trace_tail(run_id: str, limit: int = 30):
        """LIVE 'what is the agent doing right now' feed: the most recent generation (LLM thinking/
        output) + tool (name + args) observations, newest last. Powers the Dock's live-trace disclosure
        so a user can watch the agent reason/act during a coarse 'Thinking…'/'Planning…' status instead
        of only seeing the label. Reads just the TAIL of spans.jsonl (bounded regardless of run length);
        text is capped here; /spans/{sid} exposes a larger bounded/redacted detail projection."""
        rd = _run_dir(run_id)
        limit = max(1, min(int(limit or 30), 100))
        # Read BACKWARD from EOF until we have `limit` complete lines (or hit a hard ceiling), instead of
        # a fixed 256KB window: a single span line can be 100KB+ (a repo-Developer generation carries the
        # whole prompt+output on it), so a fixed window could land ENTIRELY inside one line and return an
        # empty feed exactly during the heavy generations a user most wants to watch. Still bounded so a
        # multi-MB spans.jsonl is never re-parsed in full every poll.
        recent: list[dict] = []
        from looplab.events.traceview import (
            _cap_str, _finite_number, _normalize_span)
        _CHUNK = 262144
        _MAX_TAIL = 8 * 1024 * 1024
        source_truncated = False
        p = rd / "spans.jsonl"
        try:
            sz = os.path.getsize(p)
        except FileNotFoundError:
            # Absence established by the initial lookup is a truthful complete-empty source.
            return {"schema": TRACE_PROJECTION_SCHEMA, "tail": [],
                    "projection": {"schema": TRACE_PROJECTION_SCHEMA,
                                   "truncated": False, "source_truncated": False,
                                   "visible_spans": 0, "omitted_spans": 0}}
        except OSError:
            return _trace_unavailable(tail=[])
        try:
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
                source_truncated = True
                lines = lines[1:]                    # drop the partial first line (didn't reach BOF)
            for line in lines:
                try:
                    s = json.loads(line)
                except (ValueError, TypeError):
                    continue
                s = _normalize_span(s)
                if s is None or s.get("kind") not in ("generation", "tool"):
                    continue
                a = s.get("attributes") if isinstance(s.get("attributes"), dict) else {}
                node_id = a.get("node_id")
                if not ((isinstance(node_id, int) and not isinstance(node_id, bool)
                         and 0 <= node_id <= (1 << 63) - 1)
                        or (isinstance(node_id, str) and 0 < len(node_id) <= 128)):
                    node_id = None
                elif isinstance(node_id, str):
                    node_id = _cap_str(node_id, 128)
                it = {"span_id": s.get("span_id"), "kind": s.get("kind"),
                      "node_id": node_id,
                      "start": _finite_number(s.get("start"), maximum=1e15),
                      "duration_s": _finite_number(s.get("duration_s"), nonnegative=True, maximum=1e15),
                      "status": _cap_str(str(s.get("status") or ""), 32)}
                if s.get("kind") == "generation":
                    it["model"] = _cap_str(str(a.get("model") or ""), 160)
                    txt = a.get("thinking") or a.get("output") or ""
                    it["text"] = _cap_str(txt, 500) if isinstance(txt, str) else ""
                else:
                    it["tool"] = _cap_str(str(a.get("tool") or ""), 128)
                    inp = a.get("input")
                    if isinstance(inp, dict):
                        arg = inp.get("path") or inp.get("pattern") or inp.get("query") \
                            or inp.get("command") or inp.get("root") or ""
                        it["arg"] = _cap_str(str(arg), 160)
                    it["output"] = _cap_str(str(a.get("output") or ""), 200)
                recent.append(it)
        except OSError:
            # The source existed at the initial lookup but became unreadable/vanished before or
            # during the snapshot read. Its cardinality is unavailable, not exact zero.
            return _trace_unavailable(tail=[])
        shown = recent[-limit:]
        omitted = max(0, len(recent) - len(shown))
        return {"schema": TRACE_PROJECTION_SCHEMA, "tail": shown,
                "projection": {"schema": TRACE_PROJECTION_SCHEMA,
                               "truncated": source_truncated or omitted > 0,
                               "source_truncated": source_truncated,
                               "visible_spans": len(shown), "omitted_spans": omitted}}

    @router.get("/api/runs/{run_id}/nodes/{nid}/conversation")
    def node_conversation(run_id: str, nid: int):
        """The node's trace as a LINEAR, de-duplicated conversation: the system+user request shown
        once per sub-loop, then each generation's delta (reasoning + text + tool calls) interleaved
        with the tool executions — so the agent's activity reads without the recorded tree's per-turn
        re-send of the whole message history. All text remains bounded/redacted for the browser."""
        rd = _run_dir(run_id)
        try:
            from looplab.events.traceview import (
                TRACE_CONVERSATION_SPAN_CAP, build_conversation, load_spans)
            from looplab.events.span_index import get_index
            # Read only THIS node's traces' spans (by byte offset via the index), not the whole
            # spans.jsonl — a node's conversation on a 1 GB run no longer scans the entire file.
            idx = get_index(rd / "spans.jsonl")
            total = idx.node_span_count(nid) if idx is not None else None
            spans = (idx.full_spans_for_node(nid, TRACE_CONVERSATION_SPAN_CAP)
                     if idx is not None else load_spans(rd / "spans.jsonl"))
            return build_conversation(srv.trace_scalars(rd), spans, nid, total_spans=total)
        except Exception:  # noqa: BLE001
            return _trace_unavailable(run_id=run_id, node_id=str(nid), stages=[])

    @router.get("/api/runs/{run_id}/log")
    def event_log(run_id: str, since: int = -1):
        """Raw event envelopes (for the activity feed + event/span explorer). `since` = exclusive
        seq lower bound."""
        rd = _run_dir(run_id)
        return [o for o in iter_jsonl(rd / "events.jsonl") if o.get("seq", -1) > since]

    @router.get("/api/runs/{run_id}/log-page")
    def event_log_page(
            run_id: str,
            direction: str = "tail",
            limit: int = Query(DEFAULT_ROWS, ge=1, le=MAX_ROWS),
            byte_limit: int = Query(DEFAULT_BYTES, ge=MIN_BYTES, le=MAX_BYTES),
            cursor: Optional[str] = None,
            generation: Optional[str] = None,
            anchor_seq: Optional[int] = Query(None, ge=0)):
        """Bounded timeline transport. Cursors survive append and fail closed across run reset."""
        rd = _run_dir(run_id)
        candidate = rd / "events.jsonl"
        if candidate.is_symlink():
            raise HTTPException(404, "no such run")
        log_path = candidate.resolve()
        if rd not in log_path.parents:
            raise HTTPException(404, "no such run")
        return log_pages.page(
            log_path, direction=direction, limit=limit, byte_limit=byte_limit,
            cursor=cursor, generation=generation, anchor_seq=anchor_seq)

    @router.get("/api/runs/{run_id}/artifacts")
    def artifacts(run_id: str):
        """List every file the run produced, grouped by root: the run directory (events/snapshots, the
        per-node eval workdirs under nodes/<id>/, operator subdirs) PLUS — for a RepoTask — the host
        repo / reference / data paths the task declared, so outputs a training command wrote straight
        into the actual repo (not under runs/) are reachable too. Each file carries size + mtime + a
        cheap is_text guess; /artifact serves the content."""
        rd = _run_dir(run_id)
        try:
            exposed = _artifact_exposure_policy(rd)
        except ArtifactPolicyUnavailable:
            # A 503 is deliberate: a failed security proof is unavailable, not a truthful empty list.
            raise HTTPException(503, "artifact inventory unavailable") from None
        out = []
        for r in _artifact_roots(rd):
            files, truncated = _list_artifact_files(r["base"], exposed=exposed)
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
        candidate = base / path
        try:
            target = candidate.resolve(strict=True)
            target_stat = target.stat()
        except (OSError, RuntimeError, ValueError):
            raise HTTPException(404, "no such artifact") from None
        if target != base and base not in target.parents:     # path-traversal guard
            raise HTTPException(404, "no such artifact")
        try:
            exposed = _artifact_exposure_policy(rd)
        except ArtifactPolicyUnavailable:
            raise HTTPException(503, "artifact access unavailable") from None
        expected_identity = _artifact_file_identity(target_stat)
        if expected_identity is None:
            raise HTTPException(503, "artifact access unavailable")
        if not exposed(candidate, path, target_stat):
            raise HTTPException(404, "no such artifact")
        try:
            with open(target, "rb") as f:
                opened_stat = os.fstat(f.fileno())
                opened_identity = _artifact_file_identity(opened_stat)
                if opened_identity is None:
                    raise HTTPException(503, "artifact access unavailable")
                if opened_identity != expected_identity:
                    raise HTTPException(404, "no such artifact")
                current_target = candidate.resolve(strict=True)
                if current_target != base and base not in current_target.parents:
                    raise HTTPException(404, "no such artifact")
                current_identity = _artifact_file_identity(current_target.stat())
                if current_identity is None:
                    raise HTTPException(503, "artifact access unavailable")
                if current_identity != opened_identity:
                    raise HTTPException(404, "no such artifact")
                # CODEX AGENT: authorize the opened descriptor as well as the path; writable roots can
                # otherwise swap a safe pathname to a protected trace hardlink before open. Refresh
                # protected identities too in case the run-root source was replaced during the race.
                opened_exposed = _artifact_exposure_policy(rd)
                if not opened_exposed(current_target, path, opened_stat):
                    raise HTTPException(404, "no such artifact")
                size = opened_stat.st_size
                head = f.read(_ART_MAX_BYTES + 1)
        except ArtifactPolicyUnavailable:
            raise HTTPException(503, "artifact access unavailable") from None
        except (OSError, RuntimeError, ValueError):
            raise HTTPException(404, "no such artifact") from None
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
        try:
            # light=True: strip prompt/output text — the run-level timeline needs only structure +
            # timing + token usage; a heavy run's recorded I/O can be ~50 MB and crash the browser. Served
            # via the light span index + a file-identity cache (`srv.trace_view`): reads ~20 MB of
            # structure, not the whole (up to 1 GB) spans.jsonl, so the first click is ~1 s not ~15 s.
            return srv.trace_view(rd)
        except Exception:  # noqa: BLE001 — a malformed/foreign spans.jsonl must degrade, not 500
            return _trace_unavailable(
                run_id=run_id, task_id="", nodes={}, rollups={}, unscoped=[], summary={})

    @router.get("/api/runs/{run_id}/trace/by_trace/{trace_id}")
    def trace_by_trace(run_id: str, trace_id: str):
        """Spans of ONE operation's trace (by trace_id) as a tree, WITH capped I/O — powers the
        per-event trace expansion: a strategy_decision / hypothesis_merged event carries its own
        operation's trace_id (the engine wraps each op in a named new_trace span and appends the event
        inside, so eventstore stamps it), and the UI shows only THAT trace here, not the node's whole
        Researcher+Developer trace."""
        rd = _run_dir(run_id)
        from looplab.events.traceview import (
            TRACE_DETAIL_SPAN_CAP, _bounded_tail, _cap_span_io, _normalized_id,
            _projection_counter, _response_projection, _tree, hydrate_inputs, load_spans)
        try:
            # Read only this trace's spans (by byte offset via the index), not the whole spans.jsonl.
            from looplab.events.span_index import get_index
            idx = get_index(rd / "spans.jsonl")
            safe_tid = _normalized_id(trace_id)
            if safe_tid is None:
                raise ValueError("invalid trace id")
            total = idx.trace_span_count(safe_tid) if idx is not None else None
            if idx is not None:
                raw = idx.full_spans_for_trace(safe_tid, TRACE_DETAIL_SPAN_CAP)
            else:
                raw, total = _bounded_tail(
                    (s for s in load_spans(rd / "spans.jsonl")
                     if s.get("trace_id") == safe_tid), TRACE_DETAIL_SPAN_CAP)
            # Reconstruct the retained delta-encoded input before applying browser caps, so the per-op
            # tree does not mistake a delta for a complete diagnostic projection.
            spans = [_cap_span_io(s) for s in hydrate_inputs(raw)]
            total = max(len(spans), _projection_counter(total) if total is not None else len(raw))
            projection = _response_projection(
                total_spans=total, visible_spans=len(spans),
                truncated_spans=sum(1 for span in spans
                                    if (span.get("_projection") or {}).get("truncated") is True))
            return {"schema": TRACE_PROJECTION_SCHEMA, "spans": _tree(spans, _normalized=True),
                    "count": total, "visible_count": len(spans),
                    "omitted_count": projection["omitted_spans"], "projection": projection}
        except Exception:  # noqa: BLE001 — malformed spans must degrade, not 500
            return _trace_unavailable(spans=[])

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
        current = (json.loads(snap.read_text(encoding="utf-8"))
                   if snap.exists() else Settings().masked_snapshot())
        if not isinstance(current, dict):
            raise HTTPException(500, "the run configuration snapshot is not a JSON object")
        return _run_config_payload(rd, current)

    def _run_config_payload(rd: Path, snapshot: dict) -> dict:
        """Flat, backward-compatible config plus metadata for immutable run-start semantics.

        A few older UI versions were allowed to rewrite those fields in the snapshot even though the
        engine ignored them on re-entry. Overlay the folded values so both API and form show the policy
        the run actually uses; metadata lets the form render them as read-only without duplicating the
        Python contract in JavaScript.
        """
        pinned = run_start_pinned_settings(srv.state(rd))
        mismatches = sorted(k for k, value in pinned.items() if snapshot.get(k) != value)
        effective = dict(snapshot)
        effective.update(pinned)
        effective["_looplab_config_meta"] = {
            "run_start_pinned_fields": sorted(pinned),
            "snapshot_mismatch_fields": mismatches,
        }
        return effective

    def _repair_trust_gate_event(rd: Path, requested: str) -> bool:
        """Make snapshot/event dual-write retryable without appending duplicate gate events."""
        store = EventStore(rd / "events.jsonl")
        for _attempt in range(4):
            events = store.read_all()
            if fold(events).trust_gate == requested:
                return False
            expected = events[-1].seq if events else -1
            try:
                store.append(
                    EV_TRUST_GATE_CHANGED,
                    {"trust_gate": requested, "source": "config_edit"},
                    expected_last_seq=expected,
                    require_lock=True,
                )
                return True
            except EventStoreConcurrencyError:
                # Another writer advanced the log. Refold under a fresh CAS: it may already have
                # applied this exact gate, in which case the retry becomes a no-op.
                continue
        raise HTTPException(409, "the run changed while trust_gate was being saved; retry the edit")

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
        try:
            body = await request.json()
        except Exception as e:  # noqa: BLE001 - normalize every JSON decoder/content-type failure
            raise HTTPException(400, "request body must be a JSON object") from e
        if not isinstance(body, dict):
            raise HTTPException(400, "request body must be a JSON object")
        incoming = body.get("settings", body)
        if not isinstance(incoming, dict):
            raise HTTPException(400, "settings must be a JSON object")
        # Use the SHARED single-source sets (settings_store) rather than a hardcoded {"llm_api_key"} —
        # a future SecretStr field is then masked here automatically instead of leaking into
        # config.snapshot.json in plaintext (the whole point of the _SECRET_FIELDS abstraction).
        from looplab.serve.settings_store import _ALLOWED_FIELDS, _SECRET_FIELDS
        allowed, secret = _ALLOWED_FIELDS, _SECRET_FIELDS
        current = json.loads(snap.read_text(encoding="utf-8"))
        if not isinstance(current, dict):
            raise HTTPException(500, "the run configuration snapshot is not a JSON object")
        folded = srv.state(rd)
        pinned = run_start_pinned_settings(folded)
        attempted_pinned_changes = sorted(
            key for key, value in incoming.items()
            if key in pinned and value != pinned[key]
        )
        if attempted_pinned_changes:
            raise HTTPException(
                422,
                "run-start pinned settings cannot be changed after creation: "
                + ", ".join(attempted_pinned_changes)
                + ". Start a new run to use different holdout/verifier semantics.",
            )
        updated = dict(current)
        # Repair snapshots written by older servers. These values are not a new policy decision: the
        # event fold has always been the authority used by replay/re-entry, and GET already overlays it.
        normalized_pinned = sorted(k for k, value in pinned.items() if updated.get(k) != value)
        updated.update(pinned)
        # A hand-authored/legacy sparse snapshot may omit the mutable trust gate. Preserve the folded
        # policy in that case rather than manufacturing an "audit" downgrade during an unrelated edit.
        updated.setdefault("trust_gate", folded.trust_gate)
        changed = {}
        for k, v in incoming.items():        # apply only known, non-secret, actually-set fields
            if (k in allowed and k not in secret and k not in RUN_START_PINNED_FIELDS
                    and v is not None and updated.get(k) != v):
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
        try:
            gate_repaired = _repair_trust_gate_event(rd, updated["trust_gate"])
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"snapshot updated but trust_gate event append failed: {e}")
        return {"ok": True, "config": _run_config_payload(rd, updated),
                "changed": sorted(changed), "normalized_pinned": normalized_pinned,
                "trust_gate_event_appended": gate_repaired,
                "engine_running": _engine_liveness(rd)}

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
