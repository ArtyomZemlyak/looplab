"""A stored passing flag/hash cannot replace actual complete gate evidence at CAS."""
from copy import deepcopy

import pytest

from looplab.core.errors import UpstreamRefusal
from looplab.engine.upstream_state import digest
from tests.test_upstream_lane import fixture


@pytest.mark.parametrize("damage", ["checks", "artifacts", "charge", "tolerance", "tiny_tolerance"])
def test_incomplete_saved_gate_refuses_fresh_cas_and_allows_explicit_recheck(tmp_path, damage):
    lane, store, generation, proposal = fixture(tmp_path)
    made = lane.propose(proposal)
    request = {"expected_generation": generation, "action_id": "measured-check",
        "proposal_id": made["proposal_id"]}
    original = lane.check(request)
    assert original["status"] == "succeeded"
    saved = deepcopy(store.read_all()[-1].data)
    result = saved["result"]
    if damage == "checks":
        result["checks"] = []
    elif damage == "artifacts":
        result["executions"][1]["artifacts"] = {}
        result["executions"][2]["artifacts"] = {}
    elif damage == "charge":
        for row in result["executions"]:
            row["seconds"] += 1
        result["eval_seconds"] = sum(row["seconds"] for row in result["executions"])
    else:
        result["checks"][-1]["tolerance"] = 1e-13 if damage == "tiny_tolerance" else 1e10
    saved["evidence_token"] = digest(result)
    # Model a saved producer/serialization defect after actual SGD, with a
    # consistent token. Do not invent a metric or erase the real charge events.
    store.append("upstream_gate_finished", saved)
    assert lane.check(request)["evidence_token"] == saved["evidence_token"]
    advance = {"expected_generation": generation, "action_id": "fresh-advance",
        "proposal_id": made["proposal_id"], "expected_base_revision": proposal["expected_base_revision"],
        "evidence_token": saved["evidence_token"]}
    before = store.path.read_bytes()
    with pytest.raises(UpstreamRefusal) as refusal:
        lane.advance(advance)
    assert refusal.value.code == "upstream_gate_required"
    assert store.path.read_bytes() == before
    assert not any(e.type == "base_advanced" for e in store.read_all())
    fresh = lane.check({**request, "action_id": "explicit-recheck"})
    assert fresh["status"] == "succeeded", fresh
    accepted = lane.advance({**advance, "action_id": "rechecked-advance",
        "evidence_token": fresh["evidence_token"]})
    assert accepted["status"] == "succeeded"
