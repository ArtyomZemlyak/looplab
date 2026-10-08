"""Meaningful refusal cases: real bad measurements, drift, unresolved claims and cost."""
import pytest

from looplab.core.errors import UpstreamRefusal
from looplab.engine import upstream
from looplab.engine.upstream_state import digest
from looplab.events.replay import fold
from tests.test_upstream_lane import fixture


@pytest.mark.parametrize("fault", ["default", "equivalence"])
def test_actual_bad_gate_cannot_advance_and_cost_is_retained(tmp_path, fault):
    lane, store, generation, body = fixture(tmp_path)
    if fault == "default":
        body["files"]["train.py"] = body["files"]["train.py"].replace('settings.get("MOMENTUM", "0.0")', '"0.2"')
    else:
        body["files"]["train.py"] = body["files"]["train.py"].replace("range(30)", "range(10)")
    proposed = lane.propose(body)
    checked = lane.check({"expected_generation": generation, "action_id": "bad-check", "proposal_id": proposed["proposal_id"]})
    assert checked["status"] == "failed" and checked["result"]["passed"] is False
    assert checked["result"]["eval_seconds"] > 0 and fold(store.read_all()).eval_seconds_by_kind["upstream"] > 0
    with pytest.raises(UpstreamRefusal, match="latest measured passing gate"):
        lane.advance({"expected_generation": generation, "action_id": "bad-advance", "proposal_id": proposed["proposal_id"],
            "expected_base_revision": body["expected_base_revision"], "evidence_token": checked["evidence_token"]})


def test_interrupted_claim_needs_operator_abandonment_and_new_action(tmp_path, monkeypatch):
    lane, store, generation, body = fixture(tmp_path)
    made = lane.propose(body)
    request = {"expected_generation": generation, "action_id": "lost-check", "proposal_id": made["proposal_id"]}
    real = upstream.execute_gate
    def crash(*args): raise SystemExit("private simulated process loss before execution")
    monkeypatch.setattr(upstream, "execute_gate", crash)
    with pytest.raises(SystemExit): lane.check(request)
    monkeypatch.setattr(upstream, "execute_gate", real)
    for candidate in (request, {**request, "action_id": "fresh-check"}):
        with pytest.raises(UpstreamRefusal, match="claim|Claim"): lane.check(candidate)
    recovered = lane.abandon({"expected_generation": generation, "action_id": "owner-recovery", "claim_action_id": "lost-check", "reason": "Private process exited; no evaluation started"})
    assert recovered["event_type"] == "upstream_gate_abandoned"
    checked = lane.check({**request, "action_id": "fresh-check"})
    assert checked["status"] == "succeeded"
    assert len([e for e in store.read_all() if e.type == "upstream_execution"]) == 7


@pytest.mark.parametrize("operation", ["propose", "check"])
@pytest.mark.parametrize("running", [False, True])
def test_retry_of_abandoned_claim_names_recovery_without_reexecution(tmp_path, monkeypatch, operation, running):
    lane, store, generation, proposal = fixture(tmp_path)
    if operation == "check":
        made = lane.propose(proposal)
        request = {"expected_generation": generation, "action_id": "lost-check", "proposal_id": made["proposal_id"]}
        target = "execute_gate"
    else:
        request, target = proposal, "maintainer_worktree"
    def die(*args, **kwargs):
        raise SystemExit("Private process loss after claim publication")
    with monkeypatch.context() as patch:
        patch.setattr(upstream, target, die)
        with pytest.raises(SystemExit):
            getattr(lane, operation)(request)
    recovery = {"expected_generation": generation, "action_id": "owner-recovery",
        "claim_action_id": request["action_id"], "reason": "The private claim process exited"}
    recovered = lane.abandon(recovery)
    before = store.path.read_bytes()
    probes = []
    with monkeypatch.context() as patch:
        patch.setattr(upstream, "engine_alive", lambda rd: (probes.append(rd), running)[1])
        assert lane.abandon(recovery) == recovered
        with pytest.raises(UpstreamRefusal) as refusal:
            getattr(lane, operation)(request)
        assert refusal.value.code == "upstream_claim_abandoned"
        assert "new action_id" in str(refusal.value)
        assert probes == [], "A saved refusal/ACK does not need a new engine ownership probe"
        changed = ({**request, "summary": request["summary"] + " changed"} if operation == "propose"
            else {**request, "proposal_id": "up_" + "0" * 24})
        with pytest.raises(UpstreamRefusal) as conflict:
            getattr(lane, operation)(changed)
        assert conflict.value.code == "upstream_action_conflict"
        assert probes == []
        if running:
            # The stopped lane (`upstream_mode: off`) refuses new work on a running engine; the live
            # modes queue it instead (doc 73 §2.5, `tests/test_upstream_live_lane.py`).
            live_settings = lane.settings
            lane.settings = lane.settings.model_copy(update={"upstream_mode": "off"})
            with pytest.raises(UpstreamRefusal) as busy:
                getattr(lane, operation)({**request, "action_id": "fresh-action"})
            assert busy.value.code == "upstream_engine_running"
            assert len(probes) == 1
            lane.settings = live_settings
    assert store.path.read_bytes() == before
    assert not any(e.type in ("upstream_execution", "base_advanced", "resume") for e in store.read_all())
    # The explicit fresh action, after recovery and engine exit, remains usable.
    fresh = getattr(lane, operation)({**request, "action_id": "fresh-action"})
    assert fresh["status"] == "succeeded", fresh


def test_drift_after_pass_refuses_cas_and_damaged_log_refuses_even_exact_ack(tmp_path):
    lane, store, generation, body = fixture(tmp_path)
    made = lane.propose(body)
    checked = lane.check({"expected_generation": generation, "action_id": "gate", "proposal_id": made["proposal_id"]})
    assert checked["status"] == "succeeded"
    path = lane.rd / "config.snapshot.json"
    original = path.read_bytes()
    path.write_bytes(original + b"\n")
    with pytest.raises(UpstreamRefusal, match="changed"):
        lane.advance({"expected_generation": generation, "action_id": "advance", "proposal_id": made["proposal_id"],
            "expected_base_revision": body["expected_base_revision"], "evidence_token": checked["evidence_token"]})
    path.write_bytes(original)
    with store.path.open("ab") as stream: stream.write(b'{"seq":')
    with pytest.raises(UpstreamRefusal, match="events.jsonl"): lane.propose(body)


def test_budget_refusal_buys_no_execution_and_task_policy_is_immutable(tmp_path):
    lane, store, generation, body = fixture(tmp_path)
    made = lane.propose(body)
    lane.settings.max_eval_seconds = 0.2
    checked = lane.check({"expected_generation": generation, "action_id": "budget", "proposal_id": made["proposal_id"]})
    assert checked["result"]["code"] == "upstream_budget_exhausted" and checked["result"]["executions"] == []
    assert not any(e.type == "upstream_execution" for e in store.read_all())
    lane.task.upstream["atol"] = 100
    with pytest.raises(UpstreamRefusal, match="launched upstream declaration"): lane.read(generation)
