"""UI projection of the trace (ADR-17): join the research tree (from `events.jsonl` → RunState)
to its execution detail (from `spans.jsonl`). Pure reader of files-as-truth — never a source of
truth. Produces a per-node span forest the HTML view and the future React UI both consume.

Spans nest by (trace_id, span_id, parent_id); each top-level operation is its own trace tagged
with node_id (see tracing.py), so we group traces by node_id and build a child tree per trace.
"""
from __future__ import annotations

import json
import math
import os
import sys
from collections import defaultdict
from typing import Optional

from looplab.core.models import RunState


_MAX_SPAN_ID_CHARS = 256
_MAX_NODE_ID_CHARS = 128
_MAX_TRACE_TOKENS = (1 << 63) - 1
_MAX_TRACE_SECONDS = 1e15
_MAX_TRACE_FLOAT = sys.float_info.max


def _normalized_id(value) -> Optional[str]:
    """Return one bounded, hashable span/trace id, or ``None`` when it is unusable.

    Current writers emit compact hex strings. Bounded non-negative integers are accepted for old/custom
    exporters and canonicalized to strings so parent references still compare consistently. Containers,
    booleans and enormous strings are invalid rather than becoming dictionary keys in every trace view.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value if value and len(value) <= _MAX_SPAN_ID_CHARS else None
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _MAX_TRACE_TOKENS:
        return str(value)
    return None


def _finite_number(value, *, default=0.0, nonnegative: bool = False,
                   maximum: float = _MAX_TRACE_FLOAT):
    """Coerce an untrusted JSON scalar without allowing NaN/inf/huge values into sorting or sums."""
    if isinstance(value, bool):
        return default
    if isinstance(value, str) and len(value.strip()) > 64:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if (not math.isfinite(number) or abs(number) > maximum
            or (nonnegative and number < 0.0)):
        return default
    if isinstance(value, (int, float)):
        return value
    return number


def _safe_token_count(value) -> int:
    """Signed-int64, non-negative token projection shared by normalization and roll-ups."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if 0 <= value <= _MAX_TRACE_TOKENS else 0
    if isinstance(value, str) and len(value.strip()) > 32:
        return 0
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    if not math.isfinite(number) or not number.is_integer():
        return 0
    result = int(number)
    return result if 0 <= result <= _MAX_TRACE_TOKENS else 0


def _normalize_span(value) -> Optional[dict]:
    """Validate the light structural contract of one durable span.

    A complete, valid-JSON line with a bad schema is a quarantined observation, not an end-of-log marker.
    Invalid required ids drop the one line; recoverable fields degrade to safe defaults. The returned
    dictionaries are shallow copies, so readers never mutate the durable/raw objects they were handed.
    """
    if not isinstance(value, dict):
        return None
    span_id = _normalized_id(value.get("span_id"))
    trace_id = _normalized_id(value.get("trace_id"))
    if span_id is None or trace_id is None:
        return None

    span = dict(value)
    span["span_id"] = span_id
    span["trace_id"] = trace_id
    span["parent_id"] = _normalized_id(span.get("parent_id"))
    span["start"] = _finite_number(
        span.get("start", 0.0), maximum=_MAX_TRACE_SECONDS)
    if "end" in span:
        span["end"] = _finite_number(span.get("end"), maximum=_MAX_TRACE_SECONDS)
    if "duration_s" in span:
        span["duration_s"] = _finite_number(
            span.get("duration_s"), nonnegative=True, maximum=_MAX_TRACE_SECONDS)

    raw_attributes = span.get("attributes")
    attributes = dict(raw_attributes) if isinstance(raw_attributes, dict) else {}
    node_id = attributes.get("node_id")
    if not ((isinstance(node_id, int) and not isinstance(node_id, bool)
             and 0 <= node_id <= _MAX_TRACE_TOKENS)
            or (isinstance(node_id, str) and node_id
                and len(node_id) <= _MAX_NODE_ID_CHARS)):
        attributes.pop("node_id", None)
    for key in ("phase_span", "input_from"):
        if key in attributes:
            normalized = _normalized_id(attributes.get(key))
            if normalized is None:
                attributes.pop(key, None)
            else:
                attributes[key] = normalized
    usage = attributes.get("usage")
    usage = dict(usage) if isinstance(usage, dict) else {}
    for key in ("prompt", "completion", "total"):
        if key in usage:
            usage[key] = _safe_token_count(usage[key])
    if "usage" in attributes or usage:
        attributes["usage"] = usage
    if "cost" in attributes:
        attributes["cost"] = _finite_number(attributes.get("cost"), nonnegative=True)
    if "tool_calls" in attributes and not isinstance(attributes.get("tool_calls"), list):
        attributes["tool_calls"] = []
    span["attributes"] = attributes
    return span


