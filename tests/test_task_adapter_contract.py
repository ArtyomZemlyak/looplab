"""TASK_OPTIONAL_HOOKS registry enforcement (docs/15 §P4.2).

The engine reads the TaskAdapter's optional hooks with `getattr(task, "<name>", None)` probes —
a one-sided rename historically failed SILENTLY (the run staged/scored nothing, suite green).
This test makes the seam two-way machine-checked, mirroring test_hint_forwarding /
test_signal_delivery:
  - every `getattr(task, "...")`/`getattr(self.task, "...")` probe across the consumer packages
    must name a registered hook (or a required Protocol member) — catches typo'd probes;
  - every registered hook must still have at least one consumer probe or direct call — catches
    registry rot after a refactor.
"""
from __future__ import annotations

import re
from pathlib import Path

from looplab.adapters.tasks import TASK_OPTIONAL_HOOKS

_PKG = Path(__file__).resolve().parents[1] / "looplab"

# Where the duck-typed probes live (the docstring in adapters/tasks.py names the consumers).
_CONSUMER_FILES = [
    *(_PKG / "engine").glob("*.py"),
    *(_PKG / "cli").glob("*.py"),   # cli became a package (docs/15 §P5.2) — scan every command module

    _PKG / "adapters" / "tasks.py",
    # The agent/role composition root split out of `adapters/tasks.py` (doc 25 RA-01) and took the
    # `params` probe with it; the hooks it consumes are the same hooks, in a new file.
    _PKG / "agents" / "factory.py",
    _PKG / "adapters" / "repo_task.py",
    _PKG / "adapters" / "repo_developer.py",
    _PKG / "adapters" / "repo_write_tools.py",
    _PKG / "tools" / "run_tools.py",
    _PKG / "core" / "hardware.py",
    _PKG / "runtime" / "command_eval.py",
]

_REQUIRED = {"id", "goal", "direction", "build_roles"}
# Plain DATA FIELDS of the composable task model (not optional behaviour hooks): probed by the
# lessons fingerprinting with defaults for legacy snapshots — legitimate reads, not hook seams.
# `eval` is the operator-given EvalSpec, declared only by RepoTask, which `cli/run_cmds.py::resume`
# reads with a default to re-report an unusable reader as a warning on a run that predates the
# submit-time refusal. Membership here is NOT a free pass: `test_every_data_field_probe_names_a_
# declared_model_field` below keeps the same two-way rename protection the hook registry gives.
_DATA_FIELDS = {"kind", "metric", "goal", "eval"}
# `(?:self\.)?(?:_e\.)?` covers the engine-delegate spelling `self._e.task` (lessons mixins).
_PROBE = re.compile(r'getattr\((?:self\.)?(?:_e\.)?task,\s*"([a-z_]+)"')


def _all_probes() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for f in _CONSUMER_FILES:
        if not f.exists():
            continue
        for name in _PROBE.findall(f.read_text(encoding="utf-8", errors="replace")):
            found.setdefault(name, set()).add(f.name)
    return found


def test_every_task_probe_names_a_registered_hook():
    unknown = {n: fs for n, fs in _all_probes().items()
               if n not in TASK_OPTIONAL_HOOKS and n not in _REQUIRED and n not in _DATA_FIELDS}
    assert not unknown, (
        f"getattr(task, ...) probes for unregistered hook(s) {unknown} — either a typo'd probe "
        "(silently returns None forever) or a new hook missing from "
        "adapters/tasks.py::TASK_OPTIONAL_HOOKS (register it + document it in the docstring).")


