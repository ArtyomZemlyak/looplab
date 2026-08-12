"""Full tracing (ADR-08): nested correlated spans into spans.jsonl (files-as-truth), bridged
to OpenTelemetry when present, joined to events for the UI. fold never reads spans."""
from __future__ import annotations

import json
import os
import sys
import threading
import time

import anyio
import orjson
import pytest

from looplab.events.eventstore import EventStore
from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree
from looplab.adapters.repo_task import EvalSpec, RepoTask
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.core.tracing import JsonlSpanExporter, Tracer, current_ids
from looplab.agents.tool_loop import _trace_preview
from looplab.events.traceview import build_trace_view, load_spans

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
    recs = [orjson.loads(line) for line in (tmp_path / "s.jsonl").read_bytes().splitlines()]
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
    recs = [orjson.loads(line) for line in (tmp_path / "s.jsonl").read_bytes().splitlines()]
    by = {r["name"]: r for r in recs}
    assert by["generation"]["kind"] == "generation" and by["tool"]["kind"] == "tool"
    assert by["generation"]["parent_id"] == by["propose"]["span_id"]          # nested under the op
    assert by["tool"]["parent_id"] == by["propose"]["span_id"]
    ga = by["generation"]["attributes"]
    assert ga["op"] == "chat" and ga["model"] == "m" and ga["model_parameters"]["temperature"] == 0.5
    assert ga["input"][0]["content"] == "hi" and ga["output"] == "hey" and ga["usage"]["total"] == 7
    ta = by["tool"]["attributes"]
    assert ta["tool"] == "kb_search" and ta["input"] == {"q": "x"} and ta["output"] == "(3 hits)"


def test_live_children_inherit_trace_generation_before_the_root_closes(tmp_path):
    """The attempt fence must see current children while their root is still only in memory."""
    from types import SimpleNamespace
    from looplab.core import tracing
    from looplab.events.span_index import get_index
    from looplab.events.traceview import build_conversation

    path = tmp_path / "spans.jsonl"
    tracer = Tracer(JsonlSpanExporter(path), run_id="r", capture_llm_io=True)
    state = SimpleNamespace(run_id="r", task_id="t")
    with tracer.span("create_node", new_trace=True, node_id=7, generation=1):
        with tracing.generation(
                op="chat", model="m", messages=[{"role": "user", "content": "go"}]) as obs:
            obs.output("done")

        # create_node has not closed/exported.  The generation is the only durable row, so it is
        # temporarily the structural root seen by the index.  Its inherited stamp must fence it to 1.
        idx = get_index(path)
        visible = idx.full_spans_for_node(7, generation=1)
        assert len(visible) == 1
        assert visible[0]["kind"] == "generation"
        assert visible[0]["attributes"]["generation"] == 1
        assert idx.full_spans_for_node(7, generation=0) == []
        conversation = build_conversation(
            state, visible, 7, generation=1, _normalized=True)
        assert conversation["stages"] and conversation["stages"][0]["turns"]

        # A conflicting descendant label cannot change a same-trace lifecycle.  A nested trace may
        # explicitly establish generation 2, and closing it must restore generation 1 outside.
        with tracer.span("same-trace-conflict", generation=99):
            pass
        with tracer.span("nested-root", new_trace=True, generation=2):
            with tracer.span("nested-child"):
                pass
        with tracer.span("after-nested"):
            pass

        live = get_index(path)
        generation_one = live.light_spans_for_node(7, generation=1)
        generation_two = live.light_spans_for_node(7, generation=2)
        assert {row["name"] for row in generation_one} == {
            "generation", "same-trace-conflict", "after-nested"}
        assert {row["name"] for row in generation_two} == {"nested-root", "nested-child"}
        assert all(row["attributes"]["generation"] == 1 for row in generation_one)
        assert all(row["attributes"]["generation"] == 2 for row in generation_two)


def test_live_children_inherit_generation_set_after_root_preflight(tmp_path):
    """Match evaluate's production shape: root opens first, then learns/stamps its attempt."""
    from looplab.core import tracing
    from looplab.events.span_index import get_index

    path = tmp_path / "spans.jsonl"
    tracer = Tracer(JsonlSpanExporter(path), run_id="r", capture_llm_io=True)
    with tracer.span("evaluate", new_trace=True, node_id=7) as root:
        root.set("generation", 1)
        with tracing.generation(
                op="chat", model="m", messages=[{"role": "user", "content": "score"}]) as obs:
            obs.output("done")

        index = get_index(path)
        current = index.full_spans_for_node(7, generation=1)
        assert len(current) == 1 and current[0]["kind"] == "generation"
        assert current[0]["attributes"]["generation"] == 1
        assert index.full_spans_for_node(7, generation=0) == []


def test_generation_noop_without_active_tracer():
    """A generation/tool opened with no traced operation active is a harmless no-op (no crash)."""
    from looplab.core import tracing
    with tracing.generation(op="x", model="m", messages=[{"role": "user", "content": "z"}]) as g:
        g.output("y").usage({"prompt_tokens": 1}).cost(0.0)          # must not raise
    with tracing.tool("t", {"a": 1}) as to:
        to.output("r")


def test_generation_tool_calls_follow_capture_and_redaction_boundary(tmp_path):
    from looplab.core import tracing

    saved = tracing._CAPTURE_LLM_IO
    secret = "abc"
    try:
        t = Tracer(JsonlSpanExporter(tmp_path / "s.jsonl"), run_id="r")
        tracing.set_llm_capture(False)
        with t.span("off-root", new_trace=True):
            with tracing.generation(op="capture-off", model="m") as g:
                g.tool_calls([{"name": "fetch", "arguments": "api_key=" + secret}])

        tracing.set_llm_capture(True)
        with t.span("on-root", new_trace=True):
            with tracing.generation(op="capture-on", model="m") as g:
                g.tool_calls([
                    {"name": "fetch", "arguments": "api_key=" + secret + "\x1b[2J" + "x" * 40_000}
                    for _ in range(40)
                ])

        recs = [orjson.loads(line) for line in (tmp_path / "s.jsonl").read_bytes().splitlines()]
        generations = {rec["attributes"]["op"]: rec["attributes"]
                       for rec in recs if rec["name"] == "generation"}
        assert "tool_calls" not in generations["capture-off"]
        captured = generations["capture-on"]["tool_calls"]
        persisted = orjson.dumps(captured).decode()
        assert captured[0]["name"] == "fetch"
        assert secret not in persisted and "\x1b" not in persisted and "***" in persisted
        assert len(captured) <= 32 and len(orjson.dumps(captured)) <= 65_000
    finally:
        tracing.set_llm_capture(saved)


def test_tool_trace_structured_input_masks_secret_keys_and_bounds_shape(tmp_path):
    from looplab.core import tracing

    saved = tracing._CAPTURE_LLM_IO
    tracing.set_llm_capture(True)
    try:
        path = tmp_path / "s.jsonl"
        tracer = Tracer(JsonlSpanExporter(path), run_id="r")
        with tracer.span("root", new_trace=True):
            with tracing.tool("fetch\nunsafe", {
                "password": "abc", "nested": {"api_key": "xyz"}, "safe": "ok",
                "items": list(range(100)),
            }):
                pass
        records = [orjson.loads(line) for line in path.read_bytes().splitlines()]
        attrs = next(record["attributes"] for record in records if record["kind"] == "tool")
        assert attrs["tool"] == "fetch unsafe"
        assert attrs["input"]["password"] == "***"
        assert attrs["input"]["nested"]["api_key"] == "***"
        assert attrs["input"]["safe"] == "ok" and len(attrs["input"]["items"]) == 64
        persisted = path.read_text(encoding="utf-8")
        assert '"abc"' not in persisted and '"xyz"' not in persisted
    finally:
        tracing.set_llm_capture(saved)


