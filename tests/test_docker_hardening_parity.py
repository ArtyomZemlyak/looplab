"""Every untrusted Docker tier gets the SAME hardening flags, from the SAME configuration.

LoopLab runs code behind a container on THREE surfaces: `DockerSandbox.run` executes a generated
`solution.py`, `command_eval.make_docker_wrap` wraps an arbitrary RepoTask command, and
`tools/shell_tools.py::ShellTools` runs the operator assistant's `run_command`/`run_tests`/`git`
under a non-trusted trust mode. Each of the first two used to assemble its own `docker run` argv,
kept in step by a comment reading "mirror sandbox.DockerSandbox.run". They had drifted once —
for a while the solution.py path ran with default capabilities as root and no memory or CPU bound,
which `DockerSandbox.__init__` still records — and nothing failed then, because a missing
`--cap-drop` produces a container that works perfectly right up until something abuses it.

`docker_run_argv` closed that. What it did NOT close, and what this file learned on 2026-08-15, is
the level above it: what the OPERATOR's configuration means for a container was still answered
independently at each call site, and the assistant's shell answered it with nothing at all. It
passed `(root, image, network="none")`, so it inherited every flag `docker_run_argv` applies
unconditionally and none of the caller-supplied ones — measured on shipped defaults, no `--memory
4g`; under `trust_mode="hostile"`, no `--runtime runsc`, i.e. a shared-kernel container on the tier
an operator chooses BECAUSE a shared kernel is not enough. This file's own parity table was the
reason that stayed invisible for as long as it did: it asserted parity over a set of two, and the
third surface was not in it. That is the general lesson — a parity guard is only as strong as its
tier list, so the list is now derived to the extent it can be and the missing member is the finding.

So the guard cannot be "the builder exists", and it cannot be "the two builders agree" either. It
is: build ALL THREE tiers from ONE `Settings` object and assert every boundary flag lands in each.
"""
from __future__ import annotations

import ast
import inspect
import re

import pytest

from looplab.core.config import Settings
from looplab.runtime import command_eval, sandbox
from looplab.runtime.sandbox import (DockerSandbox, docker_run_argv, docker_tier_kwargs,
                                     require_docker_cli)

# The ONE configuration all three tiers are built from below. Every value is deliberately NOT a
# shipped default, so a tier that ignored the operator and fell back to its own idea of a container
# is visible as a wrong VALUE rather than passing on a coincidence.
TIER_SETTINGS = Settings(trust_mode="hostile", docker_image="img:1", sandbox_memory="2g",
                         sandbox_cpus="1.5", sandbox_readonly_rootfs="1g")

TIERS = ("solution", "command", "assistant")


def _pairs(argv: list[str]) -> set[tuple[str, str]]:
    """Flag/value pairs, so `--memory 2g` is checked as a unit rather than two loose tokens."""
    return {(argv[i], argv[i + 1]) for i in range(len(argv) - 1)}


@pytest.fixture
def tiers(tmp_path, monkeypatch):
    """The argv each tier actually builds, for one shared `Settings`.

    Each tier is driven through its REAL entry point rather than through `docker_tier_kwargs`
    directly: a tier that stopped consulting the bundle, or consulted it and then overwrote a key,
    would still be green against a fixture that called the bundle itself.
    """
    monkeypatch.setattr(sandbox, "require_docker_cli", lambda _what: None)
    monkeypatch.setattr(command_eval, "require_docker_cli", lambda _what: None)
    captured: dict[str, list[str]] = {}

    def _fake_run_argv(argv, *_a, **_kw):
        captured["solution"] = list(argv)
        return (0, "", "", False)

    monkeypatch.setattr(sandbox, "_run_argv", _fake_run_argv)
    # The solution tier as `cli/__init__.py::_engine` builds it: the bundle, plus the two
    # subprocess-tier caps that describe no container.
    sandbox.make_sandbox(TIER_SETTINGS.trust_mode, **docker_tier_kwargs(TIER_SETTINGS),
                         mem_local="", fsize_local="").run("print(1)", str(tmp_path), timeout=30.0)

    # The command tier as `engine/eval_dispatch.py` builds it.
    wrap = command_eval.make_docker_wrap(str(tmp_path), **docker_tier_kwargs(TIER_SETTINGS))
    command = wrap(["python", "train.py"], str(tmp_path))

    # The assistant tier, driven END TO END through the gated exec the model actually reaches —
    # `ShellTools.exec_argv` -> permission mode -> wrap -> `sandbox.run_argv` — so what is captured
    # is the argv the shell RUNS, not one a test rebuilt beside it. `run_argv` (public) is a
    # different seam from `_run_argv` above: shell_tools imports the former inside the call.
    def _fake_public_run_argv(argv, *_a, **_kw):
        captured["assistant"] = list(argv)
        return (0, "", "", False)

    monkeypatch.setattr(sandbox, "run_argv", _fake_public_run_argv)
    from looplab.tools.shell_tools import ShellTools
    ShellTools([tmp_path], mode="auto", trust_mode=TIER_SETTINGS.trust_mode,
               settings=TIER_SETTINGS, approver=lambda _a: "allow_once",
               ).execute("run_command", {"command": ["python", "train.py"]})
    return {"solution": captured["solution"], "command": command,
            "assistant": captured["assistant"]}


