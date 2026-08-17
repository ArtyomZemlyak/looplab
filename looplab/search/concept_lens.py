"""Concept VIEW projections — the hierarchy/lens read-models the UI and the run tools read.

Split out of `concept_graph.py` (doc 25 SE-09). A hierarchy is COMPUTED, never stored: these turn a
SET of concept ids (plus the typed edge set and, for `node_concept_delta`, the folded run state)
into the tree shapes `serve/concept_frame.py`, `serve/routers/runs.py` and `tools/run_tools.py`
render.

**Why `search/` and not `events/` or `serve/`** — the review proposed moving these "next to the
other UI projections (events/ per the package map, or serve/)". Both destinations were measured and
both fail:

  * `serve/` is not reachable. `tools/run_tools.py` consumes `project_hierarchy` and
    `node_concept_delta`, and `tools` sits BELOW `serve`;
    `tests/test_cross_package_private_seams.py::test_the_upward_import_is_confined_to_that_one_default`
    allows exactly ONE `tools -> serve` import, inside `RunControlTools.lifecycle`. Two more are a
    red test, not a tidier layer.
  * `events/` cannot take the group whole. `events` imports nothing above `core` (measured: zero
    non-core `looplab.` imports in the package), and `node_concept_delta` needs
    `search/concept_projection.py`'s receipt-aware CURRENT projection — the boundary that decides
    whether a membership may be trusted at all, which is not duplicable into `events` without a
    second copy of the rule. `derive_lens` additionally makes an LLM call, and `events/` holds the
    PURE projections precisely so the fold's package never grows a provider edge. Landing three of
    these five in `events/` and leaving two behind would split one view API across two packages to
    satisfy the letter of a recommendation whose reason — cohesion — it breaks.

So the projections leave the god-module but stay in `search/`, which every consumer (`serve`,
`tools`, `cli`) may already import. The part of the review that was real — the view code is no
longer interleaved with the tagger and the analytics — is the part that happened.
"""
from __future__ import annotations

from typing import Optional

from looplab.core.concepts import CONCEPT_DELTA_MISSING_PARENT_REASON, resolve_concept
from looplab.core.models import NODE_CONCEPT_PROVENANCE_AUTHORED
from looplab.search.concept_graph import ConceptGraph, _normalize_concept_id


# --------------------------------------------------------------------------- #
# Per-node concept delta (PART V Phase 3, Layer 2)
# --------------------------------------------------------------------------- #

def _canonical_with_rename(raw, rename: dict) -> str:
    """Normalize `raw` and resolve a bounded, cycle-guarded consolidation rename chain (the same shape the
    /concepts frame's canonical_concept uses). "" for a malformed id, a cycle, or an over-long chain."""
    canonical, _problem = resolve_concept(raw, rename)
    return canonical or ""


