"""Run-introspection tools + the richer Researcher digest (context engineering).
Offline — synthetic RunState + a fake chat client, no model needed."""
from __future__ import annotations

import json
import math

from looplab.events import digest
from looplab.agents.agent import ToolUsingResearcher
from looplab.core.config import Settings
from looplab.core.models import (CONCEPT_DELTA_DEPENDENCY_CYCLE_REASON,
                                 NODE_CONCEPT_PROVENANCE_CLASSIFIER, Idea, Node, NodeStatus,
                                 RunState, Trial)
from looplab.agents.roles import LLMResearcher
from looplab.tools.run_tools import DataTools, RunTools
from looplab.adapters.tasks import make_roles
from looplab.adapters.toytask import ToyTask


def _st() -> RunState:
    st = RunState(goal="minimize loss", direction="min")
    st.nodes = {
        0: Node(id=0, operator="draft", code="print(0)",
                idea=Idea(operator="draft", params={"x": 0.0, "y": 0.0}, theme="seed"),
                metric=10.0, status=NodeStatus.evaluated),
        1: Node(id=1, parent_ids=[0], operator="improve", code="print(1)",
                idea=Idea(operator="improve", params={"x": 2.0, "y": 1.0}, theme="hpo",
                          rationale="move toward the optimum"),
                metric=4.0, status=NodeStatus.evaluated),
        2: Node(id=2, parent_ids=[1], operator="improve",
                idea=Idea(operator="improve", params={"x": 3.0, "y": -1.0}, theme="hpo"),
                metric=1.0, status=NodeStatus.evaluated),
        3: Node(id=3, operator="draft",
                idea=Idea(operator="draft", params={"x": 9.0, "y": 9.0}, theme="seed"),
                status=NodeStatus.failed, error_reason="crash", error="boom"),
    }
    st.best_node_id = 2
    return st


def _mark_concepts_exact(st: RunState) -> None:
    """Manual RunState fixtures must model replay's explicit inheritance provenance sidecar."""
    if isinstance(st.node_concepts, dict):
        st.node_concept_provenance = {
            node_id: NODE_CONCEPT_PROVENANCE_CLASSIFIER for node_id in st.node_concepts}


# --------------------------------------------------------------------------- digest
def test_param_distance_matches_old_ndist():
    def _ndist(a, b):
        keys = set(a) & set(b)
        if not keys:
            return float("inf")
        return math.sqrt(sum((a[k] - b[k]) ** 2 for k in keys)) / math.sqrt(len(keys))

    a, b = {"x": 1.0, "y": 2.0}, {"x": 3.0, "y": -1.0}
    assert digest.param_distance(a, b) == _ndist(a, b)
    assert digest.param_distance({"x": 1.0}, {"z": 2.0}) == float("inf")


def test_experiments_digest_content_and_cap():
    st = _st()
    d = digest.experiments_digest(st)
    assert "Strongest" in d and "#2" in d                 # winners listed
    assert "fail" in d.lower() and "crash" in d           # failure surfaced to avoid repeating
    assert "hpo" in d                                     # theme map
    capped = digest.experiments_digest(st, char_cap=40)
    assert len(capped) <= 42                              # hard budget honored
    assert digest.experiments_digest(RunState()) == ""    # empty run → no digest


# --------------------------------------------------------------------- intra-node sweep surfacing
def _sweep_st() -> RunState:
    """A run whose best node is a hyperparameter sweep: 12 finite trials + 1 that diverged."""
    st = RunState(goal="minimize loss", direction="min")
    grid = [(0.05, 0.061), (0.02, 0.22), (0.01, 0.30), (0.1, 0.075), (0.2, 0.12), (0.3, 0.18),
            (0.4, 0.25), (0.5, 0.40), (0.7, 0.50), (1.0, 0.65), (1.5, 0.80), (2.0, 0.90)]
    trials = [Trial(params={"lr": lr}, metric=m, seconds=0.1) for lr, m in grid]
    trials.append(Trial(params={"lr": 3.0}, metric=None, error="diverged: nan loss"))
    st.nodes = {
        5: Node(id=5, operator="improve",
                idea=Idea(operator="improve", params={"warmup": 100.0}, theme="hpo",
                          space={"lr": [lr for lr, _ in grid] + [3.0]}),
                metric=0.061, status=NodeStatus.evaluated, trials=trials),
    }
    st.best_node_id = 5
    return st


def test_select_trials_covers_range_bounded_and_deterministic():
    trials = _sweep_st().nodes[5].trials
    sel = digest.select_trials(trials, 5, "min")
    assert len(sel) == 5
    assert sel[0].metric == 0.061                         # best first (min direction)
    assert sel[-1].metric == 0.90                         # worst kept → range covered
    assert all(t.metric is not None for t in sel)         # the diverged trial is dropped
    assert sel == digest.select_trials(trials, 5, "min")  # deterministic
    allsel = digest.select_trials(trials, 999, "min")     # k >= count → all finite, sorted
    assert len(allsel) == 12 and [t.metric for t in allsel] == sorted(t.metric for t in allsel)
    assert digest.select_trials(trials, 3, "max")[0].metric == 0.90   # max direction flips best