# Each entry is a boundary that must hold on EVERY tier, with what it stops if it does not.
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
    # can do. Pinned as the exact mount spec on EVERY tier for the same reason every row here is.
    (("--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=1g"), "a read-only rootfs with no usable tempdir"),
    (("--tmpfs", "/var/tmp:rw,exec,nosuid,nodev,size=1g"), "the second tempdir tier evals use"),
]


@pytest.mark.parametrize("tier", TIERS)
@pytest.mark.parametrize("flag,stops", BOUNDARIES, ids=[f[0][0] for f in BOUNDARIES])
def test_every_boundary_flag_is_on_every_tier(tiers, tier, flag, stops):
    assert flag in _pairs(tiers[tier]), (
        f"the {tier} tier's docker argv is missing {flag[0]} {flag[1]}, which is what stops {stops}")


@pytest.mark.parametrize("tier", TIERS)
def test_the_container_is_removed_and_the_workdir_is_bound(tiers, tier):
    argv = tiers[tier]
    assert argv[:3] == ["docker", "run", "--rm"]     # per-run: no leaked writable layer
    assert any(a == "-v" and b.endswith(":/work") for a, b in _pairs(argv))
    assert ("-w", "/work") in _pairs(argv)


@pytest.mark.parametrize("tier", TIERS)
def test_the_image_precedes_the_in_container_command(tiers, tier):
    """A flag that lands AFTER the image is an argument to the candidate's command, not to docker —
    it would silently stop being a boundary while still appearing in the argv."""
    argv = tiers[tier]
    image = argv.index("img:1")
    for flag, _stops in BOUNDARIES:
        assert flag[0] in argv[:image], f"{flag[0]} is past the image on the {tier} tier"


@pytest.mark.parametrize("tier", TIERS)
def test_the_root_filesystem_is_read_only_on_every_tier_when_asked(tiers, tier):
    """`--read-only` is a lone flag, so it cannot ride in BOUNDARIES' flag/value table — but it is
    the same kind of row: present on every tier, and BEFORE the image or it is an argument to the
    candidate's command rather than to docker."""
    argv = tiers[tier]
    assert "--read-only" in argv, (
        f"the {tier} tier's docker argv is missing --read-only, which is what stops candidate code "
        "rewriting site-packages, /usr/bin, /etc and the interpreter inside the container")
    assert argv.index("--read-only") < argv.index("img:1")


@pytest.mark.parametrize("tier", TIERS)
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


def test_readonly_rootfs_is_off_by_default_on_every_tier(tmp_path, monkeypatch):
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
    monkeypatch.setattr(sandbox, "run_argv",
                        lambda argv, *a, **kw: (captured.__setitem__("a", list(argv)),
                                                (0, "", "", False))[1])
    from looplab.tools.shell_tools import ShellTools
    ShellTools([tmp_path], mode="auto", trust_mode="untrusted", approver=lambda _a: "allow_once",
               ).execute("run_command", {"command": ["python", "t.py"]})
    for name, argv in (("solution", captured["s"]), ("command", command),
                       ("assistant", captured["a"])):
        assert "--read-only" not in argv and "--tmpfs" not in argv, (
            f"the {name} tier now hardens the container filesystem by DEFAULT; "
            "sandbox_readonly_rootfs defaults to '' and that is the shipped behaviour")


