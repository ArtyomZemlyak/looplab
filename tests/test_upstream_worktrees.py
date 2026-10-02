"""Real Git worktrees must preserve the archived base, including ignored/raw files."""
from pathlib import Path

import pytest

from looplab.core.errors import UpstreamRefusal
from looplab.engine.seed_archive import capture_seed_archive
from looplab.engine.upstream_workspace import git_at, maintainer_worktree
from looplab.events.eventstore import EventStore


def seed(tmp_path, files):
    owner, origin, run = (tmp_path / name for name in ("owner", "origin", "run"))
    for root in (owner, origin, run):
        root.mkdir()
    for name, body in files.items():
        target = owner / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    base = capture_seed_archive(owner, origin / "base_snapshots")
    store = EventStore(origin / "events.jsonl")
    event = store.append("workspace_seeded", {"node_id": None, "materialized": [], "base_revision": base})
    return run, {"editables": [{"name": ".", "path": str(owner)}]}, {
        "run_dir": str(origin), "event_seq": event.seq, "digest": base["digest"]}


@pytest.mark.parametrize("policy", ["ignore", "attributes", "ident", "encoding"])
def test_first_and_sibling_worktrees_keep_all_recorded_bytes(tmp_path, policy):
    files = {"runner.txt": b"$Id$\r\noriginal\r\n", "recipe.env": b"MODE=old\n", "nested/recipe.env": b"MODE=nested\n"}
    files[".gitignore" if policy == "ignore" else ".gitattributes"] = (
        b"runner.txt\n*.env\n" if policy == "ignore" else b"*.txt ident\n" if policy == "ident"
        else b"*.txt working-tree-encoding=UTF-16\n" if policy == "encoding" else b"*.txt text eol=lf\n*.env text eol=crlf\n")
    if policy == "encoding":
        files["runner.txt"] = "original\r\nrunner\r\n".encode("utf16")
    run, spec, selector = seed(tmp_path, files)
    for proposal_id in ("up_" + "a" * 24, "up_" + "b" * 24):
        work = maintainer_worktree(run, spec, selector, proposal_id)
        for name, body in files.items():
            assert (work / name).read_bytes() == body
        assert git_at(work, "rev-parse", "--show-toplevel") == work.as_posix()
        assert Path(git_at(work, "rev-parse", "--path-format=absolute", "--git-common-dir")) == run / "upstream/git/.git"


def test_changed_base_ref_refuses_before_authoring_worktree(tmp_path):
    run, spec, selector = seed(tmp_path, {"runner.txt": b"original\n"})
    work = maintainer_worktree(run, spec, selector, "up_" + "a" * 24)
    (work / "runner.txt").write_bytes(b"different\n")
    git_at(work, "add", "-A")
    git_at(work, "commit", "-m", "Deliberately drift the review projection")
    git_at(run / "upstream/git", "update-ref", "refs/looplab/base/" + selector["digest"], git_at(work, "rev-parse", "HEAD"))
    with pytest.raises(UpstreamRefusal, match="recorded base"):
        maintainer_worktree(run, spec, selector, "up_" + "b" * 24)


def test_git_metadata_in_seed_cannot_redirect_private_repository(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    run, spec, selector = seed(tmp_path, {".git": f"gitdir: {outside.as_posix()}\n".encode(), "runner.txt": b"original\n"})
    with pytest.raises(UpstreamRefusal, match="Git metadata"):
        maintainer_worktree(run, spec, selector, "up_" + "a" * 24)
    assert list(outside.iterdir()) == []


def test_git_global_and_environment_config_cannot_execute_filters(tmp_path, monkeypatch):
    marker = tmp_path / "unexpected-filter-call"
    program = tmp_path / "filter.py"
    program.write_text(f"from pathlib import Path\nimport sys\nPath({str(marker)!r}).write_text('executed')\nsys.stdout.write(sys.stdin.read())\n")
    import sys
    config = tmp_path / "global.gitconfig"
    # A real executable filter; both global config and GIT_CONFIG_COUNT are ignored.
    command = f'"{sys.executable}" "{program.as_posix()}"'
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "filter.untrusted.clean")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", command)
    from looplab.runtime.sandbox import git_subprocess_env
    import subprocess
    subprocess.run(["git", "config", "--file", str(config), "filter.untrusted.clean", command],
        env=git_subprocess_env(), check=True, capture_output=True)
    run, spec, selector = seed(tmp_path, {".gitattributes": b"* filter=untrusted\n", "runner.txt": b"original\n"})
    work = maintainer_worktree(run, spec, selector, "up_" + "a" * 24)
    assert (work / "runner.txt").read_bytes() == b"original\n"
    assert not marker.exists()


def test_ignored_candidate_is_committed_and_survives_real_gate_advance(tmp_path):
    from tests.test_upstream_lane import fixture, GENERAL
    lane, store, generation, body = fixture(tmp_path, base_files={
        ".gitignore": "recipe.env\ncapability-notes.txt\n", ".gitattributes": "*.env text eol=crlf\n"})
    original_recipe = (Path(lane.task.editable_path) / "recipe.env").read_bytes()
    lane.task.edit_surface.append("capability-notes.txt")
    (lane.rd / "task.snapshot.json").write_text(lane.task.model_dump_json(), encoding="utf8")
    body["files"]["capability-notes.txt"] = "Recorded capability documentation\n"
    made = lane.propose(body)
    work = lane.rd / "upstream/proposals" / made["proposal_id"] / "worktree"
    assert git_at(work, "show", made["commit"] + ":capability-notes.txt") == body["files"]["capability-notes.txt"].strip()
    checked = lane.check({"expected_generation": generation, "action_id": "check-worktree", "proposal_id": made["proposal_id"]})
    assert checked["result"]["passed"] is True
    lane.advance({"expected_generation": generation, "action_id": "advance-worktree", "proposal_id": made["proposal_id"],
        "expected_base_revision": body["expected_base_revision"], "evidence_token": checked["evidence_token"]})
    next_work = maintainer_worktree(lane.rd, lane.task.repo_spec(), made["selector"], "up_" + "c" * 24)
    assert (next_work / "capability-notes.txt").read_bytes() == body["files"]["capability-notes.txt"].encode()
    assert (next_work / "train.py").read_bytes() == GENERAL.encode()
    assert (next_work / "recipe.env").read_bytes() == original_recipe