def test_digest_surfaces_sweep_flag_and_tuning_block():
    st = _sweep_st()
    d = digest.experiments_digest(st, char_cap=4000)
    assert "swept ×13" in d                               # node line flags the sweep (12 + 1 nometric)
    assert "Tuning of #5 (13 trials, showing 10 of 12 best→worst)" in d
    assert "→ 0.061" in d                                 # best trial's metric shown
    expected = len(digest.select_trials(st.nodes[5].trials, digest.DEFAULT_TRIAL_K, "min"))
    assert d.count(" → ") == expected <= digest.DEFAULT_TRIAL_K   # bounded representative sample


def test_read_experiment_trial_selection_default_number_and_all():
    rt = RunTools()
    rt.bind_state(_sweep_st())
    default = rt.execute("read_experiment", {"node_id": 5})
    assert "sweep: 13 trials" in default and "best [lr=0.05] metric=0.061" in default
    assert "(+1 no-metric)" in default
    assert default.count(" → ") == digest.DEFAULT_TRIAL_K   # 10-trial sample by default

    three = rt.execute("read_experiment", {"node_id": 5, "trials": "3"})
    assert three.count(" → ") == 3                          # explicit count honored

    allt = rt.execute("read_experiment", {"node_id": 5, "trials": "all"})
    assert allt.count(" → ") == 13                          # every trial incl the no-metric one
    assert "no metric" in allt and "diverged" in allt     # the failed trial is shown, with its error

    bogus = rt.execute("read_experiment", {"node_id": 5, "trials": "lots"})
    assert bogus.count(" → ") == digest.DEFAULT_TRIAL_K     # unparseable → falls back to default


def test_select_trials_k1_and_tool_never_raises_on_edge_selectors():
    """Regression: k==1 must not hit the k-1 divisor (ZeroDivisionError); and the tool must return a
    STRING (never raise) for selectors that clamp to 1 or overflow int()."""
    trials = _sweep_st().nodes[5].trials
    one = digest.select_trials(trials, 1, "min")
    assert len(one) == 1 and one[0].metric == 0.061           # the single best, no crash

    rt = RunTools()
    rt.bind_state(_sweep_st())
    for sel in ("1", "0", "0.4", "-5", "inf", "1e999", "nan"):
        out = rt.execute("read_experiment", {"node_id": 5, "trials": sel})
        assert isinstance(out, str) and "experiment #5" in out  # soft-fails to a string, never raises
    assert rt.execute("read_experiment", {"node_id": 5, "trials": "1"}).count(" → ") == 1


# --------------------------------------------------------------------------- RunTools
def test_concept_read_tools_expose_the_run_vocabulary(tmp_path):
    # Phase 0: the Researcher/Strategist can read THIS run's concept hierarchy + membership on demand,
    # so they reuse existing ids instead of minting near-duplicates. Canonicalized through consolidation.
    st = RunState(goal="g", direction="max")
    st.nodes = {
        0: Node(id=0, operator="draft",
                idea=Idea(operator="draft", params={}, concepts=["loss/contrastive/dcl", "architecture/moe"]),
                metric=0.9, status=NodeStatus.evaluated),
        1: Node(id=1, parent_ids=[0], operator="improve",
                idea=Idea(operator="improve", params={}, concepts=["loss/contrastive/mnr"]),
                metric=0.7, status=NodeStatus.evaluated),
    }
    st.node_concepts = {0: ["loss/contrast/dcl", "architecture/moe"], 1: ["loss/contrastive/mnr"]}
    _mark_concepts_exact(st)
    st.concept_consolidation = {"loss/contrast/dcl": "loss/contrastive/dcl"}   # a live rename
    rt = RunTools()
    rt.bind_state(st)
    names = {f["function"]["name"] for f in rt.specs()}
    assert {"read_concept_tree", "concept_nodes", "node_concepts"} <= names

    tree = rt.execute("read_concept_tree", {})
    assert "loss" in tree and "contrastive" in tree and "moe" in tree
    assert "loss  [2]" in tree                              # subtree count: both nodes under loss
    # membership by concept OR descendant, on the CANONICAL id (rename applied)
    under = rt.execute("concept_nodes", {"concept": "loss/contrastive"})
    assert "#0" in under and "#1" in under
    # querying the RETIRED raw id still finds the node — the query is retargeted through the rename too
    assert "#0" in rt.execute("concept_nodes", {"concept": "loss/contrast/dcl"})
    nc0 = rt.execute("node_concepts", {"node_id": 0})
    assert "loss/contrastive/dcl" in nc0 and "architecture/moe" in nc0
    assert "no experiment #99" in rt.execute("node_concepts", {"node_id": 99})