def test_the_shipped_defaults_reach_the_assistant_shell(tmp_path, monkeypatch):
    """The regression, stated as the measurement that found it.

    An assistant shell constructed with NO settings still gets the shipped container, not an
    unconfigured one: `sandbox_memory` defaults to `4g`, and that cap is what stops the operator's
    chat surface OOMing the box the engine is training on. Before 2026-08-15 this argv carried no
    `--memory` at all, while the eval tier beside it on the same box carried `--memory 4g`.

    `settings=None` resolving to `Settings()` rather than to an empty bundle is the load-bearing
    half: a surface that cannot see the configuration must not thereby get a weaker container.
    """
    monkeypatch.setattr(sandbox, "require_docker_cli", lambda _what: None)
    monkeypatch.setattr(command_eval, "require_docker_cli", lambda _what: None)
    seen: dict[str, list[str]] = {}
    monkeypatch.setattr(sandbox, "run_argv",
                        lambda argv, *a, **kw: (seen.__setitem__("a", list(argv)),
                                                (0, "", "", False))[1])
    from looplab.tools.shell_tools import ShellTools
    ShellTools([tmp_path], mode="auto", trust_mode="untrusted", approver=lambda _a: "allow_once",
               ).execute("run_command", {"command": ["echo", "hi"]})
    assert ("--memory", Settings().sandbox_memory) in _pairs(seen["a"])
    assert Settings().docker_image in seen["a"]


def test_the_assistant_shell_derives_its_runtime_from_its_own_trust_mode(tmp_path, monkeypatch):
    """`hostile` means gVisor on THIS surface too, and the mode that decided to containerize is the
    one that picks the runtime.

    `ShellTools` takes `trust_mode` as a constructor argument — it decides whether there is a
    container at all — and may be handed no `Settings`. Deriving "is this hostile?" from a different
    object than the one that said "containerize" puts a shared-kernel container on the tier chosen
    because a shared kernel is not enough, which is the whole finding one level up.
    """
    monkeypatch.setattr(sandbox, "require_docker_cli", lambda _what: None)
    monkeypatch.setattr(command_eval, "require_docker_cli", lambda _what: None)
    seen: dict[str, list[str]] = {}
    monkeypatch.setattr(sandbox, "run_argv",
                        lambda argv, *a, **kw: (seen.__setitem__("a", list(argv)),
                                                (0, "", "", False))[1])
    from looplab.tools.shell_tools import ShellTools
    ShellTools([tmp_path], mode="auto", trust_mode="hostile", approver=lambda _a: "allow_once",
               ).execute("run_command", {"command": ["echo", "hi"]})
    assert ("--runtime", sandbox.HOSTILE_RUNTIME) in _pairs(seen["a"]), (
        "a hostile-tier assistant shell ran on the default shared-kernel runtime")


@pytest.mark.parametrize("field,flag,value", [
    ("sandbox_memory", "--memory", "7g"),
    ("sandbox_cpus", "--cpus", "0.25"),
    ("docker_image", None, "acme/ml:9"),
])
@pytest.mark.parametrize("tier", TIERS)
def test_one_settings_change_reaches_every_tier(tmp_path, monkeypatch, tier, field, flag, value):
    """The property the shared derivation exists for, driven rather than pinned.

    Change ONE field on ONE `Settings` and every containerized surface must move with it. This is
    what a source pin on `docker_tier_kwargs` would NOT catch: a call site that consults the bundle
    and then overwrites a key still contains the call.
    """
    monkeypatch.setattr(sandbox, "require_docker_cli", lambda _what: None)
    monkeypatch.setattr(command_eval, "require_docker_cli", lambda _what: None)
    s = TIER_SETTINGS.model_copy(update={field: value})
    captured: dict[str, list[str]] = {}
    monkeypatch.setattr(sandbox, "_run_argv",
                        lambda argv, *a, **kw: (captured.__setitem__("solution", list(argv)),
                                                (0, "", "", False))[1])
    monkeypatch.setattr(sandbox, "run_argv",
                        lambda argv, *a, **kw: (captured.__setitem__("assistant", list(argv)),
                                                (0, "", "", False))[1])
    sandbox.make_sandbox(s.trust_mode, **docker_tier_kwargs(s), mem_local="", fsize_local="").run(
        "print(1)", str(tmp_path), timeout=30.0)
    captured["command"] = command_eval.make_docker_wrap(
        str(tmp_path), **docker_tier_kwargs(s))(["python", "t.py"], str(tmp_path))
    from looplab.tools.shell_tools import ShellTools
    ShellTools([tmp_path], mode="auto", trust_mode=s.trust_mode, settings=s,
               approver=lambda _a: "allow_once").execute("run_command", {"command": ["echo", "hi"]})
    argv = captured[tier]
    if flag is None:      # the image is positional, not a flag/value pair
        assert value in argv, f"the {tier} tier ignored {field}={value}"
    else:
        assert (flag, value) in _pairs(argv), f"the {tier} tier ignored {field}={value}"


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


def test_user_is_not_set_on_any_tier(tiers):
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


