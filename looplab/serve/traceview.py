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

from looplab.core.models import RunState


def load_spans(path: str | os.PathLike) -> list[dict]:
    """Read spans.jsonl (tolerant of a torn final line) via the shared JSONL reader."""
    from looplab.events.eventstore import iter_jsonl
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


# Trace-view I/O caps. A real repo-developer generation carries a 100KB+ prompt, and a long run
# accumulates hundreds of them — the raw trace of one such run was ~52 MB, which crashes the browser
# (observed: a black screen). The full text stays in spans.jsonl; the VIEW truncates each generation's
# input/output/reasoning and keeps only the tail of a long message list, so the payload stays bounded
# and the UI renders. Tune via these constants.
_IO_CAP = 2000          # max chars per single message/output/reasoning string
_MSGS_CAP = 10          # max messages kept in a generation's `input` (head + tail)


def _cap_str(v, n: int = _IO_CAP):
    if isinstance(v, str) and len(v) > n:
        return v[:n] + f"\n…[+{len(v) - n} chars truncated — full text in spans.jsonl]"
    return v


def _cap_msgs(msgs: list) -> list:
    if not isinstance(msgs, list):
        return msgs
    capped = [({**m, "content": _cap_str(m.get("content", ""))} if isinstance(m, dict) else m) for m in msgs]
    if len(capped) <= _MSGS_CAP:
        return capped
    head, tail = _MSGS_CAP // 2, _MSGS_CAP - _MSGS_CAP // 2
    return capped[:head] + [{"role": "system", "content": f"…[{len(capped) - _MSGS_CAP} earlier messages omitted]"}] + capped[-tail:]


def _cap_span_io(s: dict) -> dict:
    """Return the span with its heavy I/O attributes truncated (generation/tool only)."""
    a = s.get("attributes")
    if not isinstance(a, dict) or not any(k in a for k in ("input", "output", "thinking")):
        return s
    a = dict(a)
    if isinstance(a.get("output"), str):
        a["output"] = _cap_str(a["output"])
    if isinstance(a.get("thinking"), str):
        a["thinking"] = _cap_str(a["thinking"])
    if "input" in a:
        a["input"] = _cap_msgs(a["input"])
    return {**s, "attributes": a}


def _strip_span_io(s: dict) -> dict:
    """Drop the heavy I/O entirely — the run-level trace (Dock timeline) needs only structure, timing,
    model + token usage, not the prompts/outputs. Keeps the whole-run payload tiny; the per-node
    endpoint serves the (capped) full I/O for the Inspector."""
    a = s.get("attributes")
    if not isinstance(a, dict) or not any(k in a for k in ("input", "output", "thinking")):
        return s
    return {**s, "attributes": {k: v for k, v in a.items() if k not in ("input", "output", "thinking")}}


def build_trace_view(state: RunState, spans: list[dict], *, light: bool = False) -> dict:
    """Group spans into per-node trees + a run summary, correlated by node_id (carried on each
    trace's root span). Spans with no node_id land under `unscoped` (e.g. onboarding). Each span
    carries `kind` (operation/generation/tool) so the UI renders the Langfuse-style observation
    tree; `rollups` gives per-node token/cost/observation totals aggregated from generations.
    Heavy generation I/O is truncated (see `_cap_span_io`) so the payload stays browser-safe; with
    `light=True` it's dropped entirely (run-level timeline doesn't need prompts/outputs)."""
    spans = [(_strip_span_io if light else _cap_span_io)(s) for s in spans]
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
