"""UI projection of the trace (ADR-17): join the research tree (from `events.jsonl` → RunState)
to its execution detail (from `spans.jsonl`). Pure reader of files-as-truth — never a source of
truth. Produces a per-node span forest the HTML view and the future React UI both consume.

Spans nest by (trace_id, span_id, parent_id); each top-level operation is its own trace tagged
with node_id (see tracing.py), so we group traces by node_id and build a child tree per trace.
"""
from __future__ import annotations

import json
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
    peak = 0
    cost = 0.0
    for g in gens:
        a = g.get("attributes", {})
        u = a.get("usage") or {}
        p = int(u.get("prompt") or 0)
        pt += p                                       # SUM of every call's prompt (billed — a tool loop
        peak = max(peak, p)                           # re-sends the growing context, so this is O(turns²))
        ct += int(u.get("completion") or 0)
        tt += int(u.get("total") or 0)
        try:
            cost += float(a.get("cost") or 0.0)
        except (TypeError, ValueError):
            pass
    # `context` = the LARGEST single prompt = how big the LLM's context window actually got (what the
    # user reads as "the context"), distinct from `total`/`prompt` which SUM the same context re-sent
    # every turn (billed cost, not context size). The UI shows context↑ + output↓, billed in the tooltip.
    return {"generations": len(gens), "tools": len(tools),
            "tokens": {"prompt": pt, "completion": ct, "total": tt, "context": peak},
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


# ── linear conversation projection ───────────────────────────────────────────────────────────────
# The raw span tree shows every generation with the FULL message list it was sent — but the agent
# tool-loop re-sends the whole conversation on every turn, so successive generations duplicate the
# system+user prompt and every prior turn (a 206-generation node re-sends the history 206×). The
# conversation projection reconstructs the loop as a linear, de-duplicated thread: the system+user
# REQUEST once per sub-loop, then each generation's DELTA (reasoning + text + which tools it called)
# interleaved with the tool executions. It reads like the agent's actual train of thought.

def _as_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(v)


def _seg_label(gen_span: dict, by_id: dict) -> Optional[str]:
    """The sub-loop a generation belongs to (propose / implement / repair / grade), to label its request
    boundary with its phase. Prefer the `phase` stamped on the span itself (tracing._phase_ctx) — correct
    even LIVE, before the parent operation span is flushed to disk; fall back to walking to the nearest
    ancestor operation span for older traces written before phase-stamping."""
    ph = (gen_span.get("attributes") or {}).get("phase")
    if ph:
        return ph
    cur = by_id.get(gen_span.get("parent_id"))
    while cur is not None:
        if cur.get("kind") in (None, "operation"):
            return cur.get("name")
        cur = by_id.get(cur.get("parent_id"))
    return None


def _thread_turns(spans_sorted: list[dict], by_id: dict) -> list[dict]:
    """Walk one trace's spans in time order → linear turns. A `request` is emitted at the first
    generation and again whenever the sent message count DROPS (the context reset that marks a new
    sub-loop — a fresh system+user), so the request is shown once per sub-loop, never re-duplicated.
    Every generation contributes only its delta (thinking + output + tool_calls); tools interleave."""
    turns: list[dict] = []
    prev_in: Optional[int] = None
    for s in spans_sorted:
        kind = s.get("kind")
        a = s.get("attributes") or {}
        if kind == "generation":
            inp = a.get("input") if isinstance(a.get("input"), list) else []
            n = len(inp)
            if prev_in is None or n <= prev_in:      # first call, or a context reset → new sub-loop
                turns.append({"type": "request", "label": _seg_label(s, by_id),
                              "messages": [{"role": m.get("role", "user"),
                                            "content": _cap_str(_as_text(m.get("content")))}
                                           for m in inp if isinstance(m, dict)]})
            prev_in = n
            out = a.get("output")
            think = a.get("thinking")
            turns.append({"type": "generation",
                          "think": _cap_str(think) if isinstance(think, str) and think else None,
                          "output": _cap_str(out if isinstance(out, str) else _as_text(out)),
                          "model": a.get("model"),
                          "tool_calls": [(tc.get("name") if isinstance(tc, dict) else tc)
                                         for tc in (a.get("tool_calls") or [])],
                          "usage": a.get("usage") or {},
                          "status": s.get("status"), "seconds": s.get("duration_s")})
        elif kind == "tool":
            turns.append({"type": "tool", "name": a.get("tool") or s.get("name") or "tool",
                          "input": _cap_str(_as_text(a.get("input"))),
                          "output": _cap_str(_as_text(a.get("output"))),
                          "status": s.get("status"), "seconds": s.get("duration_s")})
        # operation spans carry structure only — skipped in the linear reading view.
    return turns


def build_conversation(state: RunState, spans: list[dict], node_id) -> dict:
    """Per-node linear conversation (companion to `build_trace_view`). One `stage` per trace tagged
    with this node (create_node / evaluate / …), each a de-duplicated thread of turns. Reader of
    files-as-truth; caps every string for the browser, but never re-sends the growing history."""
    by_id = {s["span_id"]: s for s in spans if s.get("span_id")}
    by_trace: dict[str, list[dict]] = defaultdict(list)
    for s in spans:
        by_trace[s.get("trace_id")].append(s)
    stages: list[dict] = []
    for tid, ss in by_trace.items():
        ss_sorted = sorted(ss, key=lambda x: x.get("start", 0.0))
        root = next((s for s in ss_sorted if s.get("parent_id") is None), ss_sorted[0] if ss_sorted else None)
        nid = _node_id_of(root) if root else None
        if nid is None or str(nid) != str(node_id):
            continue
        # `create_node` is just a WRAPPER ("Author node") the reader doesn't care about — split it into
        # its real sub-stages (propose / implement / repair) so each is its own band. Every OTHER trace
        # (evaluate, foresight_rank, strategy_consult, …) is already one meaningful stage. Group each
        # generation/tool under the TOP-LEVEL sub-operation it lives in (falling back to create_node
        # itself when a generation sits directly under the root — so nothing is ever dropped).
        if (root or {}).get("name") == "create_node" and root:
            by_sid = {s.get("span_id"): s for s in ss_sorted}
            root_sid = root["span_id"]

            def _stage_of(s):
                cur, top_op = by_sid.get(s.get("parent_id")), None
                while cur is not None and cur.get("span_id") != root_sid:
                    if cur.get("kind") == "operation":
                        top_op = cur
                    cur = by_sid.get(cur.get("parent_id"))
                if top_op is not None:
                    return top_op
                # LIVE: the enclosing operation span (e.g. implement) isn't flushed to disk yet — it's
                # written on close — so the walk can't find it and the child would fall back to the
                # create_node root (Developer calls shown under the Researcher until the node finishes).
                # The child carries its `phase` (tracing._phase_ctx); synthesize a stable stage from it so
                # it bands under the right phase immediately. Post-hoc the real op span exists and wins.
                ph = (s.get("attributes") or {}).get("phase")
                if ph:
                    return {"span_id": f"phase:{ph}", "name": ph, "start": s.get("start", 0.0)}
                return root

            groups: dict = {}
            for s in ss_sorted:
                if s.get("span_id") == root_sid:
                    continue
                stg = _stage_of(s)
                groups.setdefault(stg.get("span_id"), {"span": stg, "spans": []})["spans"].append(s)
            for g in sorted(groups.values(), key=lambda x: x["span"].get("start", 0.0)):
                grp = sorted(g["spans"], key=lambda x: x.get("start", 0.0))
                turns = _thread_turns(grp, by_id)
                if turns:
                    stages.append({"trace_id": tid, "label": g["span"].get("name"),
                                   "start": g["span"].get("start", 0.0),
                                   "rollup": _rollup(grp), "turns": turns})
            continue
        turns = _thread_turns(ss_sorted, by_id)
        if turns:
            stages.append({"trace_id": tid, "label": (root or {}).get("name"),
                           "start": (root or {}).get("start", 0.0),
                           "rollup": _rollup(ss), "turns": turns})
    stages.sort(key=lambda x: x.get("start", 0.0))
    return {"run_id": state.run_id, "task_id": state.task_id, "node_id": str(node_id), "stages": stages}


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

    # Resolve each span's EFFECTIVE node: its own stamped node_id, else the node_id on its trace's ROOT.
    # node_id is now stamped PER-SPAN (tracing._node_ctx), so a single long-lived Developer tool-loop
    # trace — which serves several nodes in sequence — splits correctly across them by each span's own
    # id. The root fallback (NOT a full ancestor walk, which would bleed one node's id onto the whole
    # of a shared trace) keeps OLD root-only logs working: a create_node trace whose children carry no
    # id attributes to its root's node. Spans with neither → `unscoped`.
    root_nid: dict[str, Optional[int]] = {}
    for tid, sps in by_trace.items():
        f = _tree(sps)
        root_nid[tid] = _node_id_of(f[0]) if f else None

    node_spans: dict[str, list[dict]] = defaultdict(list)
    unscoped_spans: list[dict] = []
    for s in spans:
        nid = _node_id_of(s)
        if nid is None:
            nid = root_nid.get(s.get("trace_id"))
        (node_spans[str(nid)] if nid is not None else unscoped_spans).append(s)
    nodes: dict[str, list[dict]] = {nid: _tree(sps) for nid, sps in node_spans.items()}
    unscoped = _tree(unscoped_spans)

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
