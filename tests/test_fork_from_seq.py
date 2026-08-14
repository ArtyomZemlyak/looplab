"""Fork-to-branch: the operator branches from a HISTORICAL snapshot with an edited idea.

The gesture is NOT a new control event. It is `inject_node` — already the one intent that carries an
operator-authored Idea, a parent and a parent-generation CAS — plus a validated `forked_from` receipt
naming what it was branched FROM and what changed. These tests drive the real HTTP surface against a
real, still-running run and then let the REAL engine serve the queued request, so what is asserted is
the node the operator actually got: its parent, its idea, and its lineage receipt.

The negative control is the point of the receipt: a fork formed against a snapshot the run has since
moved past must be REFUSED, never quietly folded against the live tail.
"""
from __future__ import annotations

from pathlib import Path

import anyio
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from looplab.adapters.toytask import ToyTask  # noqa: E402
from looplab.engine.orchestrator import Engine  # noqa: E402
from looplab.events.eventstore import EventStore  # noqa: E402
from looplab.events.replay import fold  # noqa: E402
from looplab.runtime.sandbox import SubprocessSandbox  # noqa: E402
from looplab.search.policy import GreedyTree  # noqa: E402
from looplab.serve.server import make_app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"

# The idea keys `ui/src/forkFromSeqModel.js::forkIdeaFromSnapshot` carries over from the snapshot the
# operator was reading. The concept ENVELOPE is deliberately not among them: a node whose durable idea
# authored no envelope serializes as `concepts: []` on the wire, and echoing that back makes
# `_normalize_inject_node` upgrade it to an explicit `concept_mode`, which the fold reads as a
# DIFFERENT (authoritative-empty) membership. The receipt would then truthfully report a change the
# operator never made. Mirrored here so both halves of the contract are driven by one rule.
_FORK_IDEA_KEYS = ("operator", "params", "rationale", "eval_profile", "eval_timeout", "space")


def _fork_idea(seen_idea: dict, **edits) -> dict:
    idea = {k: v for k, v in seen_idea.items() if k in _FORK_IDEA_KEYS and v is not None}
    idea.update(edits)
    return idea


def _engine(rd: Path, **kw) -> Engine:
    task = ToyTask.load(TASK)
    researcher, developer = task.build_roles()
    return Engine(rd, task=task, researcher=researcher, developer=developer,
                  sandbox=SubprocessSandbox(),
                  policy=GreedyTree(n_seeds=2, max_nodes=kw.pop("max_nodes", 4)),
                  require_approval=True, **kw)


def _live_run(root: Path, name: str = "demo") -> Path:
    """A run PAUSED awaiting approval — unfinished, exactly the state an operator steers."""
    rd = root / name
    state = anyio.run(_engine(rd).run)
    assert state.awaiting_approval and not state.finished
    return rd


def _resume(rd: Path):
    return anyio.run(_engine(rd, max_nodes=8).run)


def _fork_payload(node_id: int, generation: int, observed_seq: int, idea: dict) -> dict:
    """Exactly what the browser submits: the edited idea, the parent + its CAS, and the receipt."""
    return {
        "type": "inject_node",
        "data": {
            "idea": idea,
            "parent_id": node_id,
            "parent_generations": {str(node_id): generation},
            "forked_from": {
                "node_id": node_id, "generation": generation, "observed_seq": observed_seq,
            },
        },
    }


def _snapshot_before_the_tail(client, rd: Path):
    """Read a node from a HISTORICAL snapshot that is genuinely behind the live tail."""
    events = client.get("/api/runs/demo/log").json()
    live_seq = max(e["seq"] for e in events)
    creations = [e for e in events if e["type"] == "node_created"]
    assert creations, "the run must have built at least one node to branch from"
    view_seq = creations[0]["seq"] + 1
    assert view_seq < live_seq, "the snapshot must genuinely be behind the live tail"
    source_id = int(creations[0]["data"]["node_id"])
    seen = client.get(f"/api/runs/demo/state?seq={view_seq}").json()["state"]["nodes"][str(source_id)]
    return source_id, view_seq, seen