def test_trace_sanitizer_has_a_global_tree_item_budget():
    from looplab.core.tracing import sanitize_trace_value

    # A per-container cap alone still permits exponential 64**depth expansion.  The durable preview
    # must retain only a globally bounded prefix even when every child is another numeric container
    # (numeric leaves consume no character budget).
    nested = [[[list(range(64)) for _ in range(20)] for _ in range(20)] for _ in range(20)]
    clean = sanitize_trace_value(nested)

    def count_items(value):
        if isinstance(value, dict):
            return len(value) + sum(count_items(child) for child in value.values())
        if isinstance(value, list):
            return len(value) + sum(count_items(child) for child in value)
        return 0

    assert count_items(clean) <= 256


def test_tool_trace_preview_redacts_structured_and_free_text_secrets():
    structured = _trace_preview({
        "api_key": "sk-abcdefghijklmnopqrstuvwxyz012345",
        "stdout": "Authorization: Bearer abcdef0123456789ABCDEF and password=hunter2secret",
    })
    assert "abcdefghijklmnopqrstuvwxyz012345" not in structured
    assert "abcdef0123456789ABCDEF" not in structured
    assert "hunter2secret" not in structured
    assert "redacted" in structured.lower() or "***" in structured


def test_tool_trace_preview_is_bounded_and_hashes_only_redacted_rendering():
    token = "aZ9k2Lp7qW3xYt5Rb8Nc1Vd6Mf0Gh4J"
    preview = _trace_preview("prefix token=" + token + " " + ("x" * 500), cap=180)
    assert len(preview) <= 180
    assert token not in preview
    assert "original_chars=" in preview and "sha256=" in preview
    assert len(_trace_preview("x" * 100, cap=12)) == 12


def test_tool_trace_preview_does_not_retry_a_broken_string_conversion():
    class Broken:
        def __str__(self):
            raise RuntimeError("do not perturb execution")

    assert _trace_preview(Broken()) == "<trace preview unavailable>"


def test_generation_trace_redacts_replayed_tool_content_and_controls(tmp_path):
    from looplab.core import tracing

    saved = tracing._CAPTURE_LLM_IO
    tracing.set_llm_capture(True)
    try:
        secret = "hunter2secret"
        bearer = "abcdef0123456789ABCDEF"
        t = Tracer(JsonlSpanExporter(tmp_path / "s.jsonl"), run_id="r")
        with t.span("deep_research", new_trace=True):
            with tracing.generation(
                    op="chat", model="m",
                    messages=[{"role": "tool", "content":
                               f"password={secret}\x00 Authorization: Bearer {bearer}\x1b[2J"}]) as g:
                g.output(f"api_key={secret}\x1b[31m" + "x" * 70_000)
        recs = [orjson.loads(line) for line in (tmp_path / "s.jsonl").read_bytes().splitlines()]
        attrs = next(rec["attributes"] for rec in recs if rec["name"] == "generation")
        prompt = attrs["input"][0]["content"]
        output = attrs["output"]
        assert secret not in prompt + output and bearer not in prompt + output
        assert "\x00" not in prompt + output and "\x1b" not in prompt + output
        assert "***" in prompt + output
        assert len(output) <= 64_000 and "sha256=" in output
    finally:
        tracing.set_llm_capture(saved)


def test_generation_trace_bounds_the_whole_replayed_conversation(tmp_path):
    from looplab.core import tracing

    saved = tracing._CAPTURE_LLM_IO
    tracing.set_llm_capture(True)
    try:
        messages = [{"role": "tool", "content": f"old-{i}-" + "x" * 2_000}
                    for i in range(100)]
        path = tmp_path / "s.jsonl"
        tracer = Tracer(JsonlSpanExporter(path), run_id="r")
        with tracer.span("root", new_trace=True):
            with tracing.generation(op="chat", model="m", messages=messages):
                pass
        attrs = next(orjson.loads(line)["attributes"] for line in path.read_bytes().splitlines()
                     if orjson.loads(line)["name"] == "generation")
        persisted = orjson.dumps(attrs["input"])
        assert len(attrs["input"]) <= 64 and len(persisted) < 66_000
        assert "old-99-" in persisted.decode() and "old-0-" not in persisted.decode()
    finally:
        tracing.set_llm_capture(saved)


def test_new_trace_starts_fresh_trace(tmp_path):
    t = Tracer(JsonlSpanExporter(tmp_path / "s.jsonl"), run_id="r")
    with t.span("a", new_trace=True):
        pass
    with t.span("b", new_trace=True):
        pass
    recs = [orjson.loads(line) for line in (tmp_path / "s.jsonl").read_bytes().splitlines()]
    assert recs[0]["trace_id"] != recs[1]["trace_id"] and recs[0]["parent_id"] is None


def test_exception_marks_span_error_and_reraises(tmp_path):
    t = Tracer(JsonlSpanExporter(tmp_path / "s.jsonl"))
    with pytest.raises(ValueError):
        with t.span("boom", new_trace=True):
            raise ValueError("x")
    rec = orjson.loads((tmp_path / "s.jsonl").read_bytes().splitlines()[0])
    assert rec["status"] == "ERROR"
    assert any(e.get("name") == "exception" for e in rec["events"])


def test_exception_and_observation_errors_redact_secrets_before_jsonl(tmp_path):
    from looplab.core import tracing

    secret = "abcdef0123456789ABCDEF0123456789"
    path = tmp_path / "s.jsonl"
    tracer = Tracer(JsonlSpanExporter(path))
    with pytest.raises(RuntimeError):
        with tracer.span("provider", new_trace=True):
            raise RuntimeError(f"Authorization: Bearer {secret}")
    with tracer.span("tool-parent", new_trace=True):
        with tracing.tool("provider-call") as observation:
            observation.error(f"api_key={secret}")
    persisted = path.read_text(encoding="utf-8")
    assert secret not in persisted
    assert "Authorization: Bearer" in persisted or "redact" in persisted.lower() or "***" in persisted


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
    repo = tmp_path / "repo"
    repo.mkdir()
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


def test_finalizer_flushes_the_local_queue_before_building_trace_json(tmp_path):
    """A queued row must be present in derived trace artifacts before Engine.run returns."""
    engine = _engine(tmp_path)
    exporter = engine.tracer.exporter
    real_export_line = exporter._writer._export_line
    real_force_flush = engine.tracer.force_flush
    started = threading.Event()
    release = threading.Event()
    flush_calls = 0

    def blocked_export(line, **kwargs):
        started.set()
        assert release.wait(5)
        return real_export_line(line, **kwargs)

    def releasing_flush(*, timeout_millis):
        nonlocal flush_calls
        flush_calls += 1
        release.set()
        return real_force_flush(timeout_millis=timeout_millis)

    exporter._writer._export_line = blocked_export
    engine.tracer.force_flush = releasing_flush
    with engine.tracer.span("queued-before-finalize", new_trace=True):
        pass
    assert started.wait(1)

    anyio.run(engine.run)

    assert flush_calls >= 1
    trace_view = orjson.loads((tmp_path / "run" / "trace.json").read_bytes())
    assert b'"name":"queued-before-finalize"' in orjson.dumps(trace_view)