def node_concept_delta(state, node_id) -> dict:
    """PART V Phase 3 (Layer 2): present ONE node's concepts as a DELTA vs its parent(s) — what this
    experiment conceptually ADDED, REMOVED, or INHERITED. Pure/deterministic read-model over the folded
    per-node `node_concepts` full-sets (NO new storage; concepts stay full-sets, this only PROJECTS the
    parent diff), so it is replay-safe and answers "what did this node change relative to its parent".

    The inherited base is the UNION of ALL parents' canonical concepts (a merge inherits from every parent);
    both sides cross the strict, receipt-aware CURRENT projection, so malformed or unavailable memberships
    cannot masquerade as an honest empty set. A legacy full root
    inherits nothing; a Part-V delta-authored root inherits `run_base_concepts`. Returns
    {parent_ids, added, removed, inherited}
    with sorted canonical-id lists plus additive `partial` / `unavailable` and `reasons` receipt fields
    whenever the comparison is not exact. Never raises (malformed state fails closed)."""
    from looplab.search.concept_projection import current_concept_projection

    nodes = getattr(state, "nodes", None)
    nodes = nodes if isinstance(nodes, dict) else {}
    node = nodes.get(node_id)
    if node is None:
        return {"parent_ids": [], "added": [], "removed": [], "inherited": []}

    projection = current_concept_projection(state)
    raw_parent_ids = getattr(node, "parent_ids", None)
    if raw_parent_ids is None:
        raw_parent_ids = []
    invalid_parent_shape = not isinstance(raw_parent_ids, (list, tuple))
    raw_parent_ids = raw_parent_ids if isinstance(raw_parent_ids, (list, tuple)) else []
    integer_parent_ids = [
        parent_id for parent_id in raw_parent_ids
        if isinstance(parent_id, int) and not isinstance(parent_id, bool)
    ]
    parent_ids = list(dict.fromkeys(integer_parent_ids))
    empty = {"parent_ids": sorted(parent_ids), "added": [], "removed": [], "inherited": []}
    if (invalid_parent_shape or len(integer_parent_ids) != len(raw_parent_ids)
            or any(parent_id not in nodes for parent_id in parent_ids)):
        # silently dropping a missing edge reclassifies a corrupt child as an exact root.
        # Retain every integer reference for diagnosis and fail closed with replay's typed dependency reason.
        return {**empty, "unavailable": True,
                "reasons": [CONCEPT_DELTA_MISSING_PARENT_REASON]}
    # a malformed global identity/store projection cannot support either side of a diff.
    # Keep the stable empty shape and expose the receipt instead of calculating authoritative-looking
    # additions from the valid-looking subset that survived canonicalization.
    if projection.global_reasons:
        return {**empty, "unavailable": True, "reasons": list(projection.global_reasons)}
    node_status, node_reasons = projection.node_status(node_id)
    if node_status == "unavailable":
        return {**empty, "unavailable": True, "reasons": list(node_reasons),
                **({"untagged": True} if "membership_not_recorded" in node_reasons else {})}

    parent_reasons: set[str] = set()
    for parent_id in parent_ids:
        parent_status, problems = projection.node_status(parent_id)
        if parent_status != "complete":
            parent_reasons.update(problems or ("parent_membership_unavailable",))
    if parent_reasons:
        return {**empty, "unavailable": True, "reasons": sorted(parent_reasons)}

    own = set(projection.memberships.get(node_id, ()))
    inherited_base: set = set()
    for pid in parent_ids:
        inherited_base.update(projection.memberships.get(pid, ()))
    if not parent_ids:
        raw_deltas = getattr(state, "node_concept_deltas", None)
        provenance = getattr(state, "node_concept_provenance", None)
        if (isinstance(raw_deltas, dict) and node_id in raw_deltas
                and isinstance(provenance, dict)
                and provenance.get(node_id) == NODE_CONCEPT_PROVENANCE_AUTHORED):
            # replay gives an authored delta root the run base. Calling all of those ids
            # `added` makes an explicit zero delta look like it authored the base. Legacy full roots and
            # classifier/operator overrides have no active authored-delta sidecar and retain all-added.
            if projection.run_base_status != "complete":
                return {**empty, "unavailable": True,
                        "reasons": list(projection.run_base_reasons or ("run_base_unavailable",))}
            inherited_base.update(projection.run_base)
    result = {
        "parent_ids": sorted(parent_ids),
        "added": sorted(own - inherited_base),
        "removed": sorted(inherited_base - own),
        "inherited": sorted(own & inherited_base),
    }
    local_reasons = set(projection.partial_nodes.get(node_id, ()))
    # structural identity/store corruption affects every comparison. An unrelated receipt
    # makes the broad projection partial, but must not contaminate this node's otherwise exact delta.
    local_reasons.update(projection.global_reasons)
    if node_status == "partial" or local_reasons:
        # A retained child subset can prove concepts that remain present, but absence from that subset
        # cannot prove removal (the cap/invalid item may have omitted a still-inherited concept).
        result.update({
            "removed": [],
            "partial": True,
            "reasons": sorted(local_reasons),
            "unknown_dimensions": ["removed"],
        })
    return result


# --------------------------------------------------------------------------- #
# The RUN-CONSTANT split (2026-08-17) — which half of a membership is about the RUN
# --------------------------------------------------------------------------- #

# The receipt slugs this split can mint. They are reasons the CLAIM "true of every experiment" could
# not be made; they are never reasons a membership is corrupt (those come from the projection itself
# and are passed through verbatim).
RUN_CONSTANT_UNCLASSIFIED_REASON = "unclassified_experiment"
RUN_CONSTANT_INEXACT_REASON = "inexact_membership"
RUN_CONSTANT_SINGLE_REASON = "single_experiment"
RUN_CONSTANT_NO_EXPERIMENTS_REASON = "no_experiments"