def _normalize_spans(spans) -> list[dict]:
    out: list[dict] = []
    for value in spans or ():
        normalized = _normalize_span(value)
        if normalized is not None:
            out.append(normalized)
    return out


def load_spans(path: str | os.PathLike) -> list[dict]:
    """Read spans.jsonl, quarantining complete valid-JSON rows with an invalid span shape."""
    from looplab.events.eventstore import iter_jsonl
    return _normalize_spans(iter_jsonl(path))


def _tree(spans: list[dict], *, _normalized: bool = False) -> list[dict]:
    """Build the parent->child forest for one trace from a flat span list."""
    if not _normalized:
        spans = _normalize_spans(spans)
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


def _node_id_of(span: dict) -> Optional[int | str]:
    attributes = span.get("attributes")
    return attributes.get("node_id") if isinstance(attributes, dict) else None


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
        raw_attributes = g.get("attributes")
        a = raw_attributes if isinstance(raw_attributes, dict) else {}
        raw_usage = a.get("usage")
        u = raw_usage if isinstance(raw_usage, dict) else {}
        p = _safe_token_count(u.get("prompt"))
        pt = min(_MAX_TRACE_TOKENS, pt + p)            # SUM of every call's prompt (billed — a tool loop
        peak = max(peak, p)                           # re-sends the growing context, so this is O(turns²))
        ct = min(_MAX_TRACE_TOKENS, ct + _safe_token_count(u.get("completion")))
        tt = min(_MAX_TRACE_TOKENS, tt + _safe_token_count(u.get("total")))
        item_cost = _finite_number(a.get("cost"), nonnegative=True)
        cost = min(_MAX_TRACE_FLOAT, cost + item_cost)
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


_STRIP_IO_KEYS = ("input", "output", "thinking", "input_carry", "input_from")


def _strip_span_io(s: dict) -> dict:
    """Drop the heavy I/O entirely — the run-level trace (Dock timeline) needs only structure, timing,
    model + token usage, not the prompts/outputs. Keeps the whole-run payload tiny; the per-node
    endpoint serves the (capped) full I/O for the Inspector. Also drops the delta bookkeeping
    (`input_carry`/`input_from`): with no `input` they carry no meaning, and leaving them would let a
    stray `hydrate_inputs` on a light span reconstruct to `[]` (its `input` is gone)."""
    a = s.get("attributes")
    if not isinstance(a, dict) or not any(k in a for k in ("input", "output", "thinking")):
        return s
    return {**s, "attributes": {k: v for k, v in a.items() if k not in _STRIP_IO_KEYS}}


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
        if s.get("name") == "stage_started":
            continue          # a zero-work live-band anchor (command_eval) — not a real turn to show
        if kind == "generation":
            inp = a.get("input") if isinstance(a.get("input"), list) else []
            n = len(inp)
            # A `request` marks a sub-loop start. Delta-encoded logs (tracing.generation) say so
            # explicitly: a base generation stores `input_carry == 0` (it carried NOTHING from a prior
            # generation), so its `input` IS the full initial context and the request is shown from it
            # verbatim; a non-base generation carries a real prefix (carry > 0) and its stored `input` is
            # only the delta — (correctly) not a boundary. Keying on `input_carry == 0` (not
            # `input_from is None`) matches `hydrate_inputs`, which likewise treats carry=0 as
            # self-contained, so a degenerate carry=0-with-back-ref span is still read as a base.
            # Old full logs have no `input_carry` → fall back to the message-count-drop heuristic.
            is_base = (a.get("input_carry") == 0) if ("input_carry" in a) \
                else (prev_in is None or n <= prev_in)
            if is_base:                              # first call / context reset → new sub-loop
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
        elif kind == "operation" and a.get("stage"):
            # An eval PIPELINE stage (train / score — command_eval opens one op span per stage):
            # no LLM turns inside, but the reader wants it as a block in the node's life story
            # ("… Developer · implement, Train, Evaluate …"). Rendered via the tool turn shape.
            rc, to = a.get("exit_code"), a.get("timed_out")
            # `not rc` was truthy for BOTH exit 0 and a MISSING code — a stage span closed by an
            # exception before its exit_code was recorded (status "ERROR", rc None) then rendered "ok".
            if a.get("reused"):
                status = "reused"        # skipped on a repair re-eval; its earlier artifact is kept
            elif to:
                status = "timeout"
            elif rc is None:
                status = "error" if s.get("status") == "ERROR" else "?"
            else:
                status = "ok" if rc == 0 else f"exit {rc}"
            secs = s.get("duration_s")
            turns.append({"type": "tool", "name": str(a.get("stage")),
                          "input": "",
                          "output": f"{status}" + (f" · {round(float(secs), 1)}s" if secs else ""),
                          "status": s.get("status"), "seconds": secs})
        # other operation spans carry structure only — skipped in the linear reading view.
    return turns


