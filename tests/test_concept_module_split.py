"""The concept cluster is five modules now, and the seams that had to survive the split (doc 25 SE-09).

`search/concept_graph.py` was a 1,691-line god-module: vocabulary + taggers + analytics + view
projections + LLM consolidation. It is five files in a declared layer order —
`concept_graph` (structure/skeletons) at the bottom, `concept_tagging` and `concept_lens` above it,
`concept_analytics` above tagging, `concept_map` on top of all of them.

Splitting one module into five creates a failure mode the original could not have. `from
looplab.search.concept_tagging import tag_nodes_heuristic` binds BY VALUE, so a
`monkeypatch.setattr(concept_tagging, "tag_nodes_heuristic", …)` would stop reaching
`concept_analytics.concept_coverage` — a seam that worked before the split only because caller and
callee shared one namespace. CLAUDE.md records the same hazard for `serve/scope_actions.py`
importing store names ("binds them BY VALUE, so it is a THIRD patch surface"), and this cluster has
live patches of exactly that shape: `tests/test_retro_tag_persist.py` on the tagger,
`tests/test_authored_concepts.py` and `tests/test_snapshot_cadence_idempotence.py` on
`build_concept_map`. A lost seam does not fail loudly — the patched function is simply never called
and the assertion below it grades real output.

So a cluster module reaches a sibling's functions THROUGH the module object, and the first three
tests DRIVE that (patch, call, watch the effect land in the return value) rather than pinning an
import line. Only driving it tells "the module got imported" apart from "the patched function
actually ran".
"""
from __future__ import annotations

import ast

from _source_scan import PKG
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.search import (concept_analytics, concept_graph, concept_lens, concept_map,
                            concept_tagging)

# module stem -> layer. A module may import STRICTLY lower layers and nothing else in the cluster.
# `concept_tagging` and `concept_lens` share layer 1 because neither needs the other: tagging turns
# experiments into ids, the lens turns ids into trees, and an edge either way would put the LLM
# taggers back inside the view path.
CLUSTER_LAYERS = {
    "concept_graph": 0,
    "concept_tagging": 1,
    "concept_lens": 1,
    "concept_analytics": 2,
    "concept_map": 3,
}

# The ONLY names a cluster module may import BY NAME from a sibling — two types and one pure
# identity wrapper. Nothing monkeypatches them, so a by-value binding costs nothing, whereas routing
# ~20 `_normalize_concept_id` call sites through the module object would buy line noise and no
# protection. Every other sibling name is reached as an attribute.
VALUE_ONLY_IMPORTS = frozenset({"Concept", "ConceptGraph", "_normalize_concept_id"})

_CONCEPT_TOUCH = "negatives/external-mining"


def _one_experiment(tmp_path):
    """A folded run with exactly one idea-carrying node — enough for coverage to have a denominator."""
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "t", "task_id": "toy", "goal": "g", "direction": "max"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "rationale": "r", "theme": "th"}})
    store.append("node_evaluated", {"node_id": 0, "metric": 0.5})
    return fold(store.read_all())


# ------------------------------------------------------------------ the seams, driven

def test_patching_the_tagger_still_changes_what_the_analytics_compute(tmp_path, monkeypatch):
    """`concept_coverage(state, graph)` with `tags` omitted runs the deterministic tagger. Before the
    split that call resolved inside one module; now it crosses a file, and it must still resolve
    through `concept_tagging` so the ONE patch point stays the one every existing test uses."""
    state = _one_experiment(tmp_path)
    graph = concept_graph.dense_retrieval_skeleton()
    calls = []

    def fake_tagger(st, g):
        calls.append((st, g))
        return {0: frozenset({_CONCEPT_TOUCH})}

    monkeypatch.setattr(concept_tagging, "tag_nodes_heuristic", fake_tagger)
    coverage = concept_analytics.concept_coverage(state, graph)

    assert calls, ("concept_coverage no longer reaches concept_tagging.tag_nodes_heuristic — the "
                   "analytics bound their own copy at import time, so every monkeypatch of the "
                   "tagger now silently no-ops for them")
    # ...and the RESULT is the patched tagger's, not merely that the patched tagger ran: a call that
    # happens whose output is discarded would satisfy the counter above and nothing else.
    assert coverage["concept_touch"] == {_CONCEPT_TOUCH: 1}
    assert coverage["tagged"] == 1