def test_finalizer_does_not_swallow_a_deep_trace_json_artifact(tmp_path, monkeypatch):
    """A deep but valid projection replaces the artifact instead of tripping orjson's depth limit."""
    from looplab.engine import finalize as finalizer

    engine = _engine(tmp_path)
    anyio.run(engine.run)
    depth = 600  # above recursive framework encoders and orjson's nested-container limit
    spans = [{
        "name": "deep", "kind": "operation", "trace_id": "deep-trace",
        "span_id": f"deep-{index}",
        "parent_id": f"deep-{index - 1}" if index else None,
        "run_id": "demo", "attributes": {"node_id": 0}, "events": [],
        "status": "OK", "start": float(index), "duration_s": 0.1,
    } for index in range(depth)]
    monkeypatch.setattr(finalizer, "_flush_trace_exporter", lambda _engine: True)
    monkeypatch.setattr(finalizer, "load_span_tail", lambda *_args: (spans, len(spans)))
    artifact = engine.run_dir / "trace.json"
    artifact.write_bytes(b"stale artifact that must be replaced")

    finalizer.finalize_run(engine, entry_finished=True, start_time=time.monotonic())

    encoded = artifact.read_bytes()
    assert encoded != b"stale artifact that must be replaced"
    old_recursion_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old_recursion_limit, depth * 4))
    try:
        projection = json.loads(encoded)
    finally:
        sys.setrecursionlimit(old_recursion_limit)
    cursor, seen = projection["nodes"]["0"][0], 0
    while cursor is not None:
        seen += 1
        children = cursor.get("children") or []
        cursor = children[0] if children else None
    assert seen == depth


def test_engine_run_terminally_drops_a_background_span_closed_after_return(tmp_path):
    engine = _engine(tmp_path)
    exporter = engine.tracer.exporter
    assert exporter._lifecycle_fence is True
    opened = threading.Event()
    release = threading.Event()

    def close_after_owner_returns():
        with engine.tracer.span("late-background", new_trace=True):
            opened.set()
            assert release.wait(30)

    background = threading.Thread(target=close_after_owner_returns)
    background.start()
    assert opened.wait(1)
    try:
        anyio.run(engine.run)
        source = tmp_path / "run" / "spans.jsonl"
        at_return = source.read_bytes()
    finally:
        release.set()
        background.join(timeout=2)

    assert not background.is_alive()
    assert source.read_bytes() == at_return
    metrics = exporter.metrics()
    assert metrics["shutdown"] is True
    assert metrics["dropped_shutdown"] == 1


def test_engine_run_calls_one_bounded_terminal_shutdown_on_error():
    from looplab.core.tracing import TRACE_EXPORT_FLUSH_TIMEOUT_MILLIS

    calls = []

    class ShutdownProbe:
        def shutdown(self, *, timeout_millis):
            calls.append(timeout_millis)
            return True

    class RunFailure(Exception):
        pass

    async def fail_run():
        raise RunFailure("domain failure")

    engine = Engine.__new__(Engine)
    engine._llm_broker = None
    engine.tracer = ShutdownProbe()
    engine._run_with_llm_broker = fail_run
    with pytest.raises(RunFailure, match="domain failure"):
        anyio.run(engine.run)
    assert calls == [TRACE_EXPORT_FLUSH_TIMEOUT_MILLIS]


def test_conversation_dedups_resent_history():
    """The linear conversation projection shows the system+user request ONCE per sub-loop, and each
    generation contributes only its delta (thinking + output + tool_calls) — never the raw tree's
    per-turn re-send of the whole growing message history. A context RESET (shorter input, e.g. a
    compaction or a sub-agent handoff) starts a fresh request."""
    from types import SimpleNamespace
    from looplab.events.traceview import build_conversation

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
    repo = tmp_path / "repo"
    repo.mkdir()
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


def test_live_developer_spans_band_under_implement_not_researcher(tmp_path):
    """Regression for the trace-view bug: an operation span (implement) is written to spans.jsonl only on
    CLOSE, so while the Developer runs, its live child generations reference a parent not yet on disk and
    the per-node conversation mis-grouped them under the Researcher/create_node. Now each child carries a
    `phase` attribute (tracing._phase_ctx), so build_conversation bands it under `implement` immediately."""
    from types import SimpleNamespace
    from looplab.events.traceview import build_conversation
    T = "trace1"
    spans = [
        {"span_id": "R", "parent_id": None, "trace_id": T, "name": "create_node", "kind": "operation",
         "start": 0.0, "attributes": {"node_id": 0}},
        {"span_id": "P", "parent_id": "R", "trace_id": T, "name": "propose", "kind": "operation",
         "start": 1.0, "attributes": {"node_id": 0}},
        {"span_id": "g1", "parent_id": "P", "trace_id": T, "name": "gen", "kind": "generation", "start": 1.5,
         "attributes": {"node_id": 0, "phase": "propose", "input": [{"role": "user", "content": "x"}]}},
        # Developer generation: parent is the implement span 'I', which is NOT in the list yet (still open).
        {"span_id": "g2", "parent_id": "I", "trace_id": T, "name": "gen", "kind": "generation", "start": 3.0,
         "attributes": {"node_id": 0, "phase": "implement", "input": [{"role": "user", "content": "y"}]}},
    ]
    conv = build_conversation(SimpleNamespace(run_id="r", task_id="t", nodes={}), spans, 0)
    labels = [s["label"] for s in conv["stages"]]
    assert "implement" in labels and "propose" in labels          # two distinct phases, not merged
    impl = next(s for s in conv["stages"] if s["label"] == "implement")
    assert impl["turns"]                                          # the Developer generation lives here


def test_conversation_bands_all_phases_in_order():
    """The per-node conversation reads as ordered role/phase bands (the user's target trace): Researcher
    propose, then the Developer's stages -> plan -> implement — grouped by the `phase` stamped on each
    generation, so nesting (stages/plan sit under the orchestrator's implement span) doesn't merge them."""
    from types import SimpleNamespace
    from looplab.events.traceview import build_conversation
    T = "t"

    def gen(sid, phase, start):
        return {"span_id": sid, "parent_id": "X", "trace_id": T, "name": "gen", "kind": "generation",
                "start": start, "attributes": {"node_id": 0, "phase": phase,
                                               "input": [{"role": "user", "content": "x"}]}}
    spans = [{"span_id": "R", "parent_id": None, "trace_id": T, "name": "create_node", "kind": "operation",
              "start": 0.0, "attributes": {"node_id": 0}},
             gen("g1", "propose", 1.0), gen("g2", "stages", 2.0),
             gen("g3", "plan", 3.0), gen("g4", "implement", 4.0)]
    conv = build_conversation(SimpleNamespace(run_id="r", task_id="t", nodes={}), spans, 0)
    assert [s["label"] for s in conv["stages"]] == ["propose", "stages", "plan", "implement"]


def test_live_trace_with_no_root_bands_by_phase_never_flat_generation():
    """THE reported live bug: create_node closes only at node END, so for the node's whole life its
    trace has NO root on disk. The old code fell back to the first span (a generation) as root, never
    took the split branch, and rendered the entire node as ONE flat band labeled 'generation' whose
    turns kept appending across role changes (Developer writing into the previous Researcher block).
    A rootless trace must band by the phase stamps, chronologically."""
    from types import SimpleNamespace
    from looplab.events.traceview import build_conversation

    def gen(sid, phase, start):
        return {"span_id": sid, "parent_id": "UNWRITTEN", "trace_id": "t", "name": "generation",
                "kind": "generation", "start": start,
                "attributes": {"node_id": 0, "phase": phase,
                               "input": [{"role": "user", "content": "x"}]}}
    spans = [gen("g1", "propose", 1.0), gen("g2", "stages", 2.0), gen("g3", "implement", 3.0)]
    conv = build_conversation(SimpleNamespace(run_id="r", task_id="t", nodes={}), spans, 0)
    assert [s["label"] for s in conv["stages"]] == ["propose", "stages", "implement"]
    assert "generation" not in [s["label"] for s in conv["stages"]]