def hydrate_inputs(spans: list[dict]) -> list[dict]:
    """Reconstruct the FULL verbatim `input` of every delta-encoded generation in `spans` from its
    `input_from` chain (see `tracing.generation`): full = reconstruct(input_from)[:input_carry] + delta.
    Returns spans with `input` expanded and the `input_carry`/`input_from` bookkeeping dropped, so a
    reader (the single-observation view, the per-op trace tree) sees exactly the prompt the LLM received,
    with nothing lost. A generation with no `input_carry` (old full logs, or a fresh base) passes through
    unchanged. Reconstruct within the passed set (a whole trace) — the chain never leaves its trace.
    If an ANCESTOR span is absent (a torn/offset-skipped line — `span_index._read_full` drops one) the
    chain can't bottom out at its real base, so the reconstruction is a TRUNCATED prefix; such spans are
    stamped `input_partial=True` so a reader never presents a short input as the verbatim prompt."""
    spans = _normalize_spans(spans)
    by_sid = {s.get("span_id"): s for s in spans if s.get("span_id")}
    memo: dict = {}
    partial: dict = {}                    # sid -> True when its chain bottomed out at a missing ref/cycle

    def _full(sid) -> list:
        # Reconstruct iteratively (NOT recursively): a tool-loop can chain thousands of generations in
        # one sub-loop, and recursion would blow the stack (RecursionError past ~1000) on a deep chain
        # walked in non-file order. Walk UP the linear input_from chain collecting each delta, stopping
        # at a base / already-memoized ancestor / missing ref / cycle, then apply the deltas back DOWN.
        if sid in memo:
            return memo[sid]
        chain: list[tuple] = []           # (span_id, carry, delta_input), from `sid` upward
        seen: set = set()
        cur_sid = sid
        base: list = []
        broke = False                     # True ⇒ a referenced ancestor was missing → prefix is truncated
        while True:
            if cur_sid in memo:
                base = memo[cur_sid]
                broke = partial.get(cur_sid, False)
                break
            if cur_sid is None or cur_sid in seen:    # missing ref / cycle → empty base, INCOMPLETE
                base = []
                broke = True
                break
            seen.add(cur_sid)
            s = by_sid.get(cur_sid)
            if s is None:                             # referenced ancestor absent from the span set
                base = []
                broke = True
                break
            a = s.get("attributes") or {}
            cur = a.get("input")
            if "input_carry" not in a or not isinstance(cur, list):
                base = cur if isinstance(cur, list) else []
                break   # old log / non-list → input IS full
            frm = a.get("input_from")
            if frm is None:                            # self-contained base: its `input` is the full ctx
                memo[cur_sid] = list(cur)
                partial[cur_sid] = False
                base = memo[cur_sid]
                break
            # Coerce carry to a NON-NEGATIVE int: a malformed span (bit-rot on a network mount, or a
            # hand-edited log) whose input_carry is a string/float would make `full[:carry]` raise
            # TypeError and abort the WHOLE trace, and a negative carry would silently truncate the
            # prefix. Fall back to 0 (the delta stands as the full input) — the safe degradation the
            # non-list/absent-carry branch above already uses — instead of crashing the projection.
            raw_carry = a.get("input_carry")
            carry = raw_carry if (isinstance(raw_carry, int) and not isinstance(raw_carry, bool)
                                  and raw_carry >= 0) else 0
            chain.append((cur_sid, carry, cur))
            cur_sid = frm
        full = base
        for csid, carry, delta in reversed(chain):     # apply deltas base→leaf, memoizing every level
            full = list(full[:carry]) + list(delta)
            memo[csid] = full
            partial[csid] = broke
        if sid not in memo:
            memo[sid] = full
            partial[sid] = broke
        return memo[sid]

    out: list[dict] = []
    for s in spans:
        a = s.get("attributes")
        if isinstance(a, dict) and "input_carry" in a and s.get("kind") == "generation":
            na = {k: v for k, v in a.items() if k not in ("input_carry", "input_from")}
            na["input"] = _full(s.get("span_id"))
            if partial.get(s.get("span_id")):
                na["input_partial"] = True             # an ancestor was missing → `input` is truncated
            out.append({**s, "attributes": na})
        else:
            out.append(s)
    return out


