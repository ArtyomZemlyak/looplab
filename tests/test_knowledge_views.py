"""The cross-run knowledge read model is PUBLIC, and it is the only way in (doc 25 XP-01 / TO-09).

`tools/cross_run_tools.py`, `tools/concept_tools.py`, `cli/governance_cmds.py` and
`serve/routers/cross_run.py` used to read the shared stores through thirteen underscore-private
engine names plus the purge sentinel — lazily imported, from a package that sits BELOW the engine,
with `CrossRunTools.execute` swallowing the resulting ImportError into "(cross-run tool
unavailable)". That is a rename away from a cross-run memory that silently stops answering, and
`tests/test_cross_package_private_seams.py` could only make the breakage LOUD, not the boundary
public.

`engine/knowledge_views.py` is the boundary. Both directions matter and each catches a different
mistake:

* the surface RESOLVES and is the OWNING module's object — so `knowledge_views` cannot drift into a
  second implementation of a view, and a rename inside `concept_capsules` / `claims_health` /
  `concept_registry` is a red test here naming both ends;
* nothing outside `engine/` reaches AROUND it — importing `capsule_rows` from `engine.memory` works
  (memory re-exports it for its in-package callers) and would quietly restore the coupling the
  promotion removed, so the AST scan refuses it.

Driven, not only structural: the last test runs the real `cross_run_prior_attempts` over a real
capsule store, because the failure this whole finding is about is a tool that answers "unavailable"
while every source assertion still passes.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from _source_scan import iter_sources

from looplab.engine import claims_health, concept_capsules, concept_registry, knowledge_views

_PKG = Path(__file__).resolve().parents[1] / "looplab"

# The view -> the module that OWNS it. Written out rather than derived from `knowledge_views`'s own
# imports: the point of the registry is to say where each view lives, so a view that moves house is
# a decision recorded here, not an import line that quietly changed.
OWNERS = {
    "CONCEPT_TOMBSTONE": concept_registry,
    "capsule_completeness": concept_capsules,
    "capsule_fingerprint_scope_complete": concept_capsules,
    "capsule_rows": concept_capsules,
    "capsule_source_summary": concept_capsules,
    "claim_source_rows": claims_health,
    "dedup_valid_capsules": concept_capsules,
    "filter_capsule_rows": concept_capsules,
    "filter_claim_assessments": claims_health,
    "filter_claim_source_rows": claims_health,
    "load_claim_source_path": claims_health,
    "portfolio_concept_overview_data": concept_capsules,
    "safe_claim_source_summary": claims_health,
    "safe_research_source_summary": claims_health,
}

# The packages that read cross-run knowledge and sit outside `engine/`.
CONSUMER_PACKAGES = ("tools", "cli", "serve", "adapters", "agents", "search", "trust")


def test_the_declared_surface_is_exactly_what_the_module_exports():
    assert set(knowledge_views.KNOWLEDGE_VIEWS) == set(OWNERS), (
        "KNOWLEDGE_VIEWS and this test's OWNERS map disagree about the surface — adding a view "
        "means naming its owner here in the same change")
    assert list(knowledge_views.KNOWLEDGE_VIEWS) == sorted(knowledge_views.KNOWLEDGE_VIEWS), (
        "keep KNOWLEDGE_VIEWS sorted so two additions cannot conflict on ordering alone")
    assert knowledge_views.__all__ == list(knowledge_views.KNOWLEDGE_VIEWS)


@pytest.mark.parametrize("name", sorted(OWNERS))
def test_every_view_is_the_owning_module_s_own_object(name):
    """Not a copy, not a wrapper: ONE implementation and one docstring per view.

    A wrapper would be free to drift from the signature it is standing in for, and this surface is
    imported function-locally, so the drift would surface as a TypeError inside a swallowed tool
    call rather than at import."""
    assert hasattr(knowledge_views, name), f"knowledge_views lost the public view {name}"
    assert getattr(knowledge_views, name) is getattr(OWNERS[name], name), (
        f"knowledge_views.{name} is no longer {OWNERS[name].__name__}.{name} — either the owner "
        "renamed it (update both ends) or the facade grew a second implementation")
    assert not name.startswith("_"), "a view on the public surface may not be spelled private"


def test_the_re_export_chain_still_carries_the_same_objects():
    """`engine/memory.py` and `engine/claims.py` re-export these for their in-package callers. Both
    spellings must stay ONE object, exactly as the modules' docstrings promise, or a monkeypatch
    through the historical path becomes a silent no-op."""
    from looplab.engine import claims, memory

    for name in ("capsule_rows", "dedup_valid_capsules", "portfolio_concept_overview_data"):
        assert getattr(memory, name) is getattr(knowledge_views, name)
    for name in ("claim_source_rows", "filter_claim_assessments", "safe_claim_source_summary"):
        assert getattr(claims, name) is getattr(knowledge_views, name)


def _import_sites() -> list[str]:
    """Every `from looplab.engine.<x> import <a view>` outside `engine/` that is not the facade."""
    offenders: list[str] = []
    views = set(OWNERS)
    for path, source in iter_sources(_PKG):
        rel = path.relative_to(_PKG).as_posix()
        if not rel.split("/")[0] in CONSUMER_PACKAGES:
            continue
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if not node.module.startswith("looplab.engine"):
                continue
            if node.module == "looplab.engine.knowledge_views":
                continue
            reached = sorted({alias.name for alias in node.names} & views)
            if reached:
                offenders.append(f"{rel}:{node.lineno}: {node.module} -> {', '.join(reached)}")
    return offenders


def test_no_consumer_outside_the_engine_reaches_around_the_facade():
    """The direction that keeps the promotion from decaying.

    `engine.memory` still re-exports every capsule view, so importing one from there WORKS — which
    is exactly why this has to be a test and not a convention. The facade exists so an engine-side
    reshuffle is one edit; a consumer that binds to `engine.memory` puts the edit back in four
    packages."""
    offenders = _import_sites()
    assert not offenders, (
        "these read the cross-run knowledge views from an engine INTERNAL instead of "
        "`looplab.engine.knowledge_views`:\n  " + "\n  ".join(offenders))


def test_the_scan_can_see_a_reach_around(tmp_path):
    """A scan that matches nothing is indistinguishable from one whose walk is broken, so mutate a
    THROWAWAY tree (never the real one) and prove the offender is found."""
    source = "from looplab.engine.memory import capsule_rows\n"
    found = [alias.name for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.ImportFrom) and node.module == "looplab.engine.memory"
             for alias in node.names if alias.name in set(OWNERS)]
    assert found == ["capsule_rows"]


def test_the_cross_run_tool_still_answers_through_the_public_surface(tmp_path):
    """DRIVEN. The failure mode this finding is about is a tool that answers "(cross-run tool
    unavailable)" — a string, not an exception — so the only proof that the promotion is wired is a
    real store read back through the real tool."""
    from looplab.tools.cross_run_tools import CrossRunTools

    capsule = {
        "v": concept_capsules.CONCEPT_CAPSULE_VERSION,
        "run_id": "prior-run", "run_uid": "prior-uid", "task_id": "task-a",
        "direction": "max", "best_metric": 0.9,
        "concept_evidence": "classifier",
        "fingerprint": ["kind:text", "goal:recall"],
        "fingerprint_total": 2, "fingerprint_omitted": 0, "fingerprint_complete": True,
        "concepts": ["regularization/r-drop"],
        "concepts_total": 1, "concepts_omitted": 0, "concepts_complete": True,
        "concept_outcomes": {"regularization/r-drop": 0.9},
        "concept_outcomes_total": 1, "concept_outcomes_omitted": 0,
        "concept_outcomes_complete": True,
        "concept_evidence_nodes_total": 1, "concept_evidence_nodes_incomplete": 0,
        "concept_evidence_complete": True, "concept_evidence_observed": True,
    }
    (tmp_path / "concept_capsules.jsonl").write_text(
        json.dumps(capsule) + "\n", encoding="utf-8")

    tools = CrossRunTools(str(tmp_path))
    answer = tools.execute("cross_run_prior_attempts", {"idea": "try r-drop regularization"})
    assert "unavailable" not in answer, (
        f"the cross-run tool degraded instead of reading the store: {answer!r}")
    assert "r-drop" in answer, answer