def test_node_concept_delta_read_model_and_tool():
    # PART V Phase 3 (Layer 2): a node's concepts as a DELTA vs its parent(s) — added / removed / inherited.
    from looplab.search.concept_graph import node_concept_delta
    st = RunState(goal="g", direction="max")
    st.nodes = {
        0: Node(id=0, operator="draft", idea=Idea(operator="draft", params={}), status=NodeStatus.evaluated),
        1: Node(id=1, parent_ids=[0], operator="improve", idea=Idea(operator="improve", params={}),
                status=NodeStatus.evaluated),
        2: Node(id=2, parent_ids=[0, 1], operator="merge", idea=Idea(operator="merge", params={}),
                status=NodeStatus.evaluated),
    }
    st.node_concepts = {0: ["loss/a", "arch/moe"], 1: ["loss/a", "loss/b"], 2: ["loss/b", "data/aug"]}
    _mark_concepts_exact(st)

    # node 1 vs its parent 0: dropped arch/moe, added loss/b, kept loss/a
    d1 = node_concept_delta(st, 1)
    assert d1 == {"parent_ids": [0], "added": ["loss/b"], "removed": ["arch/moe"], "inherited": ["loss/a"]}
    # node 0 is a root -> everything it carries is 'added', nothing inherited
    d0 = node_concept_delta(st, 0)
    assert d0["parent_ids"] == [] and set(d0["added"]) == {"loss/a", "arch/moe"} and d0["inherited"] == []
    # node 2 is a MERGE -> inherits from the UNION of parents {loss/a, arch/moe, loss/b}
    d2 = node_concept_delta(st, 2)
    assert d2["parent_ids"] == [0, 1] and d2["added"] == ["data/aug"] and d2["inherited"] == ["loss/b"]
    assert set(d2["removed"]) == {"arch/moe", "loss/a"}

    rt = RunTools()
    rt.bind_state(st)
    assert "node_concept_delta" in {f["function"]["name"] for f in rt.specs()}
    out = rt.execute("node_concept_delta", {"node_id": 1})
    assert "#1 concept delta vs parent #0" in out
    assert "+added: loss/b" in out and "-removed: arch/moe" in out and "=inherited: loss/a" in out
    assert "root (no parent)" in rt.execute("node_concept_delta", {"node_id": 0})
    assert "no experiment #99" in rt.execute("node_concept_delta", {"node_id": 99})


def test_node_concept_delta_never_raises_on_non_dict_stores():
    # REVIEW: a truthy non-dict concept_consolidation/node_concepts must soft-fail (empty), not raise
    # AttributeError out of the LLM-invocable read-model/tool — matching every sibling concept read-path.
    from looplab.search.concept_graph import node_concept_delta
    st = RunState(goal="g", direction="max")
    st.nodes = {0: Node(id=0, operator="draft", idea=Idea(operator="draft", params={}),
                        status=NodeStatus.evaluated)}
    st.node_concepts = {0: ["loss/a"]}
    _mark_concepts_exact(st)
    st.concept_consolidation = ["loss/a"]                        # a list, not a dict
    # A malformed consolidation map is an unavailable identity projection: fail closed, do not silently
    # reinterpret it as "no renames". The tool must still return its stable empty shape rather than raise.
    assert node_concept_delta(st, 0)["added"] == []
    st.node_concepts = "loss/a"                                  # a str, not a dict
    d = node_concept_delta(st, 0)
    assert isinstance(d, dict) and d["added"] == [] and d["removed"] == []
    rt = RunTools()
    rt.bind_state(st)
    assert isinstance(rt.execute("node_concept_delta", {"node_id": 0}), str)   # never raises out of execute


def test_node_concept_delta_applies_consolidation_rename_on_both_sides():
    from looplab.search.concept_graph import node_concept_delta
    st = RunState(goal="g", direction="max")
    st.nodes = {
        0: Node(id=0, operator="draft", idea=Idea(operator="draft", params={}), status=NodeStatus.evaluated),
        1: Node(id=1, parent_ids=[0], operator="improve", idea=Idea(operator="improve", params={}),
                status=NodeStatus.evaluated),
    }
    # parent tagged with a RETIRED raw id; child with its canonical -> after rename they are the SAME concept
    # (inherited), not a spurious add/remove.
    st.node_concepts = {0: ["loss/contrast/dcl"], 1: ["loss/contrastive/dcl", "loss/new"]}
    _mark_concepts_exact(st)
    st.concept_consolidation = {"loss/contrast/dcl": "loss/contrastive/dcl"}
    d = node_concept_delta(st, 1)
    assert d["inherited"] == ["loss/contrastive/dcl"] and d["added"] == ["loss/new"] and d["removed"] == []


def test_node_concept_delta_distinguishes_pending_classification_from_empty_tags():
    from looplab.search.concept_graph import node_concept_delta

    st = RunState(goal="g", direction="max")
    st.nodes = {
        0: Node(id=0, operator="draft", idea=Idea(operator="draft", params={}),
                status=NodeStatus.evaluated),
        1: Node(id=1, parent_ids=[0], operator="improve", idea=Idea(operator="improve", params={}),
                status=NodeStatus.evaluated),
    }
    st.node_concepts = {0: ["loss/a"]}
    _mark_concepts_exact(st)

    pending = node_concept_delta(st, 1)
    assert pending == {"parent_ids": [0], "added": [], "removed": [], "inherited": [],
                       "unavailable": True, "reasons": ["membership_not_recorded"],
                       "untagged": True}
    tools = RunTools()
    tools.bind_state(st)
    rendered = tools.execute("node_concept_delta", {"node_id": 1})
    assert "classification pending" in rendered and "-removed" not in rendered
    assert "PARTIAL" in tools.execute("read_concept_tree", {})

    # An explicit empty classifier receipt is materially different: it says the parent tag is absent.
    st.node_concepts[1] = []
    _mark_concepts_exact(st)
    classified_empty = node_concept_delta(st, 1)
    assert classified_empty["removed"] == ["loss/a"]
    assert "untagged" not in classified_empty


