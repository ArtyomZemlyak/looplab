"""Candidate advice must use the same measured source boundary as admission."""
import httpx
from fastapi.testclient import TestClient
import pytest

from looplab.core.errors import UpstreamRefusal
from looplab.engine.upstream_state import source_node
from looplab.events.replay import fold
from looplab.harness.mcp_server import HarnessAPI
from looplab.serve.server import make_app
from looplab.tools.upstream_tools import UpstreamTools
from tests.test_upstream_lane import fixture


@pytest.mark.parametrize("fault", ["salvaged", "missing_metric", "seed_sequence",
    "seed_generation", "seed_node", "receipt_generation", "receipt_node",
    "receipt_bool_generation", "receipt_bool_node", "receipt_shape",
    "seed_sequence_shape", "seed_base_shape", "seed_bool_node", "seed_sequence_float",
    "seed_incomplete", "seed_bytes", "seed_archive", "seed_after_terminal"])
def test_ineligible_measured_source_is_not_nominated(tmp_path, fault):
    lane, store, generation, proposal = fixture(tmp_path)
    events = store.read_all()
    terminal = next(e for e in events if e.type == "node_evaluated")
    provenance = terminal.data["metric_provenance"]
    receipt = provenance["base_revision"]
    seed = next(e for e in events if e.seq == receipt["seed_event_seq"])
    # Damage provenance or remove a score from an actual completed training;
    # no test supplies a replacement measured value.
    if fault == "salvaged":
        provenance["salvaged"] = True
    elif fault == "missing_metric":
        terminal.data.pop("metric")
    elif fault == "seed_sequence":
        receipt["seed_event_seq"] = 0
    elif fault == "seed_generation":
        seed.data["generation"] = 1
    elif fault == "seed_node":
        seed.data["node_id"] = 1
    elif fault == "receipt_generation":
        receipt["generation"] = 1
    elif fault == "receipt_node":
        receipt["node_id"] = 1
    elif fault == "receipt_bool_generation":
        receipt["generation"] = False
    elif fault == "receipt_bool_node":
        receipt["node_id"] = False
    elif fault == "receipt_shape":
        provenance["base_revision"] = [receipt]
    elif fault == "seed_sequence_shape":
        receipt["seed_event_seq"] = {"seq": seed.seq}
    elif fault == "seed_bool_node":
        seed.data["node_id"] = False
    elif fault == "seed_sequence_float":
        receipt["seed_event_seq"] = float(seed.seq)
    elif fault == "seed_base_shape":
        seed.data["base_revision"] = [seed.data["base_revision"]]
    elif fault == "seed_incomplete":
        seed.data["base_revision"]["complete"] = False
    elif fault == "seed_bytes":
        seed.data["base_revision"]["bytes"] += 1
    elif fault == "seed_archive":
        seed.data["base_revision"]["archive"] = {"status": "unavailable"}
    else:
        late_seed = store.append("workspace_seeded", seed.data)
        events.append(late_seed)
        receipt["seed_event_seq"] = late_seed.seq
    store.path.write_text("".join(e.model_dump_json() + "\n" for e in events), encoding="utf8")
    before = store.path.read_bytes()
    with pytest.raises(UpstreamRefusal):
        source_node(store.read_all(), 0)
    assert lane.read(generation)["candidates"]["rows"] == []
    with pytest.raises(UpstreamRefusal):
        lane.propose(proposal)
    assert store.path.read_bytes() == before
    assert not (lane.rd / "upstream").exists()