def test_live_same_phase_retries_separate_bands_via_phase_span():
    """Two same-phase sub-loops LIVE (a retry re-runs the stages phase before any op span is on
    disk): the phase NAME alone can't tell them apart, but the stamped `phase_span` (the op's span
    id) can — each retry is its own chronological band."""
    from types import SimpleNamespace
    from looplab.events.traceview import build_conversation

    def gen(sid, ph_sid, start):
        return {"span_id": sid, "parent_id": ph_sid, "trace_id": "t", "name": "generation",
                "kind": "generation", "start": start,
                "attributes": {"node_id": 0, "phase": "stages", "phase_span": ph_sid,
                               "input": [{"role": "user", "content": "x"}]}}
    spans = [gen("g1", "S1", 1.0), gen("g2", "S2", 5.0)]
    conv = build_conversation(SimpleNamespace(run_id="r", task_id="t", nodes={}), spans, 0)
    assert [s["label"] for s in conv["stages"]] == ["stages", "stages"]      # two bands, not merged


def test_eval_pipeline_stage_ops_become_train_score_blocks():
    """The eval pipeline's per-stage op spans (train / score) surface as their own blocks in the
    node story ('… Developer · implement, Train, Evaluate·score') with a status/duration turn."""
    from types import SimpleNamespace
    from looplab.events.traceview import build_conversation
    spans = [{"span_id": "E", "parent_id": None, "trace_id": "t", "name": "evaluate",
              "kind": "operation", "start": 0.0, "attributes": {"node_id": 0}},
             {"span_id": "T", "parent_id": "E", "trace_id": "t", "name": "train", "kind": "operation",
              "start": 1.0, "duration_s": 120.0,
              "attributes": {"node_id": 0, "stage": "train", "exit_code": 0}},
             {"span_id": "S", "parent_id": "E", "trace_id": "t", "name": "score", "kind": "operation",
              "start": 130.0, "duration_s": 5.0,
              "attributes": {"node_id": 0, "stage": "score", "exit_code": 0}}]
    conv = build_conversation(SimpleNamespace(run_id="r", task_id="t", nodes={}), spans, 0)
    assert [s["label"] for s in conv["stages"]] == ["train", "score"]
    t = conv["stages"][0]["turns"][0]
    assert t["name"] == "train" and "ok" in t["output"]


def test_span_children_carry_phase_span_of_the_op(tmp_path):
    """tracing stamps (phase, phase_span=the op's span id) on non-op children — the live view's
    exact sub-loop identity."""
    tr = Tracer(JsonlSpanExporter(tmp_path / "s.jsonl"), run_id="r")
    with tr.span("implement", new_trace=True):
        with tr.span("generation", kind="generation"):
            pass
    recs = [orjson.loads(x) for x in (tmp_path / "s.jsonl").read_bytes().splitlines()]
    gen = next(r for r in recs if r["kind"] == "generation")
    op = next(r for r in recs if r["kind"] == "operation")
    assert gen["attributes"]["phase"] == "implement"
    assert gen["attributes"]["phase_span"] == op["span_id"]


def test_finished_trace_same_phase_retries_stay_separate_bands():
    """Post-hoc the REAL op span wins over the bare phase name (878e0f8's contract): a
    ValidatingDeveloper retry re-runs stages→plan→implement inside ONE node, so two `stages` op spans
    exist. Keying bands by the op SPAN (not the phase string) keeps attempt 2's stages turns as their
    own chronological band instead of merging them into attempt 1's band out of order."""
    from types import SimpleNamespace
    from looplab.events.traceview import build_conversation
    T = "t"

    def op(sid, name, start, parent="R"):
        return {"span_id": sid, "parent_id": parent, "trace_id": T, "name": name, "kind": "operation",
                "start": start, "attributes": {"node_id": 0}}

    def gen(sid, parent, phase, start):
        return {"span_id": sid, "parent_id": parent, "trace_id": T, "name": "gen", "kind": "generation",
                "start": start, "attributes": {"node_id": 0, "phase": phase,
                                               "input": [{"role": "user", "content": "x"}]}}
    spans = [{"span_id": "R", "parent_id": None, "trace_id": T, "name": "create_node",
              "kind": "operation", "start": 0.0, "attributes": {"node_id": 0}},
             op("I", "implement", 1.0),
             op("S1", "stages", 1.1, parent="I"), gen("g1", "S1", "stages", 1.2),
             gen("g2", "I", "implement", 2.0),                     # attempt 1's implement turn
             op("S2", "stages", 3.0, parent="I"), gen("g3", "S2", "stages", 3.1)]   # attempt 2
    conv = build_conversation(SimpleNamespace(run_id="r", task_id="t", nodes={}), spans, 0)
    labels = [s["label"] for s in conv["stages"]]
    assert labels == ["stages", "implement", "stages"]     # two SEPARATE stages bands, chronological


def test_generic_span_attributes_are_redacted_at_the_durable_boundary(tmp_path):
    """`spans.jsonl` (and the OTLP exporter) is a DURABLE egress boundary — redact HERE.

    Every purpose-built path sanitizes before writing (`generation`, `tool`, `output`, `thinking`),
    but `SpanHandle.set`/`set_many`/`event` took arbitrary values verbatim. `traceview` only protects
    the UI projection, so a credential passed to generic instrumentation landed in the on-disk span
    and was shipped to any configured external collector in the clear.
    """
    exporter = JsonlSpanExporter(tmp_path / "s.jsonl")
    with Tracer(exporter, run_id="r1").span("op", new_trace=True) as sp:
        sp.set("api_key", "sk-live-SUPERSECRET")
        sp.set_many(authorization="Bearer SUPERSECRET", node_id=3)
        sp.event("call", password="SUPERSECRET", tool="grep")

    raw = (tmp_path / "s.jsonl").read_text()
    assert "SUPERSECRET" not in raw, "a secret-named attribute reached the durable span log verbatim"
    rec = orjson.loads(raw.splitlines()[0])
    # two independent layers, both required: a secret-NAMED key is masked outright, and any value
    # that looks like credential material is redacted even under a key the name-matcher allows.
    assert rec["attributes"]["api_key"] == "***"
    assert "SUPERSECRET" not in str(rec["attributes"]["authorization"])
    assert rec["attributes"]["node_id"] == 3               # benign values pass through
    assert rec["events"][0]["password"] == "***" and rec["events"][0]["tool"] == "grep"


