"""The GLOBAL concept MAP fold — `search/concept_lens.py::concept_map`.

These properties used to be pinned against `engine/concept_capsules.py::portfolio_concept_graph`
(`tests/test_cross_run_concepts.py`, removed in the same change). That function chose its own
population — run-end concept capsules — and the run list's `Concepts` view chose a different one,
the live per-run rollup of the runs on screen. On the corpus this repo ships that was 3 capsules
against 15 tagged runs, so the two "global concept maps" would have drawn different labs.

The fold that survives takes per-run concept SETS and nothing else, so a caller can only ever get a
map OF THE POPULATION IT PASSED. That is the property the first test drives, because it is the one
whose absence caused the split, and it is the reason the alias/split canonicalization is NOT in
here: governance belongs to whoever owns the population's provenance.

`ui/src/conceptForest.js` is the browser half and mirrors this vocabulary; `ui/test/conceptForest.test.js`
and `ui/test/conceptCooccurrence.test.js` drive the same rules over the same shapes.
"""
from __future__ import annotations

from looplab.search.concept_lens import (MAX_MAP_CONCEPTS, MAX_MAP_RUN_CONCEPTS, concept_map,
                                         project_hierarchy)


def test_the_map_is_exactly_the_population_it_was_handed():
    # The defect the removal exists to prevent: a map that reaches past the run set it was given.
    # There is no store, no path and no default corpus in the signature, so the only way to widen the
    # population is to pass more runs — which a scoped caller does not do.
    scoped = [{"loss/dcl", "arch/moe"}, {"loss/dcl"}]
    wider = scoped + [{"data/aug", "arch/moe"}]
    assert concept_map(scoped)["n_runs"] == 2
    assert {c["concept"] for c in concept_map(scoped)["concepts"] if c["n_runs"]} == {
        "loss/dcl", "arch/moe"}
    assert "data/aug" not in {c["concept"] for c in concept_map(scoped)["concepts"]}
    assert concept_map(wider)["n_runs"] == 3


def test_nodes_carry_run_counts_over_the_path_spine():
    # PART V Phase 4/5: nodes with DISTINCT-run counts and the is_a path spine, materialized ancestors
    # included. The spine is `project_hierarchy` itself — asserted below — so the map and the per-run
    # concept view cannot drift into two hierarchies.
    runs = [{"loss/contrastive/dcl", "arch/moe"},
            {"loss/contrastive/dcl", "arch/moe"},
            {"loss/contrastive/dcl", "data/aug"}]
    m = concept_map(runs, min_cooccurrence=2)

    counts = {c["concept"]: c["n_runs"] for c in m["concepts"]}
    assert counts["loss/contrastive/dcl"] == 3 and counts["arch/moe"] == 2
    # A materialized ancestor is structure, not evidence: nobody tagged `loss`, so it carries 0 runs
    # rather than inheriting its child's. The tool render filters on exactly this.
    assert counts["loss/contrastive"] == counts["loss"] == 0
    assert m["n_explored_concepts"] == 3 and m["n_concepts"] == len(counts)

    spine = project_hierarchy({"loss/contrastive/dcl", "arch/moe", "data/aug"})
    assert m["roots"] == spine["roots"]
    for cid, node in spine["nodes"].items():
        assert m["nodes"][cid]["parent"] == node["parent"]
        assert m["nodes"][cid]["depth"] == node["depth"]
        assert m["nodes"][cid]["children"] == node["children"]
        assert m["nodes"][cid]["tagged"] == node["tagged"]
    # …and no `is_a` EDGE list beside it. The tree IS that relation; a second spelling is a second
    # thing to keep in sync, and the old graph carried both.
    assert "edges" not in m


def test_cooccurrence_weights_distinct_runs_and_honors_the_floor():
    runs = [{"loss/contrastive/dcl", "arch/moe"},
            {"loss/contrastive/dcl", "arch/moe"},
            {"loss/contrastive/dcl", "data/aug"}]
    m = concept_map(runs, min_cooccurrence=2)
    pairs = {(p["a"], p["b"]): p["n_runs"] for p in m["pairs"]}

    assert pairs == {("arch/moe", "loss/contrastive/dcl"): 2}      # sorted, unordered pair
    # Below the floor: one run emitting two labels is a tagger fact, not a lab pattern.
    assert ("data/aug", "loss/contrastive/dcl") not in pairs
    # …but the candidate is COUNTED, so a caller can say "nothing reached the floor" instead of
    # rendering silence that reads as "nothing co-occurs".
    assert m["pair_candidates"] == 2 and m["min_cooccurrence"] == 2
    assert {(p["a"], p["b"]) for p in concept_map(runs, min_cooccurrence=1)["pairs"]} == {
        ("arch/moe", "loss/contrastive/dcl"), ("data/aug", "loss/contrastive/dcl")}


def test_a_pair_repeated_inside_one_run_is_still_one_run():
    # The weight is DISTINCT RUNS. A run listing a concept twice (two raw spellings that canonicalize
    # to one id is the real way this happens) must not be able to lift a pair over the floor by itself.
    m = concept_map([["a/x", "b/y", "a/x", "b/y"]], min_cooccurrence=2)
    assert m["pairs"] == []
    assert m["pair_candidates"] == 1
    assert {c["concept"]: c["n_runs"] for c in m["concepts"]}["a/x"] == 1


