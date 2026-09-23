"""The layering clauses two engine mixins state, re-derived by AST (review 2026-09-22, ENG3-13 / doc 50
EM-17).

`engine/research_cadence.py` and `engine/strategy.py` each closed their module docstring with a
"Layering:" sentence that LISTED the packages the module imports — "only core, events and stdlib" and
"only core, events, search, agents and stdlib". Both were false the day the review read them: the
first module imports `agents` at module level, the second `trust` and four engine siblings, and the
"cli PRESETS" the second called lazy were not imported at all. A permitted edge in a wrong sentence is
still a wrong sentence, and a list of packages in prose is exactly the kind of fact that is recorded
in one place while its truth lives in another.

So the headers no longer list anything, and what they DO claim is held here, the way
`tests/test_agents_search_direction.py` holds the search/agents edge:

* neither module imports the orchestrator at run time, at ANY level (the mixin convention — `self`
  IS the Engine, so nothing in a mixin needs the class that composes it), nor `serve`, nor `cli`;
* `research_cadence.py`'s trust/search dependencies (`trust/memo_verify.py`,
  `search/hybrid_merge.py`) are imported ONLY inside the methods that call them: they are patch seams,
  resolved at call time, so a test that monkeypatches the SOURCE module intercepts the live call — a
  module-level `from … import verify_memo` would bind the original at import and every such patch
  would silently stop reaching it. The check is two-sided: the deferred import must still exist, or
  the rule guards nothing.

AST, never substrings: the scanner is exercised on a synthetic module at the end, where an import
named only in a comment or a string must not count and a `TYPE_CHECKING` import must not be mistaken
for a run-time one.
"""
from __future__ import annotations

import ast

import pytest

from _source_scan import PKG, iter_trees

# module -> the dotted modules it may import ONLY inside a function (patch seams), non-empty rows
# only. Both sides are checked: never at module level, and still imported somewhere.
DEFERRED_ONLY: dict[str, frozenset[str]] = {
    "engine/research_cadence.py": frozenset({"looplab.trust.memo_verify",
                                             "looplab.search.hybrid_merge"}),
    "engine/strategy.py": frozenset(),
}
# Never imported at run time (module level or inside a function) by any module above.
FORBIDDEN_AT_RUN_TIME = ("looplab.engine.orchestrator", "looplab.serve", "looplab.cli")


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


def classified_imports(tree: ast.AST) -> list[tuple[str, str]]:
    """`(dotted module, kind)` for every import in `tree`, kind in module / deferred / typing.

    `from looplab.engine import orchestrator` is reported as `looplab.engine.orchestrator`, so a
    submodule import cannot hide behind its package."""
    out: list[tuple[str, str]] = []

    def record(node: ast.AST, kind: str) -> None:
        if isinstance(node, ast.Import):
            out.extend((alias.name, kind) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.append((node.module, kind))
            out.extend((f"{node.module}.{alias.name}", kind) for alias in node.names)

    def walk(node: ast.AST, in_function: bool, in_typing: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.If) and _is_type_checking(child.test):
                for grand in child.body:
                    visit(grand, in_function, True)
                for grand in child.orelse:
                    visit(grand, in_function, in_typing)
                continue
            visit(child, in_function, in_typing)

    def visit(node: ast.AST, in_function: bool, in_typing: bool) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            walk(node, True, in_typing)
            return
        record(node, "typing" if in_typing else ("deferred" if in_function else "module"))
        walk(node, in_function, in_typing)

    walk(tree, False, False)
    return out


def _tree_of(rel: str) -> ast.AST:
    for path, tree in iter_trees(PKG / rel.split("/")[0]):
        if path.relative_to(PKG).as_posix() == rel:
            return tree
    raise AssertionError(f"looplab/{rel} is gone — re-point DEFERRED_ONLY")


def _reaches(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


@pytest.mark.parametrize("rel", sorted(DEFERRED_ONLY))
def test_the_mixin_never_imports_the_orchestrator_serve_or_cli_at_run_time(rel):
    offenders = sorted({module for module, kind in classified_imports(_tree_of(rel))
                        if kind != "typing"
                        and any(_reaches(module, prefix) for prefix in FORBIDDEN_AT_RUN_TIME)})
    assert not offenders, f"looplab/{rel} imports {offenders} at run time"


@pytest.mark.parametrize("rel", sorted(rel for rel, seams in DEFERRED_ONLY.items() if seams))
def test_the_patch_seams_stay_function_local(rel):
    imports = classified_imports(_tree_of(rel))
    for seam in DEFERRED_ONLY[rel]:
        hoisted = [module for module, kind in imports if kind == "module" and _reaches(module, seam)]
        assert not hoisted, (
            f"looplab/{rel} imports {seam} at MODULE level, which binds the original at import: a "
            "test that monkeypatches the source module no longer reaches the live call")
        assert any(kind == "deferred" and _reaches(module, seam) for module, kind in imports), (
            f"looplab/{rel} no longer imports {seam} at all — if that is intentional, drop its row "
            "here and the sentence in the module docstring that names it")


def test_the_scanner_sees_code_and_not_prose():
    tree = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "import looplab.core.models\n"
        "# from looplab.engine.orchestrator import Engine\n"
        "NOTE = 'from looplab.serve import app'\n"
        "if TYPE_CHECKING:\n"
        "    from looplab.engine.orchestrator import Engine\n"
        "def later():\n"
        "    from looplab.trust.memo_verify import verify_memo\n"
        "    return verify_memo\n"
        "class K:\n"
        "    def m(self):\n"
        "        from looplab.engine import orchestrator\n")
    got = set(classified_imports(tree))
    assert ("looplab.core.models", "module") in got
    assert ("looplab.engine.orchestrator.Engine", "typing") in got
    assert ("looplab.trust.memo_verify", "deferred") in got
    assert ("looplab.engine.orchestrator", "deferred") in got          # the submodule, named
    assert not any(module.startswith("looplab.serve") for module, _kind in got)