def test_fork_from_a_historical_seq_lands_with_its_lineage(tmp_path):
    """Drive the whole gesture: read a snapshot, edit its idea, branch, let the engine build it."""
    rd = _live_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    source_id, view_seq, seen = _snapshot_before_the_tail(client, rd)
    assert seen["attempt"] == 0

    # The operator EDITS the idea they can see and branches from where they are standing.
    edited = _fork_idea(seen["idea"],
                        rationale="operator: the parent's step is too coarse, halve it",
                        params={**(seen["idea"].get("params") or {}), "x": 0.125})
    client.post("/api/runs/demo/control", json={"type": "budget_extend", "data": {"add_nodes": 1}})
    posted = client.post("/api/runs/demo/control",
                         json=_fork_payload(source_id, seen["attempt"], view_seq, edited))
    assert posted.status_code == 200, posted.text

    # The durable request already carries the receipt, and its two derived fields are the SERVER's.
    queued = fold(EventStore(rd / "events.jsonl").read_all()).inject_requests[-1]
    receipt = queued["forked_from"]
    assert receipt["node_id"] == source_id and receipt["generation"] == 0
    assert receipt["observed_seq"] == view_seq
    # `card_id` rides along with the operator's own two edits and that is not noise: an inject has
    # never inherited the parent's Card, so this branch genuinely does not belong to the Card the
    # node it came from was built for. The receipt states the difference rather than hiding it.
    assert receipt["changed_fields"] == ["card_id", "params", "rationale"]
    assert receipt["base_idea_digest"].startswith("idea:v1:")

    # The REAL engine serves the queued request and builds the node.
    state = _resume(rd)
    forked = [n for n in state.nodes.values()
              if isinstance(n.forked_from, dict) and n.forked_from["node_id"] == source_id]
    assert len(forked) == 1, "exactly one node carries the operator's fork receipt"
    node = forked[0]

    # The lineage is what the operator asked for: parented on the node they branched from...
    assert node.parent_ids == [source_id]
    assert node.parent_generations == {str(source_id): 0}
    # ...carrying the idea they edited, not a re-proposal...
    assert node.idea.rationale == "operator: the parent's step is too coarse, halve it"
    assert node.idea.params["x"] == pytest.approx(0.125)
    # ...and saying, checkably, what it was forked FROM and what changed.
    assert node.forked_from["observed_seq"] == view_seq
    assert node.forked_from["changed_fields"] == ["card_id", "params", "rationale"]
    assert node.forked_from["base_idea_digest"] == receipt["base_idea_digest"]
    # The receipt's digest is CHECKABLE: re-derive it from the parent still in this same log.
    from looplab.core.models import idea_proposal_digest
    assert idea_proposal_digest(state.nodes[source_id].idea) == node.forked_from["base_idea_digest"]


def test_fork_against_a_stale_snapshot_is_refused_not_applied_to_the_live_tail(tmp_path):
    """The negative control: the run moved under the operator, so the branch must not land."""
    rd = _live_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    source_id, view_seq, seen = _snapshot_before_the_tail(client, rd)
    stale_generation = seen["attempt"]

    # The run moves: that very node is reset, so its lifecycle generation advances.
    EventStore(rd / "events.jsonl").append(
        "node_reset", {"node_id": source_id, "generation": stale_generation,
                       "from_stage": "eval"})
    assert fold(EventStore(rd / "events.jsonl").read_all()).nodes[source_id].attempt == 1

    before = len(fold(EventStore(rd / "events.jsonl").read_all()).inject_requests)
    refused = client.post("/api/runs/demo/control", json=_fork_payload(
        source_id, stale_generation, view_seq,
        _fork_idea(seen["idea"], rationale="branch from what I was looking at")))
    assert refused.status_code == 409
    assert f"stale parent #{source_id}" in refused.text
    # Nothing was folded against the live tail behind the operator's back.
    assert len(fold(EventStore(rd / "events.jsonl").read_all()).inject_requests) == before
    # ...and the engine builds no node for a request that was never queued.
    state = _resume(rd)
    assert not [n for n in state.nodes.values() if isinstance(n.forked_from, dict)]


