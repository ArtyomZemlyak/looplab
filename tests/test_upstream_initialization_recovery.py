"""Process loss while initializing private Git must not brick later proposals."""
import pytest

from looplab.core.errors import UpstreamRefusal
from looplab.engine import upstream_workspace
from tests.test_upstream_lane import fixture


@pytest.mark.parametrize("cut", ["seed", "init", "add", "commit", "update-ref", "publish"])
def test_abandoned_initialization_allows_a_fresh_measured_proposal(tmp_path, monkeypatch, cut):
    lane, store, generation, body = fixture(tmp_path)
    real_git = upstream_workspace.git_at
    real_publish = upstream_workspace.durable_no_replace_rename
    real_seed = upstream_workspace.seed_pinned_workspace

    def crash_after(root, *argv):
        result = real_git(root, *argv)
        if argv[0] == cut:
            raise SystemExit("Private process loss during initial Git publication")
        return result

    def crash_after_publish(*args, **kwargs):
        real_publish(*args, **kwargs)
        if cut == "publish":
            raise SystemExit("Private process loss after complete Git publication")

    def crash_after_seed(*args, **kwargs):
        real_seed(*args, **kwargs)
        if cut == "seed":
            raise SystemExit("Private process loss after copying the initial archive")

    with monkeypatch.context() as patch:
        patch.setattr(upstream_workspace, "git_at", crash_after)
        patch.setattr(upstream_workspace, "durable_no_replace_rename", crash_after_publish)
        patch.setattr(upstream_workspace, "seed_pinned_workspace", crash_after_seed)
        with pytest.raises(SystemExit):
            lane.propose(body)
    before = store.path.read_bytes()
    staging = lane.rd / "upstream" / (".git-init-" + store.read_all()[-1].data["proposal_id"])
    assert (lane.rd / "upstream/git").exists() == (cut == "publish")
    assert staging.exists() == (cut != "publish")
    with pytest.raises(UpstreamRefusal, match="claim"):
        lane.propose(body)
    assert store.path.read_bytes() == before
    recovered = lane.abandon({"expected_generation": generation, "action_id": "owner-abandon",
        "claim_action_id": body["action_id"], "reason": "The initial private Git process exited"})
    assert recovered["event_type"] == "upstream_gate_abandoned"
    assert not any(e.type == "base_advanced" for e in store.read_all())
    fresh = lane.propose({**body, "action_id": "fresh-proposal"})
    checked = lane.check({"expected_generation": generation, "action_id": "fresh-check",
        "proposal_id": fresh["proposal_id"]})
    assert checked["status"] == "succeeded", checked
    assert len(checked["result"]["executions"]) == 7
    assert staging.exists() == (cut != "publish"), "Interrupted evidence remains inspectable"
    assert not any(e.type == "base_advanced" for e in store.read_all())