def test_every_data_field_probe_names_a_declared_model_field():
    """The `_DATA_FIELDS` escape hatch must not become the hole the hook registry closed.

    A name listed there is exempt from `TASK_OPTIONAL_HOOKS` precisely because it is a declared
    field of the task model rather than a behaviour hook — so hold it to that. Otherwise a typo'd
    probe, or a field renamed on the adapter, goes back to silently reading `None` forever: the
    `resume` reader warning would simply stop firing, with nothing red anywhere.

    Pydantic v2 keeps fields in `model_fields`, NOT as class attributes, so `dir(cls)` cannot see
    them — checking the wrong one reports every data field as missing."""
    from looplab.adapters.tasks import _KINDS

    undeclared = {name for name in _DATA_FIELDS
                  if not any(name in getattr(cls, "model_fields", {}) for cls in _KINDS.values())}
    assert not undeclared, (
        f"_DATA_FIELDS name(s) {undeclared} are declared by NO shipped adapter — either the probe "
        "is typo'd (it silently reads None forever) or the field was renamed; fix the probe or "
        "move the name to adapters/tasks.py::TASK_OPTIONAL_HOOKS if it became a real hook.")


def test_every_registered_hook_has_a_consumer():
    probes = set(_all_probes())
    # Hooks consumed by direct attribute call (after a repo_spec/backend gate) rather than a
    # getattr probe — the source needle keeps them honest without forcing a probe style.
    direct = {"agent_brief": "task.agent_brief()", "llm_roles": "task.llm_roles("}
    text = "\n".join(f.read_text(encoding="utf-8", errors="replace")
                     for f in _CONSUMER_FILES if f.exists())
    orphaned = [h for h in TASK_OPTIONAL_HOOKS
                if h not in probes and direct.get(h, "\x00") not in text]
    assert not orphaned, (
        f"registered TaskAdapter hook(s) {orphaned} have NO consumer probe/call left — a rename "
        "or removal on the consumer side; update TASK_OPTIONAL_HOOKS + the Protocol docstring.")


def test_shipped_adapters_only_implement_registered_hooks():
    # A shipped adapter growing a would-be hook the engine never probes is dead surface; catch
    # the misspelled-implementation direction too (e.g. `asset()` instead of `assets()`).
    from looplab.adapters.tasks import _KINDS
    near_misses = {}
    for kind, cls in _KINDS.items():
        for name in dir(cls):
            if name.startswith("_") or name in _REQUIRED or name in TASK_OPTIONAL_HOOKS:
                continue
            for hook in TASK_OPTIONAL_HOOKS:
                if name != hook and (name.rstrip("s") == hook.rstrip("s")
                                     or name.replace("get_", "") == hook):
                    near_misses.setdefault(kind, []).append((name, hook))
    assert not near_misses, f"adapter member(s) one letter away from a registered hook: {near_misses}"


def test_every_cli_snapshot_reload_is_grandfathered():
    """Every `_load_task` in `cli/run_cmds.py` re-reads a run's OWN `task.snapshot.json` — the
    wrap-up recovery, `resume`, and `finalize`. All three must pass `existing_run=True`, or a
    validation rule added after that snapshot was written makes the run unresumable
    (`adapters/repo_task.py::_grandfathered`, and CLAUDE.md invariant #6 for the settings half).

    A grep guard rather than three behavioural tests, because the failure is invisible: the run
    simply refuses at the CLI boundary, and only for tasks recorded before whichever rule was added
    — never for anything the suite creates fresh. `run` builds its task through `validate_task`
    instead and deliberately keeps the strict path; if a FRESH-submission caller ever needs
    `_load_task`, exempt it here by name rather than dropping the rule."""
    import ast

    source = (_PKG / "cli" / "run_cmds.py").read_text(encoding="utf-8")
    calls = [node for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id == "_load_task"]
    assert calls, "no `_load_task` call found — did the reload path move or get renamed?"
    strict = [node.lineno for node in calls
              if not any(kw.arg == "existing_run" and getattr(kw.value, "value", None) is True
                         for kw in node.keywords)]
    assert not strict, (
        f"`_load_task` at cli/run_cmds.py line(s) {strict} reloads a run snapshot WITHOUT "
        "existing_run=True — a validation rule added since that run started will refuse to resume it.")
