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
from looplab.events import traceview
from looplab.events.eventstore import iter_jsonl
from looplab.events.traceview import (
    build_conversation, build_trace_view, hydrate_inputs, load_span_tail, load_spans)

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


def _span_row(sid: str, *, trace_id: str = "tail-trace", kind: str = "operation",
              start: float = 0.0, attributes: dict | None = None) -> dict:
    return {
        "name": "generation" if kind == "generation" else sid,
        "kind": kind,
        "trace_id": trace_id,
        "span_id": sid,
        "parent_id": None,
        "run_id": "demo",
        "attributes": {"node_id": 0, **(attributes or {})},
        "events": [],
        "status": "OK",
        "start": start,
        "duration_s": 0.1,
    }


def test_load_span_tail_bounds_materialization_and_counts_admitted_rows(tmp_path):
    """The static reader counts the accepted prefix exactly but retains only its requested tail."""
    path = tmp_path / "spans.jsonl"
    rows = [
        _span_row("s0", start=0),
        {},  # complete JSON object, but not a span: quarantine this row and keep reading
        _span_row("s1", start=1),
        _span_row("s2", start=2),
        _span_row("s3", start=3),
        _span_row("s4", start=4),
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    tail, total = load_span_tail(path, 2)
    empty_tail, repeated_total = load_span_tail(path, 0)

    assert total == repeated_total == 5
    assert [span["span_id"] for span in tail] == ["s3", "s4"]
    assert empty_tail == []


def test_load_span_tail_uses_the_append_log_prefix_and_io_contract(tmp_path):
    """Complete corruption ends the prefix; absence is empty and non-absence I/O errors propagate."""
    path = tmp_path / "spans.jsonl"
    first = json.dumps(_span_row("before")).encode("utf-8") + b"\n"
    later = json.dumps(_span_row("hidden")).encode("utf-8") + b"\n"
    path.write_bytes(first + b"{not-json}\n" + later)

    tail, total = load_span_tail(path, 8)

    assert total == 1 and [span["span_id"] for span in tail] == ["before"]
    assert load_span_tail(tmp_path / "missing.jsonl", 8) == ([], 0)
    with pytest.raises(IsADirectoryError):
        load_span_tail(tmp_path, 8)


def test_oversized_physical_trace_row_ends_prefix_with_bounded_reads(tmp_path, monkeypatch):
    """One newline-free legacy/corrupt row cannot become an arbitrarily large ``bytes`` object.

    The synthetic reader is an RSS-independent proof: it can represent a 1 GB row without storing
    one, and the scanner must stop after only ceiling+one-chunk bytes. The file assertion pins the
    append-log consequence too — a complete valid span behind that oversized envelope stays hidden.
    """
    ceiling, chunk = 257, 31

    class GeneratedOversizedRow:
        def __init__(self, size):
            self.remaining = size
            self.returned = 0
            self.requests = []

        def read(self, size=-1):
            self.requests.append(size)
            take = min(max(0, size), self.remaining)
            self.remaining -= take
            self.returned += take
            return b"x" * take

    synthetic_size = 1024 * 1024 * 1024
    reader = GeneratedOversizedRow(synthetic_size)
    assert list(traceview._iter_bounded_trace_jsonl_lines(
        reader, size=synthetic_size, max_line_bytes=ceiling,
        read_chunk_bytes=chunk)) == []
    assert reader.returned < ceiling + chunk
    assert max(reader.requests) == chunk

    import io
    exact = b"x" * (ceiling - 1) + b"\n"
    over = b"x" * ceiling + b"\n"
    assert list(traceview._iter_bounded_trace_jsonl_lines(
        io.BytesIO(exact), max_line_bytes=ceiling,
        read_chunk_bytes=chunk)) == [exact]
    assert list(traceview._iter_bounded_trace_jsonl_lines(
        io.BytesIO(over), max_line_bytes=ceiling,
        read_chunk_bytes=chunk)) == []

    before = json.dumps(_span_row("before")).encode("utf-8") + b"\n"
    after = json.dumps(_span_row("hidden")).encode("utf-8") + b"\n"
    file_ceiling = max(len(before), len(after)) + 16
    oversized = json.dumps(_span_row(
        "oversized", attributes={"output": "x" * (file_ceiling * 4)},
    )).encode("utf-8") + b"\n"
    assert len(oversized) > file_ceiling
    path = tmp_path / "spans.jsonl"
    path.write_bytes(before + oversized + after)
    monkeypatch.setattr(traceview, "TRACE_JSONL_ROW_MAX_BYTES", file_ceiling)
    assert [span["span_id"] for span in load_spans(path)] == ["before"]
    tail, total = load_span_tail(path, 8)
    assert total == 1 and [span["span_id"] for span in tail] == ["before"]


def test_bounded_static_tail_marks_an_out_of_window_input_ancestor_partial(tmp_path):
    """A tail can omit a delta ancestor, but hydration must say so and totals must remain exact."""
    path = tmp_path / "spans.jsonl"
    rows = [
        _span_row(
            "base", kind="generation", start=0,
            attributes={
                "input": [{"role": "system", "content": "retained system"}],
                "input_carry": 0,
                "input_from": None,
            }),
        _span_row("middle", trace_id="other", start=1),
        _span_row(
            "leaf", kind="generation", start=2,
            attributes={
                "input": [{"role": "user", "content": "new delta"}],
                "input_carry": 1,
                "input_from": "base",
            }),
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    tail, total = load_span_tail(path, 2)
    hydrated = hydrate_inputs(tail, _normalized=True)
    leaf = next(span for span in hydrated if span["span_id"] == "leaf")
    view = build_trace_view(
        RunState(run_id="demo", task_id="t", goal="g", direction="min"),
        hydrated,
        total_spans=total,
        span_cap=2,
    )

    assert leaf["attributes"]["input_partial"] is True
    assert leaf["attributes"]["input"] == [{"role": "user", "content": "new delta"}]
    assert view["projection"] == {
        "schema": 2,
        "light": False,
        "truncated": True,
        "total_spans": 3,
        "visible_spans": 2,
        "omitted_spans": 1,
        "truncated_spans": 0,
    }
    projected_leaf = next(
        span for span in view["nodes"]["0"] if span["span_id"] == "leaf")
    assert projected_leaf["attributes"]["input_partial"] is True


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


def _tool_detail_span(span_id: str, *, output: str) -> dict:
    return {
        "name": "tool",
        "kind": "tool",
        "trace_id": "shared-trace",
        "span_id": span_id,
        "parent_id": None,
        "run_id": "demo",
        "attributes": {"node_id": 0, "tool": "read_file", "input": {"path": "a.py"},
                       "output": output},
        "events": [],
        "status": "OK",
        "start": 1.0,
        "duration_s": 0.1,
    }


def _span_detail_client(tmp_path, spans):
    from fastapi.testclient import TestClient
    from looplab.events.eventstore import EventStore
    from looplab.serve.server import make_app

    rd = tmp_path / "demo"
    rd.mkdir()
    EventStore(rd / "events.jsonl").append(
        "run_started", {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"})
    (rd / "spans.jsonl").write_text(
        "".join(json.dumps(span) + "\n" for span in spans), encoding="utf-8")
    return TestClient(make_app(tmp_path))


def test_span_io_distinguishes_complete_detail_from_elided_siblings(tmp_path):
    pytest.importorskip("fastapi")
    spans = [
        _tool_detail_span("tool-a", output="complete a"),
        {**_tool_detail_span("tool-b", output="complete b"), "start": 2.0},
    ]
    body = _span_detail_client(tmp_path, spans).get("/api/runs/demo/spans/tool-a").json()

    assert body["projection"]["detail_truncated"] is False
    assert body["projection"]["siblings_elided"] is True
    assert body["projection"]["omitted_trace_spans"] == 1
    assert body["projection"]["truncated"] is True


def test_span_io_reports_selected_span_detail_truncation(tmp_path):
    pytest.importorskip("fastapi")
    body = _span_detail_client(
        tmp_path, [_tool_detail_span("tool-a", output="x" * 20_000)],
    ).get("/api/runs/demo/spans/tool-a").json()

    assert body["projection"]["detail_truncated"] is True
    assert body["projection"]["siblings_elided"] is False
    assert body["projection"]["omitted_trace_spans"] == 0
    assert body["projection"]["truncated"] is True


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
    # ...BOUNDED at the leaf by the retention window (review 2026-09-22, CORE-02): the writer no
    # longer windows before encoding, so the READER applies the same newest-64 window after the whole
    # chain is rebuilt. The chain itself is still walked end to end — the window keeps its tail.
    window = tracing._TRACE_MESSAGES_MAX
    for order in ([r] + gens, [r] + list(reversed(gens))):                       # file order AND reversed
        hyd = {h["span_id"]: h for h in hydrate_inputs(order)}
        assert hyd[f"g{n-1}"]["attributes"]["input"] == expected_last[-window:]
        assert hyd[f"g{window - 1}"]["attributes"]["input"] == expected_last[:window]
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


def test_input_partial_propagates_to_DESCENDANTS_not_only_from_a_chain_base(tmp_path):
    """A span's own `input_partial` must reach everything chained onto it.

    This is the durable-exporter fallback shape, NOT a torn file: g2's row and back-reference are
    both present and well-formed, and it declares that its own over-limit input was dropped
    (`tracing.generation` sets exactly this flag). Every span is in the set.

    The memo hit used to ASSIGN the ancestor's partial flag over the one accumulated while walking
    down, so in the common FILE-ORDER case — ancestors memoized before descendants — the flag never
    propagated: walking the flagged span hit its COMPLETE memoized parent, recorded partial=False
    for it, and every descendant inherited False. The flag survived only when the flagged span was
    itself a chain base, which is the one topology `test_missing_ancestor_marks_input_partial`
    above happens to cover.
    """
    rd = tmp_path / "demo"
    rd.mkdir()
    _write_toolloop(rd, n_turns=6)
    spans = load_spans(rd / "spans.jsonl")
    gens = sorted((s for s in spans if s.get("kind") == "generation"),
                  key=lambda s: s.get("start", 0.0))
    lossy = gens[2]["span_id"]
    marked = [{**s, "attributes": {**(s.get("attributes") or {}), "input_partial": True}}
              if s.get("span_id") == lossy else s
              for s in spans]

    hyd = {h["span_id"]: h for h in hydrate_inputs(marked)}
    for g in gens[:2]:
        assert not hyd[g["span_id"]]["attributes"].get("input_partial"), (
            "a span ABOVE the loss is still exact and must not be marked")
    assert hyd[lossy]["attributes"]["input_partial"] is True, "a span's own flag must survive"
    for g in gens[3:]:
        assert hyd[g["span_id"]]["attributes"].get("input_partial") is True, (
            "a descendant of a lossy span renders as COMPLETE — the operator is shown a truncated "
            "prefix with no indication it is truncated")

    # Order-independence: the defect was a function of which side of the memo the walk landed on,
    # so the same set presented ancestors-last must answer identically.
    rev = {h["span_id"]: h for h in hydrate_inputs(list(reversed(marked)))}
    assert rev[gens[-1]["span_id"]]["attributes"].get("input_partial") is True
    assert not rev[gens[0]["span_id"]]["attributes"].get("input_partial")


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


def test_a_shared_trace_splits_its_turns_by_each_span_s_own_node(tmp_path):
    """node_id is stamped PER SPAN, so one long-lived tool-loop trace can serve several nodes in
    sequence. Keying the whole trace off its ROOT span gave every turn to the node that OPENED the
    trace and left the other node an EMPTY conversation — reported as generic truncation, because
    the selection predicate (`_bounded_node_trace_tail`) had already counted those spans in. Attribute
    per span, matching `build_trace_view`'s effective-node rule."""
    def _gen(span_id, node_id, start, text):
        return {"name": "llm", "kind": "generation", "trace_id": "t0", "span_id": span_id,
                "parent_id": "r0", "run_id": "demo", "events": [], "status": "OK",
                "start": start, "duration_s": 1.0,
                "attributes": {"node_id": node_id, "output": text,
                               "input": [{"role": "user", "content": text}]}}

    spans = [
        {"name": "tool_loop", "kind": "operation", "trace_id": "t0", "span_id": "r0",
         "parent_id": None, "run_id": "demo", "attributes": {"node_id": 0}, "events": [],
         "status": "OK", "start": 0.0, "duration_s": 9.0},
        _gen("g0", 0, 1.0, "for node 0"),
        _gen("g1", 1, 2.0, "for node 1"),
    ]
    st = RunState(run_id="demo", task_id="t", goal="g", direction="min")

    def _outputs(node_id):
        convo = build_conversation(st, spans, node_id)
        return [turn.get("output") for stage in convo["stages"]
                for turn in stage["turns"] if turn["type"] == "generation"]

    assert _outputs(0) == ["for node 0"]        # the opener no longer absorbs its successor's turns
    assert _outputs(1) == ["for node 1"]        # ...and the successor is no longer blank


def test_legacy_root_only_logs_still_attribute_the_whole_trace(tmp_path):
    """The per-span rule must not regress a pre-stamping log: children carry no node_id of their own
    and fall back to their trace's root, so the whole trace stays with that node."""
    spans = [
        {"name": "create_node", "kind": "operation", "trace_id": "t0", "span_id": "r0",
         "parent_id": None, "run_id": "demo", "attributes": {"node_id": 3}, "events": [],
         "status": "OK", "start": 0.0, "duration_s": 5.0},
        {"name": "llm", "kind": "generation", "trace_id": "t0", "span_id": "g0", "parent_id": "r0",
         "run_id": "demo", "attributes": {"input": [dict(SYS), dict(USER)], "output": "o"},
         "events": [], "status": "OK", "start": 1.0, "duration_s": 1.0},
    ]
    st = RunState(run_id="demo", task_id="t", goal="g", direction="min")
    convo = build_conversation(st, spans, 3)
    assert [turn["output"] for stage in convo["stages"]
            for turn in stage["turns"] if turn["type"] == "generation"] == ["o"]
    assert build_conversation(st, spans, 4)["stages"] == []


def test_legacy_heuristic_survives_the_message_cap_plateau(tmp_path):
    """A LONG old full log must still show ONE request. `_project_messages` caps a generation's
    `input` at 10 messages, so every generation past the cap reports the same count — reading that
    plateau as a context reset re-emitted the request on EVERY turn, which is exactly the
    re-duplication the projection exists to remove. The capped span says so (`input_partial`)."""
    rd = tmp_path / "demo"
    rd.mkdir()
    full = [dict(SYS), dict(USER)]
    spans = [{"name": "create_node", "kind": "operation", "trace_id": "t0", "span_id": "r0",
              "parent_id": None, "run_id": "demo", "attributes": {"node_id": 0}, "events": [],
              "status": "OK", "start": 0.0, "duration_s": 1.0}]
    for k in range(8):                       # 4, 6, 8, 10, then four generations past the cap
        full = full + [{"role": "assistant", "content": f"a{k}"}, {"role": "user", "content": f"u{k}"}]
        spans.append({"name": "llm", "kind": "generation", "trace_id": "t0", "span_id": f"g{k}",
                      "parent_id": "r0", "run_id": "demo",
                      "attributes": {"node_id": 0, "input": [dict(m) for m in full], "output": "o"},
                      "events": [], "status": "OK", "start": float(k + 1), "duration_s": 1.0})
    (rd / "spans.jsonl").write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")
    loaded = load_spans(rd / "spans.jsonl")
    gens = sorted((s for s in loaded if s.get("kind") == "generation"), key=lambda s: s["start"])
    # the premise: the projection really does plateau, and marks the spans it capped
    assert [len(g["attributes"]["input"]) for g in gens] == [4, 6, 8, 10, 10, 10, 10, 10]
    assert [g["attributes"].get("input_partial") is True for g in gens] == [
        False, False, False, False, True, True, True, True]

    st = RunState(run_id="demo", task_id="t", goal="g", direction="min")
    convo = build_conversation(st, loaded, 0)
    reqs = [t for t in convo["stages"][0]["turns"] if t["type"] == "request"]
    assert len(reqs) == 1


def test_trace_tree_tolerates_a_deep_span_chain_without_recursionerror():
    # A crafted/corrupt spans.jsonl with a pathologically deep parent_id chain must not blow Python's
    # recursion limit when the tree is sorted — the "trace view never crashes on corrupt spans" contract
    # the projections harden for (the sibling hydrate_inputs is already iterative for the analogous case).
    from looplab.events.traceview import _tree
    spans = [{"span_id": f"s{i}", "parent_id": (f"s{i - 1}" if i else None), "start": float(i),
              "kind": "operation", "name": "op"} for i in range(6000)]
    roots = _tree(spans, _normalized=True)          # must not raise RecursionError
    assert len(roots) == 1 and roots[0]["span_id"] == "s0"


def test_trace_projection_json_encoder_has_an_explicit_aggregate_bound():
    from looplab.events.traceview import trace_projection_json_bytes

    assert json.loads(trace_projection_json_bytes({"ok": [True, "μ"]})) == {
        "ok": [True, "μ"]}
    with pytest.raises(ValueError, match="byte limit"):
        trace_projection_json_bytes({"too_large": "value"}, max_bytes=8)
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(ValueError, match="circular"):
        trace_projection_json_bytes(cyclic)


def test_already_normalized_spans_are_not_re_redacted_by_hydrate_inputs(monkeypatch, tmp_path):
    """`_normalize_span` runs redaction and entropy analysis over every text field. The finalize path
    is load_spans -> hydrate_inputs -> build_trace_view, and each of those normalized independently,
    so a large run paid the most expensive pass three times over."""
    import looplab.events.traceview as tv

    rows = [{"span_id": f"s{i}", "trace_id": "t", "parent_id": None, "kind": "generation",
             "name": "gen", "t0": 0.0, "t1": 1.0, "attrs": {"node_id": "0"},
             "input": [{"role": "user", "content": f"turn {i}"}]}
            for i in range(20)]
    path = tmp_path / "spans.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    spans = load_spans(path)                       # normalized ONCE, on the way off disk
    calls = []
    real = tv._normalize_span
    monkeypatch.setattr(tv, "_normalize_span", lambda v: (calls.append(1), real(v))[1])

    hydrated = tv.hydrate_inputs(spans, _normalized=True)
    assert calls == [], (
        f"hydrate_inputs re-normalized {len(calls)} already-normalized spans — load_spans and "
        "SpanIndex._read_full both run _normalize_span before handing them over")
    assert len(hydrated) == len(spans)
    assert [h["span_id"] for h in hydrated] == [s["span_id"] for s in spans]

    # …and the DEFAULT still normalizes, so an untrusted caller is never silently trusted.
    calls.clear()
    tv.hydrate_inputs([dict(r) for r in rows])
    assert len(calls) == len(rows)


def test_already_normalized_spans_are_not_re_redacted_by_conversation(monkeypatch):
    """The live conversation's indexed/full readers normalize once, before the expensive projector."""
    import looplab.events.traceview as tv

    spans = [
        {"span_id": "root", "trace_id": "trace", "parent_id": None,
         "kind": "operation", "name": "create_node", "start": 0.0,
         "attributes": {"node_id": 0}, "events": [], "status": "OK"},
        {"span_id": "gen", "trace_id": "trace", "parent_id": "root",
         "kind": "generation", "name": "generation", "start": 1.0,
         "attributes": {"node_id": 0, "phase": "implement", "phase_span": "root",
                        "output": "done"}, "events": [], "status": "OK"},
    ]
    calls = []
    real = tv._normalize_span
    monkeypatch.setattr(tv, "_normalize_span", lambda value: (calls.append(1), real(value))[1])
    state = RunState(run_id="demo", task_id="t", goal="g", direction="min")

    conversation = tv.build_conversation(state, spans, 0, _normalized=True)

    assert calls == []
    assert conversation["node_id"] == "0"
    assert conversation["stages"]

    tv.build_conversation(state, spans, 0)
    assert len(calls) >= len(spans)


# ------------------------------------------------------------------------------------------------
# THE DELTA SURVIVES A CONVERSATION LONGER THAN THE RETENTION WINDOW (review 2026-09-22, CORE-02).
#
# The writer compared the WINDOWED projection (`_trace_messages`: newest 64 messages / 64 000 chars)
# with the previous generation's. Past the window the window slid, the prefix compare failed, and
# every later turn stored a full ~64 KB base: `input_carry` 0 from turn 17 of a 30-turn tool loop,
# rows of 4 KB became 66 KB, and each message crossed the redactor once per turn it stayed in view.
# The extension is now decided on the RAW conversation, only appended messages are sanitized, and
# the window moved to the reader (`tracing.retained_input_window`, applied by `hydrate_inputs`).

from looplab.core import redact  # noqa: E402
from looplab.core.tracing import AsyncJsonlSpanExporter  # noqa: E402

_FIXTURE_V1 = Path(__file__).parent / "data" / "trace_delta_v1_spans.jsonl"

SHAPED = "sk-proj-A1b2C3d4E5f6G7h8I9j0K1l2"            # a known credential SHAPE
ENTROPY = "aZ9k2Lp7qW3xYt5Rb8Nc1Vd6Mf0Gh4J"             # masked only by the entropy pass
SHAPELESS = "hunter2hunter2ZZqq"                         # masked only by the env identity screen


def _long_tool_loop(tracer, n_turns: int, *, opening_extra: str = "", echo_at: int = -1,
                    reset_at: int = -1):
    """A `drive_tool_loop`-shaped conversation: ONE list grown in place, ~3.5 KB appended per turn,
    so the 64 000-char window is outgrown from turn 17. Returns the history as sent at each turn."""
    history = [{"role": "system", "content": "SYS " + "rules " * 700 + opening_extra},
               {"role": "user", "content": "TASK " + "spec " * 1_200}]
    sent = []
    with tracer.span("create_node", new_trace=True, node_id=0):
        for phase, turns in (("implement", range(n_turns if reset_at < 0 else reset_at)),
                             ("repair", range(reset_at, n_turns) if reset_at >= 0 else ())):
            if phase == "repair":                 # a context reset: the SAME opening, a new task
                history = [history[0], {"role": "user", "content": "REPAIR " + "fix " * 300}]
            with tracing.operation(phase):
                for k in turns:
                    sent.append([dict(m) for m in history])
                    with tracing.generation(op="chat", model="m", messages=history) as gen:
                        gen.output(f"{phase} turn {k} " + "answer " * 20)
                    tool = f"result {k}: " + "line of output " * 200
                    if k == echo_at:
                        tool += " " + opening_extra
                    history.append({"role": "assistant", "content": f"turn {k}: " + "plan " * 100})
                    history.append({"role": "tool", "content": tool})
    return sent


def _raw_generations(path):
    return [row for row in iter_jsonl(path) if row.get("kind") == "generation"]


def test_the_delta_survives_a_conversation_longer_than_the_window(tmp_path):
    path = tmp_path / "spans.jsonl"
    sent = _long_tool_loop(Tracer(JsonlSpanExporter(path), run_id="r", capture_llm_io=True), 40)
    assert sum(len(m["content"]) for m in sent[-1]) > 2 * tracing._TRACE_TEXT_CAP   # premise
    gens = _raw_generations(path)
    assert gens[0]["attributes"]["input_carry"] == 0
    for prev, gen in zip(gens, gens[1:]):
        attrs = gen["attributes"]
        assert attrs["input_from"] == prev["span_id"] and attrs["input_carry"] > 0, (
            "the chain broke once the conversation outgrew the window: a full base was stored")
        assert len(attrs["input"]) == 2                     # exactly the two appended messages
    # BOUNDED per generation: no row carries more than its own turn, however long the history.
    input_bytes = [len(json.dumps(g["attributes"]["input"])) for g in gens]
    turn_bytes = max(len(json.dumps(sent[k + 1][-2:])) for k in range(len(sent) - 1))
    assert max(input_bytes[1:]) <= turn_bytes + 64
    # ...so the file grows LINEARLY in the conversation, where it grew by ~64 KB per turn before.
    assert sum(input_bytes) < 1.2 * len(json.dumps(sent[-1]))


def test_the_reader_window_is_the_window_the_writer_used_to_store(tmp_path):
    """Chain + window, isolated from the browser projection: over the RAW rows, turn k's hydrated
    input is exactly `_trace_messages(what turn k sent)` — the retention window the old writer
    stored — for every turn, before and after the window is outgrown and across a context reset."""
    path = tmp_path / "spans.jsonl"
    sent = _long_tool_loop(Tracer(JsonlSpanExporter(path), run_id="r", capture_llm_io=True), 40,
                           reset_at=30)
    rows = _raw_generations(path)
    hydrated = hydrate_inputs(rows, _normalized=True)        # raw on purpose: see the docstring
    assert len(hydrated) == len(sent) == 40
    for k, (row, messages) in enumerate(zip(hydrated, sent)):
        assert row["attributes"]["input"] == tracing._trace_messages(messages), f"turn {k}"
        assert not row["attributes"].get("input_partial")
    assert [r["attributes"]["input_carry"] == 0 for r in rows].count(True) == 2   # two sub-loops


def test_a_base_beyond_the_window_chains_on_what_it_stored(tmp_path):
    """`input_carry` counts the messages the parent's RECONSTRUCTION holds, not the raw count it was
    sent: a conversation that opens past the window stores a windowed base, and its deltas carry
    exactly that base."""
    path = tmp_path / "spans.jsonl"
    tracer = Tracer(JsonlSpanExporter(path), run_id="r", capture_llm_io=True)
    history = [{"role": "user", "content": f"resumed {i} " + "context " * 150} for i in range(100)]
    sent = []
    with tracer.span("resume", new_trace=True, node_id=0):
        for k in range(4):
            sent.append([dict(m) for m in history])
            with tracing.generation(op="chat", model="m", messages=history):
                pass
            history.append({"role": "assistant", "content": f"turn {k}"})
            history.append({"role": "tool", "content": f"tool {k}"})
    rows = _raw_generations(path)
    stored_base = len(rows[0]["attributes"]["input"])
    assert stored_base < 100                                   # premise: the base was windowed
    assert [r["attributes"]["input_carry"] for r in rows] == [
        0, stored_base, stored_base + 2, stored_base + 4]
    hydrated = hydrate_inputs(rows, _normalized=True)
    for k, (row, messages) in enumerate(zip(hydrated, sent)):
        assert row["attributes"]["input"] == tracing._trace_messages(messages), f"turn {k}"


def test_a_history_rewritten_in_place_is_a_new_base_not_a_delta(tmp_path):
    """`drive_tool_loop` compacts ONE list in place (`messages[:] = ...`) and a message's dict may
    be the same object with new content. The extension check compares immutable keys taken when
    the previous generation was recorded, so a rewrite is a reset — never a delta that carries the
    pre-rewrite text forward as if it had been sent again."""
    path = tmp_path / "spans.jsonl"
    tracer = Tracer(JsonlSpanExporter(path), run_id="r", capture_llm_io=True)
    history = [{"role": "system", "content": "SYS"},
               {"role": "tool", "content": "LONG ORIGINAL TOOL OUTPUT " * 20}]
    sent = []
    with tracer.span("loop", new_trace=True, node_id=0):
        for k in range(3):
            if k == 2:
                history[1]["content"] = "[compacted: 1 tool result summarized]"   # SAME dict
            sent.append([dict(m) for m in history])
            with tracing.generation(op="chat", model="m", messages=history):
                pass
            history.append({"role": "assistant", "content": f"turn {k}"})
    rows = _raw_generations(path)
    assert [r["attributes"]["input_carry"] for r in rows][0:2] == [0, 2]
    assert rows[2]["attributes"]["input_carry"] == 0, "a rewritten history was chained as a delta"
    hydrated = hydrate_inputs(rows, _normalized=True)
    assert hydrated[2]["attributes"]["input"] == tracing._trace_messages(sent[2])
    assert "LONG ORIGINAL" not in json.dumps(hydrated[2]["attributes"]["input"])


def test_each_message_crosses_the_redactor_once_over_the_whole_loop(tmp_path, monkeypatch):
    """Not once per turn it stays in view: a message is sanitized when it is APPENDED."""
    seen: dict[int, int] = {}
    real = redact._redact_persisted

    def counting(value, **kw):
        if isinstance(value, str) and value.startswith(("turn ", "result ")):
            turn = int(value.split(" ", 2)[1].rstrip(":"))
            seen[turn] = seen.get(turn, 0) + 1
        return real(value, **kw)

    monkeypatch.setattr(redact, "_redact_persisted", counting)
    _long_tool_loop(Tracer(JsonlSpanExporter(tmp_path / "spans.jsonl"), run_id="r",
                           capture_llm_io=True), 30)
    # turn k's assistant + tool message are appended once (seen by generation k+1); the last
    # turn's pair is never sent. Two redactions per turn, whatever the history length.
    assert seen == {k: 2 for k in range(29)}, (
        "a message already persisted was sanitized again on a later turn")


def test_a_long_loop_shows_one_request_per_sub_loop(tmp_path):
    """The conversation view keys its request boundary on `input_carry == 0`; every slid base the
    old writer stored past the window re-emitted the request on each turn."""
    path = tmp_path / "spans.jsonl"
    _long_tool_loop(Tracer(JsonlSpanExporter(path), run_id="r", capture_llm_io=True), 40,
                    reset_at=30)
    st = RunState(run_id="demo", task_id="t", goal="g", direction="min")
    convo = build_conversation(st, load_spans(path), 0)
    requests = [(stage["label"], t) for stage in convo["stages"] for t in stage["turns"]
                if t["type"] == "request"]
    assert [label for label, _ in requests] == ["implement", "repair"]


@pytest.mark.parametrize("entropy_pass", [True, False], ids=["entropy-on", "entropy-off"])
def test_a_secret_in_message_one_never_reaches_any_persisted_trace_byte(
        tmp_path, monkeypatch, entropy_pass):
    """Three secret classes in message 1 of a 40-turn loop (echoed again by a turn-20 tool result,
    re-based by a context reset at turn 30), through the PRODUCTION async exporter; then every file
    the exporter left is read back whole. Stored once is not stored unmasked: the one pass a message
    now gets is the full screen. `entropy-off` disables that one pass at the redactor itself and
    must leave the shape and the env value masked — and its entropy token visible, or the knob is
    not live and the parametrization proves nothing."""
    monkeypatch.setenv("C2FIXTURE_DB_PASSWORD", SHAPELESS)
    if not entropy_pass:
        real = redact.redact_secrets
        monkeypatch.setattr(redact, "redact_secrets",
                            lambda text, **kw: real(text, **{**kw, "entropy": False}))
    secrets = f"key {SHAPED} blob {ENTROPY} pw {SHAPELESS}"
    exporter = AsyncJsonlSpanExporter(tmp_path / "spans.jsonl")
    tracer = Tracer(exporter, run_id="r", capture_llm_io=True)
    _long_tool_loop(tracer, 40, opening_extra=secrets, echo_at=20, reset_at=30)
    assert tracer.shutdown()
    persisted = b"".join(p.read_bytes() for p in sorted(tmp_path.rglob("*")) if p.is_file())
    assert len(_raw_generations(tmp_path / "spans.jsonl")) == 40
    assert SHAPED.encode() not in persisted and b"sk-***" in persisted
    assert SHAPELESS.encode() not in persisted and b"REDACTED_ENV" in persisted
    assert (ENTROPY.encode() in persisted) is (not entropy_pass)
    # ...and the READ side hands a browser none of them either (its own screen runs on top).
    shown = json.dumps(hydrate_inputs(load_spans(tmp_path / "spans.jsonl"), _normalized=True))
    assert SHAPED not in shown and SHAPELESS not in shown


def _v1_sent():
    """The conversation `tests/data/trace_delta_v1_spans.jsonl` was written from — by the
    PRE-CORE-02 writer (2026-09-23): an implement sub-loop long enough that the 64-message window
    slid (its turns 32 and 33 are stored as full bases), then a repair sub-loop."""
    out = []
    for phase, turns, task in (("implement", 34, "TASK fix the failing test in src/app.py"),
                               ("repair", 3, "REPAIR the import error in src/app.py")):
        history = [{"role": "system", "content": "SYS you are the developer; answer briefly."},
                   {"role": "user", "content": task}]
        for k in range(turns):
            out.append([dict(m) for m in history])
            history = history + [
                {"role": "assistant", "content": f"{phase} {k}: read src/mod_{k}.py"},
                {"role": "tool", "content": f"src/mod_{k}.py: def f_{k}(): return {k}"}]
    return out


def _plain_chain(rows) -> dict:
    """The chain rebuilt from the ROWS ALONE — `parent[:input_carry] + input`, no window, no code
    under test — so a reader change cannot move the reference along with the result."""
    by_id = {row["span_id"]: row for row in rows}
    memo: dict = {}

    def full(sid):
        if sid not in memo:
            attrs = by_id[sid]["attributes"]
            parent = attrs.get("input_from")
            memo[sid] = (full(parent)[:attrs["input_carry"]] if parent else []) + attrs["input"]
        return memo[sid]

    return {row["span_id"]: full(row["span_id"]) for row in rows}


def test_an_old_format_trace_still_reads(tmp_path):
    """Real bytes from the previous writer, windowed at WRITE time with slid bases. The reader's
    window must be the identity on them: every turn still hydrates to exactly the retained input
    that writer stored, and the projections that read it still build."""
    sent = _v1_sent()
    rows = _raw_generations(_FIXTURE_V1)
    assert [r["attributes"]["input_carry"] for r in rows].count(0) == 4   # premise: 2 slid bases
    hydrated = hydrate_inputs(rows, _normalized=True)
    assert len(hydrated) == len(sent)
    stored = _plain_chain(rows)
    assert max(len(v) for v in stored.values()) == tracing._TRACE_MESSAGES_MAX   # a FULL window
    for k, (row, messages) in enumerate(zip(hydrated, sent)):
        # The identity on what the old writer stored (independent of today's window code)...
        assert row["attributes"]["input"] == stored[row["span_id"]], f"turn {k}"
        # ...which is, for this conversation, the retention window of what the turn sent.
        assert row["attributes"]["input"] == tracing._trace_messages(messages), f"turn {k}"
    spans = load_spans(_FIXTURE_V1)
    st = RunState(run_id="fixture-v1", task_id="t", goal="g", direction="min")
    convo = build_conversation(st, spans, 0)
    labels = [s["label"] for s in convo["stages"]]
    assert labels == ["implement", "repair"]
    view = build_trace_view(st, hydrate_inputs(spans, _normalized=True))
    assert view["nodes"]["0"]


def test_the_span_detail_route_reads_a_long_new_format_chain(tmp_path):
    """The one live route that hydrates ONE observation, over its bounded trace window."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from looplab.events.eventstore import EventStore
    from looplab.serve.server import make_app

    rd = tmp_path / "demo"
    rd.mkdir()
    EventStore(rd / "events.jsonl").append(
        "run_started", {"run_id": "demo", "task_id": "t", "goal": "g", "direction": "min"})
    sent = _long_tool_loop(Tracer(JsonlSpanExporter(rd / "spans.jsonl"), run_id="demo",
                                  capture_llm_io=True), 40)
    last = _raw_generations(rd / "spans.jsonl")[-1]
    assert last["attributes"]["input_carry"] > 0                # premise: a delta, 39 levels deep
    body = TestClient(make_app(tmp_path)).get(f"/api/runs/demo/spans/{last['span_id']}").json()
    shown = body["attributes"]["input"]
    # The route hydrates PROJECTED spans (each message already capped at 2 000 chars), so the
    # read-time window's 64 000-char budget spans more of them than the writer's window over full
    # messages did — exact equality is pinned over raw rows above. What must hold here: the chain
    # was walked (the head is a message from deep in the loop, not a lone delta) and then WINDOWED
    # (the head is not the system prompt the 40-level reconstruction starts from).
    assert 0 < len(shown) <= 10
    assert shown[0]["content"].startswith(("result ", "turn ")), shown[0]["content"][:40]
    head_turn = int(shown[0]["content"].split(" ", 2)[1].rstrip(":"))
    assert head_turn < len(sent) - 2, "only the last delta was shown: the chain was not walked"
    assert not any(m["content"].startswith(("SYS ", "TASK ")) for m in shown), (
        "the reconstruction reached the reader unwindowed")
    assert body["attributes"]["input_partial"] is True          # head/tail of a longer input
    assert "input_carry" not in body["attributes"]
