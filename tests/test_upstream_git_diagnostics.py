"""Path failures remain readable through lost replies without buying more work."""
import errno
from pathlib import Path
import subprocess

import httpx
from fastapi.testclient import TestClient
import pytest

from looplab.core.errors import UpstreamRefusal
from looplab.engine import upstream_workspace
from looplab.harness.mcp_server import HarnessAPI
from looplab.serve.server import make_app
from tests.test_upstream_lane import fixture


@pytest.mark.parametrize("stderr,code", [
    (b"fatal: cannot change to 'private-secret': Filename too long\n", "upstream_git_path_unavailable"),
    (b"fatal: $GIT_DIR too big\n", "upstream_git_path_unavailable"),
    (b"Preparing worktree\nfatal: '$GIT_DIR' too big\n", "upstream_git_path_unavailable"),
    (b"fatal: cannot change to 'Filename too long': Permission denied\n", "upstream_git_unavailable"),
    (b"fatal: not a git repository: private-secret\n", "upstream_git_unavailable"),
])
def test_git_failure_classification_does_not_reflect_stderr(tmp_path, monkeypatch, stderr, code):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 128, b"", stderr))
    with pytest.raises(UpstreamRefusal) as refused:
        upstream_workspace.git_at(tmp_path, "init", "--template=")
    assert refused.value.code == code
    assert "private-secret" not in str(refused.value)
    if code == "upstream_git_path_unavailable":
        assert "shorter server run root" in str(refused.value)
        assert "Do not move this run or repeat training automatically" in str(refused.value)


@pytest.mark.parametrize("kind", ["posix_length", "windows_length", "missing_git", "permission"])
def test_git_spawn_only_classifies_explicit_path_length_errors(tmp_path, monkeypatch, kind):
    error = (OSError(errno.ENAMETOOLONG, "private-secret") if kind == "posix_length" else
             FileNotFoundError(errno.ENOENT, "private-secret") if kind == "missing_git" else
             PermissionError(errno.EACCES, "private-secret") if kind == "permission" else OSError("private-secret"))
    if kind == "windows_length":
        error.winerror = 206
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(UpstreamRefusal if kind.endswith("length") else type(error)) as refused:
        upstream_workspace.git_at(tmp_path, "init", "--template=")
    if kind.endswith("length"):
        assert refused.value.code == "upstream_git_path_unavailable"
        assert "private-secret" not in str(refused.value)
    else:
        assert refused.value is error


@pytest.mark.parametrize("operation", ["cwd", "worktree"])
def test_native_git_path_verdict_has_diagnostics_without_a_guessed_length_cap(tmp_path, operation):
    # Some Git builds support these paths. Probe the actual implementation,
    # rather than refuse every >260-character path or promise arbitrary depth.
    from looplab.runtime.sandbox import git_subprocess_env
    deep = tmp_path / "deep"
    while len(str(deep)) < 310:
        deep /= "nested-abcdefghijklmno"
    deep.mkdir(parents=True)
    if operation == "cwd":
        root, argv = deep, ("init", "--template=")
    else:
        root = tmp_path / "git"
        root.mkdir()
        upstream_workspace.git_at(root, "init", "--template=")
        upstream_workspace.git_at(root, "-c", "user.name=LoopLab", "-c", "user.email=looplab@localhost",
                                  "commit", "--allow-empty", "-m", "base")
        argv = ("worktree", "add", "--detach", str(deep / "review"), "HEAD")
    native = subprocess.run(["git", "-c", "core.longpaths=true", "-C", str(root), *argv],
                            env=git_subprocess_env(), capture_output=True, timeout=30)
    if native.returncode == 0:
        # Do not add the same successful worktree twice. Its cwd is the actual
        # unsupported-path boundary on older Windows Git builds.
        assert upstream_workspace.git_at(deep if operation == "cwd" else deep / "review", "rev-parse", "--git-dir")
    else:
        if operation == "worktree":
            argv = ("worktree", "add", "--detach", str(deep / "review-looplab"), "HEAD")
        with pytest.raises(UpstreamRefusal) as refused:
            upstream_workspace.git_at(root, *argv)
        assert refused.value.code == "upstream_git_path_unavailable", native.stderr
    if (root / ".git/config").exists():
        assert "longpaths" not in (root / ".git/config").read_text(encoding="utf8")


