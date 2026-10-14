"""The MCP client spools a per-query upload until LoopLab answers (doc 81 §8 leftover,
2026-10-14), so a transport failure or a lost turn is resumed with `(run_id, action_id)` alone —
the original `.npz` may be gone — and LoopLab's own action_id idempotency makes the resend exact."""
from __future__ import annotations

import httpx
import pytest

from looplab.core.npyio import write_npy, write_npz
from looplab.events.run_generation import run_generation_token
from looplab.harness import composite
from looplab.harness.mcp_server import HarnessAPI
from looplab.harness.upload_spool import UploadSpool
from test_campaign_composite import _setup


def _npz(tmp_path):
    keys = [f"q{i}" for i in range(20)]
    path = tmp_path / "pq.npz"
    path.write_bytes(write_npz({"keys": write_npy(keys, dtype="<U4"),
                                "users": write_npy([f"u{i // 2}" for i in range(20)], dtype="<U4"),
                                "m": write_npy([0.5] * 20, dtype="<f8")}))
    return path


def test_a_lost_upload_is_resumed_without_the_file(tmp_path, monkeypatch):
    root, rd, store, api_ok, server, _spawns = _setup(tmp_path, monkeypatch)
    store.append("node_created", {"node_id": 1, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft"}, "code": "print(2)"})
    store.append("node_evaluated", {"node_id": 1, "metric": 0.5})
    generation = run_generation_token(store.read_all())
    forward = api_ok.client._transport.handler
    state = {"down": True}

    def flaky(request):
        if state["down"]:
            raise httpx.ConnectError("connection reset", request=request)
        return forward(request)

    spool_dir = tmp_path / "client-requests"
    api = HarnessAPI("http://testserver/", "agent-secret", transport=httpx.MockTransport(flaky),
                     request_dir=spool_dir)
    path = _npz(tmp_path)
    kwargs = dict(node_id=1, metrics={"m": {"kind": "mean", "array": "m"}}, action_id="up-1",
                  test_label="t")
    lost = composite.upload_per_query(api, "camp", generation, path=str(path), **kwargs)
    assert lost["outcome"] == "unknown" and lost["spool"] == {"spooled": True}
    assert [p["action_id"] for p in api.uploads.pending("camp")] == ["up-1"]
    path.unlink()                                          # the agent's copy is gone
    state["down"] = False
    resumed = composite.resume_per_query_upload(api, "camp", "up-1")
    assert resumed["status"] == 200 and resumed["spool"] == {"resumed": True}
    assert api.uploads.pending() == []
    # Answered: nothing is pending any more, and the receipt says so.
    assert composite.resume_per_query_upload(api, "camp", "up-1")["code"] == "no_pending_upload"
    with pytest.raises(ValueError):
        composite.upload_per_query(api, "camp", generation, path="", node_id=1,
                                   metrics={}, action_id="never-sent")


def test_the_spool_is_bounded_private_and_verifies_its_bytes(tmp_path, monkeypatch):
    from looplab.harness import upload_spool
    spool = UploadSpool(tmp_path, "http://x/")
    assert spool.save("r", "a", b"{}") is None
    meta, body = spool._paths("r", "a")
    assert oct(body.stat().st_mode & 0o777) == "0o600"
    body.write_bytes(b"{tampered}")
    assert spool.load("r", "a") is None                  # a body that does not hash is not resent
    monkeypatch.setattr(upload_spool, "MAX_BODY_BYTES", 4)
    assert "over" in spool.save("r", "b", b"0123456789")
    spool.settle("r", "a", http_status=409)
    assert spool.entry("r", "a")["status"] == "settled" and not body.exists()
