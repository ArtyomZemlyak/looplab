"""Original proposal bodies survive before work, without granting execution authority."""
import json

import httpx
from fastapi.testclient import TestClient
import pytest

from looplab.core.errors import UpstreamRefusal
from looplab.engine import upstream
from looplab.engine.upstream_spec import normalize_request
from looplab.engine.upstream_state import digest
from looplab.harness.upstream_receipts import event
from looplab.harness.mcp_server import HarnessAPI
from looplab.serve.server import make_app
from looplab.tools.upstream_tools import UpstreamTools
from tests.test_upstream_lane import fixture


def request_path(lane, body):
    return lane.rd / "upstream/requests" / ("up_" + digest(body["action_id"])[:24]) / "request.json"


def test_process_loss_before_git_retains_exact_body_and_discoverable_path(tmp_path, monkeypatch):
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    monkeypatch.delenv("LOOPLAB_HARNESS_TOKEN", raising=False)
    lane, store, generation, body = fixture(tmp_path)
    def die(*args, **kwargs):
        raise SystemExit("Private process loss before Git")
    with monkeypatch.context() as patch:
        patch.setattr(upstream, "maintainer_worktree", die)
        with pytest.raises(SystemExit):
            lane.propose(body)
    claim = store.read_all()[-1]
    assert claim.type == "upstream_proposal_started"
    path = request_path(lane, body)
    assert path.is_file(), "The original body must exist before Git or worktree creation"
    retained = path.read_bytes()
    recovered = json.loads(retained)
    assert recovered == normalize_request("propose", body)
    assert digest(recovered) == claim.data["request_hash"]
    assert claim.data["request_path"] == path.relative_to(lane.rd).as_posix()
    before = store.path.read_bytes()
    history = lane.read(generation)["history"]
    assert history[-1]["request_path"] == claim.data["request_path"]
    assert "files" not in history[-1], "Status does not repeat the raw patch"
    with TestClient(make_app(tmp_path)) as client:
        def bridge(request):
            response = client.request(request.method, str(request.url), content=request.content, headers=request.headers)
            return httpx.Response(response.status_code, content=response.content)
        api = HarnessAPI("http://testserver", transport=httpx.MockTransport(bridge))
        try:
            page = api.upstream_status("run", generation)
            assert page["status"] == 200 and not page.get("code"), page
            assert page["body"]["history"][-1]["request_path"] == claim.data["request_path"]
        finally:
            api.client.close()
    assistant = UpstreamTools(tmp_path, mode="plan")
    result = assistant.execute("upstream_status", {"run_id": "run", "expected_generation": generation})
    assert not result.is_error and result.structured["history"][-1]["request_path"] == claim.data["request_path"]
    with pytest.raises(UpstreamRefusal, match="claim"):
        lane.propose(recovered)
    assert store.path.read_bytes() == before and path.read_bytes() == retained
    lane.abandon({"expected_generation": generation, "action_id": "owner-abandon", "claim_action_id": body["action_id"],
        "reason": "The private writer exited before Git work"})
    assert path.read_bytes() == retained
    fresh = lane.propose({**recovered, "action_id": "fresh-proposal"})
    checked = lane.check({"expected_generation": generation, "action_id": "fresh-check", "proposal_id": fresh["proposal_id"]})
    assert checked["status"] == "succeeded" and len(checked["result"]["executions"]) == 7
    assert path.read_bytes() == retained
    assert not any(e.type in ("base_advanced", "resume") for e in store.read_all())


@pytest.mark.parametrize("after_write", [False, True])
def test_request_publication_failure_starts_no_claim_or_git_and_exact_retry_recovers(tmp_path, monkeypatch, after_write):
    lane, store, _, body = fixture(tmp_path)
    real_write, calls = upstream.strict_atomic_write_bytes, []
    path = request_path(lane, body)
    def fail_request(target, data):
        if target == path:
            if after_write:
                real_write(target, data)
            raise OSError("private-secret publication failure")
        return real_write(target, data)
    def no_git(*args, **kwargs):
        calls.append(args)
        pytest.fail("Git started before request durability")
    before = store.path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(upstream, "strict_atomic_write_bytes", fail_request)
        patch.setattr(upstream, "maintainer_worktree", no_git)
        with pytest.raises(UpstreamRefusal) as refusal:
            lane.propose(body)
        assert refusal.value.code == "upstream_request_unavailable"
        assert "upstream/requests/" in str(refusal.value) and "private-secret" not in str(refusal.value)
    assert calls == [] and store.path.read_bytes() == before
    assert path.exists() == after_write
    # An indeterminate publication is re-published durably before the claim.
    writes = []
    def observe_write(target, data):
        writes.append(target)
        return real_write(target, data)
    with monkeypatch.context() as patch:
        patch.setattr(upstream, "strict_atomic_write_bytes", observe_write)
        made = lane.propose(body)
    assert made["status"] == "succeeded" and path in writes
    assert json.loads(path.read_bytes()) == normalize_request("propose", body)
    assert not any(e.type in ("upstream_execution", "base_advanced", "resume") for e in store.read_all())


@pytest.mark.parametrize("damage", ["changed", "truncated", "surrogate", "oversized"])
def test_unclaimed_original_request_cannot_be_overwritten_by_another_body(tmp_path, monkeypatch, damage):
    lane, store, _, body = fixture(tmp_path)
    real_append = lane._append
    def fail_claim(kind, payload):
        if kind == "upstream_proposal_started":
            raise OSError("Private loss before claim publication")
        return real_append(kind, payload)
    with monkeypatch.context() as patch:
        patch.setattr(lane, "_append", fail_claim)
        with pytest.raises(OSError):
            lane.propose(body)
    path = request_path(lane, body)
    assert path.is_file()
    raw = (json.dumps({**normalize_request("propose", body), "summary": "Different retained proposal"}).encode()
        if damage == "changed" else b'{"summary":' if damage == "truncated" else
        b'"\\ud800"' if damage == "surrogate" else b' ' * (2 * 1024 * 1024 + 1))
    path.write_bytes(raw)
    before = store.path.read_bytes()
    with pytest.raises(UpstreamRefusal) as refusal:
        lane.propose(body)
    assert refusal.value.code == "upstream_request_unavailable"
    assert path.read_bytes() == raw and store.path.read_bytes() == before
    assert not (lane.rd / "upstream/git").exists()


@pytest.mark.parametrize("path", [None, "../request.json", "upstream/requests/other/request.json"])
def test_typed_started_claim_refuses_wrong_optional_request_path(path):
    row = {"seq": 2, "action_id": "proposal", "request_hash": "a" * 64, "proposal_id": "up_" + "b" * 24}
    assert event(row, "upstream_proposal_started"), "Legacy claims remain readable"
    assert not event({**row, "request_path": path}, "upstream_proposal_started")
