"""Full tracing (ADR-08): nested correlated spans into spans.jsonl (files-as-truth), bridged
to OpenTelemetry when present, joined to events for the UI. fold never reads spans."""
from __future__ import annotations

import sys

import anyio
import orjson
import pytest

from looplab.events.eventstore import EventStore
from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree
from looplab.adapters.repo_task import EvalSpec, RepoTask
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.core.tracing import JsonlSpanExporter, Tracer, current_ids
from looplab.serve.traceview import build_trace_view, load_spans

_M = {"kind": "stdout_json", "key": "metric"}


# ------------------------------ unit: the tracer ------------------------------
def test_nesting_and_trace_ids(tmp_path):
    t = Tracer(JsonlSpanExporter(tmp_path / "s.jsonl"), run_id="r1")
    assert current_ids() == (None, None)              # nothing active outside a span
    with t.span("parent", new_trace=True, node_id=7) as p:
        ptid, psid = current_ids()
        assert ptid and psid
        p.set("k", "v")
        with t.span("child"):
            ctid, csid = current_ids()
            assert ctid == ptid                       # same trace
            assert csid != psid                       # distinct span
    recs = [orjson.loads(l) for l in (tmp_path / "s.jsonl").read_bytes().splitlines()]
    by_name = {r["name"]: r for r in recs}
    assert by_name["child"]["parent_id"] == by_name["parent"]["span_id"]
    assert by_name["child"]["trace_id"] == by_name["parent"]["trace_id"]
    assert by_name["parent"]["attributes"] == {"node_id": 7, "k": "v"}
    assert all("duration_s" in r for r in recs)


