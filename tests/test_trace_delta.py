"""Delta-at-write for LLM generation inputs (tracing.generation): the tool-loop re-sends the whole
growing conversation every turn, so storing each generation's full `input` made ~90% of spans.jsonl a
re-send. We now store only the delta past the common prefix + a back-ref (`input_carry`/`input_from`);
the trace views reconstruct the full verbatim prompt (`traceview.hydrate_inputs`). These tests pin:
smaller on-disk, EXACT reconstruction, unchanged conversation projection, and old-log back-compat."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from looplab.core import tracing
from looplab.core.tracing import JsonlSpanExporter, Tracer
from looplab.core.models import RunState
from looplab.events.eventstore import iter_jsonl
from looplab.events.traceview import hydrate_inputs, build_conversation, load_spans

SYS = {"role": "system", "content": "You are a developer. " + "ctx " * 200}
USER = {"role": "user", "content": "Write the solution. " + "spec " * 200}


@pytest.fixture(autouse=True)
def _restore_llm_capture():
    """These tests flip the process-global ``tracing._CAPTURE_LLM_IO`` on (via set_llm_capture) with no
    teardown of their own. Save/restore it around every test so the mutation can't leak to other tests
    in the worker and make a default-capture-off assertion order-dependent (pytest-random / xdist)."""
    saved = tracing._CAPTURE_LLM_IO
    try:
        yield
    finally:
        tracing.set_llm_capture(saved)


def _write_toolloop(rd: Path, n_turns: int = 6):
    """One node's tool-loop: n generations each re-sent the FULL growing history + a tool between them."""
    tracing.set_llm_capture(True)
    t = Tracer(JsonlSpanExporter(rd / "spans.jsonl"), run_id="demo")
    sent = []                                    # the FULL input we sent to each generation (ground truth)
    history = [SYS, USER]
    with t.span("create_node", new_trace=True, node_id=0):
        for k in range(n_turns):
            msgs = [dict(m) for m in history]    # the full re-sent history this turn
            sent.append(msgs)
            with tracing.generation(op="chat", model="m", messages=msgs) as g:
                g.output(f"turn {k} plan " + "out " * 50).usage({"prompt_tokens": 100 * k, "total_tokens": 100 * k})
            with tracing.tool("read_file", {"path": f"m{k}.py"}) as to:
                to.output("file body " + "line " * 50)
            history = history + [{"role": "assistant", "content": f"turn {k} plan " + "out " * 50},
                                 {"role": "user", "content": "file body " + "line " * 50}]
    return sent


def test_generation_input_is_delta_encoded_on_disk(tmp_path):
    rd = tmp_path / "demo"
    rd.mkdir()
    sent = _write_toolloop(rd, n_turns=6)
    gens = [s for s in iter_jsonl(rd / "spans.jsonl") if s.get("kind") == "generation"]
    assert len(gens) == 6
    # First generation is a full BASE; the rest store only their (small) delta with a back-ref.
    base = gens[0]["attributes"]
    assert base["input_from"] is None and base["input_carry"] == 0
    assert len(base["input"]) == 2                       # [system, user] — the full initial context
    carries = [g["attributes"]["input_carry"] for g in gens[1:]]
    for g in gens[1:]:
        a = g["attributes"]
        assert a["input_from"] is not None and a["input_carry"] > 0
        assert len(a["input"]) <= 2                       # each turn's delta is the ~2 new messages only
    assert carries == sorted(carries) and carries[-1] > carries[0]   # carried prefix GROWS each turn
    # on-disk size is much smaller than storing every full re-sent history would be
    stored = sum(len(json.dumps(g["attributes"]["input"])) for g in gens)
    full = sum(len(json.dumps(s)) for s in sent)
    assert stored < full / 2                             # >2x smaller (grows with turns)


def test_hydrate_reconstructs_full_input_exactly(tmp_path):
    rd = tmp_path / "demo"
    rd.mkdir()
    sent = _write_toolloop(rd, n_turns=6)
    spans = load_spans(rd / "spans.jsonl")
    hyd = {h["span_id"]: h for h in hydrate_inputs(spans)}
    gens = [s for s in spans if s.get("kind") == "generation"]
    gens.sort(key=lambda s: s.get("start", 0.0))
    for i, g in enumerate(gens):
        recon = hyd[g["span_id"]]["attributes"]["input"]
        assert recon == sent[i]                          # byte-for-byte the prompt we actually sent
        assert "input_carry" not in hyd[g["span_id"]]["attributes"]   # bookkeeping stripped