def test_patching_the_llm_tagger_still_changes_what_the_map_builds(tmp_path, monkeypatch):
    """The same seam one layer up. `build_concept_map`'s agentic path is the engine's entry point,
    and `tests/test_llm_broker.py` reaches the tagger underneath it."""
    state = _one_experiment(tmp_path)
    graph = concept_graph.dense_retrieval_skeleton()
    calls = []

    def fake_llm_tagger(st, g, client, **kwargs):
        calls.append(client)
        return {0: frozenset({_CONCEPT_TOUCH})}

    monkeypatch.setattr(concept_tagging, "tag_nodes_llm", fake_llm_tagger)
    # A bare object() as the client: consolidation and importance derivation both fail closed on it
    # (they are best-effort by contract), so nothing here reaches a provider.
    out = concept_map.build_concept_map(state, "goal", client=object(), seed_graph=graph)

    assert calls, "build_concept_map bound its own copy of tag_nodes_llm"
    assert out["raw_tags"] == {0: frozenset({_CONCEPT_TOUCH})}


def test_patching_the_analytics_still_changes_what_the_map_reports(tmp_path, monkeypatch):
    """The third cross-file call in the cluster, and the one whose loss would be quietest: a stale
    `coverage` in the returned map is a plausible-looking dict, not an error."""
    state = _one_experiment(tmp_path)
    graph = concept_graph.dense_retrieval_skeleton()
    sentinel = {"experiments": 4242, "concept_touch": {}, "tagged": 0}
    monkeypatch.setattr(concept_analytics, "concept_coverage", lambda *a, **k: dict(sentinel))

    out = concept_map.build_concept_map(state, "", client=None, seed_graph=graph)

    assert out["coverage"] == sentinel, (
        "build_concept_map bound its own copy of concept_coverage; patching the analytics no "
        "longer changes the map it reports")


# ------------------------------------------------------------------ the shape of the split

def _cluster_trees():
    for stem in sorted(CLUSTER_LAYERS):
        path = PKG / "search" / f"{stem}.py"
        yield stem, path, ast.parse(path.read_text(encoding="utf-8-sig", errors="replace"),
                                    filename=str(path))