def build_conversation(state: RunState, spans: list[dict], node_id) -> dict:
    """Per-node linear conversation (companion to `build_trace_view`). One `stage` per trace tagged
    with this node (create_node / evaluate / …), each a de-duplicated thread of turns. Reader of
    files-as-truth; caps every string for the browser, but never re-sends the growing history."""
    spans = _normalize_spans(spans)
    by_id = {s["span_id"]: s for s in spans}
    by_trace: dict[str, list[dict]] = defaultdict(list)
    for s in spans:
        by_trace[s.get("trace_id")].append(s)
    stages: list[dict] = []
    for tid, ss in by_trace.items():
        ss_sorted = sorted(ss, key=lambda x: x.get("start", 0.0))
        # The REAL root may be absent LIVE: an operation span is written only on CLOSE, and
        # `create_node` closes at node END — so for the whole life of a node its trace has no root on
        # disk. The old code then fell back to the first span (a generation), missed the create_node
        # split branch entirely, and rendered the ENTIRE node as ONE flat band labeled "generation"
        # whose turns kept appending across role changes (the "Developer writes into the previous
        # Researcher block" bug). Grouping below never needs the root, only its ABSENCE handled.
        root = next((s for s in ss_sorted if s.get("parent_id") is None), None)
        first = root or (ss_sorted[0] if ss_sorted else None)
        nid = _node_id_of(first) if first else None
        if nid is None or str(nid) != str(node_id):
            continue
        # Split EVERY trace into its sub-loop bands (propose / stages / plan / implement / repair /
        # inline_repair / …) so the conversation reads as ordered role blocks. Wrapper roots
        # (`create_node` = "Author node", `seed_workspace` around an inline repair) are structure the
        # reader doesn't care about; a trace that IS one meaningful stage (foresight_rank, lessons)
        # naturally yields a single band with that label.
        by_sid = {s.get("span_id"): s for s in ss_sorted}
        root_sid = root["span_id"] if root else None

        def _stage_of(s):
            # Band identity, best evidence first:
            #   1. `phase_span` (tracing stamp): the innermost open operation's SPAN ID — exact
            #      sub-loop identity, live and post-hoc, two same-phase retries stay separate.
            #   2. an eval pipeline stage op (train/score — carries `stage`): its own band.
            #   3. pre-phase_span traces: nearest ancestor op matching the `phase` name (post-hoc),
            #      else a band synthesized from the bare phase name (live, op not flushed yet).
            #   4. no phase at all (old traces): the top-level sub-op under the root, else the root.
            a = s.get("attributes") or {}
            ph, ph_sid = a.get("phase"), a.get("phase_span")
            if ph_sid:
                op = by_sid.get(ph_sid)
                return op if op is not None else {"span_id": ph_sid, "name": ph,
                                                  "start": s.get("start", 0.0)}
            if s.get("kind") == "operation" and a.get("stage"):
                return s
            cur, top_op, ph_op = by_sid.get(s.get("parent_id")), None, None
            while cur is not None and cur.get("span_id") != root_sid:
                if cur.get("kind") == "operation":
                    top_op = cur
                    if ph_op is None and ph and cur.get("name") == ph:
                        ph_op = cur       # nearest ancestor op matching the stamp: the real sub-loop
                cur = by_sid.get(cur.get("parent_id"))
            if ph_op is not None:
                return ph_op
            if ph:
                return {"span_id": f"phase:{ph}", "name": ph, "start": s.get("start", 0.0)}
            return top_op or root or {"span_id": f"trace:{tid}",
                                      "name": (first or {}).get("name"),
                                      "start": s.get("start", 0.0)}

        groups: dict = {}
        for s in ss_sorted:
            if root_sid is not None and s.get("span_id") == root_sid:
                continue
            stg = _stage_of(s)
            groups.setdefault(stg.get("span_id"), {"span": stg, "spans": []})["spans"].append(s)
        # Order + timestamp bands by their first CONTENT span's start, not the op span's: the
        # Developer's stages/plan phases run INSIDE the orchestrator's `implement` span, so implement
        # OPENS first even though its own turns come last — sorting by op-span start would show the
        # implement band before the stages band whose turns actually happened first. (A NESTED op
        # span rides in its parent's group and would likewise drag the parent band's start back.)
        def _first_turn_start(g):
            return min((s.get("start", 0.0) for s in g["spans"] if s.get("kind") != "operation"),
                       default=g["span"].get("start", 0.0))
        for g in sorted(groups.values(), key=_first_turn_start):
            grp = sorted(g["spans"], key=lambda x: x.get("start", 0.0))
            turns = _thread_turns(grp, by_id)
            # Keep a stage band that is still RUNNING even though it has no turns yet: a live training
            # subprocess emits only the `stage_started` anchor (its own turn is suppressed as noise) and
            # its stage op flushes on close — so without this the Train/Evaluate band would be dropped as
            # "empty" for the whole run and only appear once the stage finished. The UI renders the live
            # stage log inside the (turnless) band.
            running_stage = any(s.get("name") == "stage_started" for s in grp)
            if turns or running_stage:
                stages.append({"trace_id": tid, "label": g["span"].get("name"),
                               "start": _first_turn_start(g),
                               "rollup": _rollup(grp), "turns": turns})
    stages.sort(key=lambda x: x.get("start", 0.0))
    return {"run_id": state.run_id, "task_id": state.task_id, "node_id": str(node_id), "stages": stages}