def test_oversized_built_in_span_falls_back_and_does_not_hide_later_spans(tmp_path):
    """Repeated instrumentation cannot make our writer emit one reader-rejected poison row."""
    from looplab.core.trace_files import TRACE_JSONL_ROW_MAX_BYTES
    from looplab.events.span_index import get_index

    path = tmp_path / "spans.jsonl"
    tracer = Tracer(JsonlSpanExporter(path), run_id="r")
    payload = "x" * 64_000
    with tracer.span(
            "oversized", new_trace=True, node_id=7, generation=3) as span:
        oversized_ids = current_ids()
        for index in range(70):
            span.set(f"diagnostic_{index}", payload)
            span.event("diagnostic", n=index, detail=payload)
    with tracer.span("after", new_trace=True, node_id=8):
        pass

    raw_lines = path.read_bytes().splitlines(keepends=True)
    assert len(raw_lines) == 2
    assert all(len(line) <= TRACE_JSONL_ROW_MAX_BYTES for line in raw_lines)
    fallback, after = (orjson.loads(line) for line in raw_lines)
    assert fallback["name"] == "oversized"
    assert (fallback["trace_id"], fallback["span_id"]) == oversized_ids
    assert fallback["attributes"]["node_id"] == 7
    assert fallback["attributes"]["generation"] == 3
    assert fallback["attributes"]["looplab.trace_payload_truncated"] is True
    assert fallback["events"] == [{
        "name": "looplab.span_payload_truncated",
        "reason": "physical_row_limit",
        "limit_bytes": TRACE_JSONL_ROW_MAX_BYTES,
        "omitted_attributes": 70,
        "omitted_events": 70,
    }]
    assert fallback["status"] == "OK"
    assert isinstance(fallback["start"], float)
    assert isinstance(fallback["duration_s"], float)
    assert after["name"] == "after" and after["events"] == []

    projected = load_spans(path)
    assert [row["name"] for row in projected] == ["oversized", "after"]
    assert projected[0]["events"][0] == {
        "name": "looplab.span_payload_truncated", "reason": "physical_row_limit"}
    indexed = get_index(path)
    assert [row["name"] for row in indexed.light_spans()] == ["oversized", "after"]


def test_oversized_generation_marks_every_later_delta_input_partial(tmp_path, monkeypatch):
    """A retained back-reference cannot make an omitted over-limit prompt look complete later."""
    from looplab.core import tracing
    from looplab.events.traceview import hydrate_inputs

    path = tmp_path / "spans.jsonl"
    monkeypatch.setattr(tracing, "TRACE_JSONL_ROW_MAX_BYTES", 128 * 1024)
    tracer = Tracer(JsonlSpanExporter(path), run_id="r", capture_llm_io=True)
    base = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "request"},
    ]
    extended = base + [
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "follow-up"},
    ]
    payload = "x" * 64_000
    with tracer.span("root", new_trace=True, node_id=4):
        with tracing.generation(op="chat", model="m", messages=base) as first:
            first_sid = first._h._rec["span_id"]
            for index in range(3):
                first._h.event("diagnostic", n=index, detail=payload)
        with tracing.generation(op="chat", model="m", messages=extended) as second:
            second_sid = second._h._rec["span_id"]

    loaded = load_spans(path)
    by_id = {row["span_id"]: row for row in loaded}
    assert second_sid in by_id                    # the valid row after the fallback remains readable
    assert by_id[first_sid]["attributes"]["input_partial"] is True
    assert by_id[second_sid]["attributes"]["input_from"] == first_sid
    hydrated = {row["span_id"]: row for row in hydrate_inputs(loaded, _normalized=True)}
    assert hydrated[first_sid]["attributes"]["input_partial"] is True
    assert hydrated[second_sid]["attributes"]["input_partial"] is True
    assert hydrated[second_sid]["attributes"]["input"] == extended[2:]


def test_direct_exporter_fallback_never_sanitizes_huge_identity_or_structure(
        tmp_path, monkeypatch):
    """Cheap length gates run before redactors that could scan/copy hostile direct-export strings."""
    from looplab.core import tracing

    monkeypatch.setattr(tracing, "TRACE_JSONL_ROW_MAX_BYTES", 4096)
    huge = "x" * 5000
    real_label = tracing._trace_label
    real_identity = tracing.redact_persisted_identity
    real_attributes = tracing._sanitize_initial_attributes

    def guarded_label(value, **kwargs):
        assert value is not huge
        return real_label(value, **kwargs)

    def guarded_identity(value, **kwargs):
        assert value is not huge
        return real_identity(value, **kwargs)

    def guarded_attributes(values):
        assert huge not in values.values()
        return real_attributes(values)

    monkeypatch.setattr(tracing, "_trace_label", guarded_label)
    monkeypatch.setattr(tracing, "redact_persisted_identity", guarded_identity)
    monkeypatch.setattr(tracing, "_sanitize_initial_attributes", guarded_attributes)
    path = tmp_path / "spans.jsonl"

    JsonlSpanExporter(path).export({
        "name": huge, "kind": "operation", "trace_id": huge, "span_id": huge,
        "parent_id": huge, "run_id": huge, "status": "OK", "start": 1.0,
        "duration_s": 0.1,
        "attributes": {"node_id": huge, "generation": huge}, "events": [],
    })

    fallback = orjson.loads(path.read_bytes())
    assert fallback["name"] == "span"
    assert fallback["trace_id"] is fallback["span_id"] is fallback["parent_id"] is None
    assert fallback["run_id"] is None
    assert "node_id" not in fallback["attributes"]
    assert "generation" not in fallback["attributes"]
    assert fallback["attributes"]["looplab.trace_payload_truncated"] is True


def test_initial_span_and_llm_event_metadata_share_the_durable_boundary(tmp_path):
    """Constructor metadata is no less durable than values attached through ``SpanHandle.set``.

    Initial attributes plus record_llm_call's labels are mirrored to OTLP as well as JSONL, so they
    must be redacted, control-safe and bounded before either exporter sees them.  Ordinary topology and
    numeric metadata must remain unchanged.
    """
    from looplab.core import tracing

    secret = "abcdef0123456789ABCDEF"
    path = tmp_path / "s.jsonl"
    tracer = Tracer(JsonlSpanExporter(path), run_id="r1", capture_llm_io=True)
    with tracer.span(
            "install\npassword=hunter2secret " + "n" * 500,
            new_trace=True,
            kind="operation\x1b[2J" + "k" * 100,
            node_id=3,
            attempt=2,
            api_key="sk-live-SUPERSECRET0123456789",
            details={"authorization": f"Bearer {secret}", "safe": "kept"},
            oversized="x" * 80_000):
        tracing.record_llm_call(
            op="chat\npassword=hunter2secret " + "o" * 500,
            model=f"model\x1b[2J Authorization: Bearer {secret} " + "m" * 500,
            messages=[], completion="")

    raw = path.read_text(encoding="utf-8")
    assert "hunter2secret" not in raw and secret not in raw and "SUPERSECRET" not in raw
    assert "\x1b" not in raw
    rec = orjson.loads(raw)
    assert len(rec["name"]) <= 160 and "\n" not in rec["name"]
    assert len(rec["kind"]) <= 32 and "\n" not in rec["kind"]
    assert rec["attributes"]["node_id"] == 3 and rec["attributes"]["attempt"] == 2
    assert rec["attributes"]["api_key"] == "***"
    assert "***" in rec["attributes"]["details"]["authorization"]
    assert rec["attributes"]["details"]["safe"] == "kept"
    assert len(rec["attributes"]["oversized"]) <= 64_000
    event = next(e for e in rec["events"] if e["name"] == "llm_call")
    assert len(event["op"]) <= 160 and "\n" not in event["op"]
    assert len(event["model"]) <= 160 and "\n" not in event["model"]


def test_structural_attributes_survive_an_earlier_oversized_diagnostic(tmp_path):
    """Identity/lifecycle keys cannot be starved by kwargs insertion order.

    These attributes drive node attribution and attempt fencing.  The generic sanitizer used one
    shared first-to-last text budget, so an unrelated large field placed first erased all of them.
    """
    path = tmp_path / "s.jsonl"
    tracer = Tracer(JsonlSpanExporter(path), run_id="r")
    structural = {
        "node_id": 7, "generation": 3, "attempt": 2,
        "phase": "implement", "phase_span": "phase-1",
        "input_from": "gen-0", "input_carry": 4, "input_partial": True,
    }
    with tracer.span("payload-first", new_trace=True,
                     **{"oversized": "x" * 80_000, **structural}):
        pass
    with tracer.span("structure-first", new_trace=True,
                     **{**structural, "oversized": "x" * 80_000}):
        pass

    records = [orjson.loads(line) for line in path.read_bytes().splitlines()]
    for record in records:
        attrs = record["attributes"]
        assert {key: attrs.get(key) for key in structural} == structural
        assert "oversized" in attrs and len(attrs["oversized"]) < 64_000
        assert len(orjson.dumps(attrs)) < 65_000


