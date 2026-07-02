"""UI projection of the trace (ADR-17): join the research tree (from `events.jsonl` → RunState)
to its execution detail (from `spans.jsonl`). Pure reader of files-as-truth — never a source of
truth. Produces a per-node span forest the HTML view and the future React UI both consume.

Spans nest by (trace_id, span_id, parent_id); each top-level operation is its own trace tagged
with node_id (see tracing.py), so we group traces by node_id and build a child tree per trace.
"""
from __future__ import annotations

import os
from collections import defaultdict
from typing import Optional

from .models import RunState


def load_spans(path: str | os.PathLike) -> list[dict]:
    """Read spans.jsonl (tolerant of a torn final line) via the shared JSONL reader."""
    from .eventstore import iter_jsonl
    return list(iter_jsonl(path))


def _tree(spans: list[dict]) -> list[dict]:
    """Build the parent->child forest for one trace from a flat span list."""
    by_id = {s["span_id"]: {**s, "children": []} for s in spans}
    roots = []
    for s in by_id.values():
        parent = by_id.get(s.get("parent_id"))
        (parent["children"] if parent else roots).append(s)
    # deterministic order: by start time
    def _sort(nodes):
        nodes.sort(key=lambda n: n.get("start", 0.0))
        for n in nodes:
            _sort(n["children"])
    _sort(roots)
    return roots


def _node_id_of(span: dict) -> Optional[int]:
    return span.get("attributes", {}).get("node_id")


def _rollup(spans: list[dict]) -> dict:
    """Aggregate generation/tool usage over a flat span list — the Langfuse-style trace totals
    (tokens + cost summed from every generation, plus observation counts). Returned per node and
    for the whole run so the UI can show 'N calls · K tok · $C' without re-summing the tree."""
    gens = [s for s in spans if s.get("kind") == "generation"]
    tools = [s for s in spans if s.get("kind") == "tool"]
    pt = ct = tt = 0
    cost = 0.0
    for g in gens:
        a = g.get("attributes", {})
        u = a.get("usage") or {}
        pt += int(u.get("prompt") or 0)
        ct += int(u.get("completion") or 0)
        tt += int(u.get("total") or 0)
        try:
            cost += float(a.get("cost") or 0.0)
        except (TypeError, ValueError):
            pass
    return {"generations": len(gens), "tools": len(tools),
            "tokens": {"prompt": pt, "completion": ct, "total": tt},
            "cost": round(cost, 6)}


def build_trace_view(state: RunState, spans: list[dict]) -> dict:
    """Group spans into per-node trees + a run summary, correlated by node_id (carried on each
    trace's root span). Spans with no node_id land under `unscoped` (e.g. onboarding). Each span
    carries `kind` (operation/generation/tool) so the UI renders the Langfuse-style observation
    tree; `rollups` gives per-node token/cost/observation totals aggregated from generations."""
    by_trace: dict[str, list[dict]] = defaultdict(list)
    for s in spans:
        by_trace[s.get("trace_id")].append(s)

    nodes: dict[str, list[dict]] = defaultdict(list)
    node_spans: dict[str, list[dict]] = defaultdict(list)
    unscoped: list[dict] = []
    for sps in by_trace.values():
        forest = _tree(sps)
        root = forest[0] if forest else None
        nid = _node_id_of(root) if root else None
        if nid is not None:
            nodes[str(nid)].extend(forest)
            node_spans[str(nid)].extend(sps)
        else:
            unscoped.extend(forest)

    errors = [s for s in spans if s.get("status") == "ERROR"]
    run_roll = _rollup(spans)
    return {
        "run_id": state.run_id,
        "task_id": state.task_id,
        "nodes": {k: v for k, v in nodes.items()},
        "rollups": {k: _rollup(v) for k, v in node_spans.items()},
        "unscoped": unscoped,
        "summary": {
            "spans": len(spans),
            "errors": len(errors),
            "generations": run_roll["generations"],
            "tools": run_roll["tools"],
            "tokens": run_roll["tokens"],
            "cost": run_roll["cost"],
            "total_eval_seconds": round(state.total_eval_seconds, 3),
        },
    }