def test_benign_consolidation_merge_is_not_flagged_as_corruption():
    # Two raw spellings of the SAME source ('reg dropout' and 'reg-dropout') both canonicalize to one id.
    # That is a legitimate merge, NOT identity corruption: it must not poison global_reasons (which would
    # make every node_concept_delta unavailable and delta_safe False run-wide).
    from looplab.search.concept_projection import current_concept_projection

    st = RunState(goal="g", direction="max", run_base_concepts=["reg/dropout"])
    st.nodes = {0: Node(id=0, operator="draft", idea=Idea(operator="draft"),
                        status=NodeStatus.evaluated)}
    st.node_concepts = {0: ["reg/dropout"]}
    _mark_concepts_exact(st)
    st.concept_consolidation = {"reg dropout": "reg/dropout", "reg-dropout": "reg/dropout"}

    projection = current_concept_projection(st)
    assert "invalid_consolidation_map" not in projection.global_reasons
    assert projection.node_status(0) == ("complete", ())
    # A genuinely malformed target still fails closed.
    st.concept_consolidation = {"reg/dropout": "!!!"}
    assert "invalid_consolidation_map" in current_concept_projection(st).global_reasons


def test_all_pending_run_is_partial_not_unavailable():
    # A fresh run whose only issue is that memberships have not been recorded yet is PENDING, not corrupt.
    # The aggregate status must be "partial" (data still arriving), never "unavailable" (broken store).
    from looplab.search.concept_projection import current_concept_projection

    st = RunState(goal="g", direction="max")
    st.nodes = {0: Node(id=0, operator="draft", idea=Idea(operator="draft"),
                        status=NodeStatus.evaluated)}
    st.node_concepts = {}                                   # nothing classified yet -> 0 available nodes

    projection = current_concept_projection(st)
    assert projection.available_nodes == frozenset()
    assert projection.reasons == ("membership_not_recorded",)
    assert projection.status == "partial"                  # NOT "unavailable"
    # A real corruption reason with nothing usable still collapses to unavailable.
    st.node_concept_materialization_receipts = "not-a-dict"
    assert current_concept_projection(st).status == "unavailable"


def test_concept_tools_share_strict_receipt_and_current_lifecycle_projection():
    from looplab.search.concept_projection import (concept_inheritance_context,
                                                    current_concept_projection)

    st = RunState(goal="g", direction="max", run_base_concepts=["base/common"])
    st.nodes = {
        0: Node(id=0, operator="draft", idea=Idea(operator="draft")),
        1: Node(id=1, operator="draft", idea=Idea(operator="draft")),
        2: Node(id=2, operator="draft", idea=Idea(operator="draft"), tombstoned=True),
        3: Node(id=3, operator="draft", idea=Idea(operator="draft")),
        4: Node(id=4, operator="draft", idea=Idea(operator="draft")),
        5: Node(id=5, operator="draft", idea=Idea(operator="draft")),
    }
    st.aborted_nodes = [3]
    st.node_concepts = {
        0: [],
        1: ["safe/current"],
        2: ["secret/tombstoned"],
        3: ["secret/aborted"],
        4: [],
        5: ["<script>/override", "loss/💥"],
    }
    st.node_concept_materialization_receipts = {
        0: {"status": "unavailable", "reasons": [CONCEPT_DELTA_DEPENDENCY_CYCLE_REASON]},
        # Historical receipts follow their historical node out of the CURRENT projection.
        2: {"status": "unavailable", "reasons": [CONCEPT_DELTA_DEPENDENCY_CYCLE_REASON]},
    }
    _mark_concepts_exact(st)

    projection = current_concept_projection(st)
    assert projection.status == "partial"
    assert projection.memberships == {1: ("safe/current",), 4: (), 5: ()}
    assert projection.node_status(0) == (
        "unavailable", (CONCEPT_DELTA_DEPENDENCY_CYCLE_REASON,))
    assert concept_inheritance_context(st, 1)["delta_safe"] is True

    tools = RunTools()
    tools.bind_state(st)
    tree = tools.execute("read_concept_tree", {})
    assert "PARTIAL" in tree and "safe" in tree
    assert "script" not in tree and "tombstoned" not in tree and "aborted" not in tree
    assert "UNAVAILABLE" in tools.execute("node_concepts", {"node_id": 0})
    assert "known-empty" in tools.execute("node_concepts", {"node_id": 0})
    assert "safe/current" in tools.execute("node_concepts", {"node_id": 1})
    assert "PARTIAL" in tools.execute("node_concepts", {"node_id": 5})
    assert "#4: (no concepts tagged)" == tools.execute("node_concepts", {"node_id": 4})
    assert "#1" in tools.execute("concept_nodes", {"concept": "safe/current"})
    assert "#2" not in tools.execute("concept_nodes", {"concept": "secret/tombstoned"})
    assert "UNAVAILABLE" in tools.execute("node_concept_delta", {"node_id": 0})
    delta_spec = next(spec["function"]["description"] for spec in tools.specs()
                      if spec["function"]["name"] == "node_concept_delta")
    assert "full-mode root" in delta_spec and "delta-mode root" in delta_spec


