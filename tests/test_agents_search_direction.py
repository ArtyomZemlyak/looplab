"""`agents` may reach `search` only through a deferred import (doc 25 AG-07).

Five `search` modules import `looplab.agents` at MODULE level — `forward_hints`, `WrapsDeveloper`,
the speculation constants. That makes `search -> agents` the load-bearing direction, and leaves
`agents -> search` with exactly one legal form: a function-local import. Today there is one, in
`roles._state_brief`.

The cycle is currently held open by nothing but where that import sits. CLAUDE.md's layering rules
covered core/events/serve/engine and said nothing about this pair, so the next module-level
`from looplab.search...` added to `agents/` would close the loop into an ImportError at startup —
not a subtle wrongness, a dead process. The rule is now written down there, and enforced here,
because a comment is precisely what was missing and would be missing again.
"""
from __future__ import annotations

import ast
from pathlib import Path

_PKG = Path(__file__).resolve().parents[1] / "looplab"


def _imports(package: str, target_prefix: str):
    """(path, lineno, module, is_module_level) for every import of `target_prefix` in `package`."""
    found = []
    for path in sorted((_PKG / package).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            module = getattr(node, "module", None) if isinstance(node, ast.ImportFrom) else None
            names = ([module] if module else
                     [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
            for name in names:
                if name and name.startswith(target_prefix):
                    found.append((path.relative_to(_PKG.parent), node.lineno, name,
                                  node.col_offset == 0))
    return found


def test_agents_never_imports_search_at_module_level():
    offenders = [f"{path}:{lineno}: {module}"
                 for path, lineno, module, top_level in _imports("agents", "looplab.search")
                 if top_level]
    assert not offenders, (
        "`agents` imports `search` at MODULE level, which closes the agents<->search cycle into an "
        "ImportError at startup — `search` already imports `agents` at module scope. Move it inside "
        "the function that needs it:\n  " + "\n  ".join(offenders))


def test_the_deferred_import_that_makes_this_rule_load_bearing_still_exists():
    """A rule with no instance left is a rule nobody will notice breaking. This pins the one
    deferred import the asymmetry exists for, so removing it is a deliberate edit."""
    deferred = [entry for entry in _imports("agents", "looplab.search") if not entry[3]]
    assert deferred, (
        "no deferred agents -> search import remains. If that is intentional, this guard and the "
        "CLAUDE.md rule can go — but say so; do not leave a rule guarding nothing.")
    assert any("concept_projection" in module for _p, _l, module, _t in deferred)


def test_search_really_does_depend_on_agents_at_module_level():
    """The other half of the asymmetry. If this ever stops being true the direction is free again,
    and this test failing is the signal to revisit the rule rather than to work around it."""
    module_level = [f"{path}:{lineno}"
                    for path, lineno, _module, top_level in _imports("search", "looplab.agents")
                    if top_level]
    assert len(module_level) >= 3, (
        f"only {len(module_level)} module-level search -> agents imports found; the constraint on "
        "the reverse direction exists because of them, so re-check AG-07 before relying on it")


def test_the_rule_is_written_where_a_maintainer_will_look():
    """The finding was that nothing STATED the direction. A guard that enforces an unwritten rule
    just moves the surprise from startup to CI."""
    claude_md = (_PKG.parent / "CLAUDE.md").read_text(encoding="utf-8")
    assert "`search` may import `agents` at" in claude_md and "deferred" in claude_md, (
        "the agents<->search direction is no longer stated in CLAUDE.md's layering rule")
