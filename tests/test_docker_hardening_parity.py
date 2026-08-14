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
    DockerSandbox(image="img:1", network="none", mem="2g", cpus="1.5", runtime="runsc",
                  readonly_rootfs="1g").run("print(1)", str(tmp_path), timeout=30.0)

    wrap = command_eval.make_docker_wrap(
        str(tmp_path), image="img:1", network="none", mem="2g", cpus="1.5", runtime="runsc",
        readonly_rootfs="1g")
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
    # The tmpfs OPTIONS are part of the boundary, not a detail: docker's own `--tmpfs` default is
    # `noexec` at 64 MiB, so a tier that let the default stand would get a scratch that cannot run a
    # pip build — and a tier that spelled `exec` while its twin did not would differ in what an eval
    # can do. Pinned as the exact mount spec on BOTH tiers for the same reason every row here is.
    (("--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=1g"), "a read-only rootfs with no usable tempdir"),
    (("--tmpfs", "/var/tmp:rw,exec,nosuid,nodev,size=1g"), "the second tempdir tier evals use"),
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


@pytest.mark.parametrize("tier", ["solution", "command"])
def test_the_root_filesystem_is_read_only_on_both_tiers_when_asked(tiers, tier):
    """`--read-only` is a lone flag, so it cannot ride in BOUNDARIES' flag/value table — but it is
    the same kind of row: present on both tiers, and BEFORE the image or it is an argument to the
    candidate's command rather than to docker."""
    argv = tiers[tier]
    assert "--read-only" in argv, (
        f"the {tier} tier's docker argv is missing --read-only, which is what stops candidate code "
        "rewriting site-packages, /usr/bin, /etc and the interpreter inside the container")
    assert argv.index("--read-only") < argv.index("img:1")


@pytest.mark.parametrize("tier", ["solution", "command"])
def test_the_work_bind_is_never_read_only(tiers, tier):
    """The bind that carries the eval's OUTPUT stays writable even under `--read-only`.

    This is the row's real design content and the naive reading of "make the container read-only"
    breaks every run: `/work` is the node's own workdir on the host, and the checkpoints, the stage
    logs and the metric FILE the engine reads back afterwards all land there. A `:ro` or a
    `readonly` on THIS mount would not harden the tier, it would delete its output channel.
    Per-source write protection is a different, finer mount (`binds` -> `--mount …,readonly`)."""
    work = [b for a, b in _pairs(tiers[tier]) if a == "-v" and ":/work" in b]
    assert work, f"the {tier} tier no longer binds a host workdir at /work"
    for spec in work:
        assert not spec.endswith(":ro"), (
            f"the {tier} tier binds {spec} read-only; the eval writes its checkpoints, logs and the "
            "metric file the engine reads back through this mount")


def test_readonly_rootfs_is_off_by_default_on_both_tiers(tmp_path, monkeypatch):
    """The DEFAULT is byte-for-byte the historical writable container filesystem.

    Hardening only defaults on where it cannot break a legitimate eval, and this one can (an
    `eval.setup` running apt-get, anything writing under $HOME). So the off state is a pinned
    contract, not an accident of the fixture above passing a size."""
    monkeypatch.setattr(sandbox, "require_docker_cli", lambda _what: None)
    monkeypatch.setattr(command_eval, "require_docker_cli", lambda _what: None)
    captured: dict[str, list[str]] = {}
    monkeypatch.setattr(sandbox, "_run_argv",
                        lambda argv, *a, **kw: (captured.__setitem__("s", list(argv)), (0, "", "", False))[1])
    DockerSandbox(image="img:1").run("print(1)", str(tmp_path), timeout=30.0)
    command = command_eval.make_docker_wrap(str(tmp_path), image="img:1")(["python", "t.py"], str(tmp_path))
    for name, argv in (("solution", captured["s"]), ("command", command)):
        assert "--read-only" not in argv and "--tmpfs" not in argv, (
            f"the {name} tier now hardens the container filesystem by DEFAULT; "
            "sandbox_readonly_rootfs defaults to '' and that is the shipped behaviour")


def test_an_unmountable_scratch_size_refuses_instead_of_silently_unhardening(tmp_path, monkeypatch):
    """The asymmetry with `--memory` is deliberate. `parse_mem_bytes` returns None for garbage so a
    bad memory cap disables a cap rather than crashing an eval; here the same silence would leave the
    operator believing the container filesystem is read-only when it is not — the exact shape of
    silent downgrade `require_docker_cli` already refuses. Both tiers refuse at CONSTRUCTION."""
    from looplab.core.errors import ConfigRefusal

    monkeypatch.setattr(sandbox, "require_docker_cli", lambda _what: None)
    monkeypatch.setattr(command_eval, "require_docker_cli", lambda _what: None)
    with pytest.raises(ConfigRefusal, match="sandbox_readonly_rootfs"):
        DockerSandbox(image="img:1", readonly_rootfs="1gb")
    with pytest.raises(ConfigRefusal, match="sandbox_readonly_rootfs"):
        command_eval.make_docker_wrap(str(tmp_path), image="img:1", readonly_rootfs="1gb")


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
    for token in ("--cap-drop", "--security-opt", "--pids-limit", "--memory", "--cpus",
                  "--read-only", "--tmpfs"):
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
