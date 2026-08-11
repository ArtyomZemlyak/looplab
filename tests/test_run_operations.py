"""The run's ROOT operations listing — the surface on which a RUN-LEVEL agent is reachable at all.

The defect this guards (measured 2026-08-11 on `runs/rubert-dr-0807`, 7,289 spans): the Researcher's
`propose` span is a ROOT with no `node_id` — the idea exists before the node built from it — so it
can appear on no per-node surface, and `build_trace_view`'s `unscoped` bucket keeps only a
`TRACE_VIEW_SPAN_CAP` TAIL, which left 3 of that run's 15 proposals visible. 15 operations holding
493 generations and 944 tool calls were unreachable from the product.

Driven against a real span log written by the real Tracer, and asserted on the projection's OUTPUT —
not on the source text — because the property is "the Researcher's operations all come back", which
a re-typed guard could not distinguish from "the tail happened to include them".
"""
from __future__ import annotations

from pathlib import Path

import pytest

from looplab.core.tracing import JsonlSpanExporter, Tracer
from looplab.events.traceview import OPERATIONS_CAP, TRACE_VIEW_SPAN_CAP, project_operations


def _write_run(tmp_path: Path, *, proposals: int = 3, spans_per_proposal: int = 6) -> Path:
    """A run whose Researcher work is root-level and node-less, exactly as the engine records it."""
    sp = tmp_path / "spans.jsonl"
    tr = Tracer(exporter=JsonlSpanExporter(sp), run_id="r1")
    for _ in range(proposals):
        with tr.span("propose", new_trace=True):
            for turn in range(spans_per_proposal):
                with tr.span("generation", kind="generation",
                             usage={"prompt": 10, "completion": 2, "total": 12}):
                    pass
                with tr.span("tool", kind="tool", tool=f"read_{turn}"):
                    pass
    # …and one node-scoped operation, so "run-level" is a distinction and not the only case.
    with tr.span("evaluate", new_trace=True, node_id=4):
        with tr.span("generation", kind="generation"):
            pass
    return sp


def _spans(sp: Path) -> list[dict]:
    from looplab.events.traceview import load_spans
    return load_spans(sp)


