"""A changed ranking objective does not erase a measured primary-score source."""
import sys
import time

import httpx
from fastapi.testclient import TestClient
import pytest

from looplab.engine.upstream_state import source_node
from looplab.events.replay import fold
from looplab.harness.mcp_server import HarnessAPI
from looplab.runtime.command_eval import run_command_eval
from looplab.serve.server import make_app
from tests.factories import command_terminal, post_command
from tests.test_upstream_lane import fixture
from tests.test_upstream_multibase import create, materialize


def measured_secondary(lane, store):
    # Operator-declared alias reads the actual protected scorer's stdout. The
    # earlier node retains its original primary-only measurement unchanged.
    lane.task.eval.metrics = {"diagnostic": {"kind": "stdout_json", "key": "metric", "direction": "min"}}
    (lane.rd / "task.snapshot.json").write_text(lane.task.model_dump_json(), encoding="utf8")
    create(store, 1, fold(store.read_all()).nodes[0].files)
    work, receipt = materialize(lane, store, 1)
    receipt["node_id"], receipt["generation"] = 1, 0
    es = lane.task.eval_spec()
    start = time.perf_counter()
    result = run_command_eval([sys.executable, "score.py"], str(work), 10,
        es["metric"], stages=es["stages"], metrics=es["metrics"])
    assert result.exit_code == 0 and result.metric is not None
    assert result.extra_metrics_provenance == {"diagnostic": "declared"}
    store.append("node_evaluated", {"node_id": 1, "generation": 0, "metric": result.metric,
        "eval_seconds": time.perf_counter() - start, "violations": [],
        "extra_metrics": result.extra_metrics, "extra_metrics_provenance": result.extra_metrics_provenance,
        "extra_metrics_direction": result.extra_metrics_direction,
        "metric_provenance": {"base_revision": receipt}})


@pytest.mark.parametrize("switch", ["before_propose", "after_check"])
def test_unranked_primary_source_can_complete_upstream_transaction(tmp_path, monkeypatch, switch):
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    monkeypatch.delenv("LOOPLAB_HARNESS_TOKEN", raising=False)
    lane, store, generation, proposal = fixture(tmp_path)
    original = source_node(store.read_all(), 0)[0].task_metric
    measured_secondary(lane, store)
    if switch == "after_check":
        made = lane.propose(proposal)
        check = {"expected_generation": generation, "action_id": "checked-primary", "proposal_id": made["proposal_id"]}
        checked = lane.check(check)
        assert checked["status"] == "succeeded", checked
    with TestClient(make_app(tmp_path)) as client:
        queued = post_command(client, "metric_retarget", {"key": "diagnostic"}, run_id="run")
        assert queued.status_code in (200, 202), queued.text
        done = command_terminal(client, queued.json(), run_id="run")
        assert done["status"] == "succeeded", done
        state = fold(store.read_all())
        assert state.objective_key == "diagnostic" and state.best_node_id == 1
        assert state.nodes[0].metric is None and state.nodes[0].task_metric == original
        before = store.path.read_bytes()
        assert source_node(store.read_all(), 0)[0].task_metric == original
        def bridge(r):
            reply = client.request(r.method, str(r.url), content=r.content, headers=r.headers)
            return httpx.Response(reply.status_code, content=reply.content)
        api = HarnessAPI("http://testserver", transport=httpx.MockTransport(bridge))
        try:
            page = api.upstream_status("run", generation)
            assert page["status"] == 200
            assert any(row["node_id"] == 0 for row in page["body"]["candidates"]["rows"])
            assert store.path.read_bytes() == before
            if switch == "before_propose":
                made = api.upstream_write("run", "proposals", proposal)["body"]
                assert made["status"] == "succeeded", made
                check = {"expected_generation": generation, "action_id": "checked-primary", "proposal_id": made["proposal_id"]}
                checked = api.upstream_write("run", "check", check)["body"]
                assert checked["status"] == "succeeded", checked
            assert checked["result"]["checks"][-1]["source_reproduced"] is True
            before = store.path.read_bytes()
            ack = api.upstream_write("run", "check", check)
            assert ack["status"] == 200 and ack["body"] == checked
            assert store.path.read_bytes() == before
            advance = {"expected_generation": generation, "action_id": "advance-primary", "proposal_id": made["proposal_id"],
                "expected_base_revision": proposal["expected_base_revision"], "evidence_token": checked["evidence_token"]}
            advanced = api.upstream_write("run", "advance", advance)
            assert advanced["status"] == 200 and advanced["body"]["status"] == "succeeded", advanced
            before = store.path.read_bytes()
            assert api.upstream_write("run", "advance", advance) == advanced
            assert store.path.read_bytes() == before
        finally:
            api.client.close()
    state = fold(store.read_all())
    assert state.nodes[0].metric is None and state.nodes[0].task_metric == original
    assert state.best_node_id == 1 and state.upstream_base is not None
    assert len([e for e in store.read_all() if e.type == "upstream_execution"]) == 7
    assert not any(e.type == "resume" for e in store.read_all())
