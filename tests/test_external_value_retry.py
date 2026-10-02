"""MCTS value acknowledgements retain their original authored evidence revision."""
import pytest

from tests.test_external_selection import _session


@pytest.mark.parametrize("change", ["none", "new_node", "reset", "tombstone", "retarget", "strategy"])
def test_value_retry_requires_original_revision_after_state_changes(tmp_path, change):
    rd, store, client, gen = _session(tmp_path, policy="mcts", mcts_value_weight=.4)
    endpoint = "/api/runs/demo/harness-selection"
    if change == "retarget":
        for nid in (0, 1):
            store.append("node_evaluated", {"node_id": nid, "metric": .8,
                                           "extra_metrics": {"other": .6}})
    observed = client.get(endpoint, params={"expected_generation": gen}).json()
    body = {"expected_generation": gen, "action_id": "original-values",
        "expected_evidence_revision": observed["evidence_revision"],
        "estimates": [{"node_id": n["node_id"], "generation": n["generation"],
            "value": .7, "rationale": "Fixture belief; not a measured score"}
            for n in observed["value_candidates"]]}
    first = client.post(endpoint + "/values", json=body)
    assert first.status_code == 200 and first.json()["count"] == 2, first.text
    if change == "new_node":
        store.append("node_created", {"node_id": 2, "operator": "draft", "idea": {"operator": "draft"}})
        store.append("node_failed", {"node_id": 2, "error": "fixture"})
    elif change == "reset":
        store.append("node_reset", {"node_id": 0, "from_stage": "eval"})
        store.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": .8})
    elif change == "tombstone":
        store.append("node_tombstoned", {"node_ids": [0]})
    elif change == "retarget":
        store.append("metric_retarget", {"key": "other", "direction": "max"})
        from looplab.events.replay import fold
        assert fold(store.read_all()).objective_key == "other"
    elif change == "strategy":
        store.append("strategy_decision", {"strategy": {"policy": "greedy"}, "at_node": 2})
    before = (rd / "events.jsonl").read_bytes()
    original = client.post(endpoint + "/values", json=body)
    assert original.status_code == 200 and original.json()["replayed"], original.text
    changed = client.post(endpoint + "/values", json={**body, "expected_evidence_revision": "f" * 64})
    assert changed.status_code == 409, changed.text
    modified = [{**body["estimates"][0], "value": .2}, body["estimates"][1]]
    assert client.post(endpoint + "/values", json={**body, "estimates": modified}).status_code == 409
    assert client.post(endpoint + "/values", json={**body, "expected_generation": "e" * 64}).status_code == 409
    assert (rd / "events.jsonl").read_bytes() == before
    if change == "reset":
        live = client.get(endpoint, params={"expected_generation": gen}).json()
        assert live["value_candidates"] == [{"node_id": 0, "generation": 1, "metric": .8}]
        # Updating revision under the old action is also not a retry/approval.
        assert client.post(endpoint + "/values", json={**body,
            "expected_evidence_revision": live["evidence_revision"]}).status_code == 409


def test_multiple_value_batches_keep_their_own_original_prefix_and_need_fresh_reset_review(tmp_path):
    rd, store, client, gen = _session(tmp_path, policy="mcts", mcts_value_weight=.4)
    endpoint = "/api/runs/demo/harness-selection"
    bodies = []
    for batch in (0, 1):
        live = client.get(endpoint, params={"expected_generation": gen}).json()
        body = {"expected_generation": gen, "action_id": f"batch-{batch}",
            "expected_evidence_revision": live["evidence_revision"],
            "estimates": [{"node_id": n["node_id"], "generation": n["generation"],
                "value": .5, "rationale": "Fixture branch belief"} for n in live["value_candidates"]]}
        reply = client.post(endpoint + "/values", json=body)
        assert reply.status_code == 200 and reply.json()["count"] == (2 if batch == 0 else 1), reply.text
        bodies.append(body)
        if batch == 0:
            store.append("node_reset", {"node_id": 0, "from_stage": "eval"})
            store.append("node_evaluated", {"node_id": 0, "generation": 1, "metric": .9})
    assert not client.get(endpoint, params={"expected_generation": gen}).json()["value_candidates"]
    before = (rd / "events.jsonl").read_bytes()
    for body in bodies:
        assert client.post(endpoint + "/values", json=body).json()["replayed"]
        other = bodies[1 if body == bodies[0] else 0]["expected_evidence_revision"]
        assert client.post(endpoint + "/values", json={**body, "expected_evidence_revision": other}).status_code == 409
    assert (rd / "events.jsonl").read_bytes() == before