def test_event_name_and_run_identity_are_sanitized_before_durable_egress(tmp_path):
    """Event labels and Tracer.run_id reach JSONL/OTLP and must not bypass the boundary."""
    secret = "abcdef0123456789ABCDEF"
    path = tmp_path / "s.jsonl"
    benign_identity = "run-Ａ-42"       # NFKC would change this opaque join key to run-A-42
    with Tracer(JsonlSpanExporter(path), run_id=benign_identity).span(
            "benign", new_trace=True) as span:
        span.event(f"provider\x1b[2J Authorization: Bearer {secret}" + "n" * 500)
    unsafe_identity = f"job Authorization: Bearer {secret}\x00"
    with Tracer(JsonlSpanExporter(path), run_id=unsafe_identity).span("unsafe", new_trace=True):
        pass

    raw = path.read_text(encoding="utf-8")
    assert secret not in raw and "\x1b" not in raw and "\x00" not in raw
    records = {record["name"]: record
               for record in (orjson.loads(line) for line in raw.splitlines())}
    assert records["benign"]["run_id"] == benign_identity
    event_name = records["benign"]["events"][0]["name"]
    assert len(event_name) <= 160 and "\n" not in event_name and "***" in event_name
    assert records["unsafe"]["run_id"] != unsafe_identity


def test_dynamic_attribute_and_event_field_keys_are_sanitized_before_egress(tmp_path):
    """A credential embedded in a dynamic key leaks even when its value is perfectly harmless."""
    secret = "abcdef0123456789ABCDEF"
    path = tmp_path / "s.jsonl"
    tracer = Tracer(JsonlSpanExporter(path), run_id="r")
    with tracer.span("keys", new_trace=True) as span:
        span.set(f"note Authorization: Bearer {secret}\x1b[2J", "visible")
        span.event("dynamic", **{
            f"field Authorization: Bearer {secret}\x00": "visible",
            "api_key": "value-must-be-masked",
        })

    raw = path.read_text(encoding="utf-8")
    assert secret not in raw and "\x1b" not in raw and "\x00" not in raw
    record = orjson.loads(raw)
    dynamic_attribute = next(key for key in record["attributes"] if key.startswith("note "))
    dynamic_field = next(key for key in record["events"][0] if key.startswith("field "))
    assert "***" in dynamic_attribute and len(dynamic_attribute) <= 160
    assert "***" in dynamic_field and len(dynamic_field) <= 160
    assert record["events"][0]["api_key"] == "***"


def test_sanitized_dynamic_keys_cannot_overwrite_lifecycle_topology(tmp_path):
    path = tmp_path / "s.jsonl"
    tracer = Tracer(JsonlSpanExporter(path), run_id="r")
    with tracer.span("root", new_trace=True, node_id=7, generation=1) as root:
        root.set("generation\n", 999)
        root.set("node_id\n", 666)
        root.set("generation", "invalid-provider-label")
        root.set("node_id", {"invalid": True})
        root.set("looplab.run_id", "forged")
        with tracer.span("child"):
            pass

    records = {row["name"]: row for row in (
        orjson.loads(line) for line in path.read_bytes().splitlines())}
    root_attrs = records["root"]["attributes"]
    assert root_attrs["generation"] == 1 and root_attrs["node_id"] == 7
    assert root_attrs["field.generation"] == "invalid-provider-label"
    assert root_attrs["field.node_id"] == {"invalid": True}
    assert root_attrs["field.looplab.run_id"] == "forged"
    assert records["child"]["attributes"]["generation"] == 1
    assert records["child"]["attributes"]["node_id"] == 7


def test_dynamic_exception_type_is_sanitized_before_egress(tmp_path):
    """Provider-defined exception class names are untrusted metadata too."""
    secret = "abcdef0123456789ABCDEF"
    unsafe_error = type(f"Provider Authorization: Bearer {secret}", (RuntimeError,), {})
    path = tmp_path / "s.jsonl"
    with pytest.raises(unsafe_error):
        with Tracer(JsonlSpanExporter(path), run_id="r").span("provider", new_trace=True):
            raise unsafe_error("benign message")

    raw = path.read_text(encoding="utf-8")
    assert secret not in raw
    event = orjson.loads(raw)["events"][0]
    assert event["name"] == "exception" and "***" in event["type"]
    assert len(event["type"]) <= 160


@pytest.mark.parametrize("prefix", [b'{"committed":true}\n', b""])
def test_exporter_heals_a_torn_final_record_before_resuming(tmp_path, prefix):
    """A resumed append must not glue a new span onto a crash-torn, non-newline suffix."""
    path = tmp_path / "s.jsonl"
    torn = b'{"name":"crash-torn","attributes":'
    path.write_bytes(prefix + torn)

    with Tracer(JsonlSpanExporter(path), run_id="r1").span("resumed", new_trace=True):
        pass

    raw = path.read_bytes()
    assert raw.startswith(prefix)
    assert torn not in raw
    rows = [orjson.loads(line) for line in raw.splitlines()]
    assert rows[-1]["name"] == "resumed"
    assert len(rows) == (2 if prefix else 1)


@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_exporter_rejects_link_alias_without_mutating_private_victim(tmp_path, alias_kind):
    victim = tmp_path / "private.jsonl"
    original = b'{"private":"DO NOT TOUCH"}\n'
    victim.write_bytes(original)
    target = tmp_path / "spans.jsonl"
    try:
        if alias_kind == "symlink":
            target.symlink_to(victim)
        else:
            os.link(victim, target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"{alias_kind} unavailable: {exc}")

    with pytest.raises(OSError):
        JsonlSpanExporter(target).export({"name": "must-not-escape"})

    assert victim.read_bytes() == original


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_exporter_rejects_fifo_without_waiting_for_a_peer(tmp_path):
    target = tmp_path / "spans.jsonl"
    os.mkfifo(target)

    started = time.monotonic()
    with pytest.raises(OSError):
        JsonlSpanExporter(target).export({"name": "must-not-block"})
    assert time.monotonic() - started < 2.0


def test_exporter_revalidates_the_destination_after_its_append(tmp_path, monkeypatch):
    """A run-root replacement during export cannot be mistaken for a current-file commit."""
    from looplab.core import tracing

    source = tmp_path / "spans.jsonl"
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(b'{"replacement":true}\n')
    real_receipt = tracing._write_append_receipt

    def replace_before_append_context_exits(source_path, receipt):
        real_receipt(source_path, receipt)
        os.replace(replacement, source_path)

    monkeypatch.setattr(tracing, "_write_append_receipt", replace_before_append_context_exits)
    with pytest.raises(OSError):
        JsonlSpanExporter(source).export({"name": "detached-write"})

    assert source.read_bytes() == b'{"replacement":true}\n'


def test_append_receipt_journal_rotates_on_source_replacement(tmp_path):
    from looplab.core.trace_append import SPAN_APPEND_JOURNAL_NAME

    source = tmp_path / "spans.jsonl"
    exporter = JsonlSpanExporter(source)
    exporter.export({"name": "old"})
    journal = tmp_path / SPAN_APPEND_JOURNAL_NAME
    old_receipt = orjson.loads(journal.read_bytes().splitlines()[-1])

    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(b'{"name":"replacement-base"}\n')
    os.replace(replacement, source)
    exporter.export({"name": "new"})

    receipts = [orjson.loads(line) for line in journal.read_bytes().splitlines()]
    assert len(receipts) == 1
    assert (receipts[0]["dev"], receipts[0]["ino"]) != (
        old_receipt["dev"], old_receipt["ino"])
    assert [orjson.loads(line)["name"] for line in source.read_bytes().splitlines()] == [
        "replacement-base", "new"]


