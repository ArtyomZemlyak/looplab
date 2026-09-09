"""The four subsystems that used to share `machine_runs_tools.py` each live in their own module.

Doc 25 TO-02: one 1,988-line file held the read-only cross-run view, the run-MUTATING provider, the
launch-proposal provider, one assistant turn's durable mutation journal and the seam to the
server-owned command service. The split is not a tidiness change — the mutating provider is the one
that rewrites event logs and removes node directories, and it was the hardest of the five to read
precisely because four unrelated things sat between its verbs.

What is guarded here is the property a split can silently lose:

  * each subsystem has exactly ONE home (AST over class/def nodes, so a comment naming a class
    cannot satisfy it, and neither can a docstring that still describes the old layout);
  * the old module RE-EXPORTS NOTHING. A back-compat alias would read as harmless and is not: two
    names for one class split its monkeypatch seam in half, so a test patching
    `machine_runs_tools.RunControlTools` would leave `serve/assistant.py` — which imports the
    canonical one — running the unpatched class, with the patch reporting success;
  * the dependency direction inside the split stays one-way (providers -> fence/adapter), because a
    cycle among four fresh modules is an ImportError at startup, not a test failure.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "looplab" / "tools"


# `utf-8-sig`, for `tests/_source_scan.py`'s reason: a BOM makes `ast.parse` die on
# an unrelated file, which reads as a broken guard rather than a finding.
def ast_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


# subsystem -> (its module, the names it must define there)
HOMES = {
    "read-only cross-run view": ("machine_runs_tools.py", {"MachineRunsTools"}),
    "run-mutating provider": ("run_control_tools.py",
                              {"RunControlTools", "RunLifecycleFns", "TraceRewriteFns",
                               "_node_subtree", "_node_lifecycle_unchanged"}),
    "launch-proposal provider": ("run_launcher_tools.py", {"RunLauncherTools"}),
    "turn mutation journal": ("turn_mutation_fence.py",
                              {"_TurnMutationFence", "_MutationRecoveryBlocked"}),
    "command-service seam": ("run_command_adapter.py",
                             {"_RunCommandAdapter", "_render_command_result"}),
}


def _top_level_names(module: str) -> set[str]:
    """Every class/function DEFINED at the top level of *module* — definitions, not mentions."""
    tree = ast.parse(ast_text(TOOLS / module))
    return {node.name for node in tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))}


@pytest.mark.parametrize("subsystem", sorted(HOMES))
def test_each_split_subsystem_is_defined_in_its_own_module_and_nowhere_else(subsystem):
    module, names = HOMES[subsystem]
    defined = _top_level_names(module)
    assert names <= defined, f"{subsystem}: {sorted(names - defined)} is not defined in {module}"
    for other, _ in HOMES.values():
        if other == module:
            continue
        assert not (names & _top_level_names(other)), (
            f"{subsystem} is defined in BOTH {module} and {other} — a second definition is the "
            "duplication this split exists to end")


def test_the_old_god_module_re_exports_nothing_it_shed():
    """The seam must stay single. An importer names the module that actually holds the class."""
    from looplab.tools import machine_runs_tools

    shed = sorted({name for _module, names in HOMES.values() for name in names}
                  - _top_level_names("machine_runs_tools.py"))
    still_there = [name for name in shed if hasattr(machine_runs_tools, name)]
    assert not still_there, (
        "machine_runs_tools re-exports " + ", ".join(still_there) + " — a back-compat alias splits "
        "each class's monkeypatch seam in two")


def test_the_split_modules_import_one_way():
    """Providers reach the fence and the adapter; neither reaches back. A cycle here is a startup
    ImportError, so it must be impossible rather than merely absent today."""
    def module_level_imports(module: str) -> set[str]:
        tree = ast.parse(ast_text(TOOLS / module))
        out: set[str] = set()
        for node in tree.body:                                # top level only: a deferred import
            if isinstance(node, ast.ImportFrom) and node.module:   # inside a function is fine
                out.add(node.module)
            elif isinstance(node, ast.Import):
                out.update(alias.name for alias in node.names)
        return out

    for lower in ("turn_mutation_fence.py", "run_command_adapter.py"):
        reaches = module_level_imports(lower)
        assert not [name for name in reaches
                    if name.endswith(("run_control_tools", "run_launcher_tools",
                                      "machine_runs_tools"))], (
            f"{lower} imports a provider at module level — that closes the cycle")
    control = module_level_imports("run_control_tools.py")
    assert "looplab.tools.turn_mutation_fence" in control
    assert "looplab.tools.run_command_adapter" in control