def test_a_contract_violating_floor_falls_back_instead_of_opening_the_map():
    # `min_cooccurrence` reaches this from a tool argument and an HTTP-shaped caller. A non-integer
    # must not evaluate as "no floor", which would publish every single-run coincidence as an edge.
    runs = [{"a/x", "b/y"}]
    assert concept_map(runs, min_cooccurrence="two")["pairs"] == []
    assert concept_map(runs, min_cooccurrence=None)["pairs"] == []
    assert concept_map(runs, min_cooccurrence=0)["min_cooccurrence"] == 1     # <=0 means "keep all"
    assert len(concept_map(runs, min_cooccurrence=0)["pairs"]) == 1


def test_ids_cross_the_shared_identity_contract_so_both_halves_join():
    # The browser folds `normalize_concept_id`-shaped ids out of the run-list rollup. A caller that
    # canonicalized through the governance registry (`normalize_key`) can hand over a spelling that
    # differs only in spacing/case, and both must land on ONE node or the two halves of one map
    # disagree about how many concepts a lab studied.
    m = concept_map([{"Loss/Contrastive"}, {"loss contrastive"}, {"loss/contrastive"}])
    counts = {c["concept"]: c["n_runs"] for c in m["concepts"]}
    assert counts == {"loss": 0, "loss/contrastive": 2, "loss-contrastive": 1}
    # An id no contract can read is DROPPED, never rendered: `n_runs` counts runs that named a
    # readable id, and the empty run below is still a run.
    empty = concept_map([{"!!!"}, set()])
    assert empty["n_runs"] == 2 and empty["concepts"] == [] and empty["n_explored_concepts"] == 0


def test_the_fold_is_deterministic_across_orderings_of_one_corpus():
    runs = [{"loss/a", "loss/b"}, {"loss/b", "arch/c"}, {"loss/a", "loss/b", "arch/c"}]
    assert concept_map(runs) == concept_map(runs)
    # Same runs, different order: the map is a fold over a SET-valued population, and a per-process
    # set iteration order must not reach the payload (the ordering bug `project_hierarchy` records).
    assert concept_map(runs) == concept_map(list(reversed(runs)))


def test_pair_materialization_is_bounded_before_counting_and_says_what_it_lost():
    # Select the bounded node set BEFORE the O(k^2) pairing, and never present the response cap as a
    # count of every pair in the corpus: pairs touching a pruned node were not materialized at all.
    runs = [[f"axis{run:02}/c{index:03}" for index in range(MAX_MAP_RUN_CONCEPTS)]
            for run in range(20)]
    m = concept_map(runs, min_cooccurrence=1)

    assert len(m["concepts"]) == MAX_MAP_CONCEPTS
    assert m["pair_candidates"] <= MAX_MAP_CONCEPTS * (MAX_MAP_CONCEPTS - 1) // 2
    assert m["pair_candidates"] == 2 * (MAX_MAP_RUN_CONCEPTS * (MAX_MAP_RUN_CONCEPTS - 1) // 2)
    assert m["pair_source_scope"] == "retained_node_projection"
    assert m["pair_source_complete"] is False
    assert m["pair_source_nodes_included"] == MAX_MAP_CONCEPTS
    assert m["pair_source_nodes_pruned"] == m["concepts_omitted"] == m["n_concepts"] - MAX_MAP_CONCEPTS
    # UNKNOWN, never zero. Deriving the exact out-of-projection pair count restores the O(N^2) this
    # bound exists to prevent, so the honest answer is that we do not know it.
    assert m["pairs_outside_projection"] is None
    assert m["pairs_outside_projection_unknown"] is True
    # A truncated node set must also truncate the SPINE it publishes, or `roots`/`children` point at
    # nodes the response does not contain.
    assert set(m["roots"]) <= set(m["nodes"])
    for node in m["nodes"].values():
        assert set(node["children"]) <= set(m["nodes"])


def test_a_complete_population_declares_itself_complete():
    m = concept_map([{"a/x", "b/y"}, {"a/x", "b/y"}])
    assert m["pair_source_scope"] == "scoped_population"
    assert m["pair_source_complete"] is True
    assert m["pair_source_nodes_pruned"] == m["pairs_outside_projection"] == 0
    assert m["pairs_outside_projection_unknown"] is False


def test_one_runs_own_set_is_capped_lexically_not_by_hash_order():
    # The per-run cap is what keeps the pairing quadratic in a bound rather than in the corpus. It has
    # to take the SAME subset on every process, or two workers fold one capsule into two maps.
    wide = [f"axis/c{index:04}" for index in range(MAX_MAP_RUN_CONCEPTS + 40)]
    kept = {c["concept"] for c in concept_map([wide], min_cooccurrence=1)["concepts"]
            if c["n_runs"]}
    assert kept == set(sorted(wide)[:MAX_MAP_RUN_CONCEPTS])
    assert concept_map([wide]) == concept_map([list(reversed(wide))])