def test_path_failure_is_saved_and_exact_http_mcp_retry_starts_no_work(tmp_path, monkeypatch):
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    monkeypatch.delenv("LOOPLAB_HARNESS_TOKEN", raising=False)
    lane, store, generation, body = fixture(tmp_path)
    owner = Path(lane.task.editable_path)
    original = {p.name: p.read_bytes() for p in owner.iterdir() if p.is_file()}
    real_run, failures = subprocess.run, []
    def fail_initial_git(argv, **kwargs):
        if argv[0] == "git" and argv[-2:] == ["init", "--template="]:
            failures.append(argv)
            return subprocess.CompletedProcess(argv, 128, b"", b"fatal: $GIT_DIR too big\nprivate-secret\n")
        return real_run(argv, **kwargs)
    with TestClient(make_app(tmp_path)) as client:
        def bridge(request):
            response = client.request(request.method, str(request.url), content=request.content, headers=request.headers)
            return httpx.Response(response.status_code, content=response.content)
        api = HarnessAPI("http://testserver", transport=httpx.MockTransport(bridge))
        try:
            with monkeypatch.context() as patch:
                patch.setattr(subprocess, "run", fail_initial_git)
                first = api.upstream_write("run", "proposals", body)
                assert first["status"] == 503
                assert first["body"]["detail"]["code"] == "upstream_git_path_unavailable"
                assert "shorter server run root" in first["body"]["detail"]["message"]
                assert "private-secret" not in str(first)
                before = store.path.read_bytes()
                retry = api.upstream_write("run", "proposals", body)
                assert retry["status"] == 200 and retry["body"]["status"] == "failed", retry
                assert retry["body"]["code"] == "upstream_git_path_unavailable"
                assert api.upstream_write("run", "proposals", body)["body"] == retry["body"]
                history = api.upstream_status("run", generation)
                assert history["status"] == 200, history
                failed = history["body"]["history"][-1]
                assert failed["type"] == "upstream_proposal_failed" and failed["code"] == retry["body"]["code"]
                conflict = api.upstream_write("run", "proposals", {**body, "summary": "changed request"})
                assert conflict["status"] == 409 and conflict["body"]["detail"]["code"] == "upstream_action_conflict"
                assert len(failures) == 1 and store.path.read_bytes() == before
            assert (lane.rd / "upstream" / (".git-init-" + retry["body"]["proposal_id"])).is_dir()
            assert not any(e.type in ("upstream_proposed", "upstream_execution", "base_advanced", "resume") for e in store.read_all())
            assert original == {p.name: p.read_bytes() for p in owner.iterdir() if p.is_file()}
            # After an explicit environment repair, new work requires a new ID.
            # The old failed receipt remains failed; a new measured gate grants
            # no advance until the separate CAS operation is requested.
            fresh = api.upstream_write("run", "proposals", {**body, "action_id": "fixed-environment"})
            assert fresh["status"] == 200 and fresh["body"]["status"] == "succeeded", fresh
            check = api.upstream_write("run", "check", {"expected_generation": generation,
                "action_id": "fixed-environment-check", "proposal_id": fresh["body"]["proposal_id"]})
            assert check["body"]["status"] == "succeeded" and len(check["body"]["result"]["executions"]) == 7, check
            assert api.upstream_write("run", "proposals", body)["body"] == retry["body"]
            assert original == {p.name: p.read_bytes() for p in owner.iterdir() if p.is_file()}
            assert not any(e.type in ("base_advanced", "resume") for e in store.read_all())
        finally:
            api.client.close()
