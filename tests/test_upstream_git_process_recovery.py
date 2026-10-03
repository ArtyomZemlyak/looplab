"""Git process failures are operator diagnostics and durable failed claims."""
import errno
import json
from pathlib import Path
import subprocess
import sys

import httpx
from fastapi.testclient import TestClient
import pytest

from looplab.core.errors import UpstreamRefusal
from looplab.engine import upstream_workspace
from looplab.harness.mcp_server import HarnessAPI
from looplab.serve.server import make_app
from looplab.tools.perm_modes import APPROVAL_ALLOW_ONCE
from looplab.tools.upstream_tools import UpstreamTools
from tests.test_upstream_lane import fixture


def failure(kind):
    if kind == "timeout":
        return subprocess.TimeoutExpired(["git", "private-secret"], 30, output=b"private-secret", stderr=b"private-secret")
    if kind == "missing":
        return FileNotFoundError(errno.ENOENT, "private-secret")
    return PermissionError(errno.EACCES, "private-secret")


@pytest.mark.parametrize("kind,code", [("timeout", "upstream_git_timeout_unavailable"),
    ("missing", "upstream_git_process_unavailable"), ("permission", "upstream_git_process_unavailable")])
def test_git_process_failure_is_typed_without_exception_text(tmp_path, monkeypatch, kind, code):
    def fail(*args, **kwargs):
        raise failure(kind)
    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(UpstreamRefusal) as refused:
        upstream_workspace.git_at(tmp_path, "init", "--template=")
    assert refused.value.code == code
    assert "private-secret" not in str(refused.value)
    assert "upstream history" in str(refused.value)


def test_native_executable_lookup_failure_names_the_server_environment(tmp_path, monkeypatch):
    empty = tmp_path / "empty-executable-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(UpstreamRefusal) as refused:
        upstream_workspace.git_at(tmp_path, "init", "--template=")
    assert refused.value.code == "upstream_git_process_unavailable"
    assert "UI/engine process environment" in str(refused.value)
    assert isinstance(refused.value.__cause__, FileNotFoundError)
    assert not (tmp_path / ".git").exists()


def test_native_child_deadline_maps_to_a_failed_git_diagnostic(tmp_path, monkeypatch):
    # Drive subprocess.run's actual kill/wait timeout, using a private sleeping
    # CPU child and a short test deadline. The production Git deadline stays 30s.
    real_run = subprocess.run
    def timed_child(argv, **kwargs):
        assert argv[0] == "git" and kwargs["timeout"] == 30
        return real_run([sys.executable, "-c", "import time; time.sleep(30)"],
                        env=kwargs["env"], capture_output=True, timeout=0.05)
    monkeypatch.setattr(subprocess, "run", timed_child)
    with pytest.raises(UpstreamRefusal) as refused:
        upstream_workspace.git_at(tmp_path, "init", "--template=")
    assert refused.value.code == "upstream_git_timeout_unavailable"
    assert isinstance(refused.value.__cause__, subprocess.TimeoutExpired)
    assert not (tmp_path / ".git").exists()


def test_git_process_containment_does_not_hide_programming_errors(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("implementation defect")
    monkeypatch.setattr(subprocess, "run", broken)
    with pytest.raises(RuntimeError, match="implementation defect"):
        upstream_workspace.git_at(tmp_path, "init", "--template=")


@pytest.mark.parametrize("kind", ["timeout", "missing", "permission"])
@pytest.mark.parametrize("first_client", ["http", "assistant"])
def test_failed_git_claim_is_recoverable_in_assistant_and_mcp_without_reexecution(
        tmp_path, monkeypatch, kind, first_client):
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    monkeypatch.delenv("LOOPLAB_HARNESS_TOKEN", raising=False)
    lane, store, generation, body = fixture(tmp_path)
    owner = Path(lane.task.editable_path)
    original = {p.name: p.read_bytes() for p in owner.iterdir() if p.is_file()}
    real_run, calls = subprocess.run, []
    def fail_git(argv, **kwargs):
        if argv[0] == "git" and argv[-2:] == ["init", "--template="]:
            calls.append(argv)
            raise failure(kind)
        return real_run(argv, **kwargs)
    assistant = UpstreamTools(tmp_path, mode="auto", approver=lambda action: APPROVAL_ALLOW_ONCE)
    with TestClient(make_app(tmp_path)) as client:
        def bridge(request):
            response = client.request(request.method, str(request.url), content=request.content, headers=request.headers)
            return httpx.Response(response.status_code, content=response.content)
        api = HarnessAPI("http://testserver", transport=httpx.MockTransport(bridge))
        try:
            with monkeypatch.context() as patch:
                patch.setattr(subprocess, "run", fail_git)
                code = "upstream_git_timeout_unavailable" if kind == "timeout" else "upstream_git_process_unavailable"
                if first_client == "http":
                    initial = api.upstream_write("run", "proposals", body)
                    assert initial["status"] == 503 and initial["outcome"] == "unknown"
                    assert initial["body"]["detail"]["code"] == code
                    assert "upstream history" in initial["body"]["detail"]["message"]
                    assert "private-secret" not in str(initial)
                else:
                    initial = assistant.execute("upstream_propose", {"run_id": "run", "body": body})
                    assert initial.is_error and "upstream history" in initial.content
                    assert "private-secret" not in initial.content
                events = store.read_all()
                assert events[-1].type == "upstream_proposal_failed" and events[-1].data["code"] == code
                before = store.path.read_bytes()
                recovered = api.upstream_write("run", "proposals", body)
                assert recovered["status"] == 200 and recovered["body"]["status"] == "failed", recovered
                assert recovered["body"]["code"] == code
                reply = assistant.execute("upstream_propose", {"run_id": "run", "body": body})
                assert reply.is_error and reply.structured == recovered["body"]
                assert json.loads(reply.content)["code"] == code
                history = api.upstream_status("run", generation)
                assert history["status"] == 200 and history["body"]["history"][-1]["code"] == code
                assert len(calls) == 1 and store.path.read_bytes() == before
                assert not any(e.type in ("upstream_proposed", "upstream_execution", "base_advanced", "resume") for e in store.read_all())
                assert original == {p.name: p.read_bytes() for p in owner.iterdir() if p.is_file()}
                assert (lane.rd / "upstream" / (".git-init-" + recovered["body"]["proposal_id"])).is_dir()
        finally:
            api.client.close()
