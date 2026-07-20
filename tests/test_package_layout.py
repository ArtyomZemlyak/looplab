"""The subpackage split (docs 06: the long-planned target shape) must stay coherent:
every module lives where looplab.__init__._LAYOUT says, every legacy flat import still
resolves to the SAME module object, and no stray module reappears at the package root."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import looplab
from looplab import _LAYOUT

_PKG = Path(looplab.__file__).parent
_ROOT_ALLOWED = {"__init__", "cli", "bench", "sweep"}
_CONCEPTS_GUIDE = _PKG.parent / "docs" / "guide" / "concepts.md"
_ARCHITECTURE_GUIDE = _PKG.parent / "docs" / "guide" / "architecture.md"


def test_every_layout_entry_exists_at_its_canonical_path():
    missing = [f"{sub}/{name}.py" for name, sub in _LAYOUT.items()
               if not (_PKG / sub / f"{name}.py").exists()]
    assert not missing, f"layout map out of sync with disk: {missing}"


def test_no_stray_modules_at_package_root():
    stray = sorted(p.stem for p in _PKG.glob("*.py"))
    assert set(stray) <= _ROOT_ALLOWED, f"unexpected root modules: {set(stray) - _ROOT_ALLOWED}"


def test_no_module_missing_from_layout():
    on_disk = {p.stem for sub in set(_LAYOUT.values()) for p in (_PKG / sub).glob("*.py")
               if p.stem != "__init__"}
    assert on_disk == set(_LAYOUT), (
        f"only on disk: {on_disk - set(_LAYOUT)}; only in map: {set(_LAYOUT) - on_disk}")


def test_current_module_guide_names_canonical_part_iv_v_paths():
    module_map = _CONCEPTS_GUIDE.read_text(encoding="utf-8").split("## Module map", 1)[1]
    # CODEX AGENT: legacy flat imports remain callable, but contributor docs must identify physical
    # canonical modules so a file lookup or architecture review does not target a nonexistent root file.
    required = {
        "core/models.py", "events/replay.py", "engine/cross_run_index.py", "engine/claims.py",
        "engine/governance_health.py", "engine/steward_invocation.py", "tools/cross_run_tools.py",
        "serve/routers/cross_run.py", "ui/src/ResearchAtlas.jsx",
    }
    assert not (required - set(module_map.split("`")))
    assert "| `models.py` |" not in module_map

    architecture = _ARCHITECTURE_GUIDE.read_text(encoding="utf-8")
    architecture_required = {
        "core/concepts.py", "engine/cross_run_index.py", "engine/governance_health.py",
        "engine/steward_invocation.py", "serve/routers/cross_run.py", "ui/src/researchAtlasModel.js",
    }
    assert not (architecture_required - set(architecture.split("`")))


@pytest.mark.parametrize("name", sorted(_LAYOUT))
def test_legacy_flat_import_aliases_canonical_module(name):
    try:
        legacy = importlib.import_module(f"looplab.{name}")
    except ModuleNotFoundError as e:
        if e.name and e.name.split(".")[0] not in ("looplab",):
            pytest.skip(f"optional third-party dep absent: {e.name}")
        raise
    canonical = importlib.import_module(f"looplab.{_LAYOUT[name]}.{name}")
    assert legacy is canonical, f"looplab.{name} is not the canonical module object"
