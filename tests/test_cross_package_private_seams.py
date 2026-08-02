"""Every underscore-private name imported ACROSS a package boundary is declared here (doc 25
XP-01 / TO-09).

An underscore normally licenses the owning module to rename freely. Twenty-six of them are load
bearing across packages — `tools/` reaches into `engine.memory` and `engine.claims`, `serve/` into
`events.traceview`, four packages into `events.eventstore._interprocess_lock` — and every one of
those imports is FUNCTION-LOCAL (deliberately, to keep the import graph acyclic). So a rename
produces no import-time error at all. `CrossRunTools.execute` then swallows the ImportError into
the generic "(cross-run tool unavailable)" string, and the affected tools simply stop answering.

This is the silent-rename failure class the registries in CLAUDE.md exist to prevent, applied to
the one surface that had no registry. Two directions, both required:

* every declared edge still RESOLVES — renaming a private that another package imports is a red
  test naming both ends, not a tool that quietly answers "unavailable";
* every edge in the tree is DECLARED — adding a new cross-package private dependency means saying
  so here, which is the moment to ask whether it should be public instead.

The registry is a list of debts, not a design. The review's preferred fix is to promote what
`tools/` actually needs into a public read-model API; until then this makes the debt visible and
its breakage loud.
"""
from __future__ import annotations

import ast
import collections
import importlib
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parents[1] / "looplab"

# consumer package -> provider module -> the private names it imports.
CROSS_PACKAGE_PRIVATE_IMPORTS: dict[str, dict[str, tuple[str, ...]]] = {
    "adapters": {
        "looplab.runtime.sandbox": ("_last_json_dict",),
        "looplab.tools.reposcout": ("_SKIP_DIRS",),
    },
    "agents": {
        "looplab.core.llm": ("_reasoning_of",),
        "looplab.runtime.sandbox": ("_kill_tree",),
    },
    "cli": {
        "looplab.adapters.tasks": ("_make_abstractor",),
        "looplab.engine.claims": ("_load_claim_source_path", "_safe_claim_source_summary",
                                  "_safe_research_source_summary"),
        "looplab.engine.memory": ("_portfolio_concept_overview_data",),
        "looplab.events.eventstore": ("_interprocess_lock",),
    },
    "engine": {
        "looplab.agents.roles": ("_state_brief",),
        "looplab.events.eventstore": ("_interprocess_lock",),
        "looplab.runtime.command_eval": ("_LABEL_KEYS", "_PRED_KEYS", "_as_list"),
        "looplab.runtime.sandbox": ("_run_argv", "_to_float"),
        "looplab.search.concept_graph": ("_experiment_nodes",),
        "looplab.tools.vectorstore": ("_cosine",),
    },
    "serve": {
        "looplab.agents.roles": ("_CONCEPT_AUTHORING_GUIDANCE",),
        "looplab.core.atomicio": ("_ensure_strict_parent", "_windows_move_write_through"),
        "looplab.engine.claims": ("_filter_claim_assessments", "_safe_claim_source_summary",
                                  "_safe_research_source_summary"),
        "looplab.events.eventstore": ("_interprocess_lock",),
        "looplab.events.traceview": ("_bounded_tail", "_cap_span_io", "_cap_str", "_finite_number",
                                     "_normalize_span", "_normalized_id", "_projection_counter",
                                     "_response_projection", "_tree"),
    },
    "tools": {
        "looplab.core": ("_pathsafe",),
        "looplab.core.gitenv": ("_GIT_CRED_KEY_MARKERS", "_GIT_IDENTITY"),
        "looplab.engine.claims": ("_claim_source_rows", "_filter_claim_assessments",
                                  "_filter_claim_source_rows", "_safe_claim_source_summary",
                                  "_safe_research_source_summary"),
        "looplab.engine.concept_registry": ("_TOMBSTONE",),
        "looplab.engine.memory": ("_capsule_completeness", "_capsule_fingerprint_scope_complete",
                                  "_capsule_rows", "_capsule_source_summary",
                                  "_dedup_valid_capsules", "_filter_capsule_rows",
                                  "_portfolio_concept_overview_data"),
        "looplab.events.eventstore": ("_interprocess_lock",),
        "looplab.serve.engine_proc": ("_engine_alive", "_fresh_resume_launch_pending",
                                      "_fresh_run_launch_pending", "_run_lifecycle_lock"),
    },
}

