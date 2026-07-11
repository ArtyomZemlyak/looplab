"""String-referenced entry points must resolve (docs/15 §P4.8).

pyproject.toml wires external surfaces by DOTTED STRING — the two console scripts and the
jupyter-server-proxy launcher. No import, no test, no _LAYOUT shim protects those strings, so
moving/renaming the target used to break deploys silently (only surfacing at launch time on a
JupyterHub pod). This test resolves every declared entry point at suite time.
"""
from __future__ import annotations

import importlib
from pathlib import Path

try:  # py3.11+: stdlib tomllib
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _resolve(dotted: str):
    mod, _, attr = dotted.partition(":")
    obj = importlib.import_module(mod)
    for part in attr.split(".") if attr else []:
        obj = getattr(obj, part)
    return obj


def _declared() -> dict[str, str]:
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    out.update(data.get("project", {}).get("scripts", {}))
    for group, entries in data.get("project", {}).get("entry-points", {}).items():
        for name, target in entries.items():
            out[f"{group}:{name}"] = target
    return out


def test_every_declared_entry_point_resolves():
    declared = _declared()
    assert declared, "pyproject declares no entry points — the census moved; update this test"
    broken = {}
    for name, target in declared.items():
        try:
            obj = _resolve(target)
        except Exception as e:  # noqa: BLE001 — the failure detail IS the assertion payload
            broken[name] = f"{target} -> {type(e).__name__}: {e}"
            continue
        if obj is None:
            broken[name] = f"{target} -> resolved to None"
    assert not broken, f"entry point(s) no longer resolve: {broken}"
