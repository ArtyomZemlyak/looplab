"""Production authority, acknowledgement recovery and actual evaluator dispatch guards."""
import json
import sys

from fastapi.testclient import TestClient
import pytest

from looplab.core.errors import ConfigRefusal, UpstreamRefusal
from looplab.engine import upstream
from looplab.serve.server import make_app
from looplab.tools.upstream_tools import UpstreamTools
from tests.test_upstream_lane import fixture, TRAIN, SOURCE, GENERAL


def test_engine_presence_refuses_new_work_but_exact_saved_ack_is_readable(tmp_path, monkeypatch):
    lane, store, generation, proposal = fixture(tmp_path)
    made = lane.propose(proposal)
    monkeypatch.setattr(upstream, "engine_alive", lambda rd: True)
    before = store.path.read_bytes()
    assert lane.propose(proposal) == made
    with pytest.raises(UpstreamRefusal, match="Pause"):
        lane.check({"expected_generation": generation, "action_id": "running", "proposal_id": made["proposal_id"]})
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("fault", ["origin", "candidate", "source_seed", "seed_generation", "probe_overlay"])
def test_incomplete_or_masked_evidence_never_advances(tmp_path, fault):
    lane, store, generation, proposal = fixture(tmp_path)
    if fault == "probe_overlay":
        lane.task.upstream["tests"][0]["files"] = {"train.py": GENERAL}
        events = store.read_all()
        events[0].data["upstream"] = lane.task.upstream
        store.path.write_text("".join(e.model_dump_json() + "\n" for e in events), encoding="utf8")
        with pytest.raises(UpstreamRefusal, match="actual old/new base"): lane.propose(proposal)
    else:
        made = lane.propose(proposal)
        if fault == "origin":
            path = tmp_path / "origin" / "base_snapshots" / lane.task.seed_base["digest"] / "train.py"
        elif fault == "candidate":
            path = lane.rd / "base_snapshots" / made["selector"]["digest"] / "train.py"
        else:
            path = None
            events = store.read_all()
            terminal = next(e for e in events if e.type == "node_evaluated")
            if fault == "seed_generation":
                seed = next(e for e in events if e.seq == terminal.data["metric_provenance"]["base_revision"]["seed_event_seq"])
                seed.data["generation"] = 1
            else:
                terminal.data["metric_provenance"]["base_revision"]["seed_event_seq"] = 0
            store.path.write_text("".join(e.model_dump_json() + "\n" for e in events), encoding="utf8")
        if path is not None: path.write_text("changed bytes", encoding="utf8")
        with pytest.raises((UpstreamRefusal, ConfigRefusal)):
            lane.check({"expected_generation": generation, "action_id": "invalid", "proposal_id": made["proposal_id"]})
    assert not any(e.type in ("base_advanced", "upstream_execution") for e in store.read_all())


@pytest.mark.parametrize("opaque", [False, True])
def test_real_gate_runs_training_once_per_full_evaluation(tmp_path, opaque):
    counter = '\nwith Path("training-count.txt").open("a") as f: f.write("trained\\n")\n'
    lane, _, generation, proposal = fixture(tmp_path, base_train=TRAIN + counter,
        source_files={"train.py": SOURCE + counter, "recipe.env": "MOMENTUM=0.2\n"})
    proposal["files"]["train.py"] = GENERAL + counter
    if opaque:
        lane.task.eval.stages = None
        lane.task.eval.command = [sys.executable, "-c", "import runpy;runpy.run_path('train.py');runpy.run_path('score.py')"]
    else:
        # A hostile preceding training declaration must not override operator stages.
        manifest = {"stages": [{"name": "duplicate", "command": [sys.executable, "train.py"]}]}
        lane.task.edit_surface.append("looplab_stages.json")
        proposal["files"]["looplab_stages.json"] = json.dumps(manifest)
    (lane.rd / "task.snapshot.json").write_text(lane.task.model_dump_json(), encoding="utf8")
    made = lane.propose(proposal)
    checked = lane.check({"expected_generation": generation, "action_id": "dispatch", "proposal_id": made["proposal_id"]})
    assert checked["status"] == "succeeded", checked
    from pathlib import Path
    evaluations = [r for r in checked["result"]["executions"] if "source" in r["label"]]
    assert len(evaluations) == 4
    for row in evaluations:
        assert (lane.rd / row["workdir"] / "training-count.txt").read_text().splitlines() == ["trained"]
        assert len(row["stages"] or []) == (0 if opaque else 2)


def test_scoped_token_refuses_internal_writes_and_operator_recovery(tmp_path, monkeypatch):
    lane, store, generation, proposal = fixture(tmp_path)
    lane.settings.external_harness = False
    (lane.rd / "config.snapshot.json").write_text(lane.settings.model_dump_json(), encoding="utf8")
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-upstream-secret")
    monkeypatch.setenv("LOOPLAB_HARNESS_TOKEN", "agent-upstream-secret")
    before = store.path.read_bytes()
    with TestClient(make_app(tmp_path)) as client:
        headers = {"X-LoopLab-Token": "agent-upstream-secret"}
        assert client.get("/api/runs/run/upstream", params={"expected_generation": generation}, headers=headers).status_code == 200
        denied = client.post("/api/runs/run/upstream/proposals", json=proposal, headers=headers)
        assert denied.status_code == 403 and denied.json()["detail"]["code"] == "agent_token_refused"
        denied = client.post("/api/runs/run/upstream/recover", headers=headers, json={"expected_generation": generation,
            "action_id": "recover", "claim_action_id": "claim", "reason": "Attempt forbidden cleanup"})
        assert denied.status_code == 403
    assert store.path.read_bytes() == before


def test_assistant_plan_read_and_exact_full_body_approval_binding(tmp_path):
    lane, store, generation, proposal = fixture(tmp_path)
    planned = UpstreamTools(tmp_path, mode="plan")
    assert {row["function"]["name"] for row in planned.specs()} == {"upstream_status", "upstream_request"}
    assert planned.execute("upstream_status", {"run_id": "run", "expected_generation": generation}).structured["enabled"]
    assert planned.execute("upstream_propose", {"run_id": "run", "body": proposal}).is_error
    scopes = []
    def deny(action):
        scopes.append(action["scope"])
        return "deny"
    tool = UpstreamTools(tmp_path, mode="auto", approver=deny)
    before = store.path.read_bytes()
    assert tool.execute("upstream_propose", {"run_id": "run", "body": proposal}).is_error
    other = json.loads(json.dumps(proposal))
    other["files"]["train.py"] += "\n# change beyond preview\n"
    assert tool.execute("upstream_propose", {"run_id": "run", "body": other}).is_error
    assert len(scopes) == 2 and scopes[0]["request_digest"] != scopes[1]["request_digest"]
    assert store.path.read_bytes() == before