def test_append_receipt_journal_is_bounded_and_hardlink_safe(tmp_path, monkeypatch):
    from looplab.core import tracing
    from looplab.core.trace_append import SPAN_APPEND_JOURNAL_NAME

    source = tmp_path / "spans.jsonl"
    exporter = JsonlSpanExporter(source)
    # One receipt fits, two do not: the second append compacts to its own checkpoint transition.
    # The cap is measured off the receipt the CURRENT schema writes rather than hard-coded: a literal
    # byte count quietly turns this from "one fits" into "none fit" the first time the wire contract
    # gains a field, and the test would keep passing while proving something else.
    exporter.export({"name": "one"})
    journal = tmp_path / SPAN_APPEND_JOURNAL_NAME
    one_receipt = len(journal.read_bytes())
    assert one_receipt > 0 and len(journal.read_bytes().splitlines()) == 1
    monkeypatch.setattr(tracing, "SPAN_APPEND_JOURNAL_MAX_BYTES", one_receipt + 1)
    exporter.export({"name": "two"})
    assert len(journal.read_bytes()) <= one_receipt + 1
    assert len(journal.read_bytes().splitlines()) == 1

    journal.unlink()
    victim = tmp_path / "journal-victim"
    original = b"PRIVATE RECEIPT TARGET\n"
    victim.write_bytes(original)
    try:
        os.link(victim, journal)
    except OSError as exc:
        pytest.skip(f"hard links are unavailable on this filesystem: {exc}")
    exporter.export({"name": "three"})       # span truth commits; unsafe receipt is best-effort dropped
    assert victim.read_bytes() == original


def test_exporters_share_a_canonical_path_lock_while_healing(tmp_path):
    """Two exporter objects cannot let one healer truncate a row the other just committed."""
    path = tmp_path / "s.jsonl"
    path.write_bytes(b'{"committed":true}\n{"torn":')
    alias_parent = tmp_path / "alias"
    alias_parent.mkdir()
    first = JsonlSpanExporter(path)
    second = JsonlSpanExporter(alias_parent / ".." / "s.jsonl")
    assert first._lock is second._lock

    started = threading.Event()
    finished = threading.Event()

    def append_second():
        started.set()
        second.export({"name": "second"})
        finished.set()

    # Hold the shared guard, start the competing exporter, then re-enter it through the first
    # exporter.  RLock makes the owner re-entry legal while the other thread remains fenced.
    with first._lock:
        worker = threading.Thread(target=append_second)
        worker.start()
        assert started.wait(1)
        first.export({"name": "first"})
        assert not finished.is_set()
    worker.join(timeout=2)
    assert not worker.is_alive() and finished.is_set()

    rows = [orjson.loads(line) for line in path.read_bytes().splitlines()]
    assert rows[0] == {"committed": True}
    assert [row["name"] for row in rows[1:]] == ["first", "second"]


@pytest.mark.skipif(not hasattr(__import__("os"), "fork"), reason="POSIX fork regression")
def test_inherited_exporter_does_not_deadlock_after_multithreaded_fork(tmp_path):
    """A fork child must not inherit an RLock owned by a vanished parent thread."""
    import os
    import signal

    path = tmp_path / "s.jsonl"
    exporter = JsonlSpanExporter(path)
    held = threading.Event()
    release = threading.Event()

    def hold_export_lock():
        with exporter._lock:
            held.set()
            release.wait(5)

    worker = threading.Thread(target=hold_export_lock)
    worker.start()
    assert held.wait(1)
    pid = os.fork()
    if pid == 0:  # pragma: no cover - assertions are made from the parent process
        try:
            exporter.export({"name": "fork-child"})
        except BaseException:
            os._exit(3)
        os._exit(0)

    release.set()
    worker.join(timeout=2)
    status = None
    deadline = time.monotonic() + 3
    try:
        while time.monotonic() < deadline:
            waited, candidate = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                status = candidate
                break
            time.sleep(0.02)
    finally:
        if status is None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
    assert status is not None, "fork child deadlocked on an inherited exporter lock"
    assert os.waitstatus_to_exitcode(status) == 0
    assert orjson.loads(path.read_bytes()) == {"name": "fork-child"}


def test_new_trace_span_is_a_real_root(tmp_path):
    """A `new_trace` span must have NO parent_id, or its trace has no discoverable root.

    `new_trace` already gives the span a fresh trace_id, but `parent_id` still pointed at the
    enclosing span — ACROSS traces. `events/traceview.py` finds a trace's root by `parent_id is None`,
    so the new trace had none and rendered orphaned under the outer one.
    """
    exporter = JsonlSpanExporter(tmp_path / "s.jsonl")
    t = Tracer(exporter, run_id="r1")
    with t.span("outer", new_trace=True):
        with t.span("inner_same_trace") as child:        # ordinary nesting still links
            pass
        with t.span("isolated", new_trace=True):
            pass

    recs = {orjson.loads(line)["name"]: orjson.loads(line)
            for line in (tmp_path / "s.jsonl").read_text().splitlines()}
    assert recs["inner_same_trace"]["parent_id"] == recs["outer"]["span_id"]
    assert recs["inner_same_trace"]["trace_id"] == recs["outer"]["trace_id"]
    assert recs["isolated"]["trace_id"] != recs["outer"]["trace_id"]
    assert recs["isolated"]["parent_id"] is None, (
        "a new_trace span kept a cross-trace parent — traceview finds a root by parent_id is None, "
        "so this trace has none")