def run_constant_split(state) -> dict:
    """Split a run's per-node concept memberships into the half that is about the RUN and the half that
    is about an EXPERIMENT — the operator's own framing: *"нафига нам eval/recall_top_k и data/esci
    концепты если они всегда будут? вот их надо на ран вешать. А так они просто захламляют."*

    A concept carried by EVERY experiment in the run distinguishes none of them. It is a fact about the
    run's task/stack, it belongs beside the run, and repeated once per node it is noise that crowds out
    the one tag that actually says what the experiment did. Measured on `rubertlite-dr-unified-v9`
    (2026-08-17): 40 of its 48 tag slots — 83.3 % — are the same five ids on all eight experiments, so
    exactly ONE tag per node carries information and it is fifth in a list of six.

    **This is a READER, and that is a decision, not an implementation detail.** "Constant across the
    run" is a CROSS-NODE property and is therefore undecidable when a tag is written: when node 0 is
    tagged nothing is known about node 7, so a writer-side `run_constant` flag would mean "constant
    among the nodes that existed when I fired" and would say different things about the same concept
    depending on when it was stamped. That is the identical ruling `extra_metrics` already carries
    ("constancy … is undecidable at capture anyway because variance is a cross-node property, so a tag
    derived from it would change as later nodes arrived"), and the same answer follows: derive it in
    the projection, where the whole population is in hand, and write nothing new.

    **It is deliberately NOT `RunState.run_base_concepts`, and that distinction is the finding.** The
    run base is SEEDED from the first evaluated node's authored set
    (`engine/concept_cadence.py::_maybe_seed_run_base_concepts`) and every later delta-authoring node
    then inherits it through `replay.py::_materialize_concept_deltas` — so the base is SELF-CONFIRMING:
    the derived intersection can never be smaller than the seeded base no matter what the later
    Researchers meant, and on v9 the base therefore carries `training/negative_mining`, which is node
    0's OWN subject (its hypothesis is "scale hard-negative mining … raise n_negatives from 2 to 4-8").
    Deriving the intersection instead also works on the runs that have no base at all: `v7` has no
    `run_concepts` event and still has two ids on all eight of its experiments.

    FAIL-CLOSED on coverage, because "true of every experiment" is a claim about EVERY experiment. The
    split is made only when every current experiment carries an EXACT membership; a run with one
    unclassified node, one inexact receipt, or one experiment total gets an EMPTY `run_constant` and a
    reason, which renders exactly as today. That is what keeps the negative control honest: on this
    corpus v6 (4 of 7 unclassified), `rubertlite-dense-retrieval` (1 of 81) and v8 (16 of 16 tagged,
    empty intersection) are all byte-identical before and after.

    Returns, always, with sorted canonical ids:
      {"run_constant": [ids],          # [] whenever `coverage` is not "complete"
       "distinguishing": {nid: [ids]}, # every CURRENT node's membership minus `run_constant`
       "no_distinguishing": [nids],    # tagged experiments whose whole membership is run-constant
       "unclassified": [nids],         # current experiments carrying no membership at all
       "population": int,              # experiments the intersection was taken over
       "coverage": "complete"|"partial"|"unavailable",
       "reasons": [slugs]}

    `no_distinguishing` is the population the split EXISTS to make visible, and it is a DIFFERENT defect
    from the redundancy: on v9 it is nodes 0 and 4 (on v7, nodes 0 and 7). Node 0 is one of that run's
    FOUR hard-negative experiments and the only one the taxonomy cannot name — today it wears five chips
    and reads as classified; named here it reads as what it is, an experiment no recorded tag separates
    from node 4. The pass that would classify it is the concept cadence, which never fired in that run
    (docs/BACKLOG.md §0.12). Never raises; malformed state fails closed."""
    from looplab.search.concept_projection import current_concept_projection

    projection = current_concept_projection(state)
    memberships = {int(nid): sorted(set(ids))
                   for nid, ids in (projection.memberships or {}).items()}
    current = sorted(projection.active_nodes or ())
    empty = {"run_constant": [],
             "distinguishing": {nid: list(memberships.get(nid, ())) for nid in current},
             "no_distinguishing": [], "unclassified": [], "population": 0}
    if projection.global_reasons:
        # Structural identity/store corruption is a statement about the whole projection: no subset of
        # it supports a claim about every experiment. Hand back today's rendering with the receipt.
        return {**empty, "coverage": "unavailable",
                "reasons": sorted(set(projection.global_reasons))}

    reasons: set[str] = set()
    unclassified: list[int] = []
    population: list[int] = []
    for nid in current:
        status, node_reasons = projection.node_status(nid)
        ids = memberships.get(nid)
        if not ids:
            # An experiment with no membership is not evidence that a concept is universal — it is
            # evidence that nobody classified it. Both an absent row and an exact known-empty one land
            # here, and both are equally fatal to a "true of every experiment" claim.
            unclassified.append(nid)
            reasons.add(RUN_CONSTANT_UNCLASSIFIED_REASON)
            continue
        if status != "complete":
            # A partial membership is a SUBSET of the truth, so its absence of a concept proves nothing
            # and its presence cannot complete the population either. Keep the row rendered, refuse the
            # claim, and name why through the projection's own reason vocabulary.
            reasons.add(RUN_CONSTANT_INEXACT_REASON)
            reasons.update(node_reasons or ())
            continue
        population.append(nid)
    if not current:
        reasons.add(RUN_CONSTANT_NO_EXPERIMENTS_REASON)
    elif len(current) < 2:
        # With one experiment every concept is trivially "constant" and the split would empty the only
        # node in the run. A claim about variation needs something to vary against.
        reasons.add(RUN_CONSTANT_SINGLE_REASON)
    if reasons or len(population) != len(current) or len(population) < 2:
        # `unclassified` is the actionable half of a refusal and is reported even though no claim was
        # made: "this run has no run-level concepts" and "three experiments were never classified" are
        # different sentences and the operator can only act on the second.
        return {**empty, "unclassified": unclassified,
                "coverage": "partial" if current else "unavailable",
                "reasons": sorted(reasons)}

    constant = set(memberships[population[0]])
    for nid in population[1:]:
        constant &= set(memberships[nid])
    distinguishing = {nid: sorted(set(memberships.get(nid, ())) - constant) for nid in current}
    return {
        "run_constant": sorted(constant),
        "distinguishing": distinguishing,
        "no_distinguishing": [nid for nid in current
                              if memberships.get(nid) and not distinguishing[nid]],
        "unclassified": [],
        "population": len(population),
        "coverage": "complete",
        "reasons": [],
    }


