"""ONE operation's trace, read the way a node's trace is read.

The operator's report: the Researcher's reasoning rendered as a raw span tree — in the card AND in
the node's research disclosure — with the view switcher beside it doing nothing, while the identical
surface gave a node both a conversation and a span tree. The reason was structural: `build_conversation`
selects spans BY NODE, and a proposal's spans carry no node_id at all (measured 2026-08-12 on
`runs/rubertlite-dr-unified-v5` card-0: 252 spans, 86 generations, 165 tool calls, `node_id=None` on
every one), so no node conversation could ever contain them.

These drive the two halves of the fix: the shared band/thread reading now reachable per TRACE, and
the routes that serve it — including the window the proposal actually needs, since a real proposal
exceeds this route's 256-span default.
"""
from __future__ import annotations

import orjson
import pytest

from looplab.events.eventstore import EventStore
from looplab.events.span_index import invalidate
from looplab.events.traceview import (
    TRACE_CONVERSATION_SPAN_CAP,
    build_conversation,
    build_trace_conversation,
)
from looplab.core.models import RunState

ST = RunState(run_id="demo", task_id="task")

PROPOSE_TRACE = "proposetrace0001"
BUILD_TRACE = "buildtrace000001"


def _generations(turns) -> int:
    """Count the model's own turns. The thread also carries a `request` turn per sub-loop; these
    synthetic spans share no message history, so each one opens its own request — a fixture artefact
    that says nothing about the reading under test."""
    return sum(1 for turn in turns if turn.get("type") == "generation")


def _span(*, trace_id, span_id, parent_id=None, kind="generation", name=None, start=0.0,
          attributes=None):
    return {
        "name": name or kind, "kind": kind, "trace_id": trace_id, "span_id": span_id,
        "parent_id": parent_id, "run_id": "demo", "attributes": attributes or {},
        "events": [], "status": "OK", "start": start, "duration_s": 0.1,
    }


def _corpus(propose_turns: int = 4):
    """A Researcher proposal (no node_id anywhere) beside a Developer build for node 0."""
    spans = [_span(trace_id=PROPOSE_TRACE, span_id="propose-root", kind="operation",
                   name="propose", start=0.0, attributes={"card_id": "card-0"})]
    for index in range(propose_turns):
        spans.append(_span(
            trace_id=PROPOSE_TRACE, span_id=f"propose-gen-{index}", parent_id="propose-root",
            start=1.0 + index,
            attributes={"phase": "propose", "phase_span": "propose-root",
                        "output": f"idea {index}"}))
    spans.append(_span(trace_id=BUILD_TRACE, span_id="build-root", kind="operation",
                       name="create_node", start=10.0, attributes={"node_id": 0}))
    spans.append(_span(trace_id=BUILD_TRACE, span_id="build-gen", parent_id="build-root",
                       start=11.0,
                       attributes={"node_id": 0, "phase": "implement", "phase_span": "build-root",
                                   "output": "the code"}))
    return spans


def test_a_proposal_is_readable_as_a_conversation_and_a_node_one_can_never_contain_it():
    spans = _corpus()
    # The premise, stated rather than assumed: no node conversation reaches the proposal. This is
    # what makes the trace-scoped reading necessary rather than merely convenient.
    node_view = build_conversation(ST, spans, 0)
    assert [stage["trace_id"] for stage in node_view["stages"]] == [BUILD_TRACE]

    conversation = build_trace_conversation(ST, spans, PROPOSE_TRACE)
    assert conversation["trace_id"] == PROPOSE_TRACE
    assert [stage["label"] for stage in conversation["stages"]] == ["propose"]
    turns = conversation["stages"][0]["turns"]
    assert _generations(turns) == 4, "every generation in the proposal is a turn"
    assert conversation["projection"]["omitted_spans"] == 0


def test_a_trace_conversation_never_borrows_another_operation_s_spans():
    """Handed the whole run, it still reads ONE operation. A conversation that widened to the run
    would put the Developer's code under the Researcher's proposal — the same mis-attribution the
    card's research matching refuses to make from timing."""
    conversation = build_trace_conversation(ST, _corpus(), PROPOSE_TRACE)
    assert {stage["trace_id"] for stage in conversation["stages"]} == {PROPOSE_TRACE}
    assert not any("the code" in orjson.dumps(stage).decode() for stage in conversation["stages"])


def test_the_window_widens_the_reading_the_way_the_node_twin_does():
    """A bigger window must actually buy turns — the property a pager exists for."""
    spans = _corpus(propose_turns=40)
    narrow = build_trace_conversation(ST, spans, PROPOSE_TRACE, span_cap=8)
    wide = build_trace_conversation(ST, spans, PROPOSE_TRACE, span_cap=512)
    narrow_turns = sum(_generations(stage["turns"]) for stage in narrow["stages"])
    wide_turns = sum(_generations(stage["turns"]) for stage in wide["stages"])
    assert narrow_turns < wide_turns == 40
    assert narrow["projection"]["omitted_spans"] > 0
    assert wide["projection"]["omitted_spans"] == 0