def test_llm_capture_policy_is_bound_per_tracer_across_interleaved_traces(tmp_path):
    """Two runs in ONE process with OPPOSITE capture policies must not decide for each other.

    The permission used to be a single module global (`_CAPTURE_LLM_IO`): whichever run called
    `set_llm_capture` last won for ALL live tracers, so an opted-OUT run's prompts landed in its
    spans.jsonl — or an opted-IN run's went missing — purely on interleaving. It is now declared on
    the Tracer and scoped by `Tracer.span`; the module global is only the process-wide DEFAULT for
    tracers that declare nothing.
    """
    from looplab.core import tracing

    saved = tracing._CAPTURE_LLM_IO
    try:
        # What an opted-IN run (or the CLI's `set_llm_capture(settings.trace_llm_io)`) leaves behind.
        tracing.set_llm_capture(True)
        secret_prompt = [{"role": "user", "content": "opted-out prompt body"}]
        off = Tracer(JsonlSpanExporter(tmp_path / "off.jsonl"), run_id="off", capture_llm_io=False)
        on = Tracer(JsonlSpanExporter(tmp_path / "on.jsonl"), run_id="on", capture_llm_io=True)

        # INTERLEAVED: the opted-out run's span opens while the opted-in run's span is live.
        with on.span("on-root", new_trace=True):
            with off.span("off-root", new_trace=True):
                with tracing.generation(op="off", model="m", messages=secret_prompt) as g:
                    g.output("opted-out completion").thinking("opted-out reasoning")
                    g.tool_calls([{"name": "fetch", "arguments": "opted-out args"}])
                tracing.record_llm_call(op="off", model="m", messages=secret_prompt,
                                        completion="opted-out completion")
            # ...and the opted-in run keeps capturing after the opted-out span closes.
            with tracing.generation(op="on", model="m",
                                    messages=[{"role": "user", "content": "opted-in prompt"}]) as g:
                g.output("opted-in completion")

        off_text = (tmp_path / "off.jsonl").read_text(encoding="utf-8")
        assert "opted-out" not in off_text, (
            "an opted-OUT run persisted LLM I/O because another run had flipped the process-global "
            "capture flag on — the policy is not bound to the tracer")
        off_recs = [orjson.loads(line) for line in off_text.splitlines()]
        off_gen = next(r for r in off_recs if r["kind"] == "generation")
        assert "input" not in off_gen["attributes"] and "output" not in off_gen["attributes"]
        assert not any(e["name"] == "llm_call"
                       for r in off_recs for e in r["events"])

        on_recs = [orjson.loads(line)
                   for line in (tmp_path / "on.jsonl").read_text(encoding="utf-8").splitlines()]
        on_gen = next(r for r in on_recs if r["kind"] == "generation")
        assert on_gen["attributes"]["input"] and on_gen["attributes"]["output"]

        # And the mirror image: an opted-IN run must still capture while the process default is OFF.
        tracing.set_llm_capture(False)
        on2 = Tracer(JsonlSpanExporter(tmp_path / "on2.jsonl"), run_id="on2", capture_llm_io=True)
        plain = Tracer(JsonlSpanExporter(tmp_path / "plain.jsonl"), run_id="plain")
        with on2.span("on2-root", new_trace=True):
            with tracing.generation(op="on2", model="m",
                                    messages=[{"role": "user", "content": "kept"}]) as g:
                g.output("kept")
            # A tracer that declares NOTHING follows the process default, even nested inside a
            # declared one — it must not inherit the enclosing run's opt-in.
            with plain.span("plain-root", new_trace=True):
                with tracing.generation(op="plain", model="m",
                                        messages=[{"role": "user", "content": "dropped"}]) as g:
                    g.output("dropped")
        assert "kept" in (tmp_path / "on2.jsonl").read_text(encoding="utf-8")
        assert "dropped" not in (tmp_path / "plain.jsonl").read_text(encoding="utf-8")
        # Leaving the inner span restores the enclosing run's policy rather than the process default.
        on2_recs = [orjson.loads(line)
                    for line in (tmp_path / "on2.jsonl").read_text(encoding="utf-8").splitlines()]
        assert next(r for r in on2_recs if r["kind"] == "generation")["attributes"]["output"]
    finally:
        tracing.set_llm_capture(saved)


def test_capture_policy_survives_concurrent_opposite_policy_runs(tmp_path):
    """The scoped policy is a contextvar, so two THREADS driving opposite-policy tracers stay
    independent — the failure mode the module global had was exactly this race."""
    import threading

    from looplab.core import tracing

    saved = tracing._CAPTURE_LLM_IO
    try:
        tracing.set_llm_capture(False)
        step = threading.Barrier(2)
        errors: list[BaseException] = []

        def drive(name: str, capture: bool) -> None:
            tracer = Tracer(JsonlSpanExporter(tmp_path / f"{name}.jsonl"), run_id=name,
                            capture_llm_io=capture)
            try:
                for i in range(20):
                    with tracer.span(f"{name}-root", new_trace=True):
                        step.wait(timeout=10)      # both threads inside a span at once
                        with tracing.generation(op=name, model="m",
                                                messages=[{"role": "user",
                                                           "content": f"{name}-body-{i}"}]) as g:
                            g.output(f"{name}-out-{i}")
                        step.wait(timeout=10)      # ...and both still inside while attaching
            except BaseException as exc:           # noqa: BLE001 - surface on the main thread
                errors.append(exc)
                step.abort()

        threads = [threading.Thread(target=drive, args=("yes", True)),
                   threading.Thread(target=drive, args=("no", False))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not errors, errors
        assert not any(t.is_alive() for t in threads)

        yes_text = (tmp_path / "yes.jsonl").read_text(encoding="utf-8")
        no_text = (tmp_path / "no.jsonl").read_text(encoding="utf-8")
        assert yes_text.count("yes-body-") == 20 and yes_text.count("yes-out-") == 20
        assert "no-body-" not in no_text and "no-out-" not in no_text
    finally:
        tracing.set_llm_capture(saved)


def test_capture_policy_follows_its_run_across_a_worker_thread_hop(tmp_path):
    """The engine builds/evaluates in `anyio.to_thread` workers, which get a COPY of the parent
    context — so the run's capture policy must travel with it, in both directions."""
    from looplab.core import tracing

    def worker() -> None:
        with tracing.generation(op="in-thread", model="m",
                                messages=[{"role": "user", "content": "PAYLOAD"}]) as g:
            g.output("OUT")

    async def drive(tracer) -> None:
        with tracer.span("root", new_trace=True, node_id=1):
            await anyio.to_thread.run_sync(worker)

    saved = tracing._CAPTURE_LLM_IO
    try:
        # Opted IN while the process default is OFF: the worker must still capture.
        tracing.set_llm_capture(False)
        on = Tracer(JsonlSpanExporter(tmp_path / "on.jsonl"), run_id="on", capture_llm_io=True)
        anyio.run(drive, on)
        assert "PAYLOAD" in (tmp_path / "on.jsonl").read_text(encoding="utf-8")

        # Opted OUT while the process default is ON: the worker must stay silent.
        tracing.set_llm_capture(True)
        off = Tracer(JsonlSpanExporter(tmp_path / "off.jsonl"), run_id="off", capture_llm_io=False)
        anyio.run(drive, off)
        off_text = (tmp_path / "off.jsonl").read_text(encoding="utf-8")
        assert "PAYLOAD" not in off_text and "OUT" not in off_text
    finally:
        tracing.set_llm_capture(saved)


def test_a_non_numeric_provider_token_count_cannot_take_down_the_traced_call():
    """Tracing must never perturb the operation it observes. A provider is free to report "n/a" (or
    None, or a float) where the schema says a number, and a bare `int()` on that raised straight out
    of the tracer — taking the traced call with it."""
    from looplab.core.tracing import _norm_usage, _token_int

    assert _token_int("n/a") == 0 and _token_int(None) == 0
    assert _token_int(12.7) == 12 and _token_int(-5) == 0      # no negative token counts
    assert _norm_usage({"prompt_tokens": "n/a", "completion_tokens": 3}) == {
        "prompt": 0, "completion": 3, "total": 3}
    assert _norm_usage({"prompt": 2, "completion": 2, "total": "?"}) == {
        "prompt": 2, "completion": 2, "total": 0}


def test_a_pathologically_deep_span_chain_still_renders_a_tree(tmp_path):
    """`traceview._tree` was made ITERATIVE because a crafted or corrupt spans.jsonl can carry a
    pathologically deep parent chain. The HTML renderer still recurses, so without a cap it raised
    RecursionError — and finalize swallows that, losing the ENTIRE tree.html rather than one deep
    branch, which is the opposite of the 'tolerate corrupt spans' contract."""
    from looplab.events.htmlview import _MAX_SPAN_DEPTH, _span_forest_html

    root = {"name": "root", "children": []}
    cursor = root
    for i in range(_MAX_SPAN_DEPTH * 10):
        child = {"name": f"n{i}", "children": []}
        cursor["children"].append(child)
        cursor = child

    out = _span_forest_html([root])          # no RecursionError
    assert out.startswith("<ul") and "root" in out
    assert f"deeper than {_MAX_SPAN_DEPTH} levels" in out    # and it SAYS it stopped
    assert "<li>" in out

    shallow = _span_forest_html([{"name": "a", "children": [{"name": "b", "children": []}]}])
    assert "deeper than" not in shallow      # a real trace is untouched