def build_trace_view(state: RunState, spans: list[dict], *, light: bool = False) -> dict:
    """Group spans into per-node trees + a run summary, correlated by node_id (carried on each
    trace's root span). Spans with no node_id land under `unscoped` (e.g. onboarding). Each span
    carries `kind` (operation/generation/tool) so the UI renders the Langfuse-style observation
    tree; `rollups` gives per-node token/cost/observation totals aggregated from generations.
    Heavy generation I/O is truncated (see `_cap_span_io`) so the payload stays browser-safe; with
    `light=True` it's dropped entirely (run-level timeline doesn't need prompts/outputs)."""
    spans = [(_strip_span_io if light else _cap_span_io)(s) for s in _normalize_spans(spans)]
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
        f = _tree(sps, _normalized=True)
        root_nid[tid] = _node_id_of(f[0]) if f else None

    node_spans: dict[str, list[dict]] = defaultdict(list)
    unscoped_spans: list[dict] = []
    for s in spans:
        nid = _node_id_of(s)
        if nid is None:
            nid = root_nid.get(s.get("trace_id"))
        (node_spans[str(nid)] if nid is not None else unscoped_spans).append(s)
    nodes: dict[str, list[dict]] = {
        nid: _tree(sps, _normalized=True) for nid, sps in node_spans.items()
    }
    unscoped = _tree(unscoped_spans, _normalized=True)

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
