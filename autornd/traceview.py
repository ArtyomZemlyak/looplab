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


def build_trace_view(state: RunState, spans: list[dict]) -> dict:
    """Group spans into per-node trees + a run summary, correlated by node_id (carried on each
    trace's root span). Spans with no node_id land under `unscoped` (e.g. onboarding)."""
    by_trace: dict[str, list[dict]] = defaultdict(list)
    for s in spans:
        by_trace[s.get("trace_id")].append(s)

    nodes: dict[str, list[dict]] = defaultdict(list)
    unscoped: list[dict] = []
    for sps in by_trace.values():
        forest = _tree(sps)
        root = forest[0] if forest else None
        nid = _node_id_of(root) if root else None
        (nodes[str(nid)] if nid is not None else unscoped).extend(forest)

    errors = [s for s in spans if s.get("status") == "ERROR"]
    return {
        "run_id": state.run_id,
        "task_id": state.task_id,
        "nodes": {k: v for k, v in nodes.items()},
        "unscoped": unscoped,
        "summary": {
            "spans": len(spans),
            "errors": len(errors),
            "total_eval_seconds": round(state.total_eval_seconds, 3),
        },
    }