def test_a_missing_docker_cli_refuses_loudly_on_every_tier(monkeypatch, tmp_path):
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
    # The assistant shell's `execute` contains every exception so a tool call can never kill the
    # chat loop, so its refusal is a RETURNED string rather than a raise — but the property that
    # matters is the same one and is asserted on the EFFECT: the command must not run. A shell that
    # fell through to the host here would be the un-sandboxed execution `require_docker_cli` exists
    # to prevent, reported to the operator as a successful command.
    ran: list[list[str]] = []
    monkeypatch.setattr(sandbox, "run_argv",
                        lambda argv, *a, **kw: (ran.append(list(argv)), (0, "", "", False))[1])
    from looplab.tools.shell_tools import ShellTools
    out = ShellTools([tmp_path], mode="auto", trust_mode="untrusted",
                     approver=lambda _a: "allow_once").execute("run_command",
                                                               {"command": ["echo", "hi"]})
    assert ran == [], "the assistant shell ran a command on the host with no docker CLI present"
    assert "docker CLI" in out and "trusted_local" in out


def test_the_assistant_composition_root_hands_the_shell_its_settings(tmp_path, monkeypatch):
    """The WIRING, which the tier fixture cannot see because it constructs `ShellTools` directly.

    `serve/assistant.py::build_tools` is where the operator's `Settings` and this shell meet. A
    dropped `settings=` there does not fail: it falls back to `Settings()`, i.e. every operator who
    RAISED `sandbox_memory` for a big box, or set `sandbox_readonly_rootfs`, silently gets the
    shipped default on this surface instead. That is the quiet half of the same defect, so it is
    asserted on the composed object rather than on the call.
    """
    pytest.importorskip("fastapi")
    from looplab.serve.assistant import build_tools
    from looplab.tools.shell_tools import ShellTools

    s = TIER_SETTINGS.model_copy(update={"sandbox_memory": "11g"})
    tools = build_tools(str(tmp_path), mode="auto", trust_mode="untrusted", settings=s)
    shells = [p for p in tools.providers if isinstance(p, ShellTools)]
    assert shells, "the assistant no longer composes a ShellTools in a mutating mode"
    for sh in shells:
        assert docker_tier_kwargs(sh._settings, trust_mode=sh.trust_mode)["mem"] == "11g", (
            "the assistant's shell was built without the operator's container configuration")


@pytest.mark.parametrize("dotted", [
    "looplab.cli.__init__",           # the solution tier
    "looplab.engine.eval_dispatch",   # the RepoTask command tier
    "looplab.tools.shell_tools",      # the operator assistant's shell
])
def test_no_tier_translates_the_settings_itself(dotted):
    """The residue tier-1 cannot reach: that a FOURTH surface is not quietly re-deriving the bundle.

    A driven test proves the three tiers agree TODAY. It cannot prove the next call site will ask
    `docker_tier_kwargs` rather than hand-copying `"runsc" if trust_mode == "hostile" else None` — a
    hand copy is green until the day the rule changes. So this reads the three real modules for the
    two tokens a re-derivation would need. Negative pins on purpose (a commented-out copy is as much
    of a drift risk as a live one), and paired with a POSITIVE check that the shared name is there,
    since "the token is absent" is also satisfied by a file that builds no container at all.
    """
    import importlib
    from pathlib import Path

    src = Path(importlib.import_module(dotted).__file__).read_text(encoding="utf-8")
    assert "docker_tier_kwargs" in src, (
        f"{dotted} no longer asks the shared Settings->container translation")
    # `HOSTILE_RUNTIME` is what the constant is FOR; the bare literal is the second copy.
    assert '"runsc"' not in src and "'runsc'" not in src, (
        f"{dotted} spells the hostile runtime itself again; it belongs to sandbox.HOSTILE_RUNTIME "
        "so a tier cannot come to disagree about what 'hostile' means")
    for knob in ("sandbox_memory", "sandbox_cpus", "sandbox_readonly_rootfs"):
        assert f"{knob} or None" not in src and f"{knob}," not in src, (
            f"{dotted} translates {knob} into a container argument itself; that translation is "
            "docker_tier_kwargs' one job")


def test_an_empty_cap_is_omitted_rather_than_passed_as_an_empty_flag():
    """Both callers spell "unbounded" as a falsy value; `--memory ""` would be a docker CLI error."""
    argv = docker_run_argv("img:1", network="none", mount_root=".", workdir="/work",
                           mem="", cpus=None)
    assert "--memory" not in argv and "--cpus" not in argv
    assert ("--cap-drop", "ALL") in _pairs(argv)      # ...while the unconditional caps stay
