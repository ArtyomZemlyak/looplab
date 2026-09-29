"""The concept frame the server EMITS is the frame the browser ACCEPTS.

`ui/src/ConceptView.jsx::validateConceptPayload` refuses, whole, any frame whose receipts disagree,
and every UI test fed it HAND-BUILT frames. Those frames never carried a derived edge, so when the
server began counting the co_occurs edges it derives from the membership snapshot in
`completeness.included.edges` (and only the recorded ones in `completeness.source.edges`), the
browser's `source.edges >= included.edges` receipt refused every run where two concepts share two
experiments — 108 of 120 generated frames — and the operator read "Concept projection unavailable.
Run unchanged; retry when reachable." over a healthy server. A retry read the same bytes.

The rows below are REAL server frames, shipped to `tests/fixtures/concept_frame_cases.json`;
`ui/test/conceptFrameContract.test.js` validates every one through the browser half.
`test_the_shared_fixture_is_what_the_server_emits` refuses a stale file; to regenerate it:

    PYTHONPATH=. python tests/test_concept_frame_contract.py
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from looplab.events.eventstore import EventStore
from looplab.serve.server import make_app

FIXTURE = Path(__file__).parent / "fixtures" / "concept_frame_cases.json"
# The run generation digests the log file's identity, which differs per temp dir; the browser only
# needs a well-formed one, so every row carries this fixed value.
FIXED_GENERATION = "c" * 64


def _node(store, node_id, concepts, metric, parents=()):
    store.append("node_created", {
        "node_id": node_id, "parent_ids": list(parents), "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "rationale": "r", "concepts": concepts},
    })
    store.append("node_evaluated", {"node_id": node_id, "metric": metric})


def _shared_pair(store):
    """Two concepts co-tagged on two experiments: the projection derives one co_occurs edge."""
    _node(store, 0, ["pair/x", "pair/y"], 1.0)
    _node(store, 1, ["pair/x", "pair/y", "solo/z"], 2.0, parents=[0])


def _recorded_and_derived(store):
    _shared_pair(store)
    store.append("concept_edge", {"edges": [{
        "src": "pair/x", "rel": "uses", "dst": "solo/z",
        "confidence": 0.8, "provenance": "asserted"}]})


# (name, writer, query): every row is a frame the server answers 200 to.
CASES = [
    ("a derived co_occurs edge only, on its own lens", _shared_pair, {"lens": "co_occurs"}),
    ("a derived co_occurs edge only, on the default hierarchy", _shared_pair, {}),
    ("a recorded uses edge beside a derived one, on the uses lens",
     _recorded_and_derived, {"lens": "uses"}),
    ("a recorded uses edge beside a derived one, on the co_occurs lens",
     _recorded_and_derived, {"lens": "co_occurs"}),
    ("a historical frame read before the last experiment was scored", _shared_pair,
     {"lens": "co_occurs", "seq": 3}),
]


def emitted_cases() -> list[dict]:
    rows = []
    for name, writer, query in CASES:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "demo").mkdir()
            store = EventStore(root / "demo" / "events.jsonl")
            store.append("run_started", {
                "run_id": "demo", "task_id": "toy", "goal": "g", "direction": "max"})
            writer(store)
            response = TestClient(make_app(root)).get("/api/runs/demo/concepts", params=query)
            assert response.status_code == 200, (name, response.text)
            frame = response.json()
        assert isinstance(frame.get("generation"), str)
        frame["generation"] = FIXED_GENERATION
        rows.append({"name": name, "expected": {
            "runId": "demo", "requestedLens": query.get("lens", "is_a"),
            "requestedSeq": query.get("seq")}, "frame": frame})
    return rows


def test_the_fixture_carries_the_receipt_the_browser_once_refused():
    rows = emitted_cases()
    derived = [row for row in rows
               if row["frame"]["completeness"]["included"]["derived_edges"] > 0]
    # the defect's own shape: more edges included than the log recorded, the surplus derived
    assert any(row["frame"]["completeness"]["included"]["edges"]
               > row["frame"]["completeness"]["source"]["edges"] for row in derived)
    assert any(row["frame"]["completeness"]["source"]["edges"] > 0 for row in derived)
    for row in rows:
        included = row["frame"]["completeness"]["included"]
        source = row["frame"]["completeness"]["source"]
        assert included["derived_edges"] <= included["edges"]
        assert included["edges"] - included["derived_edges"] <= source["edges"]


def test_the_shared_fixture_is_what_the_server_emits():
    assert json.loads(FIXTURE.read_text(encoding="utf-8")) == emitted_cases(), (
        "tests/fixtures/concept_frame_cases.json is stale: "
        "PYTHONPATH=. python tests/test_concept_frame_contract.py")


if __name__ == "__main__":
    FIXTURE.write_text(json.dumps(emitted_cases(), indent=1, sort_keys=True) + "\n",
                       encoding="utf-8")