def test_fork_receipt_must_name_a_parent_and_a_seq_the_run_reached(tmp_path):
    rd = _live_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    state = client.get("/api/runs/demo/state").json()["state"]
    ids = sorted(int(k) for k in state["nodes"])
    source_id, other_id = ids[0], ids[-1]
    assert source_id != other_id
    idea = _fork_idea(state["nodes"][str(source_id)]["idea"])

    # A receipt naming a node that is NOT this experiment's parent would claim a lineage the DAG
    # does not have.
    body = _fork_payload(source_id, 0, 1, idea)
    body["data"]["forked_from"]["node_id"] = other_id
    mismatch = client.post("/api/runs/demo/control", json=body)
    assert mismatch.status_code == 400
    assert "fork_parent_mismatch" in mismatch.text

    # A vantage point the run has never reached is not a vantage point.
    future = client.post("/api/runs/demo/control",
                         json=_fork_payload(source_id, 0, 10_000_000, idea))
    assert future.status_code == 400
    assert "fork_observed_seq_out_of_range" in future.text

    # The two derived fields are the server's answer, never the client's claim.
    for forged in ("changed_fields", "base_idea_digest"):
        body = _fork_payload(source_id, 0, 1, idea)
        body["data"]["forked_from"][forged] = [] if forged == "changed_fields" else "idea:v1:x"
        response = client.post("/api/runs/demo/control", json=body)
        assert response.status_code == 400
        assert "fork_receipt_forged" in response.text

    # An unknown field is refused rather than written straight through into the durable event.
    body = _fork_payload(source_id, 0, 1, idea)
    body["data"]["forked_from"]["note"] = "hello"
    assert client.post("/api/runs/demo/control", json=body).status_code == 400

    # A receipt missing one of its three authored fields is refused too — a partial receipt would
    # record a lineage claim nobody can check.
    body = _fork_payload(source_id, 0, 1, idea)
    body["data"]["forked_from"].pop("observed_seq")
    incomplete = client.post("/api/runs/demo/control", json=body)
    assert incomplete.status_code == 400 and "observed_seq" in incomplete.text

    # Nothing above was admitted.
    assert not fold(EventStore(rd / "events.jsonl").read_all()).inject_requests


def test_the_gesture_travels_the_generation_fenced_command_path_the_ui_actually_uses(tmp_path):
    """`CONTROL.forkFrom` submits through `/commands`, not the legacy `/control` route.

    The two paths share `normalize_control`, so the receipt is validated identically — but only this
    one carries an idempotency key and the exact run generation the operator observed, and it is the
    path the browser takes. A gesture proved only against `/control` would be proved on a route no
    first-party client uses. Validation is SYNCHRONOUS here even though the append is not: a 200
    `accepted` record is the proof the receipt passed, and a refusal never mints one.
    """
    rd = _live_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    source_id, view_seq, seen = _snapshot_before_the_tail(client, rd)
    generation = client.get("/api/runs/demo/state").json()["generation"]
    assert isinstance(generation, str) and len(generation) == 64
    edited = _fork_idea(seen["idea"], rationale="branch through the durable command lifecycle")

    def _submit(key: str, payload: dict):
        return client.post("/api/runs/demo/commands", headers={"Idempotency-Key": key},
                           json={"type": "inject_node", "data": payload,
                                 "expected_generation": generation})

    payload = _fork_payload(source_id, seen["attempt"], view_seq, edited)["data"]
    accepted = _submit("fork-1", payload)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] in {"accepted", "executing", "succeeded", "noop"}
    # The idempotency key is what stops a lost response from buying the experiment twice.
    assert _submit("fork-1", payload).json()["id"] == accepted.json()["id"]

    # The same receipt is refused on this path too once the parent's lifecycle moves — and the
    # refusal is SYNCHRONOUS, so no durable command record is minted for it.
    EventStore(rd / "events.jsonl").append(
        "node_reset", {"node_id": source_id, "generation": seen["attempt"],
                       "from_stage": "eval"})
    moved = _submit("fork-2", payload)
    # The durable path does NOT answer 409: it mints a REJECTED record carrying the refusal, which
    # is the shape `ui/src/forkFromSeqModel.js::classifyForkFailure` has to read to tell "the run
    # moved under you" apart from "the outcome is unknown". Pinned here because it is the contract
    # the browser half depends on and no HTTP status carries it.
    assert moved.status_code == 200, moved.text
    record = moved.json()
    assert record["status"] == "rejected"
    assert record["error"]["code"] == "command_target_not_found"
    assert record["error"]["message"] == f"stale parent #{source_id}: current generation is 1"
    assert record["error"]["retryable"] is False
    assert record["id"] != accepted.json()["id"]