_DECLARED = {(consumer, module, name)
             for consumer, modules in CROSS_PACKAGE_PRIVATE_IMPORTS.items()
             for module, names in modules.items() for name in names}


def _actual_edges() -> set[tuple[str, str, str]]:
    edges: set[tuple[str, str, str]] = set()
    for path in sorted(_PKG.rglob("*.py")):
        consumer = path.relative_to(_PKG).as_posix().split("/")[0]
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except SyntaxError:                       # not this guard's job to police
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if not node.module.startswith("looplab."):
                continue
            provider = node.module.split(".")[1]
            if provider == consumer:              # inside one package a private stays private
                continue
            for alias in node.names:
                if alias.name.startswith("_"):
                    edges.add((consumer, node.module, alias.name))
    return edges


@pytest.mark.parametrize("consumer,module,name", sorted(_DECLARED))
def test_every_declared_private_seam_still_resolves(consumer, module, name):
    """The direction that catches a rename.

    These imports are function-local, so nothing fails at import time; the consumer just starts
    raising ImportError deep inside a tool call that swallows it. Resolving each name here turns
    that into a red test that names both ends."""
    provider = importlib.import_module(module)
    assert hasattr(provider, name), (
        f"{module}.{name} is gone, but {consumer}/ still imports it. Renaming a private that "
        "another PACKAGE depends on needs the consumer updated in the same change (or the name "
        "promoted to a public read-model API — see doc 25 XP-01).")


def test_no_undeclared_cross_package_private_import_exists():
    """The direction that catches a new debt.

    A new private cross-package import is not forbidden — it is a decision, and this is where it
    gets made explicitly instead of by autocomplete."""
    undeclared = sorted(_actual_edges() - _DECLARED)
    assert not undeclared, (
        "these cross-package private imports are not declared in "
        "CROSS_PACKAGE_PRIVATE_IMPORTS:\n  "
        + "\n  ".join(f"{consumer}/ <- {module}.{name}" for consumer, module, name in undeclared)
        + "\nDeclare them, or import a public name instead.")


def test_the_registry_does_not_carry_edges_that_no_longer_exist():
    """A stale entry is worse than no entry: it makes the surface look bigger than it is, and it
    keeps a name alive that the owning module could otherwise rename freely."""
    stale = sorted(_DECLARED - _actual_edges())
    assert not stale, (
        "these declared seams are gone from the tree — drop them from the registry so the debt "
        "shrinks visibly:\n  "
        + "\n  ".join(f"{consumer}/ <- {module}.{name}" for consumer, module, name in stale))


def test_the_scan_can_actually_see_a_cross_package_private_import():
    """A scan that matches nothing is indistinguishable from one whose walk is broken."""
    edges = _actual_edges()
    assert edges, "the AST walk found no cross-package private imports at all"
    # The most-depended-on one, named explicitly: four packages outside `events` take this lock.
    lock_consumers = {consumer for consumer, module, name in edges
                      if name == "_interprocess_lock"}
    assert len(lock_consumers) >= 3, (
        f"expected several packages to depend on events.eventstore._interprocess_lock, saw "
        f"{sorted(lock_consumers)} — either the walk regressed or the name was finally promoted")


def test_the_widest_debts_are_the_ones_the_review_named():
    """Keeps the registry honest about WHERE the pressure is, so promoting to a public API can be
    prioritized by size rather than by whoever trips over it first."""
    per_provider = collections.Counter(
        module for _consumer, module, _name in _DECLARED)
    assert per_provider["looplab.events.traceview"] >= 9, (
        "serve/ leans on nine traceview privates — doc 25 SR-* proposes a public projection API")
    assert per_provider["looplab.engine.memory"] >= 7, (
        "tools/ + cli/ lean on the capsule read-model — doc 25 XP-01's primary promotion candidate")
