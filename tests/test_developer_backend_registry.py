"""`developer_backend`'s closed set has one home, in the layer that validates it (doc 25 XP-04).

`Settings` rejects an unknown `developer_backend` on every construction, and it used to do that by
importing `agents.cli_agent.PRESETS` — the ONE upward import out of `core` in the whole tree. Two
things were wrong with it and only one was cosmetic: `core` is documented as importing nothing above
itself, and more concretely an import-time error anywhere in `agents/cli_agent.py` broke ALL config
loading, including for runs that never touch an external coding agent.

The authority is inverted rather than lost. The closed set lives in `core.config`, and
`agents/cli_agent.py` asserts at import time that its PRESETS are covered. This file checks both
directions, because each one fails silently on its own:

* a preset missing from the set makes a REAL backend unconfigurable — `Settings` rejects it as a
  typo, and the operator gets "must be default|aider|…" naming a backend that exists;
* a set entry with no preset is worse: `Settings` accepts it, and `adapters/tasks.py` then wires the
  DEFAULT developer for anything not in PRESETS — a silent downgrade, which is the exact failure the
  original validation was added to stop.

The SECOND vocabulary in this file is the LIVE-SWAP one (`developer_switch_names`), and it is a
different question from "what may `Settings` be launched with": the Strategist may move a running run
onto another backend, and that path accepts one extra spelling — `llm`, the alias
`make_developer_factory` resolves to the in-house `default`. It had three hand-written homes and they
disagreed three ways, which is what put an `if "agentless" in ctx.available_developers` arm in
`agents/strategist.py` that could never be true. So the registry-guard family gains a member here
(`CLAUDE.md`'s REGISTRY-GUARDED list), with the same two-way source scan: every offered name resolves
to a real backend, and no producer in the tree names a developer the vocabulary does not hold.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from _source_scan import iter_trees

from looplab.agents.cli_agent import PRESETS
from looplab.core.config import (
    DEVELOPER_BACKEND_ALIASES, DEVELOPER_BACKENDS, Settings, developer_switch_names,
)

_PKG = Path(__file__).resolve().parents[1] / "looplab"


def test_every_preset_is_configurable():
    missing = sorted(set(PRESETS) - set(DEVELOPER_BACKENDS))
    assert not missing, (
        f"cli_agent PRESETS {missing} are not in core.config.DEVELOPER_BACKENDS, so Settings "
        "rejects them as typos and the backend cannot be selected at all")


def test_every_configurable_backend_exists():
    """The silent-downgrade direction: accepted by config, then quietly replaced by the default."""
    extra = sorted(set(DEVELOPER_BACKENDS) - set(PRESETS) - {"default"})
    assert not extra, (
        f"DEVELOPER_BACKENDS names {extra} with no matching preset — Settings would accept them and "
        "adapters/tasks.py would silently wire the DEFAULT developer instead")


def test_default_is_the_in_house_developer_and_has_no_preset():
    """`default` is deliberately in the set and deliberately not a preset; pinned so a future
    refactor cannot quietly turn the in-house Developer into an external-agent key."""
    assert "default" in DEVELOPER_BACKENDS
    assert "default" not in PRESETS


@pytest.mark.parametrize("backend", sorted(DEVELOPER_BACKENDS))
def test_settings_accepts_every_declared_backend(backend):
    assert Settings(developer_backend=backend).developer_backend == backend


def test_settings_still_rejects_a_typo_loudly():
    """The whole point of the validation. A typo must not fall through to the default developer."""
    with pytest.raises(ValueError, match="developer_backend must be"):
        Settings(developer_backend="opencode2")


def test_core_imports_nothing_above_itself():
    """The layering rule this finding was really about — asserted for the whole package, not just
    the one import that broke it. `core` is the bottom layer; anything it imports upward can take
    config loading down with it."""
    upward = {"adapters", "agents", "cli", "engine", "events", "runtime", "search", "serve",
              "tools", "trust"}
    offenders = []
    for path, tree in iter_trees(_PKG / "core"):
        for node in ast.walk(tree):
            module = getattr(node, "module", None) if isinstance(node, ast.ImportFrom) else None
            names = ([module] if module else
                     [a.name for a in node.names] if isinstance(node, ast.Import) else [])
            for name in names:
                if not name or not name.startswith("looplab."):
                    continue
                if name.split(".")[1] in upward:
                    offenders.append(
                        f"{path.relative_to(_PKG.parent)}:{node.lineno}: {name}")
    assert not offenders, (
        "core/ imports from a higher layer — core is the bottom of the stack and must import "
        "nothing above itself:\n  " + "\n  ".join(offenders))


# --------------------------------------------------------------------------- #
# The LIVE-SWAP vocabulary (`developer_switch_names`) — the second registry in this file
# --------------------------------------------------------------------------- #

def test_switch_vocabulary_is_the_closed_set_plus_its_aliases():
    """Both directions at once: nothing offered that is not registered, nothing registered that is
    not offered. Order is `default` first because every no-factory caller degrades to it."""
    assert developer_switch_names() == (*DEVELOPER_BACKENDS, *DEVELOPER_BACKEND_ALIASES)
    assert developer_switch_names()[0] == "default"
    assert len(set(developer_switch_names())) == len(developer_switch_names())


def test_every_alias_resolves_to_a_real_backend_and_shadows_none():
    """An alias that resolved to nothing would be offered to the Strategist and then blow up in the
    factory; an alias that SHADOWED a canonical name would silently redirect a real backend."""
    for alias, target in DEVELOPER_BACKEND_ALIASES.items():
        assert target in DEVELOPER_BACKENDS, f"alias {alias!r} -> {target!r} is not a real backend"
        assert alias not in DEVELOPER_BACKENDS, f"alias {alias!r} shadows a canonical backend name"


def test_an_alias_is_deliberately_not_a_settings_value():
    """The two vocabularies are NOT the same set and the difference is load-bearing: an alias has no
    preset, so admitting it into `DEVELOPER_BACKENDS` would make `Settings` accept a launch value
    `adapters/tasks.py` then silently downgrades to the default — the failure the other direction of
    this file exists to stop. `llm` is a live-swap spelling only."""
    for alias in DEVELOPER_BACKEND_ALIASES:
        with pytest.raises(ValueError, match="developer_backend must be"):
            Settings(developer_backend=alias)


def test_the_engine_offers_exactly_the_registered_vocabulary(tmp_path):
    """`_available_developers` is what `validate_strategy` gates on, so a name missing from it is a
    Strategist decision that is DROPPED with no receipt (see that function's comment). Derived, not
    re-spelled — pinned here against the registry in both directions."""
    from tests.factories import make_engine

    with_factory = make_engine(tmp_path / "with-factory", developer_factory=lambda name: object())
    assert tuple(with_factory._available_developers()) == developer_switch_names()

    # No factory wired: nothing CAN be swapped, so only the in-house Developer is offered.
    without = make_engine(tmp_path / "no-factory")
    assert without.developer_factory is None
    assert without._available_developers() == ["default"]


def test_every_offered_name_reaches_the_factory_as_a_settings_valid_backend(monkeypatch):
    """The join the three lists used to get wrong, DRIVEN through the real factory: whatever the
    Strategist may ask for must resolve to a `developer_backend` the config layer accepts. The alias
    resolution lives in `make_developer_factory`; capture what it hands `make_roles`."""
    from looplab.adapters.toytask import ToyTask
    from looplab.agents import factory as factory_mod

    seen: list[str] = []

    def fake_make_roles(task, settings, *a, **kw):
        seen.append(settings.developer_backend)
        return object(), object()

    monkeypatch.setattr(factory_mod, "make_roles", fake_make_roles)
    task = ToyTask.load(Path(__file__).resolve().parents[1] / "examples" / "toy_task.json")
    build = factory_mod.make_developer_factory(task, Settings(developer_backend="default"))

    for name in developer_switch_names():
        seen.clear()
        build(name)
        assert seen, f"{name!r} never reached make_roles"
        # The effective backend is registered AND constructible — no silent downgrade, no refusal.
        assert Settings(developer_backend=seen[0]).developer_backend == seen[0]
        assert seen[0] == DEVELOPER_BACKEND_ALIASES.get(name, name)


def _developer_literals_in_sources() -> list[tuple[str, str]]:
    """Every string literal the PACKAGE uses as a developer-backend name, with where it came from.

    Two AST shapes, which are the two halves of the defect this guard replaces: a membership test
    against `available_developers` (the dead `if "agentless" in ctx.available_developers` arm) and a
    literal ASSIGNED to a `developer` strategy key (the `strat["developer"] = "agentless"` under it).
    AST, not substrings — a commented-out copy must not satisfy or trip this."""
    found: list[tuple[str, str]] = []
    for path, tree in iter_trees(_PKG):
        where = str(path.relative_to(_PKG.parent))
        for node in ast.walk(tree):
            # `"<name>" in <...available_developers>`
            if isinstance(node, ast.Compare) and isinstance(node.left, ast.Constant) \
                    and isinstance(node.left.value, str) \
                    and all(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
                for comparator in node.comparators:
                    tail = (comparator.attr if isinstance(comparator, ast.Attribute)
                            else comparator.id if isinstance(comparator, ast.Name) else "")
                    if tail.endswith("available_developers") or tail.endswith("_BACKENDS"):
                        found.append((node.left.value, f"{where}:{node.lineno}"))
            # `<x>["developer"] = "<name>"`
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Subscript) \
                            and isinstance(target.slice, ast.Constant) \
                            and target.slice.value == "developer":
                        found.append((node.value.value, f"{where}:{node.lineno}"))
            # `{..., "developer": "<name>", ...}` — but ONLY in a dict that is recognisably a
            # Strategy. `"developer"` is not a reserved word: `search/speculation_calibration.py`'s
            # roles descriptor uses the same key for a role CLASS path, which is a different
            # vocabulary entirely, so the sibling keys are what disambiguate.
            if isinstance(node, ast.Dict):
                keys = {k.value for k in node.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)}
                if not keys & {"policy", "fidelity", "rationale", "operators", "novelty_stance"}:
                    continue
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == "developer" \
                            and isinstance(value, ast.Constant) and isinstance(value.value, str):
                        found.append((value.value, f"{where}:{node.lineno}"))
    return found


def test_no_source_names_a_developer_the_vocabulary_does_not_hold():
    """The direction that had NO guard, and the one that cost a permanently dead decision arm.

    `agents/strategist.py` proposed `"agentless"` behind `if "agentless" in ctx.available_developers`
    with the comment *"only when C5 has landed"* — unreachable, because that name is in no registry.
    A branch like it is now a red test naming the file and line. Had it fired, `validate_strategy`
    would have dropped the field silently: no `developer`, no `developer_application` receipt, and a
    rationale in the durable log describing a switch that never happened."""
    vocabulary = set(developer_switch_names())
    offenders = [f"{where}: {literal!r}"
                 for literal, where in _developer_literals_in_sources()
                 if literal not in vocabulary]
    assert not offenders, (
        "source names a developer backend that is in neither core.config.DEVELOPER_BACKENDS nor "
        "DEVELOPER_BACKEND_ALIASES — engine/strategy.py::_available_developers can never offer it, "
        "so validate_strategy drops it with no refusal receipt:\n  " + "\n  ".join(offenders))


def test_the_agentless_arm_is_gone_and_stays_gone():
    """A NEGATIVE pin (substrings on purpose, per CLAUDE.md): what must not come back is the TEXT,
    including a commented-out copy, because a re-introduced arm is a promise the code cannot keep."""
    source = (_PKG / "agents" / "strategist.py").read_text()
    assert 'strat["developer"] = "agentless"' not in source
    assert '"agentless" in ctx.available_developers' not in source