def _run(tmp_path, spans):
    run_dir = tmp_path / "demo"
    run_dir.mkdir()
    store = EventStore(run_dir / "events.jsonl")
    store.append("run_started", {
        "run_id": "demo", "task_id": "task", "goal": "goal", "direction": "min"})
    store.append("node_created", {
        "node_id": 0, "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "rationale": "", "card_id": "card-0"},
    })
    span_path = run_dir / "spans.jsonl"
    span_path.write_bytes(b"".join(orjson.dumps(span) + b"\n" for span in spans))
    invalidate(span_path)
    return run_dir


@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient

    from looplab.serve.server import make_app

    _run(tmp_path, _corpus(propose_turns=6))
    return TestClient(make_app(tmp_path))


def test_the_route_serves_the_proposal_s_conversation_and_names_its_subject(client):
    response = client.get("/api/runs/demo/trace/by_trace/%s/conversation" % PROPOSE_TRACE)
    assert response.status_code == 200
    body = response.json()
    # The echo is the FENCE: the browser renders a trace payload only for the trace it asked for,
    # exactly as it fences a node payload on node_id/attempt.
    assert body["trace_id"] == PROPOSE_TRACE
    assert [stage["label"] for stage in body["stages"]] == ["propose"]
    assert sum(_generations(stage["turns"]) for stage in body["stages"]) == 6


def test_the_span_tree_route_names_its_subject_too(client):
    body = client.get("/api/runs/demo/trace/by_trace/%s" % PROPOSE_TRACE).json()
    assert body["trace_id"] == PROPOSE_TRACE
    assert body["spans"], "the span tree is still served"


def test_an_unreadable_trace_still_names_the_subject_it_failed_to_read(client):
    """An unavailable receipt that could not be fenced would render under ANOTHER trace's heading."""
    for suffix in ("", "/conversation"):
        body = client.get("/api/runs/demo/trace/by_trace/no-such-trace%s" % suffix).json()
        assert body["trace_id"] == "no-such-trace"
        assert not body.get("stages") and not body.get("spans")


def test_both_by_trace_routes_page_and_refuse_a_negative_window(tmp_path):
    """The default cap really binds on a real proposal (252/272 spans measured), so the shared
    surface's reach-for-earlier-steps has to work here or the operator meets a wall the identical
    surface lifts for a node."""
    from fastapi.testclient import TestClient

    from looplab.serve.server import make_app

    # Past BOTH defaults: the span tree's 256 and the conversation's 512. The conversation half used
    # to bind at 296 spans because a flat 256-turn RENDER cap cut a window the server had already
    # read; since 2026-08-13 the render caps are the window's own arithmetic bound
    # (`conversation_render_caps`), so what a widen buys is exactly the spans it newly reads — which
    # is the property this test was always about, now stated where it is actually true.
    big = TRACE_CONVERSATION_SPAN_CAP + 40
    _run(tmp_path, _corpus(propose_turns=big))
    client = TestClient(make_app(tmp_path))

    default = client.get("/api/runs/demo/trace/by_trace/%s" % PROPOSE_TRACE).json()
    assert default["projection"]["omitted_spans"] > 0
    widened = client.get(
        "/api/runs/demo/trace/by_trace/%s?limit=1024" % PROPOSE_TRACE).json()
    assert widened["projection"]["visible_spans"] > default["projection"]["visible_spans"]
    assert widened["projection"]["omitted_spans"] == 0

    narrow = client.get(
        "/api/runs/demo/trace/by_trace/%s/conversation" % PROPOSE_TRACE).json()
    narrow_turns = sum(_generations(stage["turns"]) for stage in narrow["stages"])
    wide = client.get(
        "/api/runs/demo/trace/by_trace/%s/conversation?limit=2048" % PROPOSE_TRACE).json()
    wide_turns = sum(_generations(stage["turns"]) for stage in wide["stages"])
    assert narrow["projection"]["omitted_spans"] > 0, "the span window is what withholds turns"
    assert wide_turns > narrow_turns
    # And the wider read withholds NOTHING it read: every turn it derived is rendered.
    assert wide["projection"]["omitted_turns"] == 0
    assert wide["projection"]["omitted_stages"] == 0

    # A client defect must fail loudly, the same wire contract as both node twins.
    for suffix in ("", "/conversation"):
        assert client.get(
            "/api/runs/demo/trace/by_trace/%s%s?limit=-1" % (PROPOSE_TRACE, suffix)
        ).status_code == 422