def test_generation_and_tool_are_first_class_observations(tmp_path):
    """Langfuse-style: an LLM call is a `generation` child span (input/output/model/usage), a tool
    invocation is a `tool` child span (input/output) — both nested under the operation span."""
    from looplab.core import tracing
    tracing.set_llm_capture(True)
    t = Tracer(JsonlSpanExporter(tmp_path / "s.jsonl"), run_id="r")
    with t.span("propose", new_trace=True, node_id=1):
        with tracing.generation(op="chat", model="m", messages=[{"role": "user", "content": "hi"}],
                                model_parameters={"temperature": 0.5}) as g:
            g.output("hey").usage({"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}).cost(0.0)
        with tracing.tool("kb_search", {"q": "x"}) as to:
            to.output("(3 hits)")
    recs = [orjson.loads(l) for l in (tmp_path / "s.jsonl").read_bytes().splitlines()]
    by = {r["name"]: r for r in recs}
    assert by["generation"]["kind"] == "generation" and by["tool"]["kind"] == "tool"
    assert by["generation"]["parent_id"] == by["propose"]["span_id"]          # nested under the op
    assert by["tool"]["parent_id"] == by["propose"]["span_id"]
    ga = by["generation"]["attributes"]
    assert ga["op"] == "chat" and ga["model"] == "m" and ga["model_parameters"]["temperature"] == 0.5
    assert ga["input"][0]["content"] == "hi" and ga["output"] == "hey" and ga["usage"]["total"] == 7
    ta = by["tool"]["attributes"]
    assert ta["tool"] == "kb_search" and ta["input"] == {"q": "x"} and ta["output"] == "(3 hits)"


def test_generation_noop_without_active_tracer():
    """A generation/tool opened with no traced operation active is a harmless no-op (no crash)."""
    from looplab.core import tracing
    with tracing.generation(op="x", model="m", messages=[{"role": "user", "content": "z"}]) as g:
        g.output("y").usage({"prompt_tokens": 1}).cost(0.0)          # must not raise
    with tracing.tool("t", {"a": 1}) as to:
        to.output("r")


def test_new_trace_starts_fresh_trace(tmp_path):
    t = Tracer(JsonlSpanExporter(tmp_path / "s.jsonl"), run_id="r")
    with t.span("a", new_trace=True):
        pass
    with t.span("b", new_trace=True):
        pass
    recs = [orjson.loads(l) for l in (tmp_path / "s.jsonl").read_bytes().splitlines()]
    assert recs[0]["trace_id"] != recs[1]["trace_id"] and recs[0]["parent_id"] is None


def test_exception_marks_span_error_and_reraises(tmp_path):
    t = Tracer(JsonlSpanExporter(tmp_path / "s.jsonl"))
    with pytest.raises(ValueError):
        with t.span("boom", new_trace=True):
            raise ValueError("x")
    rec = orjson.loads((tmp_path / "s.jsonl").read_bytes().splitlines()[0])
    assert rec["status"] == "ERROR"
    assert any(e.get("name") == "exception" for e in rec["events"])


# --------------------------- correlation: events <-> spans --------------------
def test_event_store_stamps_active_span(tmp_path):
    t = Tracer(JsonlSpanExporter(tmp_path / "s.jsonl"))
    store = EventStore(tmp_path / "events.jsonl")
    with t.span("op", new_trace=True):
        tid, sid = current_ids()
        e = store.append("thing", {"a": 1})
    assert e.trace_id == tid and e.span_id == sid
    e2 = store.append("outside", {})                  # no active span -> no ids
    assert e2.trace_id is None and e2.span_id is None


# ------------------------------ engine end-to-end -----------------------------
def _engine(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "run.py").write_text('import json; print(json.dumps({"metric": 1.0}))\n',
                                 encoding="utf-8")
    t = RepoTask(id="tr", direction="max", editable_path=str(repo), edit_surface=["*.txt"],
                 eval=EvalSpec(command=[sys.executable, "run.py"], metric=_M))
    r, d = t.build_roles()
    return Engine(tmp_path / "run", task=t, researcher=r, developer=d,
                  sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=2))


def test_engine_emits_full_span_tree_and_correlated_events(tmp_path):
    anyio.run(_engine(tmp_path).run)
    run_dir = tmp_path / "run"
    spans = load_spans(run_dir / "spans.jsonl")
    names = {s["name"] for s in spans}
    # full coverage: node creation + its roles, and the eval with its phases
    assert {"create_node", "propose", "implement", "evaluate", "command", "read_metric"} <= names
    # eval phases nest under the evaluate trace
    ev = next(s for s in spans if s["name"] == "evaluate")
    cmd = next(s for s in spans if s["name"] == "command")
    assert cmd["trace_id"] == ev["trace_id"]
    # events are correlated to spans
    events = list(EventStore(run_dir / "events.jsonl").read_all())
    assert any(e.type == "node_evaluated" and e.trace_id for e in events)
    assert all(e.v == 1 for e in events)              # envelope versioned


def test_trace_json_written_and_grouped_by_node(tmp_path):
    state = anyio.run(_engine(tmp_path).run)
    run_dir = tmp_path / "run"
    tv = orjson.loads((run_dir / "trace.json").read_bytes())
    assert tv["run_id"] == state.run_id
    assert "0" in tv["nodes"]                          # node 0's traces grouped under it
    # rebuilding from files gives the same shape (pure reader)
    tv2 = build_trace_view(state, load_spans(run_dir / "spans.jsonl"))
    assert tv2["summary"]["spans"] == tv["summary"]["spans"]


def test_conversation_dedups_resent_history():
    """The linear conversation projection shows the system+user request ONCE per sub-loop, and each
    generation contributes only its delta (thinking + output + tool_calls) — never the raw tree's
    per-turn re-send of the whole growing message history. A context RESET (shorter input, e.g. a
    compaction or a sub-agent handoff) starts a fresh request."""
    from types import SimpleNamespace
    from looplab.serve.traceview import build_conversation

    def span(sid, kind, start, parent, **attrs):
        return {"span_id": sid, "parent_id": parent, "trace_id": "t1", "kind": kind,
                "name": attrs.pop("name", kind), "start": start, "duration_s": 0.1,
                "status": "OK", "attributes": attrs}

    sysm, usrm = {"role": "system", "content": "SYS"}, {"role": "user", "content": "USR"}
    spans = [
        {"span_id": "root", "parent_id": None, "trace_id": "t1", "kind": "operation",
         "name": "create_node", "start": 0.0, "duration_s": 5, "status": "OK", "attributes": {"node_id": 3}},
        {"span_id": "op1", "parent_id": "root", "trace_id": "t1", "kind": "operation",
         "name": "propose", "start": 0.1, "duration_s": 4, "status": "OK", "attributes": {}},
        span("g1", "generation", 0.2, "op1", input=[sysm, usrm], thinking="think-1",
             output="out-1", model="m", tool_calls=[{"name": "grep"}], usage={"total": 10}),
        span("x1", "tool", 0.3, "op1", tool="grep", input={"q": "a"}, output="hit"),
        span("g2", "generation", 0.4, "op1", thinking="think-2", output="out-2", model="m", usage={"total": 12},
             input=[sysm, usrm, {"role": "assistant", "content": "out-1"}, {"role": "tool", "content": "hit"}]),
        span("g3", "generation", 0.5, "op1", input=[sysm, usrm], thinking="think-3", output="done", model="m"),
    ]
    conv = build_conversation(SimpleNamespace(run_id="r", task_id="tk"), spans, 3)
    assert len(conv["stages"]) == 1
    turns = conv["stages"][0]["turns"]
    # first + the reset => exactly TWO requests, not one per generation (no duplication)
    assert [t["type"] for t in turns] == ["request", "generation", "tool", "generation", "request", "generation"]
    reqs = [t for t in turns if t["type"] == "request"]
    assert all([m["role"] for m in r["messages"]] == ["system", "user"] for r in reqs)
    assert reqs[0]["label"] == "propose"                    # labelled by the nearest ancestor operation
    gens = [t for t in turns if t["type"] == "generation"]
    assert [g["think"] for g in gens] == ["think-1", "think-2", "think-3"]   # each turn's own delta
    assert gens[0]["tool_calls"] == ["grep"]
    # node filter: a different node id yields no stages
    assert build_conversation(SimpleNamespace(run_id="r", task_id="tk"), spans, 99)["stages"] == []


def test_fold_ignores_spans(tmp_path):
    # Determinism: the read model / fold depends only on events, never on spans.
    from looplab.events.replay import fold
    anyio.run(_engine(tmp_path).run)
    run_dir = tmp_path / "run"
    s1 = fold(list(EventStore(run_dir / "events.jsonl").read_all()))
    (run_dir / "spans.jsonl").write_text("garbage not json\n", encoding="utf-8")
    s2 = fold(list(EventStore(run_dir / "events.jsonl").read_all()))
    assert s1.best_node_id == s2.best_node_id and len(s1.nodes) == len(s2.nodes)


# #5 — an exception inside a span marks it ERROR (and re-raises)
def test_span_error_status(tmp_path):
    tr = Tracer(JsonlSpanExporter(tmp_path / "s.jsonl"), run_id="r")
    try:
        with tr.span("boom", new_trace=True):
            raise ValueError("x")
    except ValueError:
        pass
    rec = orjson.loads((tmp_path / "s.jsonl").read_bytes().splitlines()[0])
    assert rec["status"] == "ERROR"


# #9 — confirm events are correlated to their span (carry a trace_id)
def test_confirm_events_carry_trace_id(tmp_path):
    from looplab.events.eventstore import EventStore
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "run.py").write_text('import json; print(json.dumps({"metric": 1.0}))\n',
                                 encoding="utf-8")
    t = RepoTask(id="ce", direction="max", editable_path=str(repo), edit_surface=["*.txt"],
                 eval=EvalSpec(command=[sys.executable, "run.py"], metric=_M))
    r, d = t.build_roles()
    eng = Engine(tmp_path / "run", task=t, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=3),
                 confirm_top_k=1, confirm_seeds=1)
    anyio.run(eng.run)
    events = list(EventStore(tmp_path / "run" / "events.jsonl").read_all())
    confirm_evs = [e for e in events if e.type == "confirm_eval"]
    assert confirm_evs and all(e.trace_id for e in confirm_evs)
