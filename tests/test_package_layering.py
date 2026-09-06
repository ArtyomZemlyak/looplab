"""The package layering matrix, machine-checked (doc 50 XP-07; doc 52 row 27).

CLAUDE.md states the layering in prose — `core` imports nothing above itself, `events` and
`runtime` only `core`, `search` may import `agents` at module level while `agents` reaches
`search` only through a deferred import, `tools` reaches `serve` only by injection, the engine
must not grow a dependency on `serve` — and until this file only a third of it was guarded
(`runtime` purity, the `agents→search` direction, the private-name seams). The rest held by
convention, and the review measured why convention is not a guard: 38 % of intra-`looplab`
import edges are function-local, so hoisting them would collapse the graph into eight cycles.

MEASURED 2026-09-06, the day this landed: 1,533 cross-package import sites under `looplab/`,
826 of them (53.9 %) function-local. Every stated rule held except one sentence — "`tools`
reaches `engine` only function-locally" — which two module-level imports of engine LEAVES had
made false (`tools/dev_commands.py` → `engine/workspace_seed.py`, `tools/node_diff.py` →
`engine/comparability.py`, both importing nothing from `looplab`). The sentence now states the
rule the tree keeps, and that rule is pinned below as `LEAF_ONLY`.

THREE KINDS OF EDGE, because "imports" hides the distinction the layering lives on:
  * MODULE-LEVEL — a runtime dependency taken at import time. `MODULE_LEVEL` IS the graph, two
    ways: a new upward edge is a red test, and a listed edge the tree no longer has is a stale
    row, so the table can never be a superset the reader merely believes.
  * DEFERRED — taken only inside a function body. `DEFERRED` names every such edge WITH THE
    REASON it is deferred rather than hoisted, two ways as well; an edge that is also taken at
    module level is not deferred and does not belong there.
  * TYPING-ONLY — under `if TYPE_CHECKING:`. Not an edge at runtime; classified so it can never
    be mistaken for one, and pinned by nothing.
The scanner is exercised on a synthetic tree at the end, so a comment cannot satisfy it.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests._source_scan import iter_trees

ROOT = Path(__file__).resolve().parents[1] / "looplab"
PACKAGE_ROOT = "looplab"

# Every package under `looplab/` plus the three top-level modules, each a unit of its own.
UNITS = sorted(p.name for p in ROOT.iterdir() if p.is_dir() and (p / "__init__.py").is_file()) \
    + sorted(p.stem for p in ROOT.glob("*.py"))

# The MODULE-LEVEL graph. A row lists every unit the key imports at module scope (typing-only
# imports excluded). `looplab` is the package root itself (`from looplab import __version__`).
MODULE_LEVEL: dict[str, frozenset[str]] = {
    "__init__": frozenset(),
    "bench": frozenset({"core", "adapters"}),
    "sweep": frozenset(),                       # runs INSIDE the sandbox; imports nothing of ours
    "core": frozenset(),
    "events": frozenset({"core"}),
    "runtime": frozenset({"core"}),
    "trust": frozenset({"core", "events"}),
    "tools": frozenset({"core", "events", "runtime", "trust", "engine"}),
    "search": frozenset({"core", "events", "tools", "agents"}),
    "agents": frozenset({"core", "tools"}),
    "adapters": frozenset({"core", "tools", "agents"}),
    "engine": frozenset({"core", "events", "runtime", "tools", "trust", "search", "agents"}),
    "judgebench": frozenset({"core"}),
    "maintenance": frozenset({"events", "runtime"}),
    "serve": frozenset({"core", "events", "tools", "trust", "engine", "adapters", "looplab"}),
    "cli": frozenset({"core", "events", "runtime", "tools", "trust", "search", "engine",
                      "adapters", "serve", "looplab"}),
}

# Module-level edges that may reach only LEAVES of the target: modules whose OWN module-level
# imports stay within what the source may already import (its row above, minus the target), so
# taking them pulls nothing new into the source's import graph. `tools` depends on three such
# engine modules at import time — `comparability` and `workspace_seed` (nothing of ours) and
# `governance_health` (`core`, `events`) — and on nothing else in the engine until a call is made.
LEAF_ONLY: frozenset[tuple[str, str]] = frozenset({("tools", "engine")})

# Edges taken ONLY inside function bodies, each with the reason it stays deferred.
DEFERRED: dict[tuple[str, str], str] = {
    ("adapters", "engine"): "`mlebench_campaign` reads a finished run's champion caveats; "
                            "`repo_developer` verifies a repair (`repair_verify`) at build time",
    ("adapters", "events"): "`mlebench_campaign` folds finished runs' logs at report time",
    ("adapters", "runtime"): "task adapters build sandboxes and stage pipelines when an eval is "
                             "prepared, not when the task is loaded",
    ("adapters", "trust"): "`mlebench_extras` runs the rule-violation judge on demand",
    ("agents", "adapters"): "a role reads `repo_task` only when bound to a repo task (the "
                            "module-level name is typing-only)",
    ("agents", "engine"): "roles reach `node_build`/`repair_judgment`/`triage` inside a call the "
                          "engine makes",
    ("agents", "events"): "`roles.py` renders a digest for one prompt",
    ("agents", "runtime"): "`cli_agent` spawns its sandbox per run",
    ("agents", "search"): "the documented one-way rule: `search` imports `agents` at module "
                          "level, so `agents` may reach `search` only function-locally "
                          "(`tests/test_agents_search_direction.py`)",
    ("cli", "agents"): "role builders are constructed per command",
    ("cli", "bench"): "the capability harness is loaded by its command only",
    ("cli", "judgebench"): "the bait instruments are loaded by their commands only",
    ("cli", "maintenance"): "the backfill scripts are loaded by their commands only",
    ("engine", "adapters"): "the engine names a task type only at the seams that need one — "
                            "holdout splits, MLE-bench grading, the toy task (doc 50 RA-10)",
    ("judgebench", "adapters"): "`bait` reads the MLE-bench extras at audit time",
    ("judgebench", "engine"): "`score` re-runs the engine's triage/train-monitor rules over a "
                              "bench case",
    ("judgebench", "events"): "`bait` folds a run's log at audit time",
    ("judgebench", "trust"): "`bait` invokes the structured judge at audit time",
    ("search", "adapters"): "`speculation_quality` builds the toy task for its calibration "
                            "benchmark",
    ("search", "trust"): "`foresight`/`graded_novelty` call the verifier inside a scoring step",
    ("serve", "agents"): "the assistant and preflight routes build roles per request",
    ("serve", "runtime"): "the engine process and the runs router reach the sandbox and "
                          "`command_eval` per request",
    ("serve", "search"): "the concept routes reach the concept cluster per request",
    ("tools", "adapters"): "`machine_runs_tools` loads a task on demand",
    ("tools", "agents"): "`asset_brief`/`run_tools` build an agent inside one tool call",
    ("tools", "search"): "the cross-run tools reach the concept cluster per call",
    ("tools", "serve"): "the declared debt: `machine_runs_tools` takes `engine_proc`/"
                        "`run_files` as its DEFAULT primitives and by injection otherwise "
                        "(`tests/test_cross_package_private_seams.py`)",
    ("trust", "agents"): "`judge` builds its agent per invocation",
    ("trust", "engine"): "`memo_verify` reads `engine.memory` when it finalizes evidence",
    ("trust", "search"): "`lesson_guard` tags concepts inside one guard call",
    ("trust", "tools"): "`memo_verify` hands the judge the run tools per invocation",
}

# Units that may be reached by NOTHING outside themselves, at any level. `cli` is the process
# entry; a library module that imported it would pull Typer and every command group into the
# engine's import graph.
ENTRY_ONLY = frozenset({"cli"})
# The engine must not grow a dependency on the server, at any level (CLAUDE.md, Layering).
FORBIDDEN_ANY_LEVEL: frozenset[tuple[str, str]] = frozenset({("engine", "serve")})
# Purity: what these units may import from `looplab` at ANY level, module or function.
PURE: dict[str, frozenset[str]] = {
    "core": frozenset(), "events": frozenset({"core"}), "runtime": frozenset({"core"}),
    "sweep": frozenset(),
}


# ------------------------------------------------------------------------------- the scanner
def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


def _unit_of(path: Path, root: Path) -> str:
    rel = path.relative_to(root).parts
    return rel[0] if len(rel) > 1 else path.stem


def _target_unit(module: str, names: list[str], *, package_root: str, units: set[str]) -> list[str]:
    """The unit(s) an import statement reaches, or [] when it is not ours."""
    parts = module.split(".")
    if parts[0] != package_root:
        return []
    if len(parts) > 1:
        return [parts[1]]
    # `from looplab import X`: X is a subpackage when its name is one, else the package root.
    return [name if name in units else package_root for name in names] or [package_root]


def _resolve_relative(path: Path, root: Path, node: ast.ImportFrom, package_root: str) -> str:
    """The absolute dotted module a relative import names, for `_target_unit`."""
    package_parts = list(path.relative_to(root).with_suffix("").parts)
    if path.name != "__init__.py":
        package_parts = package_parts[:-1]
    base = package_parts[:len(package_parts) - (node.level - 1)] if node.level > 1 else package_parts
    dotted = ".".join([package_root, *base] + ([node.module] if node.module else []))
    return dotted


def scan(root: Path = ROOT, *, package_root: str = PACKAGE_ROOT) -> dict[str, dict[tuple[str, str], list[str]]]:
    """`{"module": {(src, dst): [sites]}, "deferred": {...}, "typing": {...}}` over `root`."""
    units = {p.name for p in root.iterdir() if p.is_dir() and (p / "__init__.py").is_file()}
    out: dict[str, dict[tuple[str, str], list[str]]] = {"module": {}, "deferred": {}, "typing": {}}

    def record(kind: str, src: str, dst: str, site: str) -> None:
        out[kind].setdefault((src, dst), []).append(site)

    for path, tree in iter_trees(root):
        src = _unit_of(path, root)

        def walk(node: ast.AST, in_function: bool, in_typing: bool) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    walk(child, True, in_typing)
                    continue
                if isinstance(child, ast.If) and _is_type_checking(child.test):
                    for grand in child.body:
                        walk_stmt(grand, in_function, True)
                    for grand in child.orelse:
                        walk_stmt(grand, in_function, in_typing)
                    continue
                walk_stmt(child, in_function, in_typing)

        def walk_stmt(child: ast.AST, in_function: bool, in_typing: bool) -> None:
            targets: list[str] = []
            if isinstance(child, ast.Import):
                for alias in child.names:
                    targets += _target_unit(alias.name, [], package_root=package_root, units=units)
            elif isinstance(child, ast.ImportFrom):
                module = (child.module or "") if child.level == 0 else _resolve_relative(
                    path, root, child, package_root)
                targets += _target_unit(module, [a.name for a in child.names],
                                        package_root=package_root, units=units)
            site = f"{path.relative_to(root).as_posix()}:{getattr(child, 'lineno', 0)}"
            for dst in targets:
                if dst == src:
                    continue
                kind = "typing" if in_typing else ("deferred" if in_function else "module")
                record(kind, src, dst, site)
            walk(child, in_function, in_typing)

        walk(tree, False, False)
    return out


def deferred_only(graph: dict) -> dict[tuple[str, str], list[str]]:
    """The edges taken ONLY inside function bodies: a deferred site of an edge that is also taken
    at module level is one more site of a module-level edge, not a deferred edge."""
    return {edge: sites for edge, sites in graph["deferred"].items() if edge not in graph["module"]}


@pytest.fixture(scope="module")
def graph():
    return scan()


# --------------------------------------------------------------------------------- the rules
def test_every_unit_has_a_row_and_no_row_names_a_unit_that_does_not_exist():
    assert set(MODULE_LEVEL) == set(UNITS), (
        f"missing rows: {set(UNITS) - set(MODULE_LEVEL)}; stale rows: {set(MODULE_LEVEL) - set(UNITS)}")
    known = set(UNITS) | {PACKAGE_ROOT}
    for src, targets in MODULE_LEVEL.items():
        assert targets <= known, f"{src} lists unknown units {sorted(targets - known)}"
    assert set(PURE) <= set(UNITS) and ENTRY_ONLY <= set(UNITS)


def test_the_module_level_matrix_is_the_graph_both_ways(graph):
    measured = set(graph["module"])
    declared = {(src, dst) for src, targets in MODULE_LEVEL.items() for dst in targets}
    new = sorted(measured - declared)
    stale = sorted(declared - measured)
    detail = "; ".join(f"{s}->{d} at {graph['module'][(s, d)][:3]}" for s, d in new)
    assert not new, f"module-level edges the matrix does not allow: {detail}"
    assert not stale, f"matrix rows the tree no longer has (delete them): {stale}"


def test_every_deferred_edge_is_declared_with_its_reason_both_ways(graph):
    deferred = deferred_only(graph)
    measured = set(deferred)
    undeclared = sorted(measured - set(DEFERRED))
    detail = "; ".join(f"{s}->{d} at {deferred[(s, d)][:3]}" for s, d in undeclared)
    assert not undeclared, f"function-local edges with no declared reason: {detail}"
    gone = sorted(set(DEFERRED) - measured)
    assert not gone, f"DEFERRED rows no site takes any more (delete them): {gone}"
    hoisted = sorted(set(DEFERRED) & set(graph["module"]))
    assert not hoisted, f"edges declared deferred but taken at module level: {hoisted}"
    assert all(reason.strip() for reason in DEFERRED.values())


def test_leaf_only_edges_reach_only_leaves(graph):
    for src, dst in LEAF_ONLY:
        assert dst in MODULE_LEVEL[src], f"{src}->{dst} is leaf-only but not a module-level edge"
        allowed = MODULE_LEVEL[src] - {dst}
        for site in graph["module"][(src, dst)]:
            file, line = site.rsplit(":", 1)
            tree = ast.parse((ROOT / file).read_text(encoding="utf-8-sig", errors="replace"))
            node = next(n for n in ast.walk(tree)
                        if isinstance(n, (ast.Import, ast.ImportFrom)) and n.lineno == int(line))
            modules = ([a.name for a in node.names] if isinstance(node, ast.Import)
                       else [node.module])
            for module in modules:
                target = Path(*module.split(".")[1:])
                prefixes = (target.with_suffix(".py").as_posix() + ":",
                            (target / "__init__.py").as_posix() + ":")
                # What the imported module itself imports at module level: the edges out of `dst`
                # whose sites lie in that file.
                own = {edge[1] for edge, sites in graph["module"].items()
                       if edge[0] == dst and any(s.startswith(prefixes) for s in sites)}
                assert own <= allowed, (
                    f"{site} imports {module} at module level, which is not a leaf for {src}: "
                    f"it imports {sorted(own - allowed)} that {src} may not")


def test_core_events_runtime_and_the_sandbox_helper_are_pure(graph):
    for unit, allowed in PURE.items():
        for kind in ("module", "deferred"):
            reached = {dst for (src, dst) in graph[kind] if src == unit}
            assert reached <= allowed, (
                f"{unit} reaches {sorted(reached - allowed)} ({kind}); it may reach only "
                f"{sorted(allowed)} at any level")


def test_the_engine_never_reaches_serve_and_nothing_reaches_the_cli(graph):
    for kind in ("module", "deferred"):
        for src, dst in graph[kind]:
            assert (src, dst) not in FORBIDDEN_ANY_LEVEL, f"{src}->{dst} ({kind}) is forbidden"
            assert dst not in ENTRY_ONLY or src in ENTRY_ONLY, (
                f"{src} reaches the process entry {dst} ({kind}) at {graph[kind][(src, dst)][:3]}")


def test_the_two_documented_one_way_rules_are_deferred_not_hoisted(graph):
    # `search` -> `agents` at module level and `agents` -> `search` only deferred; `tools` ->
    # `serve` only deferred (the injection debt). Both are in the tables above; this states them.
    assert ("search", "agents") in graph["module"]
    assert ("agents", "search") in deferred_only(graph)
    assert ("tools", "serve") in deferred_only(graph)


# ---------------------------------------------------------------------- the scanner's teeth
def test_the_scanner_classifies_all_three_kinds_and_resolves_relative_imports(tmp_path):
    root = tmp_path / "pkg"
    for unit in ("a", "b", "c"):
        (root / unit).mkdir(parents=True)
        (root / unit / "__init__.py").write_text("")
    (root / "a" / "x.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "import pkg.b.y\n"                                  # module-level a->b
        "from ..c import z\n"                               # relative, module-level a->c
        "from pkg import __version__\n"                     # the package root
        "if TYPE_CHECKING:\n"
        "    from pkg.b import y as typed\n"                # typing-only a->b (not an edge)
        "def f():\n"
        "    from pkg.c.z import q\n"                       # deferred a->c, but a->c is module-level
        "    return q\n"
        "def g():\n"
        "    import pkg.b\n"                                # deferred a->b, also module-level
        "    from pkg import b as sub\n"                    # `from pkg import <unit>` names the unit
        "class K:\n"
        "    def m(self):\n"
        "        from pkg.a import w\n"                     # intra-unit: never an edge\n"
        "        return w\n")
    (root / "b" / "y.py").write_text("def h():\n    from pkg.a import x\n    return x\n")
    (root / "c" / "z.py").write_text("q = 1\n")
    (root / "top.py").write_text("import pkg.c.z\n")
    graph = scan(root, package_root="pkg")
    assert set(graph["module"]) == {("a", "b"), ("a", "c"), ("a", "pkg"), ("top", "c")}
    assert graph["module"][("a", "b")] == ["a/x.py:2"], "module-level sites only, never a merge"
    assert set(graph["deferred"]) == {("a", "c"), ("a", "b"), ("b", "a")}
    assert set(deferred_only(graph)) == {("b", "a")}, (
        "a deferred site of an edge also taken at module level is not a deferred edge")
    assert set(graph["typing"]) == {("a", "b")}
