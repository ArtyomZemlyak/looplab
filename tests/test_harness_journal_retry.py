"""Lost acknowledgements restore original receipts, never refresh evidence gates."""
import json

import pytest

from looplab.events.run_generation import run_generation_token
from tests.test_external_progress import _run


def test_canonical_comparison_keeps_json_types_and_every_non_context_field():
    from looplab.harness.journals import same_receipt_request

    original = {"at_node": 1, "evidence_revision": "old", "options_considered": 1, "run_uid": "one"}
    assert same_receipt_request(original, {**original, "at_node": 2, "evidence_revision": "new"})
    assert not same_receipt_request(original, {**original, "options_considered": True})
    assert not same_receipt_request(original, {**original, "run_uid": "other"})


def _body(kind, gen):
    common = {"expected_generation": gen, "phase_id": "lessons" if kind == "reviews" else "novelty",
              "action_id": "original", "reason": "No independent evidence supports a reusable conclusion."}
    return {**common, **({"decision": "no_applicable_action", "evidence": [0, 1]} if kind == "reviews" else
                         {"decision": "submit", "idea": {"operator": "draft"}})}


@pytest.mark.parametrize("kind", ["reviews", "decisions"])
@pytest.mark.parametrize("change", ["new_node", "reset", "tombstone"])
def test_exact_retry_after_evidence_changes_restores_old_receipt_without_new_work(tmp_path, kind, change):
    rd, store, client = _run(tmp_path)
    gen = run_generation_token(store.read_all())
    body = _body(kind, gen)
    path = "/api/runs/demo/harness-" + kind
    first = client.post(path, json=body)
    assert first.status_code == 200, first.text
    saved = first.json()["review" if kind == "reviews" else "decision"]
    if change == "new_node":
        store.append("node_created", {"node_id": 2, "operator": "draft", "idea": {"operator": "draft"}})
        store.append("node_failed", {"node_id": 2, "reason": "crash", "error": "fixture failure"})
    elif change == "reset":
        store.append("node_reset", {"node_id": 0, "from_stage": "eval"})
        store.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": .5})
    else:
        store.append("node_tombstoned", {"node_ids": [0]})
    before = (rd / "events.jsonl").read_bytes(), (rd / f"harness_{kind}.jsonl").read_bytes()
    reply = client.post(path, json=body)
    assert reply.status_code == 200, reply.text
    assert reply.json()["replayed"] and reply.json()["review" if kind == "reviews" else "decision"] == saved
    assert before == ((rd / "events.jsonl").read_bytes(), (rd / f"harness_{kind}.jsonl").read_bytes())
    progress = client.get("/api/runs/demo/harness-progress", params={"expected_generation": gen}).json()
    assert progress["history"][kind]["items"][0]["validity"] == "superseded"
    if kind == "reviews":
        assert "lessons" in progress["finish_reviews_due"]
    assert client.post(path, json={**body, "reason": "Changed content"}).status_code == 409
    if kind == "reviews" and change == "tombstone":
        assert client.post(path, json={**body, "action_id": "fresh"}).status_code == 409


@pytest.mark.parametrize("kind", ["reviews", "decisions"])
def test_legacy_journal_receipt_without_new_fields_replays_after_change(tmp_path, kind):
    # The fix must also apply to already saved receipts, not only future rows.
    rd, store, client = _run(tmp_path)
    gen = run_generation_token(store.read_all())
    body = _body(kind, gen)
    path = "/api/runs/demo/harness-" + kind
    assert client.post(path, json=body).status_code == 200
    row = json.loads((rd / f"harness_{kind}.jsonl").read_text())
    assert "request_sha256" not in row
    store.append("node_reset", {"node_id": 0})
    assert client.post(path, json=body).status_code == 200


@pytest.mark.parametrize("kind", ["reviews", "decisions"])
def test_replaced_generation_cannot_recover_an_old_receipt(tmp_path, kind):
    _, store, client = _run(tmp_path)
    body = _body(kind, run_generation_token(store.read_all()))
    path = "/api/runs/demo/harness-" + kind
    assert client.post(path, json=body).status_code == 200
    assert client.post(path, json={**body, "expected_generation": "b" * 64}).status_code == 409


@pytest.mark.parametrize("kind,change", [
    ("reviews", {"evidence": [1]}), ("reviews", {"action_ref": "other-action"}),
    ("reviews", {"phase_id": "skill_candidates"}),
    ("decisions", {"idea": {"operator": "draft", "params": {"x": 7}}}),
    ("decisions", {"alternatives": [{"operator": "alternative"}]}),
    ("decisions", {"decision": "reject"}), ("decisions", {"phase_id": "strategy"}),
])
def test_reused_action_rejects_different_authored_content_after_new_outcome(tmp_path, kind, change):
    _, store, client = _run(tmp_path)
    body = _body(kind, run_generation_token(store.read_all()))
    path = "/api/runs/demo/harness-" + kind
    assert client.post(path, json=body).status_code == 200
    store.append("node_reset", {"node_id": 0})
    assert client.post(path, json={**body, **change}).status_code == 409