# --------------------------------------------------------------------------- #
# Hierarchy + lens projections — any concept can be an axis
# --------------------------------------------------------------------------- #

def project_hierarchy(concept_ids, *, graph: Optional[ConceptGraph] = None,
                      edges=None, lens: str = "is_a") -> dict:
    """Project a HIERARCHY (tree) from a SET of concept ids under a chosen LENS — the pure read-model
    behind "any concept can be an axis". A hierarchy is COMPUTED, never stored: swap the lens and the
    same concepts re-project. Deterministic (id-sorted), no I/O → replay-safe.

    lens="is_a" (the default, and today's only wired lens): nesting follows the concept PATH — the
    parent of `a/b/c` is `a/b`, roots are the top segment `a`. Every ancestor PREFIX is materialized as
    a node even when only a deep leaf was tagged, so `loss` shows as a group when only
    `loss/contrastive/dcl` was touched. `edges` (a typed concept-edge set) is accepted for the FUTURE
    lenses (uses / co_occurs / …) and ignored under is_a; `graph` is accepted for future multi-parent
    resolution and unused here (an LLM-grown concept is single-parent by its path).

    Returns {"lens", "roots": [top ids sorted], "nodes": {id: {"parent": id|None, "depth": int,
    "children": [ids sorted], "tagged": bool}}} — `tagged` marks ids actually in the input (vs a
    synthetic ancestor materialized only for structure)."""
    tagged: set[str] = set()
    for cid in (concept_ids or ()):
        c = _normalize_concept_id(cid)
        if c:
            tagged.add(c)
    all_ids: set[str] = set()
    for cid in tagged:
        parts = cid.split("/")
        for k in range(1, len(parts) + 1):
            all_ids.add("/".join(parts[:k]))
    children: dict[Optional[str], list[str]] = {}
    nodes: dict[str, dict] = {}
    # REVIEW(2026-07-16): iterate the SORTED ids, not the raw set. A raw-set iteration makes the `nodes`
    # dict's insertion order follow string-hash order (randomized per process via PYTHONHASHSEED), so
    # json.dumps of this projection is NOT byte-stable across processes — contradicting the "Deterministic
    # (id-sorted)" docstring and breaking the HTTP caching/etag and diff-based tests that feed View 1.
    # Sorting here matches the sibling read-models (`concept_metrics` -> `for cid in sorted(touched)`,
    # `concept_coverage` -> `dict(sorted(first_touch.items()))`).
    for cid in sorted(all_ids):
        parts = cid.split("/")
        parent = "/".join(parts[:-1]) if len(parts) > 1 else None
        nodes[cid] = {"parent": parent, "depth": len(parts) - 1,
                      "children": [], "tagged": cid in tagged}
        children.setdefault(parent, []).append(cid)
    for cid in nodes:
        nodes[cid]["children"] = sorted(children.get(cid, []))
    return {"lens": lens, "roots": sorted(children.get(None, [])), "nodes": nodes}


