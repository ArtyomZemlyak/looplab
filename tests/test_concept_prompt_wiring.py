"""The concept-consolidation PromptStore override reaches every product entry point.

`concept_consolidate_system` was registered and rendered inside `consolidate_concepts`, but the
public `build_concept_map` entry had no PromptStore parameter.  The live cadence and both CLI
callers therefore advertised a hot-reloadable override that could never affect a production call.
"""
from __future__ import annotations

import ast
from pathlib import Path

from looplab.core.models import RunState
from looplab.core.prompts import PromptStore
from looplab.search import concept_map, concept_tagging
from looplab.search.concept_graph import ConceptGraph


ROOT = Path(__file__).resolve().parents[1]


def test_build_concept_map_forwards_the_prompt_store_to_consolidation(monkeypatch):
    graph = ConceptGraph(task_type="test")
    prompts = PromptStore()
    captured = {}

    def fake_tags(_state, built_graph, _client, **_kwargs):
        built_graph.ensure("data/augmentation")
        built_graph.ensure("augmentation/general")
        return {0: frozenset({"data/augmentation"})}

    def fake_consolidate(built_graph, tags, **kwargs):
        captured["prompts"] = kwargs.get("prompts")
        return built_graph, tags, {}

    monkeypatch.setattr(concept_tagging, "tag_nodes_llm", fake_tags)
    monkeypatch.setattr(concept_map, "consolidate_concepts", fake_consolidate)
    monkeypatch.setattr(concept_map, "derive_reference_concepts", lambda *_a, **_k: [])

    out = concept_map.build_concept_map(
        RunState(goal="g"), client=object(), seed_graph=graph, prompts=prompts)

    assert out["mode"] == "llm"
    assert captured["prompts"] is prompts


def test_every_product_concept_map_call_supplies_the_prompt_store():
    """Guard the three production callers; library callers may intentionally use shipped defaults."""
    call_sites = []
    for relative in ("looplab/engine/concept_cadence.py", "looplab/cli/concept_cmds.py"):
        path = ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute) else "")
            if name == "build_concept_map":
                call_sites.append((relative, node.lineno, {kw.arg for kw in node.keywords}))

    assert len(call_sites) == 3, (
        "the product build_concept_map call-site inventory changed; review the new caller's "
        "PromptStore contract")
    missing = [(path, line) for path, line, keywords in call_sites if "prompts" not in keywords]
    assert not missing, (
        f"concept-map product caller(s) {missing} silently bypass prompt_dir; pass a PromptStore "
        "or explicitly document why shipped defaults are required")
