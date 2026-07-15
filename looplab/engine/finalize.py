"""Run finalization (extracted from the tail of Engine.run(), a pure move): the budget
summary, diversity-archive dump, LLM cost roll-up, cross-run case store + reflection note,
the derived SQLite read-model, and the trace.json / tree.html UI projections. Event emission
order is preserved exactly — resume/replay semantics depend on it.

Layering: the trace/html projections moved from `serve/` to their dependency-true home
`events/` (they are pure RunState -> string readers), so the engine no longer touches `serve`
at all — the imports below are ordinary downward engine -> events imports."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time
from typing import TYPE_CHECKING

import orjson

from looplab.core.atomicio import atomic_write_bytes, atomic_write_text
from looplab.core.models import RunState
from looplab.events.htmlview import render_html
from looplab.events.readmodel import build_readmodel
from looplab.events.replay import fold
from looplab.events.traceview import build_trace_view, hydrate_inputs, load_spans
from looplab.events.types import (EV_BUDGET, EV_DIVERSITY_ARCHIVE, EV_FINALIZATION_FINISHED,
                                  EV_LLM_COST, EV_READMODEL_SKIPPED)
from looplab.search.archive import DiversityArchive

if TYPE_CHECKING:  # engine type hint only — no runtime import of the orchestrator
    from looplab.engine.orchestrator import Engine


def _has_finish_step(events, event_type: str, finish_seq: int) -> bool:
    """Whether one durable finalization step already committed for this exact finish.

    A reopened run has another ``run_finished`` sequence, so its roll-up is intentionally distinct;
    a retry after a crash in the middle of one roll-up skips only the steps already committed for
    that sequence.
    """
    return any(e.type == event_type and e.data.get("finish_seq") == finish_seq for e in events)


def _build_readmodel_atomic(events, path: Path) -> RunState:
    """Build the rebuildable SQLite projection off to the side, then atomically publish it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    # sqlite connects to an existing empty file just fine. Keep its sidecars scoped to the unique
    # temporary name; clean all of them if construction or publication fails.
    try:
        state = build_readmodel(events, tmp)
        os.replace(tmp, path)
        return state
    finally:
        for candidate in (tmp, Path(f"{tmp}-journal"), Path(f"{tmp}-wal"), Path(f"{tmp}-shm")):
            try:
                candidate.unlink()
            except OSError:
                pass