def test_the_receipt_reports_an_envelope_the_client_manufactured(tmp_path):
    """`changed_fields` is a fact about two durable ideas, not a claim about a browser.

    Echoing the wire projection's `concepts: []` back makes the intake upgrade the idea to an explicit
    `concept_mode`, which the fold reads as an authoritative-empty membership rather than an absent
    one. That IS a difference from the parent, and the receipt says so — which is why
    `forkIdeaFromSnapshot` does not carry the envelope over in the first place.
    """
    rd = _live_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    source_id, view_seq, seen = _snapshot_before_the_tail(client, rd)
    echoed = {k: v for k, v in seen["idea"].items() if v is not None}
    echoed["rationale"] = "echo the whole projection back"
    posted = client.post("/api/runs/demo/control",
                         json=_fork_payload(source_id, seen["attempt"], view_seq, echoed))
    assert posted.status_code == 200, posted.text
    receipt = fold(EventStore(rd / "events.jsonl").read_all()).inject_requests[-1]["forked_from"]
    assert "concept_mode" in receipt["changed_fields"]


def test_inject_without_a_fork_receipt_is_unchanged(tmp_path):
    """The receipt is OPTIONAL: an ordinary inject must not gain a key it never had."""
    rd = _live_run(tmp_path)
    client = TestClient(make_app(tmp_path))
    state = client.get("/api/runs/demo/state").json()["state"]
    parent_id = sorted(int(k) for k in state["nodes"])[0]
    client.post("/api/runs/demo/control", json={"type": "budget_extend", "data": {"add_nodes": 2}})
    posted = client.post("/api/runs/demo/control", json={
        "type": "inject_node",
        "data": {"idea": {"operator": "manual", "params": {"x": 0.5}},
                 "parent_id": parent_id, "parent_generations": {str(parent_id): 0}},
    })
    assert posted.status_code == 200, posted.text
    queued = fold(EventStore(rd / "events.jsonl").read_all()).inject_requests[-1]
    assert "forked_from" not in queued

    # An EXPLICIT null is absent, not a receipt with a null value: the durable row must not look
    # like a branch while carrying no lineage.
    nulled = client.post("/api/runs/demo/control", json={
        "type": "inject_node",
        "data": {"idea": {"operator": "manual", "params": {"x": 0.75}}, "forked_from": None,
                 "parent_id": parent_id, "parent_generations": {str(parent_id): 0}},
    })
    assert nulled.status_code == 200, nulled.text
    assert "forked_from" not in fold(
        EventStore(rd / "events.jsonl").read_all()).inject_requests[-1]

    folded = _resume(rd)
    assert folded.injects_done == 2, "both ordinary injects still materialize"
    created = [e for e in EventStore(rd / "events.jsonl").read_all() if e.type == "node_created"]
    assert created and all("forked_from" not in e.data for e in created)
    assert all(n.forked_from is None for n in folded.nodes.values())
