"""A RUNTIME wire variable is never a Settings env name (critic 2026-09-26).

`LOOPLAB_<FIELD>` is `Settings`' namespace: every field reads its env twin 1:1. The engine also
signals an eval launch through env variables (`runtime/seccomp.py::SECCOMP_ENV`,
`runtime/landlock.py::LANDLOCK_ENV`, `runtime/read_fence.py::FENCE_DIR_ENV`, ...), and
`runtime/sandbox.py::run_argv` builds a child's environment ON TOP of the engine's own process
environment. A wire name inside the Settings namespace is therefore read twice: the syscall fence's
was `LOOPLAB_SYSCALL_FENCE`, so an engine launched with `Settings.syscall_fence` in its environment —
Replay exports every frozen setting, the default `off` included — wrapped every eval in the launcher
with the policy `off`, which the launcher refuses. Every eval of a replayed run failed before it ran.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

from looplab.core.config import Settings
from looplab.runtime import seccomp
from looplab.runtime.sandbox import run_argv

_ROOT = Path(__file__).resolve().parents[1] / "looplab"
_PREFIX = Settings.model_config.get("env_prefix", "LOOPLAB_")


def _wire_names() -> dict[str, str]:
    """Every module-level `LOOPLAB_*` string constant under `runtime/` and `engine/`, by where."""
    found = {}
    for package in ("runtime", "engine"):
        for path in sorted((_ROOT / package).rglob("*.py")):
            for node in ast.parse(path.read_text(encoding="utf-8")).body:
                targets = (node.targets if isinstance(node, ast.Assign)
                           else [node.target] if isinstance(node, ast.AnnAssign) else [])
                value = getattr(node, "value", None)
                if (isinstance(value, ast.Constant) and isinstance(value.value, str)
                        and value.value.startswith(_PREFIX)):
                    for target in targets:
                        if isinstance(target, ast.Name):
                            found[value.value] = f"{path.relative_to(_ROOT.parent)}::{target.id}"
    return found


def test_the_scan_sees_the_wire_names_it_guards():
    names = _wire_names()
    assert seccomp.SECCOMP_ENV in names and "LOOPLAB_LANDLOCK_ALLOWLIST" in names, sorted(names)


def test_no_runtime_wire_name_is_a_settings_env_name():
    settings_names = {f"{_PREFIX}{field.upper()}" for field in Settings.model_fields}
    clashes = {name: where for name, where in _wire_names().items() if name in settings_names}
    assert not clashes, (
        f"these runtime wire variables are also Settings env names, so an engine launched with the "
        f"setting in its environment hands it to every child as the wire signal: {clashes}")


def test_the_syscall_setting_in_the_engines_environment_does_not_wrap_an_eval(tmp_path, monkeypatch):
    """Driven: the environment Replay hands a relaunched engine carries `syscall_fence`'s frozen
    value, `off` by default. The eval runs unwrapped, as the setting says."""
    monkeypatch.setenv(f"{_PREFIX}SYSCALL_FENCE", "off")
    rc, out, err, _ = run_argv([sys.executable, "-c", "print('RAN')"], str(tmp_path), 60)
    assert rc == 0 and out.strip() == "RAN", (rc, out, err)