def test_run_base_materialization_receipt_blocks_delta_even_with_valid_subset():
    from types import SimpleNamespace

    from looplab.search.concept_projection import concept_inheritance_context

    state = SimpleNamespace(
        nodes={}, node_concepts={}, run_base_concepts=["base/reliable-subset"],
        run_base_concept_receipt={
            "status": "partial", "reasons": ["concepts_per_node_cap"]},
    )

    context = concept_inheritance_context(state, None)

    assert context["run_base"] == ["base/reliable-subset"]
    assert context["run_base_status"] == "partial"
    assert context["run_base_reasons"] == ["concepts_per_node_cap"]
    assert context["delta_safe"] is False


def test_missing_or_untrusted_membership_provenance_is_partial_and_forbids_delta():
    from looplab.core.models import NODE_CONCEPT_PROVENANCE_UNTRUSTED
    from looplab.search.concept_projection import concept_inheritance_context, current_concept_projection

    state = RunState(goal="g", direction="max", run_base_concepts=["base/x"])
    state.nodes = {0: Node(id=0, operator="draft", idea=Idea(operator="draft"))}
    state.node_concepts = {0: ["safe/parent"]}

    for provenance in ({}, {0: NODE_CONCEPT_PROVENANCE_UNTRUSTED}):
        state.node_concept_provenance = provenance
        projection = current_concept_projection(state)
        assert projection.trusted_memberships == {}
        assert projection.node_status(0) == (
            "partial", ("delta_dependency_unknown_parent_membership",))
        assert concept_inheritance_context(state, 0)["delta_safe"] is False

    tools = RunTools()
    tools.bind_state(state)
    rendered = tools.execute("node_concepts", {"node_id": 0})
    assert "PARTIAL" in rendered and "safe/parent" in rendered


def test_missing_base_and_partial_receipts_reach_all_concept_tools():
    from looplab.search.concept_graph import node_concept_delta

    st = RunState(goal="g", direction="max")
    st.nodes = {
        0: Node(id=0, operator="draft", idea=Idea(operator="draft", params={}),
                status=NodeStatus.evaluated),
        1: Node(id=1, parent_ids=[2], operator="improve",
                idea=Idea(operator="improve", params={}), status=NodeStatus.evaluated),
        2: Node(id=2, operator="draft", idea=Idea(operator="draft", params={}),
                status=NodeStatus.evaluated),
    }
    st.node_concepts = {0: [], 1: ["known/base", "known/retained"], 2: ["known/base"]}
    _mark_concepts_exact(st)
    st.node_concept_materialization_receipts = {
        0: {"status": "unavailable", "reasons": ["delta_dependency_missing_run_base"]},
        1: {"status": "partial", "reasons": ["concepts_per_node_cap"]},
    }

    unavailable = node_concept_delta(st, 0)
    assert unavailable["unavailable"] is True
    assert unavailable["reasons"] == ["delta_dependency_missing_run_base"]
    assert unavailable["added"] == unavailable["removed"] == unavailable["inherited"] == []
    partial = node_concept_delta(st, 1)
    assert partial["partial"] is True and partial["reasons"] == ["concepts_per_node_cap"]
    assert partial["added"] == ["known/retained"] and partial["inherited"] == ["known/base"]
    assert partial["removed"] == [] and partial["unknown_dimensions"] == ["removed"]

    tools = RunTools()
    tools.bind_state(st)
    node = tools.execute("node_concepts", {"node_id": 0})
    delta = tools.execute("node_concept_delta", {"node_id": 0})
    assert "UNAVAILABLE" in node and "known-empty" in node
    assert "UNAVAILABLE" in delta and "no empty delta inferred" in delta
    retained = tools.execute("node_concepts", {"node_id": 1})
    retained_delta = tools.execute("node_concept_delta", {"node_id": 1})
    assert "PARTIAL" in retained and "known/retained" in retained
    assert "PARTIAL" in retained_delta and "+added: known/retained" in retained_delta
    assert "?removed: unknown" in retained_delta and "-removed" not in retained_delta

    tree = tools.execute("read_concept_tree", {})
    absent = tools.execute("concept_nodes", {"concept": "unknown/missing"})
    assert "PARTIAL" in tree and "available strict subset" in tree
    assert "NOT a complete zero" in absent
    assert "delta_dependency_missing_run_base" in tree and "concepts_per_node_cap" in tree


def test_deleted_missing_base_receipt_does_not_poison_current_concept_tools():
    st = RunState(goal="g", direction="max")
    st.nodes = {
        0: Node(id=0, operator="draft", idea=Idea(operator="draft", params={}),
                status=NodeStatus.evaluated, tombstoned=True),
        1: Node(id=1, operator="draft", idea=Idea(operator="draft", params={}),
                status=NodeStatus.evaluated),
    }
    st.node_concepts = {0: [], 1: []}
    _mark_concepts_exact(st)
    st.node_concept_materialization_receipts = {
        0: {"status": "unavailable", "reasons": ["delta_dependency_missing_run_base"]}}
    tools = RunTools()
    tools.bind_state(st)

    tree = tools.execute("read_concept_tree", {})
    nodes = tools.execute("concept_nodes", {"concept": "known/x"})
    assert "no concepts tagged yet" in tree and "UNAVAILABLE" not in tree
    assert "no experiments tagged" in nodes and "complete zero" not in nodes