def concept_touch_counts(node_concepts) -> dict:
    """Per-concept touch count from `node_concepts` (how many nodes carry each id) — the orientation
    signal a symmetric (co_occurs) lens needs (higher touch = the hub/parent). Multiple raw ids on one
    node that normalize/consolidate to the same canonical id count as ONE touch. Pure/deterministic."""
    from collections import Counter as _C
    c: _C = _C()
    for ids in (node_concepts or {}).values():
        # Touch is a distinct-node statistic, not a tag-occurrence statistic. The endpoint
        # applies consolidation before this function; normalizing and deduplicating per node here prevents
        # an alias+canonical pair (or case/whitespace variants) from inflating hub orientation.
        seen = {k for k in (_normalize_concept_id(cid) for cid in (ids or ())) if k}
        c.update(seen)
    return dict(sorted(c.items()))


# --------------------------------------------------------------------------- #
# The GLOBAL concept MAP (PART V Phase 4/5) — one fold, any population.
#
# This used to be `engine/concept_capsules.py::portfolio_concept_graph`, which read run-end concept
# CAPSULES and canonicalized them itself. That made the population part of the function, and the
# population is exactly what a second consumer disagrees about: the browser's `Concepts` view folds
# the LIVE per-run `concepts` rollup of the runs the list is showing, and on the real corpus here the
# two populations were 15 tagged runs versus 3 capsules. Two maps of one lab, neither wrong about its
# own corpus, is how a view silently reports a different population than the surface around it.
#
# So the fold takes per-run concept SETS and nothing else. The CALLER chooses and scopes the
# population, and the caller canonicalizes (the governance alias/split registry belongs to whoever
# owns the population's provenance, not to a projection). `ui/src/conceptForest.js` is the browser
# half and mirrors these field names, exactly as it already mirrors `project_hierarchy` — one
# vocabulary, two runtimes, no second rule.
# --------------------------------------------------------------------------- #

MAX_MAP_CONCEPTS = 512

MAX_MAP_PAIRS = 2_048

MAX_MAP_RUN_CONCEPTS = 256      # cap ONE run's set before the O(k^2) pairing


