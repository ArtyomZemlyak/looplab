"""Both untrusted Docker tiers get the SAME hardening flags (doc 25 RA-03).

LoopLab runs candidate code two ways behind a container: `DockerSandbox.run` executes a generated
`solution.py`, and `command_eval.make_docker_wrap` wraps an arbitrary RepoTask command. Each used to
assemble its own `docker run` argv, kept in step by a comment reading "mirror
sandbox.DockerSandbox.run". They had already drifted once — for a while the solution.py path ran
with default capabilities as root and no memory or CPU bound, which `DockerSandbox.__init__` still
records — and nothing failed when it happened, because a missing `--cap-drop` produces a container
that works perfectly right up until something abuses it.

So the guard cannot be "the builder exists". It is: drive BOTH tiers with the same inputs and assert
each boundary flag is present in each, and that neither tier reaches `docker` without the builder.
"""
from __future__ import annotations

import ast
import inspect
import re

import pytest

from looplab.runtime import command_eval, sandbox
from looplab.runtime.sandbox import DockerSandbox, docker_run_argv, require_docker_cli


def _pairs(argv: list[str]) -> set[tuple[str, str]]:
    """Flag/value pairs, so `--memory 2g` is checked as a unit rather than two loose tokens."""
    return {(argv[i], argv[i + 1]) for i in range(len(argv) - 1)}


@pytest.fixture
def tiers(tmp_path, monkeypatch):
    """The argv each tier actually builds, for one shared configuration."""
    monkeypatch.setattr(sandbox, "require_docker_cli", lambda _what: None)
    monkeypatch.setattr(command_eval, "require_docker_cli", lambda _what: None)
    captured: dict[str, list[str]] = {}

    def _fake_run_argv(argv, *_a, **_kw):
        captured["solution"] = list(argv)
        return (0, "", "", False)

    monkeypatch.setattr(sandbox, "_run_argv", _fake_run_argv)
    DockerSandbox(image="img:1", network="none", mem="2g", cpus="1.5",
                  runtime="runsc").run("print(1)", str(tmp_path), timeout=30.0)

    wrap = command_eval.make_docker_wrap(
        str(tmp_path), image="img:1", network="none", mem="2g", cpus="1.5", runtime="runsc")
    command = wrap(["python", "train.py"], str(tmp_path))
    return {"solution": captured["solution"], "command": command}


# Each entry is a boundary that must hold on BOTH tiers, with what it stops if it does not.
BOUNDARIES = [
    (("--cap-drop", "ALL"), "ambient Linux capabilities inside the container"),
    (("--security-opt", "no-new-privileges"), "setuid privilege escalation from inside"),
    (("--pids-limit", "1024"), "a fork bomb taking the host down"),
    (("--network", "none"), "network egress from candidate code"),
    (("--memory", "2g"), "one tenant OOMing the host"),
    (("--cpus", "1.5"), "one tenant saturating every core"),
    (("--runtime", "runsc"), "falling back to the shared-kernel runtime on the hostile tier"),
]


@pytest.mark.parametrize("tier", ["solution", "command"])
@pytest.mark.parametrize("flag,stops", BOUNDARIES, ids=[f[0][0] for f in BOUNDARIES])
def test_every_boundary_flag_is_on_both_tiers(tiers, tier, flag, stops):
    assert flag in _pairs(tiers[tier]), (
        f"the {tier} tier's docker argv is missing {flag[0]} {flag[1]}, which is what stops {stops}")


@pytest.mark.parametrize("tier", ["solution", "command"])
def test_the_container_is_removed_and_the_workdir_is_bound(tiers, tier):
    argv = tiers[tier]
    assert argv[:3] == ["docker", "run", "--rm"]     # per-run: no leaked writable layer
    assert any(a == "-v" and b.endswith(":/work") for a, b in _pairs(argv))
    assert ("-w", "/work") in _pairs(argv)


@pytest.mark.parametrize("tier", ["solution", "command"])
def test_the_image_precedes_the_in_container_command(tiers, tier):
    """A flag that lands AFTER the image is an argument to the candidate's command, not to docker —
    it would silently stop being a boundary while still appearing in the argv."""
    argv = tiers[tier]
    image = argv.index("img:1")
    for flag, _stops in BOUNDARIES:
        assert flag[0] in argv[:image], f"{flag[0]} is past the image on the {tier} tier"


def test_user_is_not_set_on_either_tier(tiers):
    """Pinned as a DECISION, not an oversight: the /work bind is host-owned, so a non-root uid often
    cannot write the predictions and artifacts the eval must produce. Someone adding `--user` for
    defence in depth should have to change this test and read why."""
    for tier, argv in tiers.items():
        assert "--user" not in argv, f"{tier} tier sets --user; see docker_run_argv's docstring"


def _code(fn) -> str:
    """Function source with docstrings stripped — a docstring may legitimately DISCUSS the flags."""
    tree = ast.parse(inspect.getsource(fn).lstrip())
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and body
                and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            node.body = body[1:]
    return ast.unparse(tree)


@pytest.mark.parametrize("fn,name", [
    (DockerSandbox.run, "DockerSandbox.run"),
    (command_eval.make_docker_wrap, "make_docker_wrap"),
])
def test_neither_tier_assembles_its_own_hardening_argv(fn, name):
    code = _code(fn)
    assert "docker_run_argv" in code, f"{name} no longer builds its argv through the shared builder"
    for token in ("--cap-drop", "--security-opt", "--pids-limit", "--memory", "--cpus"):
        assert token not in code, (
            f"{name} spells {token} itself again; the flag belongs to docker_run_argv so the other "
            "tier cannot be left without it")
    # `docker run` itself must not be re-spelled either — that is how a second argv starts.
    assert not re.search(r'"docker"\s*,\s*"run"', code), f"{name} re-opens its own docker argv"


def test_a_missing_docker_cli_refuses_loudly_on_both_tiers(monkeypatch, tmp_path):
    """Silently running unsandboxed is the one outcome `trust_mode='untrusted'` must never have."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="docker CLI"):
        DockerSandbox().run("print(1)", str(tmp_path))
    with pytest.raises(RuntimeError, match="docker CLI"):
        command_eval.make_docker_wrap(str(tmp_path), image="img:1")
    # The message must name the escape hatch, or the operator's only option is to guess.
    with pytest.raises(RuntimeError, match="trusted_local"):
        require_docker_cli("solution")


def test_an_empty_cap_is_omitted_rather_than_passed_as_an_empty_flag():
    """Both callers spell "unbounded" as a falsy value; `--memory ""` would be a docker CLI error."""
    argv = docker_run_argv("img:1", network="none", mount_root=".", workdir="/work",
                           mem="", cpus=None)
    assert "--memory" not in argv and "--cpus" not in argv
    assert ("--cap-drop", "ALL") in _pairs(argv)      # ...while the unconditional caps stay
