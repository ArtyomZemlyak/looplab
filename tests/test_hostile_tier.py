"""B4+ true-isolation tier: gVisor (runsc) runtime selected by trust_mode='hostile'."""
from __future__ import annotations

import shutil

import pytest

from looplab.runtime.sandbox import DockerSandbox, make_sandbox


def test_hostile_tier_uses_gvisor_runtime():
    s = make_sandbox("hostile", image="python:3.12-slim")
    assert isinstance(s, DockerSandbox) and s.runtime == "runsc"


def test_untrusted_tier_uses_default_runtime():
    s = make_sandbox("untrusted")
    assert isinstance(s, DockerSandbox) and s.runtime is None


def test_explicit_runtime_override():
    s = make_sandbox("hostile", runtime="kata-runtime")
    assert s.runtime == "kata-runtime"


# `docker` marker: selection only (`-m "not docker"`); the skipif stays the enforcement gate.
@pytest.mark.docker
@pytest.mark.skipif(not shutil.which("docker"), reason="docker not on PATH")
def test_docker_wrap_includes_runtime_flag(tmp_path):
    from looplab.runtime.command_eval import make_docker_wrap
    w = make_docker_wrap(str(tmp_path), "python:3.12-slim", runtime="runsc")
    argv = w(["python", "x.py"], str(tmp_path))
    assert "--runtime" in argv and "runsc" in argv