def project_concept_map(run_concepts, *, min_cooccurrence: int = 2) -> dict:
    """The GLOBAL cross-run concept MAP over an ALREADY-SCOPED population.

    The 'мега общая карта концептов': one shared taxonomy view across every run the caller passed.

    `run_concepts` is an iterable of per-RUN concept-id iterables: one element per run, each the ids that
    run is evidence for. Pure/deterministic, no I/O, no governance lookup, ADVISORY — a read-model, never
    a selection input. Ids cross `core.concepts.normalize_concept_id`, the same bounded identity contract
    the run-list rollup and `project_hierarchy` use, so the Python map and the browser forest join.

    The SPINE is `project_hierarchy` verbatim: the parent of `a/b/c` is `a/b`, every ancestor prefix is
    materialized so `loss` groups even when only `loss/contrastive/dcl` was tagged, and `tagged` marks
    the ids a run actually named. There is deliberately no `is_a` EDGE list any more — the tree IS the
    is_a relation, and a second spelling of it is a second thing to keep in sync.

    The one relation that is not derivable from an id is `co_occurs`: an UNORDERED concept PAIR that
    appeared TOGETHER in one run, weighted by the number of DISTINCT runs the pair co-occurred in.
    `min_cooccurrence` (default 2 runs) is a FLOOR, so a single-run coincidence is not an edge — one run
    tagging `a` and `b` says the tagger emitted two labels, not that the lab pairs them.

    Everything is bounded, and every bound is REPORTED. Node omissions are exact for the whole scoped
    population; pair omissions are exact only INSIDE the retained node projection, and pairs touching a
    pruned node stay explicitly UNKNOWN (their exact distinct-pair count would restore an unbounded
    O(N^2)) — never counted as zero.
    """
    per_run: list[set[str]] = []
    concept_runs: dict[str, int] = {}
    for concepts in (run_concepts or ()):
        canon = set()
        # Cap the run's own set BEFORE pairing, and take the lexical head so the retained subset is the
        # same on every process (a raw-set slice follows PYTHONHASHSEED and would make the map differ
        # run to run for one corpus).
        for cid in sorted({c for c in (_normalize_concept_id(x) for x in (concepts or ())) if c}):
            if len(canon) >= MAX_MAP_RUN_CONCEPTS:
                break
            canon.add(cid)
        per_run.append(canon)
        for cid in canon:
            concept_runs[cid] = concept_runs.get(cid, 0) + 1

    explored = len(concept_runs)
    hierarchy = project_hierarchy(concept_runs.keys())
    for cid in hierarchy["nodes"]:
        concept_runs.setdefault(cid, 0)          # a materialized ancestor nobody tagged: structure, 0 runs
    ranked = sorted(concept_runs, key=lambda cid: (-concept_runs[cid], cid))
    kept = set(ranked[:MAX_MAP_CONCEPTS])
    pruned_nodes = len(ranked) - len(kept)

    # Select the bounded node set BEFORE the O(k^2) pairing: retaining pair sets for every source
    # concept when at most `MAX_MAP_CONCEPTS` of them can reach the response is the cost this ordering
    # exists to avoid.
    pair_runs: dict[tuple[str, str], int] = {}
    for canon in per_run:
        inside = sorted(canon & kept)
        for i, a in enumerate(inside):           # unordered sorted pair; one count per distinct run
            for b in inside[i + 1:]:
                pair_runs[(a, b)] = pair_runs.get((a, b), 0) + 1
    try:
        threshold = max(1, int(min_cooccurrence))     # a per-pair run-count floor; <=0 means "keep all"
    except (TypeError, ValueError):
        threshold = 2                            # a contract-violating caller falls back to the default
    above = sorted(((a, b, n) for (a, b), n in pair_runs.items() if n >= threshold),
                   key=lambda t: (-t[2], t[0], t[1]))
    pairs = [{"a": a, "b": b, "n_runs": n} for a, b, n in above[:MAX_MAP_PAIRS]]

    # Bounding is not the only way a pair can be missing: a pruned node hides every pair incident to it
    # even when the retained pairs fit under their own cap. Keep those two facts separate.
    complete = pruned_nodes == 0
    return {
        "n_runs": len(per_run),
        "n_concepts": len(ranked),
        "n_explored_concepts": explored,
        "concepts": [{"concept": cid, "n_runs": concept_runs[cid]} for cid in ranked[:MAX_MAP_CONCEPTS]],
        "concepts_omitted": pruned_nodes,
        "roots": [cid for cid in hierarchy["roots"] if cid in kept],
        "nodes": {cid: {**node, "n_runs": concept_runs[cid],
                        "children": [c for c in node["children"] if c in kept]}
                  for cid, node in hierarchy["nodes"].items() if cid in kept},
        "min_cooccurrence": threshold,
        "pairs": pairs,
        "pair_candidates": len(pair_runs),
        # Exact only within `pair_source_scope`: response-cap omissions, not unknown out-of-projection pairs.
        "pairs_omitted": max(0, len(above) - len(pairs)),
        "pair_source_scope": "scoped_population" if complete else "retained_node_projection",
        "pair_source_complete": complete,
        "pair_source_nodes_included": len(kept),
        "pair_source_nodes_pruned": pruned_nodes,
        "pairs_outside_projection": 0 if complete else None,
        "pairs_outside_projection_unknown": not complete,
    }