def test_current_primary_source_requires_fresh_measurement_after_reset(tmp_path):
    lane, store, generation, _ = fixture(tmp_path)
    before = store.path.read_bytes()
    node, receipt = source_node(store.read_all(), 0)
    rows = lane.read(generation)["candidates"]["rows"]
    assert rows and {row["node_id"] for row in rows} == {node.id}
    assert {row["generation"] for row in rows} == {receipt["generation"]}
    assert store.path.read_bytes() == before
    store.append("node_reset", {"node_id": 0})
    before = store.path.read_bytes()
    assert lane.read(generation)["candidates"]["rows"] == []
    assert store.path.read_bytes() == before
    from tests.test_upstream_multibase import evaluate
    measured = evaluate(lane, store, 0)
    before = store.path.read_bytes()
    node, receipt = source_node(store.read_all(), 0)
    assert node.metric == measured and receipt["generation"] == 1
    rows = lane.read(generation)["candidates"]["rows"]
    assert rows and {row["generation"] for row in rows} == {1}
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("missing_primary", [False, True])
def test_retargeted_value_does_not_replace_missing_primary_evidence(tmp_path, missing_primary):
    lane, store, generation, _ = fixture(tmp_path)
    events = store.read_all()
    terminal = next(e for e in events if e.type == "node_evaluated")
    # Relabel an existing measured value on a diagnostic channel, then remove
    # its primary channel in the negative case; do not invent a measured value.
    terminal.data["extra_metrics"] = {"diagnostic": terminal.data["metric"]}
    terminal.data["extra_metrics_provenance"] = {"diagnostic": "declared"}
    terminal.data["extra_metrics_direction"] = {"diagnostic": "min"}
    if missing_primary:
        terminal.data.pop("metric")
    store.path.write_text("".join(e.model_dump_json() + "\n" for e in events), encoding="utf8")
    store.append("metric_retarget", {"key": "diagnostic"})
    before = store.path.read_bytes()
    node = fold(store.read_all()).nodes[0]
    assert node.metric is not None
    if missing_primary:
        assert node.task_metric is None
        with pytest.raises(UpstreamRefusal) as exc:
            source_node(store.read_all(), 0)
        assert exc.value.code == "upstream_source_not_measured"
        assert lane.read(generation)["candidates"]["rows"] == []
    else:
        assert source_node(store.read_all(), 0)[0].task_metric == node.task_metric
        assert lane.read(generation)["candidates"]["rows"]
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("fault", ["salvaged", "receipt_shape"])
def test_source_advice_and_historical_ack_through_http_mcp_assistant(tmp_path, monkeypatch, fault):
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    monkeypatch.delenv("LOOPLAB_HARNESS_TOKEN", raising=False)
    lane, store, generation, proposal = fixture(tmp_path)
    saved = lane.propose(proposal)
    events = store.read_all()
    provenance = next(e for e in events if e.type == "node_evaluated").data["metric_provenance"]
    if fault == "salvaged":
        provenance["salvaged"] = True
    else:
        provenance["base_revision"] = [provenance["base_revision"]]
    store.path.write_text("".join(e.model_dump_json() + "\n" for e in events), encoding="utf8")
    before = store.path.read_bytes()
    with TestClient(make_app(tmp_path)) as client:
        response = client.get("/api/runs/run/upstream", params={"expected_generation": generation})
        assert response.status_code == 200 and response.json()["candidates"]["rows"] == []
        def bridge(r):
            reply = client.request(r.method, str(r.url), content=r.content, headers=r.headers)
            return httpx.Response(reply.status_code, content=reply.content)
        api = HarnessAPI("http://testserver", transport=httpx.MockTransport(bridge))
        try:
            page = api.upstream_status("run", generation)
            assert page["status"] == 200 and page["body"]["candidates"]["rows"] == []
            assistant = UpstreamTools(tmp_path, mode="plan")
            read = assistant.execute("upstream_status", {"run_id": "run", "expected_generation": generation})
            assert not read.is_error and not read.structured["candidates"]["rows"]
            ack = api.upstream_write("run", "proposals", proposal)
            assert ack["status"] == 200 and ack["body"] == saved
            refused = api.upstream_write("run", "proposals", {**proposal, "action_id": "fresh-proposal"})
            assert refused["status"] == (409 if fault == "salvaged" else 503)
            expected = "upstream_source_not_measured" if fault == "salvaged" else "upstream_source_unavailable"
            assert refused["body"]["detail"]["code"] == expected
        finally:
            api.client.close()
    assert store.path.read_bytes() == before
    assert not any(e.type in ("upstream_execution", "base_advanced", "resume") for e in store.read_all())