def finalize_run(engine: "Engine", *, entry_finished: bool, start_time: float) -> RunState:
    """Finalize a run() invocation and return the final RunState (the fold, or the read-model's
    equivalent fold). The durable wrap-up runs whenever the fold reports `finalization_pending()`
    (finished, but the `finalization_finished` marker doesn't yet name this finish sequence) — so a
    re-entry after a crash mid-finalization RETRIES the idempotent steps; a fully finalized run is a
    no-op. `start_time` is run()'s wall-clock entry time (for the budget summary). `entry_finished` is
    accepted-but-ignored (`del` below): it's retained only for existing callers' signatures — the
    `finalization_pending()` gate, not this flag, decides whether wrap-up runs."""
    # Durable wrap-up: `run_finished` and finalization are separate crash boundaries. A process that
    # died between them re-enters with finished=True but no finalization_finished marker; retry the
    # idempotent/last-wins side effects until the marker names this exact finish sequence. A fully
    # finalized done run remains a no-op, while a reopened run's later finish has a new seq.
    del entry_finished  # retained in the public signature for compatibility with existing callers
    cur = fold(engine.store.read_all())
    if cur.finalization_pending():
        finish_seq = cur.last_finish_seq
        complete = True
        events = engine.store.read_all()
        if not _has_finish_step(events, EV_BUDGET, finish_seq):
            try:
                engine.store.append(EV_BUDGET, {                   # budget summary (I13 + #2)
                    "elapsed_s": round(time.time() - start_time, 3),
                    "eval_s": round(cur.total_eval_seconds, 3),
                    "nodes": len(cur.nodes),
                    "finish_seq": finish_seq,
                })
            except Exception:  # noqa: BLE001 - leave the marker pending so a re-entry retries
                complete = False
        events = engine.store.read_all()
        if not _has_finish_step(events, EV_DIVERSITY_ARCHIVE, finish_seq):
            try:
                summary = DiversityArchive(engine.archive_resolution).summary(cur)
                engine.store.append(EV_DIVERSITY_ARCHIVE, {**summary, "finish_seq": finish_seq})
            except Exception:  # noqa: BLE001 - leave the marker pending so a re-entry retries
                complete = False
        events = engine.store.read_all()
        if not _has_finish_step(events, EV_LLM_COST, finish_seq):
            # True means either a cost event committed or there were no LLM calls to report. A
            # required append failure keeps this exact finish pending so a later re-entry retries.
            if not emit_llm_cost(engine, finish_seq=finish_seq):  # LLM cost/tokens roll-up (UI)
                complete = False

        # The cross-run stores already implement idempotent upserts/watermarks. Isolate their failure
        # from the terminal run state, but don't claim the durable marker until both completed.
        try:
            engine._store_case(fold(engine.store.read_all()))       # cross-run memory (I19)
            # CODEX AGENT: `_store_research_claims` is accidentally gated by the unrelated concept-capsule
            # flag below. A run can enable Deep Research + `cross_run_read_tools`/advisory, successfully
            # verify D8 claims, and still finalize forever without publishing any of them when
            # `cross_run_concepts=False`. Give D8 persistence its own explicit gate (or persist whenever a
            # memory_dir + memo exist) and cover the concepts-off/read-tools-on finalization combination.
            if getattr(engine, "_cross_run_concepts", False):        # §21.20 Step 2: cross-run concept capsule
                engine._store_concept_capsule(fold(engine.store.read_all()))
                engine._store_research_claims(fold(engine.store.read_all()))   # + D8 claims cross-run
        except Exception:  # noqa: BLE001 - retry on a later finalization re-entry
            complete = False
        # §22.4 AGENTIC taxonomy steward — portfolio-scoped, DECOUPLED from the run's terminal state, so a
        # curation hiccup (or an LLM call) never blocks/retries finalization (it does NOT touch `complete`).
        # Gated on `cross_run_curation` + an available LLM client; off => byte-identical finalize.
        try:
            engine._store_concept_curation(fold(engine.store.read_all()))
            engine._store_claim_curation(fold(engine.store.read_all()))   # agentic claim ratify/reject/pin
        except Exception:  # noqa: BLE001 — agentic curation must never affect the run's finalization
            pass
        try:
            engine._write_reflection_note(                         # E4 cross-run meta-review prior
                fold(engine.store.read_all()))
        except Exception:  # noqa: BLE001 - retry on a later finalization re-entry
            complete = False
        latest = fold(engine.store.read_all())
        if complete and latest.finished and latest.last_finish_seq == finish_seq:
            try:
                engine.store.append(EV_FINALIZATION_FINISHED, {"finish_seq": finish_seq})
            except Exception:  # noqa: BLE001 - the exact-finish marker remains retryable
                pass

    # The SQLite read-model is a DERIVED, rebuildable cache that nothing in-process reads (the UI
    # folds events.jsonl / reads trace.json). On a FUSE/S3 run dir (JupyterHub geesefs) sqlite's
    # byte-range locks are unsupported and the write can raise `database is locked` / `disk I/O
    # error` — which must NOT abort an otherwise-finished run. Build best-effort; the run state we
    # actually need comes from the event fold regardless.
    try:
        final = _build_readmodel_atomic(
            engine.store.read_all(), engine.run_dir / "readmodel.sqlite")
    except Exception as e:  # noqa: BLE001 - derived cache; a FUSE sqlite failure must not kill finalize
        final = fold(engine.store.read_all())
        try:
            engine.store.append(EV_READMODEL_SKIPPED, {"error": str(e)[:300]})
        except Exception:  # noqa: BLE001 - even the audit note is best-effort
            pass
    # UI projection (ADR-17): join the research tree (events) to its execution detail
    # (spans) -> trace.json for the React UI + an inline span tree in the static HTML.
    # Hydrate the delta-encoded generation inputs first (tracing.generation stores only each turn's
    # delta on disk) so the archived trace.json holds the FULL verbatim prompts — same as the live
    # `/trace/by_trace` endpoint (hydrate → then build_trace_view caps). Without this the persisted
    # projection would disagree with the live one (delta vs full input) for any external reader.
    try:
        tv = build_trace_view(final, hydrate_inputs(load_spans(engine.run_dir / "spans.jsonl")))
    except Exception:  # noqa: BLE001 - derived projection; event fold remains authoritative
        return final
    try:
        atomic_write_bytes(engine.run_dir / "trace.json", orjson.dumps(tv))
    except Exception:  # noqa: BLE001 - a stale/missing derived projection never re-terminalizes a run
        pass
    try:
        atomic_write_text(engine.run_dir / "tree.html", render_html(final, tv))
    except Exception:  # noqa: BLE001 - trace and HTML publish independently
        pass
    return final


def emit_llm_cost(engine: "Engine", *, finish_seq: int | None = None) -> bool:
    """Best-effort LLM cost/token roll-up for the UI cost panel. Duck-types the role graph
    (researcher/developer may be wrapped by ToolUsingResearcher/ValidatingDeveloper) to find
    every CostAccountant, dedupes by identity, and emits one `llm_cost` event. Local models
    have no $ price (spent=0.0) but tokens are the real cost signal. Skips silently for the
    offline/toy backend (no client, no accountant) — never breaks a run. Returns True when the
    event committed OR no event was required, False when a required roll-up failed so durable
    finalization can remain pending and retry it."""
    try:
        seen: dict[int, object] = {}
        stack = [engine.researcher, engine.developer]
        while stack:
            obj = stack.pop()
            if obj is None:
                continue
            acc = getattr(obj, "accountant", None)
            if acc is not None and id(acc) not in seen:
                seen[id(acc)] = acc
            for attr in ("client", "inner", "fallback", "researcher", "developer",
                         "strategist", "tools"):
                child = getattr(obj, attr, None)
                if child is not None and child is not obj:
                    stack.append(child)
            # Unified agent: per-stage clients (strategy/pilot) not on the attr graph above.
            for c in (getattr(obj, "stage_clients", None) or []):
                if c is not None and c is not obj:
                    stack.append(c)
        if not seen:
            return True
        accs = list(seen.values())
        if not any(getattr(a, "calls", 0) for a in accs):
            return True  # no LLM calls actually happened (e.g. toy run) — nothing to report
        payload = {
            "cost": round(sum(getattr(a, "spent", 0.0) for a in accs), 6),
            "calls": sum(getattr(a, "calls", 0) for a in accs),
            "prompt_tokens": sum(getattr(a, "prompt_tokens", 0) for a in accs),
            "completion_tokens": sum(getattr(a, "completion_tokens", 0) for a in accs),
            "total_tokens": sum(getattr(a, "total_tokens", 0) for a in accs),
        }
        if finish_seq is not None:
            payload["finish_seq"] = finish_seq
        engine.store.append(EV_LLM_COST, payload)
        return True
    except Exception:  # noqa: BLE001 - cost telemetry must NEVER abort run finalization
        return False
