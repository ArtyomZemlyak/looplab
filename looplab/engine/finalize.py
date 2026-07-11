"""Run finalization (extracted from the tail of Engine.run(), a pure move): the budget
summary, diversity-archive dump, LLM cost roll-up, cross-run case store + reflection note,
the derived SQLite read-model, and the trace.json / tree.html UI projections. Event emission
order is preserved exactly — resume/replay semantics depend on it.

Layering: the trace/html projections moved from `serve/` to their dependency-true home
`events/` (they are pure RunState -> string readers), so the engine no longer touches `serve`
at all — the imports below are ordinary downward engine -> events imports."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

import orjson

from looplab.core.models import RunState
from looplab.events.htmlview import render_html
from looplab.events.readmodel import build_readmodel
from looplab.events.replay import fold
from looplab.events.traceview import build_trace_view, load_spans
from looplab.events.types import (EV_BUDGET, EV_DIVERSITY_ARCHIVE, EV_LLM_COST,
                                  EV_READMODEL_SKIPPED)
from looplab.search.archive import DiversityArchive

if TYPE_CHECKING:  # engine type hint only — no runtime import of the orchestrator
    from looplab.engine.orchestrator import Engine


def finalize_run(engine: "Engine", *, entry_finished: bool, start_time: float) -> RunState:
    """Finalize a run() invocation and return the final RunState (the fold, or the read-model's
    equivalent fold). `entry_finished` is whether the run was ALREADY finished when run() was
    entered; `start_time` is run()'s wall-clock entry time (for the budget summary)."""
    # Finalize only on real completion (not when paused for approval / idempotent
    # resume of a done run).
    if not entry_finished and fold(engine.store.read_all()).finished:
        cur = fold(engine.store.read_all())
        engine.store.append(EV_BUDGET, {                       # budget summary (I13 + #2)
            "elapsed_s": round(time.time() - start_time, 3),
            "eval_s": round(cur.total_eval_seconds, 3),
            "nodes": len(cur.nodes),
        })
        engine.store.append(EV_DIVERSITY_ARCHIVE,              # diversity archive (I22)
                            DiversityArchive(engine.archive_resolution).summary(cur))
        emit_llm_cost(engine)                               # LLM cost/tokens roll-up (UI)
        engine._store_case(fold(engine.store.read_all()))       # cross-run memory (I19)
        engine._write_reflection_note(fold(engine.store.read_all()))   # E4 cross-run meta-review prior

    # The SQLite read-model is a DERIVED, rebuildable cache that nothing in-process reads (the UI
    # folds events.jsonl / reads trace.json). On a FUSE/S3 run dir (JupyterHub geesefs) sqlite's
    # byte-range locks are unsupported and the write can raise `database is locked` / `disk I/O
    # error` — which must NOT abort an otherwise-finished run. Build best-effort; the run state we
    # actually need comes from the event fold regardless.
    try:
        final = build_readmodel(engine.store.read_all(), engine.run_dir / "readmodel.sqlite")
    except Exception as e:  # noqa: BLE001 - derived cache; a FUSE sqlite failure must not kill finalize
        final = fold(engine.store.read_all())
        try:
            engine.store.append(EV_READMODEL_SKIPPED, {"error": str(e)[:300]})
        except Exception:  # noqa: BLE001 - even the audit note is best-effort
            pass
    # UI projection (ADR-17): join the research tree (events) to its execution detail
    # (spans) -> trace.json for the React UI + an inline span tree in the static HTML.
    tv = build_trace_view(final, load_spans(engine.run_dir / "spans.jsonl"))
    (engine.run_dir / "trace.json").write_bytes(orjson.dumps(tv))
    (engine.run_dir / "tree.html").write_text(render_html(final, tv), encoding="utf-8")
    return final


def emit_llm_cost(engine: "Engine") -> None:
    """Best-effort LLM cost/token roll-up for the UI cost panel. Duck-types the role graph
    (researcher/developer may be wrapped by ToolUsingResearcher/ValidatingDeveloper) to find
    every CostAccountant, dedupes by identity, and emits one `llm_cost` event. Local models
    have no $ price (spent=0.0) but tokens are the real cost signal. Skips silently for the
    offline/toy backend (no client, no accountant) — never breaks a run."""
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
            return
        accs = list(seen.values())
        if not any(getattr(a, "calls", 0) for a in accs):
            return  # no LLM calls actually happened (e.g. toy run) — nothing to report
        engine.store.append(EV_LLM_COST, {
            "cost": round(sum(getattr(a, "spent", 0.0) for a in accs), 6),
            "calls": sum(getattr(a, "calls", 0) for a in accs),
            "prompt_tokens": sum(getattr(a, "prompt_tokens", 0) for a in accs),
            "completion_tokens": sum(getattr(a, "completion_tokens", 0) for a in accs),
            "total_tokens": sum(getattr(a, "total_tokens", 0) for a in accs),
        })
    except Exception:  # noqa: BLE001 - cost telemetry must NEVER abort run finalization
        return