def test_partial_child_delta_never_infers_removal_from_an_omitted_membership():
    from looplab.search.concept_graph import node_concept_delta

    st = RunState(goal="g", direction="max")
    st.nodes = {
        0: Node(id=0, operator="draft", idea=Idea(operator="draft")),
        1: Node(id=1, parent_ids=[0], operator="improve", idea=Idea(operator="improve")),
    }
    st.node_concepts = {
        0: ["base/a", "maybe/still-inherited"],
        1: ["base/a", "new/retained"],
    }
    _mark_concepts_exact(st)
    st.node_concept_materialization_receipts = {
        1: {"status": "partial", "reasons": ["concepts_per_node_cap"]}}

    delta = node_concept_delta(st, 1)

    assert delta["added"] == ["new/retained"]
    assert delta["inherited"] == ["base/a"]
    assert delta["removed"] == []
    assert delta["unknown_dimensions"] == ["removed"]
    tools = RunTools()
    tools.bind_state(st)
    rendered = tools.execute("node_concept_delta", {"node_id": 1})
    assert "+added: new/retained" in rendered and "=inherited: base/a" in rendered
    assert "?removed: unknown" in rendered and "-removed: maybe/still-inherited" not in rendered


def test_unknown_parent_reference_is_unavailable_instead_of_becoming_a_root():
    from looplab.search.concept_graph import node_concept_delta

    st = RunState(goal="g", direction="max")
    st.nodes = {1: Node(id=1, parent_ids=[99], operator="improve", idea=Idea(operator="improve"))}
    st.node_concepts = {1: ["loss/a"]}
    _mark_concepts_exact(st)

    delta = node_concept_delta(st, 1)

    assert delta == {
        "parent_ids": [99],
        "added": [],
        "removed": [],
        "inherited": [],
        "unavailable": True,
        "reasons": ["delta_dependency_missing_parent"],
    }
    tools = RunTools()
    tools.bind_state(st)
    rendered = tools.execute("node_concept_delta", {"node_id": 1})
    assert "parent #99" in rendered and "UNAVAILABLE" in rendered
    assert "delta_dependency_missing_parent" in rendered and "+added" not in rendered


def test_malformed_historical_receipt_remains_global_integrity_failure():
    from looplab.search.concept_projection import current_concept_projection

    st = RunState(goal="g", direction="max")
    st.nodes = {
        0: Node(id=0, operator="draft", idea=Idea(operator="draft"), tombstoned=True),
        1: Node(id=1, operator="draft", idea=Idea(operator="draft")),
    }
    st.node_concepts = {0: [], 1: []}
    _mark_concepts_exact(st)
    # Valid historical receipts are ignored by the current projection; malformed durable metadata is
    # still source corruption and must agree with ConceptFrame's global validation boundary.
    st.node_concept_materialization_receipts = {
        0: {"status": "unavailable", "reasons": ["delta_dependency_missing_run_base"]}}
    assert current_concept_projection(st).status == "complete"

    st.node_concept_materialization_receipts[0] = {
        "status": "unavailable", "reasons": ["future_invalid_reason"]}
    projection = current_concept_projection(st)
    assert projection.status == "partial"
    assert projection.global_reasons == ("invalid_concept_materialization_receipt",)
    tools = RunTools()
    tools.bind_state(st)
    assert "PARTIAL" in tools.execute("read_concept_tree", {})
    assert "invalid_concept_materialization_receipt" in tools.execute("read_concept_tree", {})


def test_concept_nodes_reports_omitted_experiments():
    st = RunState(goal="g", direction="max")
    st.nodes = {
        nid: Node(id=nid, operator="draft", idea=Idea(operator="draft", params={}),
                  status=NodeStatus.evaluated)
        for nid in range(65)
    }
    st.node_concepts = {nid: ["loss/a"] for nid in st.nodes}
    _mark_concepts_exact(st)
    tools = RunTools()
    tools.bind_state(st)

    rendered = tools.execute("concept_nodes", {"concept": "loss/a"})

    assert rendered.startswith("65 experiment(s)")
    assert "(+5 more experiment(s), not shown)" in rendered
    assert "#59" in rendered and "#60" not in rendered


def test_concept_read_tools_normalize_ids_to_the_frame_vocabulary(tmp_path):
    # REVIEW: the tools must NORMALIZE ids (case/space/slash) the SAME way project_hierarchy + the
    # /concepts frame + the UI do, or the rendered tree's subtree counts collapse to [0] and a query on
    # the DISPLAYED (normalized) id misses. An LLM authoring 'loss/InfoNCE'/'architecture/MoE' triggers it.
    st = RunState(goal="g", direction="max")
    st.nodes = {
        0: Node(id=0, operator="draft", idea=Idea(operator="draft", params={}), status=NodeStatus.evaluated),
        1: Node(id=1, operator="draft", idea=Idea(operator="draft", params={}), status=NodeStatus.evaluated),
    }
    st.node_concepts = {0: ["loss/InfoNCE", "architecture/MoE"], 1: ["loss/InfoNCE"]}
    _mark_concepts_exact(st)
    rt = RunTools()
    rt.bind_state(st)
    tree = rt.execute("read_concept_tree", {})
    assert "loss  [2]" in tree and "infonce  [2]" in tree and "moe  [1]" in tree   # counts join, not [0]
    # the agent queries the DISPLAYED (normalized) id and finds both nodes
    assert "#0" in rt.execute("concept_nodes", {"concept": "loss/infonce"})
    assert "#1" in rt.execute("concept_nodes", {"concept": "loss/infonce"})
    # node_concepts returns normalized ids
    assert "loss/infonce" in rt.execute("node_concepts", {"node_id": 0})


