"""I21/ADR-11 + ADR-13: secret-leak gate and trust-mode sandbox selection."""
from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest

from looplab.core.config import Settings
from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree
from looplab.runtime.sandbox import DockerSandbox, SubprocessSandbox, make_sandbox
from looplab.adapters.toytask import ToyTask

ROOT = Path(__file__).resolve().parents[1]
TASK_FILE = ROOT / "examples" / "toy_task.json"


def test_trust_mode_selects_sandbox():
    assert isinstance(make_sandbox("trusted_local"), SubprocessSandbox)
    d = make_sandbox("untrusted", image="img:9")
    assert isinstance(d, DockerSandbox) and d.image == "img:9"


def test_untrusted_without_docker_fails_loudly(monkeypatch, tmp_path):
    # No docker on PATH -> the untrusted tier refuses to run rather than silently
    # executing the solution unsandboxed (degrading the security boundary).
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _x: None)
    with pytest.raises(RuntimeError, match="needs the docker CLI"):
        make_sandbox("untrusted").run("print(1)", str(tmp_path), 5.0)


def test_untrusted_builds_docker_argv(monkeypatch, tmp_path):
    # With docker present, the solution runs inside `docker run --network none` with the
    # scratch workdir bind-mounted; capture the argv via a stubbed runner (no real docker).
    import shutil

    import looplab.runtime.sandbox as sb
    monkeypatch.setattr(shutil, "which", lambda _x: "/usr/bin/docker")
    seen = {}

    def fake_run_argv(argv, workdir, timeout, env=None, max_output_bytes=64_000, cancel=None):
        seen["argv"] = argv
        return 0, '{"metric": 1.0}', "", False

    monkeypatch.setattr(sb, "_run_argv", fake_run_argv)
    res = make_sandbox("untrusted", image="img:1").run("print(1)", str(tmp_path), 5.0)
    assert res.metric == 1.0
    a = seen["argv"]
    assert a[:5] == ["docker", "run", "--rm", "--network", "none"]
    # the container self-limits via coreutils `timeout` (so a runaway exits from inside + --rm)
    assert "img:1" in a and "timeout" in a
    assert a[-2:] == ["python", "solution.py"]
    assert (tmp_path / "solution.py").read_text(encoding="utf-8") == "print(1)"


@pytest.mark.parametrize("requested, expected", [(float("inf"), 30.0), (1e18, 24 * 3600.0)])
def test_untrusted_bounds_timeout_before_building_docker_argv(
        monkeypatch, tmp_path, requested, expected):
    """The daemon-owned container deadline must be finite/bounded before int() and argv creation."""
    import shutil

    import looplab.runtime.sandbox as sb

    monkeypatch.setattr(shutil, "which", lambda _x: "/usr/bin/docker")
    seen = {}

    def fake_run_argv(argv, workdir, timeout, env=None, max_output_bytes=64_000, cancel=None):
        seen.update(argv=argv, timeout=timeout)
        return 0, '{"metric": 1.0}', "", False

    monkeypatch.setattr(sb, "_run_argv", fake_run_argv)
    res = sb.DockerSandbox().run("print(1)", str(tmp_path), requested)

    assert res.metric == 1.0
    i = seen["argv"].index("timeout")
    assert seen["argv"][i:i + 4] == ["timeout", "-k", "5", str(int(expected))]
    assert seen["timeout"] == expected + 15.0


def test_unknown_trust_mode_rejected():
    with pytest.raises(ValueError):
        make_sandbox("bogus")


def test_secret_is_masked_and_never_persisted(tmp_path, monkeypatch):
    secret = "sk-TESTSECRET-DEADBEEF-d0n0tle4k"
    monkeypatch.setenv("LOOPLAB_LLM_API_KEY", secret)

    s = Settings()
    assert s.llm_api_key is not None
    # Masked snapshot + its JSON never contains the secret value.
    snap = s.masked_snapshot()
    assert snap["llm_api_key"] == "***"
    assert secret not in json.dumps(snap)
    # SecretStr repr does not leak.
    assert secret not in repr(s)

    # Run the engine and scan all on-disk artifacts for the secret value.
    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "config.snapshot.json").write_text(json.dumps(snap), encoding="utf-8")
    task = ToyTask.load(TASK_FILE)
    researcher, developer = task.build_roles()
    eng = Engine(rd, task=task, researcher=researcher, developer=developer,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=5))
    anyio.run(eng.run)

    for f in rd.rglob("*"):
        if f.is_file():
            assert secret not in f.read_text(encoding="utf-8", errors="replace"), f"leak in {f}"


def test_sandbox_redacts_secret_env(tmp_path, monkeypatch):
    from looplab.runtime.sandbox import SubprocessSandbox
    monkeypatch.setenv("MY_SECRET_TOKEN", "leak-me")
    monkeypatch.setenv("LLM_API_KEY", "sk-leak")
    code = ("import os, json\n"
            "print(json.dumps({'secret': 'MY_SECRET_TOKEN' in os.environ,"
            " 'apikey': 'LLM_API_KEY' in os.environ, 'has_path': 'PATH' in os.environ}))\n"
            "print(json.dumps({'metric': 0.0}))\n")
    res = SubprocessSandbox().run(code, str(tmp_path), timeout=30.0)
    assert "sk-leak" not in res.stdout
    info = json.loads(res.stdout.splitlines()[0])
    assert info["secret"] is False and info["apikey"] is False   # secrets stripped from child env
    assert info["has_path"] is True                              # but PATH (functionality) kept
