"""The real gate must use the same live timeout contract as node evaluation."""
import pytest

from looplab.core.errors import UpstreamRefusal
from looplab.runtime import command_eval
from tests.test_upstream_lane import fixture


@pytest.mark.parametrize("timeout", [5, 30])
def test_gate_full_pipeline_honors_live_operator_timeout(tmp_path, monkeypatch, timeout):
    lane, store, generation, proposal = fixture(tmp_path)
    made = lane.propose(proposal)
    store.append("budget_extend", {"eval_timeout": timeout})
    actual, calls = command_eval.run_command_eval, []

    def execute(*args, **kwargs):
        if kwargs.get("stages"):
            calls.append((args[2], [s["timeout"] for s in kwargs["stages"]]))
        return actual(*args, **kwargs)

    monkeypatch.setattr(command_eval, "run_command_eval", execute)
    checked = lane.check({"expected_generation": generation, "action_id": "live-budget", "proposal_id": made["proposal_id"]})
    assert checked["status"] == "succeeded", checked
    assert calls == [(timeout, [timeout, timeout])] * 4
    assert len(checked["result"]["executions"]) == 7


def test_timeout_change_invalidates_gate_before_cas_but_preserves_exact_ack(tmp_path):
    lane, store, generation, proposal = fixture(tmp_path)
    made = lane.propose(proposal)
    request = {"expected_generation": generation, "action_id": "old-budget", "proposal_id": made["proposal_id"]}
    checked = lane.check(request)
    assert checked["status"] == "succeeded", checked
    store.append("budget_extend", {"eval_timeout": 30})
    with pytest.raises(UpstreamRefusal) as refusal:
        lane.advance({"expected_generation": generation, "action_id": "changed-budget", "proposal_id": made["proposal_id"],
            "expected_base_revision": proposal["expected_base_revision"], "evidence_token": checked["evidence_token"]})
    assert refusal.value.code == "upstream_evidence_changed"
    before = len(store.read_all())
    assert lane.check(request) == checked and len(store.read_all()) == before
    fresh = lane.check({**request, "action_id": "new-budget"})
    assert fresh["status"] == "succeeded", fresh


@pytest.mark.parametrize("subject,passed", [(None, False), ("missing.json", False), ("predictions.json", True)])
def test_required_metric_subject_is_enforced_on_real_gate(tmp_path, subject, passed):
    lane, store, generation, proposal = fixture(tmp_path)
    lane.settings.metric_subject = "require"
    if subject:
        lane.task.eval.metric["subject"] = [subject]
    (lane.rd / "task.snapshot.json").write_text(lane.task.model_dump_json(), encoding="utf8")
    (lane.rd / "config.snapshot.json").write_text(lane.settings.model_dump_json(), encoding="utf8")
    made = lane.propose(proposal)
    checked = lane.check({"expected_generation": generation, "action_id": "subject", "proposal_id": made["proposal_id"]})
    assert checked["result"]["passed"] is passed, checked
    evaluations = checked["result"]["executions"][-4:]
    assert all(r["valid"] is passed for r in evaluations)
    assert all(r["metric_subject"]["subject_bound"] is passed for r in evaluations)
    from looplab.harness.mcp_server import HarnessAPI
    import httpx
    api = HarnessAPI("http://localhost", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=checked)))
    try:
        result = api.upstream_write("run", "check", {"expected_generation": generation, "action_id": "subject", "proposal_id": made["proposal_id"]})
        assert result["body"]["status"] == ("succeeded" if passed else "failed") and not result.get("code"), result
    finally:
        api.client.close()