def test_concept_read_tools_follow_full_rename_chain_and_never_raise(tmp_path):
    # REVIEW: a MULTI-hop rename chain must resolve fully (server + UI both do bounded-chain + cycle
    # resolution), and a non-dict consolidation/membership store must soft-fail, never raise out of execute.
    st = RunState(goal="g", direction="max")
    st.nodes = {0: Node(id=0, operator="draft", idea=Idea(operator="draft", params={}),
                        status=NodeStatus.evaluated)}
    st.node_concepts = {0: ["loss/a"]}
    _mark_concepts_exact(st)
    st.concept_consolidation = {"loss/a": "loss/b", "loss/b": "loss/c"}   # 2-hop chain
    rt = RunTools()
    rt.bind_state(st)
    assert "loss/c" in rt.execute("node_concepts", {"node_id": 0})        # resolved to the chain END
    assert "#0" in rt.execute("concept_nodes", {"concept": "loss/c"})     # query the true canonical
    assert "#0" in rt.execute("concept_nodes", {"concept": "loss/a"})     # or any earlier chain id

    # non-dict stores: must return a string, not raise AttributeError (execute doesn't catch it)
    st.concept_consolidation = ["loss/a"]                                 # a list, not a dict
    assert isinstance(rt.execute("read_concept_tree", {}), str)
    assert isinstance(rt.execute("node_concepts", {"node_id": 0}), str)
    st.node_concepts = "loss/a"                                           # a str, not a dict
    assert isinstance(rt.execute("read_concept_tree", {}), str)


def test_run_tools_read_and_rank():
    rt = RunTools()
    rt.bind_state(_st())
    names = {f["function"]["name"] for f in rt.specs()}
    assert {"list_experiments", "read_experiment", "read_code", "find_analogous", "list_themes"} <= names

    best = rt.execute("list_experiments", {"sort": "best", "limit": 2})
    assert best.index("#2") < best.index("#1")            # min: lowest metric first
    assert "#0" in rt.execute("list_experiments", {"sort": "worst", "limit": 1})

    rd = rt.execute("read_experiment", {"node_id": 1})
    assert "operator=improve" in rd and "metric=4" in rd and "optimum" in rd
    assert "print(1)" in rt.execute("read_code", {"node_id": 1})

    ana = rt.execute("find_analogous", {"node_id": 2, "k": 2})
    assert "#1" in ana                                    # node 1 is nearest to node 2
    themes = rt.execute("list_themes", {})
    assert "hpo" in themes and "2 experiment" in themes
    assert "no experiment" in rt.execute("read_experiment", {"node_id": 99}).lower()


def test_list_experiments_theme_filter_matches_folded_concept_axis():
    # PART V Phase 6a: list_themes advertises the folded CONCEPT AXES (node_axes over state.node_concepts,
    # multi-membership), so the list_experiments theme filter, the per-line {label} AND the always-on
    # experiments_digest must ALL use that same canonical vocabulary — one concept set across every surface
    # (no more advertise-vs-filter divergence). The CANONICAL folded set wins over the frozen authoring.
    st = RunState(goal="g", direction="max")
    st.nodes = {
        0: Node(id=0, operator="draft",
                idea=Idea(operator="draft", params={}, concepts=["loss/contrastive"]),
                metric=0.9, status=NodeStatus.evaluated),
        1: Node(id=1, operator="improve",
                idea=Idea(operator="improve", params={}, concepts=["loss/triplet"]),
                metric=0.7, status=NodeStatus.evaluated),
        2: Node(id=2, operator="draft",
                idea=Idea(operator="draft", params={}, theme="legacy-theme",
                          concepts=["data/synth"]),
                metric=0.5, status=NodeStatus.evaluated),
    }
    # Node 0 has been re-tagged by the classifier: its FOLDED concept set is the canonical current one and
    # drives every surface (nodes 1 & 2 have no folded set -> fall back to first authored axis / legacy theme).
    st.node_concepts[0] = ["arch/moe"]
    st.node_concept_provenance[0] = NODE_CONCEPT_PROVENANCE_CLASSIFIER
    rt = RunTools()
    rt.bind_state(st)
    themes = rt.execute("list_themes", {})
    assert "arch" in themes and "loss" in themes and "legacy-theme" in themes   # folded axis + fallbacks
    arch = rt.execute("list_experiments", {"theme": "arch"})
    assert "#0" in arch and "{arch}" in arch                # node 0 filters + labels by its FOLDED axis
    loss = rt.execute("list_experiments", {"theme": "loss"})
    assert "#1" in loss and "#0" not in loss                # node 0 is no longer under its frozen authored axis
    assert "no matching" not in loss.lower()
    assert "{arch}" in digest.experiments_digest(st)        # the always-on digest agrees (canonical label)
    legacy = rt.execute("list_experiments", {"theme": "legacy-theme"})
    assert "#2" in legacy and "{legacy-theme}" in legacy    # pre-concept node still groups by its legacy theme


def test_run_tools_unbound_is_safe():
    assert "unavailable" in RunTools().execute("list_themes", {}).lower()


