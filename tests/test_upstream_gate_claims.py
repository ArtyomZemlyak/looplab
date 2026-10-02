"""Real gate results retain authority only within one current, unrevoked claim."""
from copy import deepcopy

import pytest

from looplab.core.errors import UpstreamRefusal
from looplab.engine import upstream
from looplab.engine.upstream_state import digest
from tests.test_upstream_lane import fixture


def assert_refused_then_recheck(lane, store, generation, proposal, made, checked):
    advance = {"expected_generation": generation, "action_id": "unbound-advance",
        "proposal_id": made["proposal_id"], "expected_base_revision": proposal["expected_base_revision"],
        "evidence_token": checked["evidence_token"]}
    before = store.path.read_bytes()
    with pytest.raises(UpstreamRefusal) as refusal:
        lane.advance(advance)
    assert refusal.value.code == "upstream_gate_required"
    assert "events.jsonl" in str(refusal.value)
    assert store.path.read_bytes() == before
    assert not any(e.type == "base_advanced" for e in store.read_all())
    fresh = lane.check({"expected_generation": generation, "action_id": "explicit-recheck",
        "proposal_id": made["proposal_id"]})
    assert fresh["status"] == "succeeded"
    assert lane.advance({**advance, "action_id": "rechecked-advance",
        "evidence_token": fresh["evidence_token"]})["status"] == "succeeded"


@pytest.mark.parametrize("damage", ["missing_start", "request", "context", "late_start", "duplicate_start",
    "late_charge", "duplicate_finish", "wrong_generation"])
def test_saved_executions_need_one_matching_preceding_claim(tmp_path, damage):
    lane, store, generation, proposal = fixture(tmp_path)
    made = lane.propose(proposal)
    checked = lane.check({"expected_generation": generation, "action_id": "measured-check",
        "proposal_id": made["proposal_id"]})
    assert checked["status"] == "succeeded"
    events = store.read_all()
    index = next(i for i, e in enumerate(events) if e.type == "upstream_gate_started")
    claim = events[index]
    if damage == "missing_start":
        claim.type, claim.data = "phase_progress", {"phase": "upstream", "detail": "Private missing-claim fixture"}
    elif damage == "request":
        claim.data["request_hash"] = "0" * 64
    elif damage == "context":
        claim.data["input_identity"] = "0" * 64
    elif damage == "late_start":
        first_charge = events[index + 1]
        claim.type, first_charge.type = first_charge.type, claim.type
        claim.data, first_charge.data = first_charge.data, claim.data
    elif damage == "late_charge":
        charge, finish = events[-2:]
        charge.type, finish.type = finish.type, charge.type
        charge.data, finish.data = finish.data, charge.data
    elif damage == "wrong_generation":
        foreign_hash = digest({"expected_generation": "0" * 64, "action_id": "measured-check",
                               "proposal_id": made["proposal_id"]})
        for event in events[index:]:
            event.data["request_hash"] = foreign_hash
    elif damage == "duplicate_finish":
        duplicate = events[-1].model_copy(deep=True)
        duplicate.seq = len(events)
        events.append(duplicate)
    else:
        events.insert(index + 1, claim.model_copy(deep=True))
        for seq, event in enumerate(events):
            event.seq = seq
    # Preserve the real measured scores, execution bodies and costs. Model only
    # a stored claim-link defect; the log still has a dense syntactic sequence.
    store.path.write_text("".join(e.model_dump_json() + "\n" for e in events), encoding="utf8")
    assert_refused_then_recheck(lane, store, generation, proposal, made, checked)


def test_late_completion_after_operator_abandonment_cannot_advance(tmp_path, monkeypatch):
    lane, store, generation, proposal = fixture(tmp_path)
    made = lane.propose(proposal)
    request = {"expected_generation": generation, "action_id": "lost-reply-check",
        "proposal_id": made["proposal_id"]}
    real, retained = upstream.execute_gate, {}

    def die_after_measuring(*args):
        retained["result"] = deepcopy(real(*args))
        raise SystemExit("Private process loss after actual SGD, before gate publication")

    with monkeypatch.context() as patch:
        patch.setattr(upstream, "execute_gate", die_after_measuring)
        with pytest.raises(SystemExit):
            lane.check(request)
    assert retained["result"]["passed"]
    assert len([e for e in store.read_all() if e.type == "upstream_execution"]) == 7
    recovered = lane.abandon({"expected_generation": generation, "action_id": "owner-abandon",
        "claim_action_id": request["action_id"], "reason": "The private gate process exited without a verdict"})
    assert recovered["event_type"] == "upstream_gate_abandoned"
    late = {"action_id": request["action_id"], "request_hash": digest(request),
        "proposal_id": made["proposal_id"], "result": retained["result"],
        "evidence_token": digest(retained["result"])}
    # Retaining a delayed result cannot revoke the operator's explicit abandon.
    store.append("upstream_gate_finished", late)
    before = store.path.read_bytes()
    # A historical exact ACK remains readable; abandonment only revokes its
    # authority for fresh CAS, rather than rewriting the real measurements.
    with monkeypatch.context() as patch:
        patch.setattr(upstream, "engine_alive", lambda rd: True)
        assert lane.check(request)["result"] == retained["result"]
    assert store.path.read_bytes() == before
    assert_refused_then_recheck(lane, store, generation, proposal, made, late)