def test_every_researcher_operation_is_listed_not_just_the_tail(tmp_path):
    # More spans than the run-level trace view would keep, so a tail-based projection MUST lose some.
    per = 8
    proposals = (TRACE_VIEW_SPAN_CAP // (per * 2)) + 4
    sp = _write_run(tmp_path, proposals=proposals, spans_per_proposal=per)
    spans = _spans(sp)
    assert len(spans) > TRACE_VIEW_SPAN_CAP, "the fixture must exceed the tail cap to be meaningful"

    out = project_operations(spans)
    listed = [o for o in out["operations"] if o["name"] == "propose"]
    assert len(listed) == proposals, "a tail-shaped projection is what hid the Researcher"
    assert out["projection"]["omitted_operations"] == 0
    assert out["projection"]["total_operations"] == proposals + 1


def test_a_run_level_operation_reports_node_id_None_and_keeps_its_counts(tmp_path):
    sp = _write_run(tmp_path, proposals=2, spans_per_proposal=5)
    out = project_operations(_spans(sp))
    proposals = [o for o in out["operations"] if o["name"] == "propose"]
    assert proposals and all(o["node_id"] is None for o in proposals), (
        "run-level is a FACT about the operation; the UI groups on it")
    for o in proposals:
        assert o["generations"] == 5 and o["tools"] == 5
        assert o["spans"] == 11          # root + 5 generations + 5 tools
        assert o["tokens"]["total"] == 60
        assert o["trace_id"] and o["span_id"]

    evaluate = [o for o in out["operations"] if o["name"] == "evaluate"]
    assert len(evaluate) == 1 and evaluate[0]["node_id"] == 4, (
        "node-scoped operations must still report their node")


def test_operations_are_newest_first(tmp_path):
    sp = _write_run(tmp_path, proposals=4, spans_per_proposal=2)
    out = project_operations(_spans(sp))
    starts = [o["start"] for o in out["operations"]]
    assert starts == sorted(starts, reverse=True)


def test_the_cap_is_a_receipt_not_a_silent_truncation(tmp_path):
    sp = _write_run(tmp_path, proposals=6, spans_per_proposal=2)
    out = project_operations(_spans(sp), cap=3)
    assert len(out["operations"]) == 3
    assert out["projection"]["visible_operations"] == 3
    assert out["projection"]["omitted_operations"] == 4      # 6 proposals + 1 evaluate - 3
    assert out["projection"]["truncated"] is True


def test_a_trace_with_two_roots_lists_both_and_counts_the_work_once(tmp_path):
    """Partial/corrupt logs produce an orphan beside a real root. Dropping the extra root is how an
    operation goes missing — the defect this whole module exists to fix — but counting the trace
    twice would make one operation read as two full ones in any UI that sums rows."""
    spans = [
        {"name": "propose", "kind": "operation", "trace_id": "t", "span_id": "a",
         "parent_id": None, "start": 1.0, "duration_s": 1.0, "status": "OK", "attributes": {}},
        {"name": "orphan", "kind": "operation", "trace_id": "t", "span_id": "b",
         "parent_id": None, "start": 2.0, "duration_s": 1.0, "status": "OK", "attributes": {}},
        {"name": "generation", "kind": "generation", "trace_id": "t", "span_id": "c",
         "parent_id": "a", "start": 1.5, "duration_s": 0.1, "status": "OK",
         "attributes": {"usage": {"prompt": 3, "completion": 1, "total": 4}}},
    ]
    out = project_operations(spans)
    names = sorted(o["name"] for o in out["operations"])
    assert names == ["orphan", "propose"], "both roots are listed"
    assert sum(o["generations"] for o in out["operations"]) == 1, "counted once, by the first root"
    assert all(o["trace_roots"] == 2 for o in out["operations"])


def test_an_empty_or_unreadable_run_returns_an_empty_listing_not_an_error():
    out = project_operations([])
    assert out["operations"] == []
    assert out["projection"]["total_operations"] == 0
    assert out["projection"]["omitted_operations"] == 0


# --------------------------------------------------------------------------- the HTTP contract
@pytest.fixture()
def client(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from looplab.serve.server import make_app
    root = tmp_path / "runs"
    (root / "r1").mkdir(parents=True)
    (root / "r1" / "events.jsonl").write_text("")     # a run dir is identified by its event log
    _write_run(root / "r1", proposals=3, spans_per_proposal=4)
    return TestClient(make_app(root)), root


def test_route_lists_run_level_operations_and_states_its_limits(client):
    c, _ = client
    r = c.get("/api/runs/r1/operations")
    assert r.status_code == 200
    body = r.json()
    proposals = [o for o in body["operations"] if o["name"] == "propose"]
    assert len(proposals) == 3 and all(o["node_id"] is None for o in proposals)
    assert body["run_id"] == "r1"
    assert body["projection"]["omitted_operations"] == 0

    capped = c.get("/api/runs/r1/operations?limit=2").json()
    assert len(capped["operations"]) == 2
    assert capped["projection"]["omitted_operations"] == 2

    # `limit` may only ever LOWER the cap: a client cannot widen the listing past the module bound.
    wide = c.get(f"/api/runs/r1/operations?limit={OPERATIONS_CAP * 10}").json()
    assert len(wide["operations"]) == 4


def test_route_refuses_a_negative_limit_rather_than_reading_it_as_the_default(client):
    c, _ = client
    assert c.get("/api/runs/r1/operations?limit=-1").status_code == 422


def test_route_degrades_on_a_malformed_span_log_instead_of_500(client, tmp_path):
    c, root = client
    (root / "r1" / "spans.jsonl").write_bytes(b"{not json at all\n\x00\xff")
    from looplab.events import span_index
    span_index.invalidate(root / "r1" / "spans.jsonl")
    r = c.get("/api/runs/r1/operations")
    assert r.status_code == 200
    assert r.json()["operations"] == []
