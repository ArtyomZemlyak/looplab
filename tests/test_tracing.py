"""Full tracing (ADR-08): nested correlated spans into spans.jsonl (files-as-truth), bridged
to OpenTelemetry when present, joined to events for the UI. fold never reads spans."""
from __future__ import annotations

import sys

import anyio
import orjson
import pytest

from looplab.eventstore import EventStore
from looplab.orchestrator import Engine
from looplab.policy import GreedyTree
from looplab.repo_task import EvalSpec, RepoTask
from looplab.sandbox import SubprocessSandbox
from looplab.tracing import JsonlSpanExporter, Tracer, current_ids
from looplab.traceview import build_trace_view, load_spans

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
    from looplab import tracing
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
    from looplab import tracing
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


def test_fold_ignores_spans(tmp_path):
    # Determinism: the read model / fold depends only on events, never on spans.
    from looplab.replay import fold
    anyio.run(_engine(tmp_path).run)
    run_dir = tmp_path / "run"
    s1 = fold(list(EventStore(run_dir / "events.jsonl").read_all()))
    (run_dir / "spans.jsonl").write_text("garbage not json\n", encoding="utf-8")
    s2 = fold(list(EventStore(run_dir / "events.jsonl").read_all()))
    assert s1.best_node_id == s2.best_node_id and len(s1.nodes) == len(s2.nodes)