def default_lenses() -> list[dict]:
    """The shipped lens pack — each a pure PROJECTION spec (no data of its own). `is_a` nests by concept
    path; the rest nest by a stored typed-edge relation. Order = display order; `is_a` is the default."""
    return [
        {"name": "is_a", "label": "Family · is-a", "rels": ["is_a"], "kind": "path"},
        {"name": "uses", "label": "Usage · uses", "rels": ["uses"], "kind": "edge"},
        {"name": "part_of", "label": "Composition · part-of", "rels": ["part_of"], "kind": "edge"},
        {"name": "co_occurs", "label": "Empirical · co-occurs", "rels": ["co_occurs"], "kind": "edge"},
    ]


_SYMMETRIC_RELS = frozenset({"co_occurs", "related_to"})


def _lens_rels(lens) -> Optional[set]:
    # The STRING "is_a" must filter to the is_a relation, NOT return None: a None here means "no rel
    # filter", so `project_lens(ids, edges, "is_a")` would build a tree from EVERY stored relation
    # (is_a + uses + co_occurs mixed, co_occurs even flipped to touch-orientation) — never a meaningful
    # hierarchy, and disagreeing with the equivalent dict form ({"rels": ["is_a"]}) that default_lenses()
    # emits. Phase 2c asserts real is_a edges, so map the string to {"is_a"} for a consistent filter.
    # (The concepts endpoint routes is_a to project_hierarchy, so this is the defensive path for any
    # direct project_lens("is_a") caller.)
    if isinstance(lens, str):
        return {lens}
    rels = lens.get("rels") if isinstance(lens, dict) else None
    return set(rels) if rels else None


def project_lens(concept_ids, edges, lens="co_occurs", *, touch=None) -> dict:
    """Project a hierarchy from the TYPED concept-edge set under a non-`is_a` lens — the multi-lens
    payoff of the edge substrate. `edges` is `RunState.concept_edges` ({key: {src, rel, dst, confidence}}).
    A DIRECTED rel (uses / part_of) reads (src, rel, dst) as "src's parent is dst"; a SYMMETRIC rel
    (co_occurs / related_to) is oriented by `touch` (parent = the higher-touch endpoint; ties → the
    smaller id), so the most-used concept becomes the hub. Each concept gets ONE primary parent (highest
    confidence, then id); the rest are kept as `cross_parents`. A greedy, DETERMINISTIC spanning
    arborescence with cycle-avoidance (a candidate parent that would close a cycle is skipped) — pure +
    replay-safe. Returns {"lens", "roots", "nodes": {id: {parent, depth, children, tagged,
    cross_parents}}}."""
    from collections import deque as _deque
    tagged = {c for c in (_normalize_concept_id(x) for x in (concept_ids or ())) if c}
    lens_name = lens if isinstance(lens, str) else str((lens or {}).get("name") or "custom")
    rels = _lens_rels(lens)
    touch = touch or {}
    cand: dict[str, list] = {}                     # child -> [(confidence, parent)]
    all_ids: set[str] = set(tagged)
    for e in (edges or {}).values():
        if not isinstance(e, dict):
            continue
        rel = e.get("rel")
        if rels is not None and rel not in rels:
            continue
        src, dst = _normalize_concept_id(e.get("src")), _normalize_concept_id(e.get("dst"))
        if not src or not dst or src == dst:
            continue
        conf = float(e.get("confidence") or 0.0)
        all_ids.add(src)
        all_ids.add(dst)
        if rel in _SYMMETRIC_RELS:
            ts, td = touch.get(src, 0), touch.get(dst, 0)
            parent = (src if ts > td else dst if td > ts else min(src, dst))   # higher touch; tie→min id
            child = dst if parent == src else src
        else:
            parent, child = dst, src                # src <rel> dst  =>  dst is the parent
        cand.setdefault(child, []).append((conf, parent))
    parent_of: dict[str, str] = {}

    def _would_cycle(child, parent):
        x, seen = parent, set()
        while x is not None and x not in seen:
            if x == child:
                return True
            seen.add(x)
            x = parent_of.get(x)
        return False
    for cid in sorted(all_ids):                    # deterministic assignment order
        for _conf, p in sorted(cand.get(cid, []), key=lambda t: (-t[0], t[1])):
            if p in all_ids and not _would_cycle(cid, p):
                parent_of[cid] = p
                break
    children: dict[Optional[str], list] = {}
    for cid in sorted(all_ids):
        children.setdefault(parent_of.get(cid), []).append(cid)
    roots = sorted(children.get(None, []))
    depth: dict[str, int] = {}
    dq = _deque((r, 0) for r in roots)
    while dq:
        cid, d = dq.popleft()
        if cid in depth:
            continue
        depth[cid] = d
        for ch in sorted(children.get(cid, [])):
            dq.append((ch, d + 1))
    nodes = {}
    # Iterate SORTED ids (not the raw set): a raw-set iteration makes the `nodes` dict's key order
    # string-hash-dependent (randomized per process via PYTHONHASHSEED), so json.dumps of this
    # projection is not byte-stable across processes despite the "DETERMINISTIC" docstring — the concepts
    # endpoint returns this tree, so byte-instability breaks its HTTP etag/caching and diff-based tests.
    # (Matches the sibling project_hierarchy fix.)
    for cid in sorted(all_ids):
        prim = parent_of.get(cid)
        cross = sorted({p for _, p in cand.get(cid, []) if p != prim and p in all_ids})
        nodes[cid] = {"parent": prim, "depth": depth.get(cid, 0),
                      "children": sorted(children.get(cid, [])),
                      "tagged": cid in tagged, "cross_parents": cross}
    return {"lens": lens_name, "roots": roots, "nodes": nodes}


