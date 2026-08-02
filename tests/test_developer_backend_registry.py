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
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from looplab.agents.cli_agent import PRESETS
from looplab.core.config import DEVELOPER_BACKENDS, Settings

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
    for path in sorted((_PKG / "core").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
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