# --------------------------------------------------------------------------- DataTools
class _FakeTask:
    def columns(self):
        return {"f0": [1.0, 2.0, 3.0], "label": [0.0, 1.0, 0.0]}

    def assets(self):
        return {"train.json": '{"X": [[1, 2]], "y": [0]}'}


def test_data_tools_graceful_and_with_data():
    st = _st()
    bare = DataTools(object())
    bare.bind_state(st)
    assert "no structured schema" in bare.execute("data_schema", {}).lower()
    assert "no data assets" in bare.execute("read_asset", {}).lower()

    dt = DataTools(_FakeTask())
    dt.bind_state(st)
    sch = dt.execute("data_schema", {})
    assert "f0" in sch and "label" in sch
    assert "train.json" in dt.execute("read_asset", {})
    assert "X" in dt.execute("read_asset", {"name": "train.json"})
    assert "no data profile" in dt.execute("data_profile", {}).lower()


# --- schema/profile DERIVED from a tabular asset when the task declares no columns() -----------
class _CsvTask:
    """A task with no columns(), only a raw train.csv asset — like mlebench_real."""
    def assets(self):
        return {"train.csv": "id,height,city,target\n1,1.8,NY,0\n2,1.6,LA,1\n3,,NY,0\n",
                "test.csv": "id,height,city\n9,1.7,LA\n"}


def test_data_schema_inferred_from_csv_when_no_columns():
    dt = DataTools(_CsvTask())
    dt.bind_state(_st())
    sch = dt.execute("data_schema", {})
    assert "inferred from train.csv" in sch          # used the training table, not test.csv
    assert "height (numeric)" in sch                 # numeric column inferred
    assert "city (categorical)" in sch               # categorical column inferred
    assert "target" in sch


def test_data_profile_computed_from_csv_when_unrecorded():
    dt = DataTools(_CsvTask())
    dt.bind_state(_st())                              # _st() has no data_profile -> fall back to CSV
    prof = dt.execute("data_profile", {})
    assert "train.csv" in prof
    assert "height: numeric" in prof and "min=1.6" in prof and "max=1.8" in prof
    assert "missing=0.33" in prof                    # 1 of 3 height values is blank
    assert "city: categorical" in prof and "unique=2" in prof


class _SentinelTask:
    def assets(self):
        return {"train.csv": "x,y\n1.0,NaN\n2.0,inf\n3.0,Infinity\n"}


def test_csv_nan_inf_sentinels_not_numeric():
    """A column of textual NaN/inf sentinels must read as categorical (needs handling), not numeric
    with NaN/inf-poisoned stats."""
    dt = DataTools(_SentinelTask())
    dt.bind_state(_st())
    sch = dt.execute("data_schema", {})
    assert "x (numeric)" in sch and "y (categorical)" in sch
    prof = dt.execute("data_profile", {})
    assert "x: numeric" in prof
    assert "y: categorical" in prof and "nan" not in prof.lower().split("y:")[1]


class _RaggedTask:
    def assets(self):                                # header a,b,c,d ; rows 2-4 truncated before d
        return {"train.csv": "a,b,c,d\n1,2,3,9\n1,2\n1,2\n1,2\n"}


def test_csv_ragged_rows_count_truncated_as_missing():
    dt = DataTools(_RaggedTask())
    dt.bind_state(_st())
    prof = dt.execute("data_profile", {})
    dline = [line for line in prof.splitlines() if line.strip().startswith("d:")][0]
    assert "missing=0.75" in dline                   # d present in only 1 of 4 rows, not 0.00


# --------------------------------------------------------------------------- agent loop
class _FakeChatClient:
    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.turns = []

    def chat(self, messages, tools, tool_choice="auto"):
        self.turns.append(list(messages))
        return self.scripted.pop(0)


def _tool_call(name, args):
    return {"content": "", "tool_calls": [
        {"id": "c1", "function": {"name": name, "arguments": json.dumps(args)}}]}


def test_agent_uses_run_tool_then_emits():
    st = _st()
    client = _FakeChatClient([
        _tool_call("read_experiment", {"node_id": 2}),                 # consult the best node
        _tool_call("emit", {"operator": "improve",
                            "params": {"x": 3.0, "y": -1.0}, "rationale": "refine the leader",
                            "concept_mode": "full"}),
    ])
    r = ToolUsingResearcher(client, RunTools(),
                            bounds={"x": (-10.0, 10.0), "y": (-10.0, 10.0)})
    idea = r.propose(st, st.nodes[2])
    assert idea.operator == "improve"
    # bind_state ran: the tool returned the REAL node-2 detail, fed back as a tool message.
    tool_msgs = [m for m in client.turns[1] if m.get("role") == "tool"]
    assert tool_msgs and "operator=improve" in tool_msgs[0]["content"]


# --------------------------------------------------------------------------- wiring
def test_make_roles_wraps_tool_researcher_by_default():
    researcher, _ = make_roles(ToyTask(), Settings(backend="llm", unified_agent=False))
    assert isinstance(researcher, ToolUsingResearcher)


def test_make_roles_flag_off_is_plain_researcher():
    researcher, _ = make_roles(ToyTask(), Settings(backend="llm", unified_agent=False,
                                                   researcher_tools=False))
    assert isinstance(researcher, LLMResearcher)