def test_conversation_projection_unchanged_by_delta(tmp_path):
    """build_conversation shows the request once (from the base) — a delta log yields the same thread as
    if every generation had stored its full input."""
    rd = tmp_path / "demo"
    rd.mkdir()
    _write_toolloop(rd, n_turns=6)
    st = RunState(run_id="demo", task_id="t", goal="g", direction="min")
    convo = build_conversation(st, load_spans(rd / "spans.jsonl"), 0)
    stages = convo["stages"]
    assert stages
    turns = stages[0]["turns"]
    requests = [t for t in turns if t["type"] == "request"]
    gens = [t for t in turns if t["type"] == "generation"]
    assert len(requests) == 1                            # ONE request for the sub-loop, not one per turn
    assert len(gens) == 6
    # the request carries the real initial context (system + user), shown once
    assert any("You are a developer" in (m.get("content") or "") for m in requests[0]["messages"])


def test_span_io_endpoint_reconstructs_bounded_input_with_omission_truth(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from looplab.serve.server import make_app
    from looplab.events.eventstore import EventStore
    rd = tmp_path / "demo"
    rd.mkdir()
    s = EventStore(rd / "events.jsonl")
    s.append("run_started", {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"})
    sent = _write_toolloop(rd, n_turns=6)
    gens = [sp for sp in iter_jsonl(rd / "spans.jsonl") if sp.get("kind") == "generation"]
    last = sorted(gens, key=lambda x: x.get("start", 0.0))[-1]
    client = TestClient(make_app(tmp_path))
    body = client.get(f"/api/runs/demo/spans/{last['span_id']}").json()
    # The chain is reconstructed before projection, then the browser receives only a bounded head/tail
    # preview with explicit truth that messages were omitted. Raw exact diagnostics remain in JSONL.
    shown = body["attributes"]["input"]
    assert len(shown) <= 10
    assert shown[0] == sent[-1][0] and shown[-1] == sent[-1][-1]
    assert body["attributes"]["input_partial"] is True
    assert body["projection"]["omitted_messages"] == len(sent[-1]) - len(shown)
    assert "input_carry" not in body["attributes"]


def test_sub_loop_reset_within_a_trace_keeps_its_request(tmp_path):
    """A trace with TWO sub-loops (implement, then a repair that RESETS the conversation — sharing only
    the system prefix) must show a request for EACH. Regression: delta-chaining across the reset would
    make the repair generation a delta (input_from set), so `_thread_turns` wouldn't mark it a boundary
    and the repair sub-loop's initial context would vanish from the conversation. The strict-extension
    rule keeps the reset a full base."""
    rd = tmp_path / "demo"
    rd.mkdir()
    tracing.set_llm_capture(True)
    t = Tracer(JsonlSpanExporter(rd / "spans.jsonl"), run_id="demo")
    sys = {"role": "system", "content": "You are a developer"}
    sent = {}
    with t.span("create_node", new_trace=True, node_id=0):
        for phase, first_user in (("implement", "IMPLEMENT the solution"), ("repair", "REPAIR the failing test")):
            with tracing.operation(phase):
                hist = [sys, {"role": "user", "content": first_user}]
                for k in range(3):
                    msgs = [dict(m) for m in hist]
                    with tracing.generation(op="chat", model="m", messages=msgs) as g:
                        g.output(f"{phase} {k}")
                    sent[(phase, k)] = msgs
                    hist = hist + [{"role": "assistant", "content": f"{phase} {k}"},
                                   {"role": "user", "content": f"tool {phase} {k}"}]
    st = RunState(run_id="demo", task_id="t", goal="g", direction="min")
    convo = build_conversation(st, load_spans(rd / "spans.jsonl"), 0)
    bands = {s["label"]: s for s in convo["stages"]}
    assert set(bands) == {"implement", "repair"}
    for phase, first_user in (("implement", "IMPLEMENT"), ("repair", "REPAIR")):
        reqs = [x for x in bands[phase]["turns"] if x["type"] == "request"]
        assert len(reqs) == 1, f"{phase} lost its request"
        assert any(first_user in (m.get("content") or "") for m in reqs[0]["messages"])
    # each sub-loop's FIRST generation is a full base; reconstruction is still exact everywhere
    hyd = {h["span_id"]: h for h in hydrate_inputs(load_spans(rd / "spans.jsonl"))}
    gens = sorted((s for s in load_spans(rd / "spans.jsonl") if s.get("kind") == "generation"),
                  key=lambda s: s.get("start", 0.0))
    bases = [g for g in gens if (g.get("attributes") or {}).get("input_from") is None]
    assert len(bases) == 2                                  # implement-base + repair-base
    # reconstruction stays EXACT across the reset — each generation's full input matches what was sent
    by_out = {(g.get("attributes") or {}).get("output"): g["span_id"] for g in gens}
    for (phase, k), msgs in sent.items():
        assert hyd[by_out[f"{phase} {k}"]]["attributes"]["input"] == msgs


def test_zero_carry_with_backref_reads_as_a_base(tmp_path):
    """A generation with `input_carry == 0` is a self-contained base EVEN IF it also carries an
    `input_from` back-ref (a degenerate span an older writer could emit when the prior generation had
    empty messages). The reader keys the request boundary on carry==0 (matching `hydrate_inputs`), so
    the band still shows its request instead of swallowing it. Guards against the input_from-based check
    regressing."""
    spans = [{"name": "create_node", "kind": "operation", "trace_id": "t0", "span_id": "r0",
              "parent_id": None, "run_id": "demo", "attributes": {"node_id": 0}, "events": [],
              "status": "OK", "start": 0.0, "duration_s": 3.0}]
    # g0 base (carry 0, no ref). g1 delta extends g0. g2 is a RESET but still names a back-ref with
    # carry=0 — the degenerate shape; it must be read as a fresh base, not a delta of g1.
    g0_in = [{"role": "system", "content": "SYS-A"}, {"role": "user", "content": "do A"}]
    g1_in = [{"role": "assistant", "content": "a0"}, {"role": "user", "content": "tool0"}]
    g2_in = [{"role": "system", "content": "SYS-B"}, {"role": "user", "content": "do B"}]
    for k, (frm, carry, inp) in enumerate([(None, 0, g0_in), ("g0", 2, g1_in), ("g1", 0, g2_in)]):
        spans.append({"name": "llm", "kind": "generation", "trace_id": "t0", "span_id": f"g{k}",
                      "parent_id": "r0", "run_id": "demo",
                      "attributes": {"node_id": 0, "input": inp, "input_carry": carry,
                                     "input_from": frm, "output": f"o{k}"},
                      "events": [], "status": "OK", "start": float(k + 1), "duration_s": 1.0})
    (tmp_path / "spans.jsonl").write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")
    st = RunState(run_id="demo", task_id="t", goal="g", direction="min")
    convo = build_conversation(st, load_spans(tmp_path / "spans.jsonl"), 0)
    turns = convo["stages"][0]["turns"]
    reqs = [t for t in turns if t["type"] == "request"]
    assert len(reqs) == 2                                 # g0 base + g2 (carry=0) base — g1 is not one
    assert any("do B" in (m.get("content") or "") for m in reqs[1]["messages"])   # g2's request kept
    # hydration reconstructs g2's full input from its own delta (carry=0 ⇒ parent ignored)
    hyd = {h["span_id"]: h for h in hydrate_inputs(load_spans(tmp_path / "spans.jsonl"))}
    assert hyd["g2"]["attributes"]["input"] == g2_in


def test_trace_json_projection_holds_full_input(tmp_path):
    """`finalize` writes trace.json as `build_trace_view(state, hydrate_inputs(load_spans(...)))` — the
    persisted per-op tree must carry the FULL prompt (like the live `/trace/by_trace`), not the on-disk
    delta. Without the hydrate step a non-base generation's `input` in trace.json is just its delta
    (missing the system+earlier turns), disagreeing with the live endpoint."""
    from looplab.events.traceview import build_trace_view

    def _gens(view):
        out = []
        def walk(ns):
            for n in ns:
                if n.get("kind") == "generation":
                    out.append(n)
                walk(n.get("children") or [])
        for nid_spans in view["nodes"].values():
            walk(nid_spans)
        return out

    rd = tmp_path / "demo"
    rd.mkdir()
    _write_toolloop(rd, n_turns=6)
    st = RunState(run_id="demo", task_id="t", goal="g", direction="min")
    spans = load_spans(rd / "spans.jsonl")
    raw = _gens(build_trace_view(st, spans))                         # NO hydrate — the buggy path
    hyd = _gens(build_trace_view(st, hydrate_inputs(spans)))         # finalize's actual path
    # the LAST generation carried the whole grown history; its delta stored only the ~2 new messages,
    # but the hydrated projection reconstructs the full context (system prompt present, more messages).
    raw.sort(key=lambda g: g.get("start", 0.0))
    hyd.sort(key=lambda g: g.get("start", 0.0))
    raw_last, hyd_last = raw[-1]["attributes"]["input"], hyd[-1]["attributes"]["input"]
    assert not any(m.get("role") == "system" for m in raw_last)      # delta alone lost the system prompt
    assert any(m.get("role") == "system" for m in hyd_last)          # hydrated projection has it back
    assert len(hyd_last) > len(raw_last)
    # the delta bookkeeping is stripped from the archived projection (hydrate drops it)
    assert "input_carry" not in hyd[-1]["attributes"] and "input_from" not in hyd[-1]["attributes"]


def test_deep_chain_hydrates_without_recursion(tmp_path):
    """A very long single-sub-loop tool-loop chains thousands of generations; reconstruction must not
    blow the Python stack (RecursionError past ~1000) regardless of the order spans are presented in.
    `hydrate_inputs` walks the chain iteratively, so a 3000-deep chain in EITHER order reconstructs
    exactly."""
    n = 3000
    r = {"name": "create_node", "kind": "operation", "trace_id": "t0", "span_id": "r0",
         "parent_id": None, "run_id": "demo", "attributes": {"node_id": 0}, "events": [],
         "status": "OK", "start": 0.0, "duration_s": 1.0}
    gens = []
    for k in range(n):
        # g0 is a base ([m0]); each g_{k} carries the whole prior chain (carry=k+1) + one new message.
        frm, carry, delta = (None, 0, [{"role": "user", "content": "m0"}]) if k == 0 \
            else (f"g{k-1}", k + 1, [{"role": "user", "content": f"m{k}"}])
        gens.append({"name": "llm", "kind": "generation", "trace_id": "t0", "span_id": f"g{k}",
                     "parent_id": "r0", "run_id": "demo",
                     "attributes": {"node_id": 0, "input": delta, "input_carry": carry, "input_from": frm},
                     "events": [], "status": "OK", "start": float(k + 1), "duration_s": 1.0})
    expected_last = [{"role": "user", "content": f"m{k}"} for k in range(n)]     # full grown history
    for order in ([r] + gens, [r] + list(reversed(gens))):                       # file order AND reversed
        hyd = {h["span_id"]: h for h in hydrate_inputs(order)}
        assert hyd[f"g{n-1}"]["attributes"]["input"] == expected_last
        assert hyd["g0"]["attributes"]["input"] == [{"role": "user", "content": "m0"}]


def test_missing_ancestor_marks_input_partial(tmp_path):
    """Delta-encoding chains a generation's input on its ancestors. If a middle ancestor is absent from
    the span set (a torn / offset-skipped line — `span_index._read_full` drops one), the reconstruction
    is a TRUNCATED prefix. `hydrate_inputs` must NOT present that as the verbatim prompt: it stamps
    `input_partial=True` on every span whose chain couldn't reach its real base, preserving the
    span-index 'never silently wrong data' contract."""
    rd = tmp_path / "demo"
    rd.mkdir()
    sent = _write_toolloop(rd, n_turns=6)
    spans = load_spans(rd / "spans.jsonl")
    gens = sorted((s for s in spans if s.get("kind") == "generation"), key=lambda s: s.get("start", 0.0))
    dropped = gens[2]["span_id"]                                   # remove a MIDDLE generation from the set
    pruned = [s for s in spans if s.get("span_id") != dropped]
    hyd = {h["span_id"]: h for h in hydrate_inputs(pruned)}
    # gens[0], gens[1] are at/above the gap → still exact and NOT marked
    assert hyd[gens[0]["span_id"]]["attributes"]["input"] == sent[0]
    assert not hyd[gens[1]["span_id"]]["attributes"].get("input_partial")
    # gens[3..5] chain THROUGH the missing gens[2] → truncated + flagged, never shown as verbatim
    for g in gens[3:]:
        att = hyd[g["span_id"]]["attributes"]
        assert att.get("input_partial") is True
        assert len(att["input"]) < len(sent[gens.index(g)])       # a short prefix, honestly marked


def test_cyclic_input_from_is_guarded(tmp_path):
    """A corrupt/looping `input_from` (should never happen, but the reader is files-as-truth over an
    external file) resolves without hanging — the cycle guard bounds reconstruction."""
    spans = [{"name": "llm", "kind": "generation", "trace_id": "t0", "span_id": "g0", "parent_id": None,
              "run_id": "demo", "attributes": {"node_id": 0, "input": [{"role": "user", "content": "x"}],
              "input_carry": 1, "input_from": "g1"}, "events": [], "status": "OK", "start": 1.0,
              "duration_s": 1.0},
             {"name": "llm", "kind": "generation", "trace_id": "t0", "span_id": "g1", "parent_id": None,
              "run_id": "demo", "attributes": {"node_id": 0, "input": [{"role": "user", "content": "y"}],
              "input_carry": 1, "input_from": "g0"}, "events": [], "status": "OK", "start": 2.0,
              "duration_s": 1.0}]
    hyd = {h["span_id"]: h["attributes"]["input"] for h in hydrate_inputs(spans)}   # terminates
    assert hyd["g0"] and hyd["g1"]                                                  # both resolve (bounded)


def test_old_full_input_logs_still_work(tmp_path):
    """A pre-delta spans.jsonl (every generation carries its full input, no input_carry) is untouched:
    hydrate is a no-op and the conversation still de-duplicates via the message-count-drop heuristic."""
    rd = tmp_path / "demo"
    rd.mkdir()
    full = [dict(SYS), dict(USER)]
    spans = [{"name": "create_node", "kind": "operation", "trace_id": "t0", "span_id": "r0",
              "parent_id": None, "run_id": "demo", "attributes": {"node_id": 0}, "events": [],
              "status": "OK", "start": 0.0, "duration_s": 1.0}]
    for k in range(3):
        full = full + [{"role": "assistant", "content": f"a{k}"}, {"role": "user", "content": f"u{k}"}]
        spans.append({"name": "llm", "kind": "generation", "trace_id": "t0", "span_id": f"g{k}",
                      "parent_id": "r0", "run_id": "demo",
                      "attributes": {"node_id": 0, "input": [dict(m) for m in full], "output": "o"},
                      "events": [], "status": "OK", "start": float(k + 1), "duration_s": 1.0})
    (rd / "spans.jsonl").write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")
    hydrated = hydrate_inputs(spans)
    assert [item["attributes"]["input"] for item in hydrated[1:]] == [
        item["attributes"]["input"] for item in spans[1:]]
    assert all(item["_projection"]["schema"] >= 2 for item in hydrated)
    st = RunState(run_id="demo", task_id="t", goal="g", direction="min")
    convo = build_conversation(st, load_spans(rd / "spans.jsonl"), 0)
    reqs = [t for t in convo["stages"][0]["turns"] if t["type"] == "request"]
    assert len(reqs) == 1                                 # legacy len-drop heuristic still de-duplicates


def test_trace_tree_tolerates_a_deep_span_chain_without_recursionerror():
    # A crafted/corrupt spans.jsonl with a pathologically deep parent_id chain must not blow Python's
    # recursion limit when the tree is sorted — the "trace view never crashes on corrupt spans" contract
    # the projections harden for (the sibling hydrate_inputs is already iterative for the analogous case).
    from looplab.events.traceview import _tree
    spans = [{"span_id": f"s{i}", "parent_id": (f"s{i - 1}" if i else None), "start": float(i),
              "kind": "operation", "name": "op"} for i in range(6000)]
    roots = _tree(spans, _normalized=True)          # must not raise RecursionError
    assert len(roots) == 1 and roots[0]["span_id"] == "s0"
