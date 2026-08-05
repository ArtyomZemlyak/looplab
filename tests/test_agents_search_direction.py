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

from _source_scan import iter_trees

_PKG = Path(__file__).resolve().parents[1] / "looplab"


def _imports(package: str, target_prefix: str):
    """(path, lineno, module, is_module_level) for every import of `target_prefix` in `package`."""
    found = []
    for path, tree in iter_trees(_PKG / package):
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


# --- SE-02: one researcher-wrapper base, and the divergences it must NOT absorb ------------------

def test_all_three_researcher_wrappers_share_the_base():
    from looplab.agents.roles import WrapsResearcher
    from looplab.search.foresight import ForesightPanelResearcher
    from looplab.search.panel import PanelResearcher
    from looplab.search.surrogate import SurrogateResearcher

    for cls in (PanelResearcher, SurrogateResearcher, ForesightPanelResearcher):
        assert issubclass(cls, WrapsResearcher), f"{cls.__name__} left the wrapper contract"


def test_a_bare_surrogate_wrapper_still_hides_its_fallback_client():
    """The divergence a shared `client` pass-through would have destroyed, and the reason doc 25
    SE-02's recommendation could not be implemented literally.

    `cli/__init__.py` gates foresight wiring on `getattr(researcher, "client", None) is not None`.
    A surrogate wrapping a client-bearing fallback must still read None, or that gate flips on for a
    run that never configured foresight."""
    from looplab.search.surrogate import SurrogateResearcher

    class _Fallback:
        client = object()
        space_hint = "sh"

    wrapper = SurrogateResearcher({}, fallback=_Fallback())
    assert getattr(wrapper, "client", None) is None, (
        "the surrogate wrapper surfaced its fallback's client; the foresight gate in cli/__init__ "
        "now turns on for runs that never asked for it")
    assert wrapper.space_hint == "sh", "space_hint IS shared and must still forward"


def test_the_panel_wrapper_still_forwards_the_configured_client():
    """The opposite divergence, equally load-bearing: chain-walkers getattr `client`/`prompts`/
    `parser` off the ACTIVE researcher, which may be this wrapper, and a missing attr silently
    shadowed the run's configured PromptStore/parser/client behind the defaults."""
    from looplab.search.panel import PanelResearcher

    class _Base:
        client, prompts, parser = object(), object(), object()
        space_hint = "sh"

    base = _Base()
    wrapper = PanelResearcher.__new__(PanelResearcher)
    wrapper.base = base
    for attr in ("client", "prompts", "parser"):
        assert getattr(wrapper, attr) is getattr(base, attr), f"panel stopped forwarding {attr}"
    assert wrapper.space_hint == "sh"


def test_the_shared_base_forwards_nothing_it_should_not():
    """The base is deliberately thin. If it ever grows `client`/`prompts`/`parser`, the surrogate's
    fall-through breaks silently — so the base's own surface is pinned here."""
    from looplab.agents.roles import WrapsResearcher

    owned = {n for n in vars(WrapsResearcher) if not n.startswith("__")}
    assert owned == {"_delegate", "space_hint"}, (
        f"WrapsResearcher grew {owned - {'_delegate', 'space_hint'}}; a member here applies to all "
        "three wrappers, and their client/parser/prompts rules genuinely differ")