def _sibling_imports(tree):
    """(lineno, sibling stem, [imported names]) for every intra-cluster ImportFrom, at ANY depth."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        parts = node.module.split(".")
        if parts[:2] != ["looplab", "search"]:
            continue
        if len(parts) == 3 and parts[2] in CLUSTER_LAYERS:            # from looplab.search.X import y
            yield node.lineno, parts[2], [a.name for a in node.names]
        elif len(parts) == 2:                                          # from looplab.search import X
            for alias in node.names:
                if alias.name in CLUSTER_LAYERS:
                    yield node.lineno, alias.name, []


def test_the_cluster_import_graph_stays_a_dag_in_the_declared_order():
    """The reason the split is five modules and not the review's four: consolidation and
    `build_concept_map` had no home in the recommendation, and leaving them in `concept_graph.py`
    would have made the base module import its own dependents.

    A module-level cycle is an ImportError, so it cannot ship — but a FUNCTION-LOCAL one runs fine
    and silently re-tangles the cluster, which is what this walks the whole tree for."""
    offenders = []
    for stem, path, tree in _cluster_trees():
        for lineno, sibling, _names in _sibling_imports(tree):
            if CLUSTER_LAYERS[sibling] >= CLUSTER_LAYERS[stem]:
                offenders.append(f"{path.relative_to(PKG.parent)}:{lineno}: {stem} "
                                 f"(layer {CLUSTER_LAYERS[stem]}) -> {sibling} "
                                 f"(layer {CLUSTER_LAYERS[sibling]})")
    assert not offenders, (
        "a concept module imports a sibling at its own layer or above, which puts the god-module's "
        "coupling back (function-local imports included — those do not raise):\n  "
        + "\n  ".join(offenders))


def test_no_cluster_module_imports_a_sibling_function_by_name():
    """The by-value binding rule. Anything but a type or the identity wrapper must be reached
    through the module object, or a monkeypatch on the owning module stops reaching this caller."""
    offenders = []
    for stem, path, tree in _cluster_trees():
        for lineno, sibling, names in _sibling_imports(tree):
            bad = sorted(set(names) - VALUE_ONLY_IMPORTS)
            if bad:
                offenders.append(f"{path.relative_to(PKG.parent)}:{lineno}: {stem} imports "
                                 f"{bad} from {sibling} by name")
    assert not offenders, (
        "import the sibling MODULE and call through it — a `from … import <function>` binds the "
        "function object at import time, so `monkeypatch.setattr(<sibling>, name, …)` silently "
        "stops reaching this module:\n  " + "\n  ".join(offenders))


def test_the_base_module_did_not_grow_a_reexport_facade():
    """A re-export would make every old spelling resolve again — and that is the trap, not the fix.
    `concept_graph.tag_nodes_heuristic` that resolves while `concept_analytics` calls its own
    binding is the silent no-op above wearing a green test. An ImportError naming both ends is the
    outcome worth having."""
    moved = {"tag_text": concept_tagging, "tag_nodes_heuristic": concept_tagging,
             "tag_nodes_llm": concept_tagging, "experiment_nodes": concept_tagging,
             "node_text": concept_tagging, "concept_coverage": concept_analytics,
             "concept_metrics": concept_analytics, "uncovered_regions": concept_analytics,
             "project_hierarchy": concept_lens, "project_lens": concept_lens,
             "derive_lens": concept_lens, "node_concept_delta": concept_lens,
             "build_concept_map": concept_map, "consolidate_concepts": concept_map}
    for name, owner in moved.items():
        assert hasattr(owner, name), f"{owner.__name__} lost {name}"
        assert not hasattr(concept_graph, name), (
            f"concept_graph re-exports {name}; it lives in {owner.__name__} now, and a resolving "
            "alias turns a stale monkeypatch from an AttributeError into a silent no-op")


def test_the_analytics_module_cannot_reach_a_provider():
    """The property the split made statable: `concept_analytics` is pure, so a replay recomputes it
    byte-identically (engine invariant 5's neighbourhood). While it lived inside the god-module,
    "no LLM here" was a claim about a REGION of a file that also held four LLM call sites; it is now
    a claim about a whole file, and this is what holds it.

    AST rather than a driven test because purity is the absence of an effect: there is no call that
    proves no provider was reached. Comments are not AST nodes, so a commented-out import does not
    satisfy it either."""
    path = PKG / "search" / "concept_analytics.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="replace"), filename=str(path))
    forbidden = ("looplab.core.parse", "looplab.core.llm", "looplab.agents", "pydantic",
                 "looplab.core.llm_broker")
    bad_imports = sorted(
        f"{node.lineno}: {node.module}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
        and node.module.startswith(forbidden))
    assert not bad_imports, (
        "concept_analytics imported a provider path; the analytics must stay pure so replay "
        "recomputes them byte-identically:\n  " + "\n  ".join(bad_imports))
    io_calls = sorted({node.func.id for node in ast.walk(tree)
                       if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                       and node.func.id in {"open", "input"}})
    assert not io_calls, f"concept_analytics does I/O: {io_calls}"