def derive_lens(prompt, edges, client, *, concepts=None, parser: str = "tool_call",
                raise_on_failure: bool = False) -> Optional[dict]:
    """Agentically MINT a lens in the moment from a natural-language request — the "create a lens" tool.
    A lens is a pure PROJECTION spec (a relation-subset + labels); it writes NO events
    and grows NO edges, so it is entirely view-state and REPLAY-CLEAN. The model picks which of the
    AVAILABLE relation types (those present in `edges`, plus is_a) count as nesting. Best-effort:
    returns None on no client / empty prompt / any failure / an empty-or-invalid choice, so the caller
    falls back to a default lens. Impure (one LLM call) by design; the returned spec is consumed by
    project_lens / project_hierarchy. Root focus is intentionally absent until projection implements
    subtree filtering; advertising and persisting a no-op root would be a false product promise."""
    if client is None or not str(prompt or "").strip():
        return None
    try:
        from pydantic import BaseModel, Field

        from looplab.core.parse import parse_structured

        avail = sorted({str(e.get("rel")) for e in (edges or {}).values()
                        if isinstance(e, dict) and e.get("rel")} | {"is_a"})
        vocab = sorted({v for e in (edges or {}).values() if isinstance(e, dict)
                        for v in (_normalize_concept_id(e.get("src")), _normalize_concept_id(e.get("dst")))
                        if v} | {c for c in (_normalize_concept_id(x) for x in (concepts or [])) if c})

        class _LensOut(BaseModel):
            name: str = ""
            label: str = ""
            rels: list[str] = Field(default_factory=list)

        system = (
            "You turn a user's request into a LENS over a concept graph — a way to VIEW its hierarchy. A "
            "lens picks which RELATION TYPES count as nesting (the tree follows edges of those types). "
            "Choose ONLY from the AVAILABLE relations below. Give a short lower-case `name` slug and a "
            "human `label`. Call `emit` once.\n\nAVAILABLE RELATIONS: " + ", ".join(avail)
            + ("\n\nCONCEPT VOCABULARY (sample):\n" + "\n".join(f"- {c}" for c in vocab[:60])
               if vocab else ""))
        out = parse_structured(client, [{"role": "system", "content": system},
                                        {"role": "user", "content": str(prompt).strip()[:800]}],
                               _LensOut, parser)
        rels = [r for r in (out.rels or []) if r in set(avail)]
        if not rels:
            return None
        name = _normalize_concept_id(out.name) or "-".join(rels)[:24] or "lens"
        spec = {"name": name, "label": (out.label or name).strip()[:60], "rels": rels,
                "kind": "path" if rels == ["is_a"] else "edge", "provenance": "agent"}
        return spec
    except Exception:
        if raise_on_failure:
            raise
        # Non-HTTP callers remain deliberately best-effort. The paid endpoint opts into the strict
        # path so a transport failure can never be reported as an authoritative model decline.
        return None
