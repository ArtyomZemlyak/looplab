"""A mutable authoring worktree cannot publish bytes outside the approved patch."""
import pytest

from looplab.core.errors import UpstreamRefusal
from looplab.engine import upstream
from tests.test_upstream_lane import fixture


@pytest.mark.parametrize("drift", ["extra", "rewrite", "delete"])
def test_worktree_drift_refuses_publication_before_any_gate(tmp_path, monkeypatch, drift):
    lane, store, generation, body = fixture(tmp_path)
    real = upstream.write_overlay
    def write_and_drift(work, files, deleted):
        real(work, files, deleted)
        if drift == "extra":
            (work / "unsubmitted.env").write_bytes(b"UNAPPROVED=1\n")
        elif drift == "rewrite":
            (work / "README.md").write_bytes(b"Different authoring result from the approved documentation\n")
        else:
            (work / "recipe.env").unlink()
    monkeypatch.setattr(upstream, "write_overlay", write_and_drift)
    with pytest.raises(UpstreamRefusal, match="approved request"):
        lane.propose(body)
    events = store.read_all()
    assert events[-1].type == "upstream_proposal_failed"
    assert not any(e.type in ("upstream_proposed", "upstream_execution", "base_advanced") for e in events)
    before = store.path.read_bytes()
    retry = lane.propose(body)
    assert retry["status"] == "failed" and retry["code"] == "upstream_candidate_changed"
    assert store.path.read_bytes() == before


def test_approved_addition_and_deletion_keep_exact_archive_and_measured_gate(tmp_path):
    lane, store, generation, body = fixture(tmp_path, base_files={"legacy.txt": "Old helper\n"})
    lane.task.edit_surface.extend(["legacy.txt", "new-capability.txt"])
    (lane.rd / "task.snapshot.json").write_text(lane.task.model_dump_json(), encoding="utf8")
    body["files"]["new-capability.txt"] = "Shared helper documentation\n"
    body["deleted"] = ["legacy.txt"]
    made = lane.propose(body)
    from looplab.engine.seed_base import selected_seed_base
    archive, _ = selected_seed_base(made["selector"])
    assert not (archive / "legacy.txt").exists()
    assert (archive / "new-capability.txt").read_bytes() == body["files"]["new-capability.txt"].encode()
    checked = lane.check({"expected_generation": generation, "action_id": "bound-gate", "proposal_id": made["proposal_id"]})
    assert checked["result"]["passed"] is True
    assert len(checked["result"]["executions"]) == 7


def test_drift_at_archive_capture_cannot_pass_an_earlier_snapshot_read(tmp_path, monkeypatch):
    lane, store, generation, body = fixture(tmp_path)
    real = upstream.capture_seed_archive
    def capture_after_drift(snapshot, destination, **kwargs):
        (snapshot / "unsubmitted.env").write_bytes(b"CHANGED_DURING_CAPTURE=1\n")
        return real(snapshot, destination, **kwargs)
    monkeypatch.setattr(upstream, "capture_seed_archive", capture_after_drift)
    with pytest.raises(UpstreamRefusal, match="approved request"):
        lane.propose(body)
    assert store.read_all()[-1].type == "upstream_proposal_failed"
    assert not any(e.type in ("upstream_proposed", "upstream_execution", "base_advanced") for e in store.read_all())
