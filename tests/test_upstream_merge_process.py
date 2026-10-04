"""Rebasing must distinguish scientific conflicts from an unavailable Git process."""
import errno
import subprocess

import pytest

from looplab.core.errors import UpstreamRefusal
from looplab.engine import upstream_workspace


OLD = b"first\nmiddle\nlast\n"
NEW = b"shared\nmiddle\nlast\n"
EDIT = b"first\nmiddle\nexperiment\n\n"
MERGED = b"shared\nmiddle\nexperiment\n\n"


@pytest.mark.parametrize("source", ["global", "environment", "owner"])
def test_real_merge_ignores_broken_host_git_config(tmp_path, monkeypatch, source):
    config = tmp_path / "broken.gitconfig"
    config.write_text("[unfinished\n", encoding="utf8")
    if source == "global":
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    elif source == "environment":
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "merge.conflictStyle")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "invalid-style")
    else:
        upstream_workspace.git_at(tmp_path, "init", "--template=")
        (tmp_path / ".git/config").write_text("[unfinished\n", encoding="utf8")
        monkeypatch.chdir(tmp_path)
    assert upstream_workspace.merge_text(OLD, NEW, EDIT) == MERGED
    assert upstream_workspace.merge_text(OLD, NEW, b"scientific\nmiddle\nlast\n") is None


def test_real_merge_preserves_unicode_crlf_and_final_blank_lines():
    old = "первое\r\nсередина\r\nпоследнее\r\n".encode()
    new = "общее\r\nсередина\r\nпоследнее\r\n".encode()
    edit = "первое\r\nсередина\r\nопыт 🧪\r\n\r\n".encode()
    assert upstream_workspace.merge_text(old, new, edit) == "общее\r\nсередина\r\nопыт 🧪\r\n\r\n".encode()


@pytest.mark.parametrize("code", [128, 255, -9])
def test_failed_merge_process_is_unavailable_not_a_scientific_conflict(monkeypatch, code):
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw:
        subprocess.CompletedProcess(argv, code, b"private-secret", b"fatal: private-secret"))
    with pytest.raises(UpstreamRefusal) as refused:
        upstream_workspace.merge_text(OLD, NEW, EDIT)
    assert refused.value.code == "upstream_git_unavailable"
    assert "private-secret" not in str(refused.value)


@pytest.mark.parametrize("kind,code", [("missing", "upstream_git_process_unavailable"),
    ("permission", "upstream_git_process_unavailable"), ("timeout", "upstream_git_timeout_unavailable")])
def test_merge_startup_and_deadline_refusals_are_typed(monkeypatch, kind, code):
    def fail(argv, **kw):
        if kind == "timeout":
            raise subprocess.TimeoutExpired(argv, kw["timeout"], output=b"private-secret")
        if kind == "missing":
            raise FileNotFoundError(errno.ENOENT, "private-secret")
        raise PermissionError(errno.EACCES, "private-secret")
    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(UpstreamRefusal) as refused:
        upstream_workspace.merge_text(OLD, NEW, EDIT)
    assert refused.value.code == code
    assert "private-secret" not in str(refused.value)


def test_merge_child_environment_is_scrubbed_and_keeps_ten_second_deadline(monkeypatch):
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "private-secret")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "merge.conflictStyle")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "invalid-style")
    real_run, seen = subprocess.run, []
    def observe(argv, **kw):
        seen.append((argv, kw))
        return real_run(argv, **kw)
    monkeypatch.setattr(subprocess, "run", observe)
    assert upstream_workspace.merge_text(OLD, NEW, EDIT) == MERGED
    assert len(seen) == 1
    argv, kw = seen[0]
    assert kw["timeout"] == 10 and "-C" in argv
    assert "LOOPLAB_UI_TOKEN" not in kw["env"]
    assert "GIT_CONFIG_COUNT" not in kw["env"]


@pytest.mark.parametrize("code", [1, 127])
def test_conflict_exit_counts_never_install_conflict_markers(monkeypatch, code):
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw:
        subprocess.CompletedProcess(argv, code, b"<<<<<<< unresolved\n", b""))
    assert upstream_workspace.merge_text(OLD, NEW, EDIT) is None


def test_rebase_git_failure_preserves_real_two_base_pending_workspace(tmp_path, monkeypatch):
    from tests.test_upstream_multibase import twice, materialize, RATE_GENERAL
    from looplab.events.replay import fold
    lane, store, _, _, _, _ = twice(tmp_path)
    work = lane.rd / "nodes/node_5"
    original = {p.name: p.read_bytes() for p in work.iterdir() if p.is_file()}
    before = store.path.read_bytes()
    authored = fold(store.read_all()).nodes[5].model_dump()
    real_run, calls = subprocess.run, []
    def fail_merge(argv, **kw):
        if "merge-file" in argv:
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 128, b"", b"fatal: private-secret")
        return real_run(argv, **kw)
    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "run", fail_merge)
        with pytest.raises(UpstreamRefusal) as refused:
            materialize(lane, store, 5)
        assert refused.value.code == "upstream_git_unavailable"
    assert len(calls) == 1
    assert store.path.read_bytes() == before
    assert fold(store.read_all()).nodes[5].model_dump() == authored
    assert original == {p.name: p.read_bytes() for p in work.iterdir() if p.is_file()}
    # Explicit recovery retries only materialization; no third gate or training.
    materialize(lane, store, 5)
    assert (work / "train.py").read_bytes() == RATE_GENERAL.replace("range(30)", "range(20)").encode()
    assert (work / "recipe.env").read_bytes() == b"MOMENTUM=0.4\n"
    assert len([e for e in store.read_all() if e.type == "upstream_execution"]) == 14
