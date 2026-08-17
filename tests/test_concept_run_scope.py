"""The RUN-CONSTANT split: which half of a node's concept membership is about the RUN.

Driven, not pinned (CLAUDE.md guard-test ladder, tier 1): every case below FOLDS a real event log —
`tests/fixtures/concept_run_scope_corpus.json` holds the concept-bearing rows of
`runs/rubertlite-dr-unified-v9` and `runs/rubertlite-dr-unified-v8` verbatim — and then reads the
projection, the /concepts frame and the agent tool the way a reader does. A comment carrying the word
`run_constant` satisfies nothing here.

THE MEASUREMENT (2026-08-17, re-derived from `runs/`):

  run                        nodes  tagged  tag slots  run-constant slots   provenance
  rubertlite-dr-unified-v9       8       8         48       40  (83.3 %)   researcher-authored
  rubertlite-dr-unified-v7       8       8         25       16  (64.0 %)   researcher-authored
  rubertlite-dr-unified-v8      16      16        120        0  ( 0.0 %)   classifier
  rubertlite-dr-unified-v6       7       3         23        0  ( 0.0 %)   classifier
  rubertlite-dense-retrieval    81      80        349        0  ( 0.0 %)   classifier

so the defect is not one run's accident and it is not universal either: it is the AUTHORED-tag regime,
where the run base is seeded from node 0's own declaration and every later node inherits it back
through the fold's delta materialization. v8 is the negative control and it is a real one — 16 of 16
nodes tagged, intersection EMPTY — so its rendering must not move by a byte.
"""
import json
from pathlib import Path

import pytest

from looplab.events.eventstore import Event
from looplab.events.replay import fold
from looplab.search.concept_lens import (RUN_CONSTANT_SINGLE_REASON,
                                         RUN_CONSTANT_UNCLASSIFIED_REASON, run_constant_split)

_FIXTURE = Path(__file__).parent / "fixtures" / "concept_run_scope_corpus.json"


def _fold(rows):
    return fold([Event(seq=i, ts=float(i), type=row["type"], data=row["data"])
                 for i, row in enumerate(rows, start=1)])


@pytest.fixture(scope="module")
def corpus():
    return json.loads(_FIXTURE.read_text())


@pytest.fixture(scope="module")
def v9(corpus):
    return _fold(corpus["v9"])


@pytest.fixture(scope="module")
def v8(corpus):
    return _fold(corpus["v8"])


@pytest.fixture(scope="module")
def v7(corpus):
    return _fold(corpus["v7"])


# --------------------------------------------------------------------------- the fixture is REAL

def test_the_fixture_reproduces_v9s_measured_shape(v9):
    """Guard the guard: if this fold stops matching the run the numbers were measured on, every
    assertion below is about a shape nobody observed."""
    assert sorted(v9.nodes) == list(range(8))
    # Five ids the Researcher authored on node 0, which `_maybe_seed_run_base_concepts` then promoted
    # to the run base and every later delta node inherited through `_materialize_concept_deltas`.
    assert v9.run_base_concepts == [
        "loss/contrastive/dcl/nll_cos", "regularization/rdrop/symmetric-kl",
        "training/negative_mining", "data/esci", "eval/recall_at_k"]
    # THE FOLD IS NOT THE DEFECT and this is the assertion that says so: nodes 3-7 carry `concepts: []`
    # on the wire and are NOT empty in state — they authored `concept_mode: "delta"`, and the fold
    # materializes base + delta. Reading the raw event is what makes v9 look like five untagged nodes.
    assert all(v9.node_concepts.get(nid) for nid in range(8)), "no node in v9 is untagged"
    assert sum(len(v9.node_concepts[nid]) for nid in range(8)) == 48


# --------------------------------------------------------------------------- the property

def test_the_run_constant_half_is_derived_and_is_v9s_five(v9):
    split = run_constant_split(v9)
    assert split["coverage"] == "complete"
    assert split["population"] == 8
    assert split["run_constant"] == [
        "data/esci", "eval/recall_at_k", "loss/contrastive/dcl/nll_cos",
        "regularization/rdrop/symmetric-kl", "training/negative_mining"]
    # 40 of 48 slots move to the run; 8 stay on the experiments.
    assert sum(len(ids) for ids in split["distinguishing"].values()) == 8


def test_a_reader_asking_which_experiments_are_about_hard_negatives(v9):
    """The operator's own question. Ground truth from the recorded hypotheses: nodes 0 (scale mining
    `n_negatives` 2->4), 2 (`dcl_threshold=0.05` aggressive selection), 5 (mining ENCODER swap) and 6
    (BM25 lexical negatives) are the run's four hard-negative experiments.

    Before the split the concept lane answered that question two ways and both were useless: match
    `training/negative_mining` and you get all EIGHT nodes (it is in the run base), match the specific
    ids and you get one visible leaf per node buried under five constants. After it, the distinguishing
    lane names 2, 5 and 6, and node 0 is named in `no_distinguishing` — which is the honest answer,
    because node 0's ENTIRE authored membership is the run constant and no recorded tag separates it
    from node 4. That is a stated gap the operator can act on, not a silent one; the classifier pass
    that would close it never fired in this run (docs/BACKLOG.md §0.12)."""
    split = run_constant_split(v9)
    negatives = {nid for nid, ids in split["distinguishing"].items()
                 if any("negative" in cid for cid in ids)}
    assert negatives == {2, 5, 6}
    # The run-level lane still carries the run's own negative-mining fact, so nothing was withheld.
    assert "training/negative_mining" in split["run_constant"]
    # And node 0 is not silently absent: it is in the population the split exists to make visible.
    assert split["no_distinguishing"] == [0, 4]
    assert not split["distinguishing"][0] and not split["distinguishing"][4]


def test_the_split_is_DERIVED_and_not_read_off_the_seeded_run_base(v7):
    """The mutation this case exists for: swap the intersection for `state.run_base_concepts` and every
    v9 assertion above still passes, because on v9 the two COINCIDE — the run base is seeded from node
    0's authored set and `_materialize_concept_deltas` hands it back to every delta node, so the derived
    intersection can never be smaller than it. That self-confirming property is exactly why the base
    cannot be the answer.

    `runs/rubertlite-dr-unified-v7` is the discriminating shape and it is real: EIGHT experiments, all
    tagged `concept_mode: "full"`, and NO `run_concepts` event at all — its base is empty. A split read
    off the base answers nothing there; the derived one answers `loss/contrastive` +
    `regularization/r-drop`, 16 of its 25 tag slots."""
    assert v7.run_base_concepts == [], "the fixture must be the no-run-base shape"
    split = run_constant_split(v7)
    assert split["coverage"] == "complete" and split["population"] == 8
    assert split["run_constant"] == ["loss/contrastive", "regularization/r-drop"]
    assert sum(len(ids) for ids in split["distinguishing"].values()) == 25 - 16
    assert split["no_distinguishing"] == [0, 7]


def test_every_id_survives_the_split(v9):
    """It REORDERS and LABELS; it never withholds. The union of the two halves is the membership."""
    split = run_constant_split(v9)
    constant = set(split["run_constant"])
    for nid, ids in v9.node_concepts.items():
        assert set(ids) == constant | set(split["distinguishing"][nid])


# --------------------------------------------------------------------------- the negative control

def test_a_run_whose_tags_already_distinguish_does_not_move(v8):
    """v8: 16 of 16 nodes classifier-tagged, 120 slots, intersection EMPTY. The split must make no
    claim, and every reader must render exactly what it rendered before."""
    from looplab.search.concept_projection import current_concept_projection
    split = run_constant_split(v8)
    assert split["coverage"] == "complete"
    assert split["run_constant"] == []
    assert split["no_distinguishing"] == []
    # `distinguishing` degenerates to the identity, which is what keeps every reader byte-identical.
    # Compared against the CANONICAL projection, not the raw fold: v8 carries a real
    # `concept_consolidation` rename (`evaluation/recall-at-k` -> `eval/recall_at_k`) and every reader
    # here already reads through it, so comparing with raw `node_concepts` would test the rename.
    memberships = current_concept_projection(v8).memberships
    for nid in v8.nodes:
        assert split["distinguishing"][nid] == sorted(memberships.get(nid, ()))


def test_the_agent_tool_line_is_byte_identical_when_no_claim_is_made(v8, v9):
    """The rendering half of the negative control, through the real tool method."""
    from looplab.search.concept_projection import current_concept_projection
    from looplab.tools.run_tools import RunTools
    tools = RunTools.__new__(RunTools)
    memberships = current_concept_projection(v8).memberships
    for nid in sorted(v8.nodes):
        ids = sorted(memberships.get(nid, ()))
        if not ids:
            continue
        # This IS the pre-change expression, spelled out rather than imported.
        assert (RunTools._node_concepts_tool(tools, v8, nid)
                == f"#{nid} concepts: " + ", ".join(ids))
    # …and on v9 the line now LEADS with the experiment's own concept.
    line = RunTools._node_concepts_tool(tools, v9, 2)
    assert line.startswith("#2 concepts: loss/contrastive/dcl/hard-negative — plus 5 carried by all 8")
    assert "none of its own" in RunTools._node_concepts_tool(tools, v9, 0)
    # Nothing is withheld: every id the membership holds is still in the rendered line.
    for cid in v9.node_concepts[2]:
        assert cid in line


# --------------------------------------------------------------------------- fail-closed coverage

def test_one_unclassified_experiment_refuses_the_claim(corpus):
    """"True of every experiment" is a claim about EVERY experiment. An unclassified node is not
    evidence that a concept is universal — it is evidence that nobody classified it — so the split
    refuses and names the nodes. Measured: this is why `rubertlite-dr-unified-v6` (4 of 7) and
    `rubertlite-dense-retrieval` (1 of 81) render unchanged."""
    rows = [row for row in corpus["v9"]]
    rows.append({"type": "node_created",
                 "data": {"node_id": 8, "parent_ids": [], "operator": "draft",
                          "idea": {"operator": "draft"}}})
    state = _fold(rows)
    split = run_constant_split(state)
    assert split["coverage"] == "partial"
    assert split["run_constant"] == []
    assert split["unclassified"] == [8]
    assert RUN_CONSTANT_UNCLASSIFIED_REASON in split["reasons"]
    # …and the untouched nodes still render their full membership.
    assert split["distinguishing"][2] == sorted(set(state.node_concepts[2]))


def test_a_single_experiment_run_makes_no_claim(corpus):
    """With one node every concept is trivially constant and the split would empty the only card in
    the run. A claim about variation needs something to vary against."""
    rows = [row for row in corpus["v9"]
            if row["type"] != "node_created" or row["data"]["node_id"] == 0]
    split = run_constant_split(_fold(rows))
    assert split["coverage"] == "partial"
    assert split["run_constant"] == []
    assert RUN_CONSTANT_SINGLE_REASON in split["reasons"]


def test_an_inexact_membership_refuses_the_claim(corpus):
    """A partial membership is a SUBSET of the truth: its ABSENCE of a concept proves nothing, so it
    can neither join the intersection nor be excluded from the population. Driven through the real
    fold: node 3 authors one MALFORMED id beside its real delta, which is exactly what
    `_materialize_concept_deltas` mints a `partial` / `invalid_concept_id` receipt for. Its retained
    subset still renders — the refusal is of the run-wide CLAIM, not of the node's row."""
    rows = json.loads(json.dumps(corpus["v9"]))
    for row in rows:
        if row["type"] == "node_created" and row["data"]["node_id"] == 3:
            row["data"]["idea"]["concepts_added"] = ["training/batch_size", "NOT A VALID ID!!"]
    state = _fold(rows)
    receipt = state.node_concept_materialization_receipts.get(3)
    assert receipt and receipt["status"] == "partial", "fixture must go inexact"
    split = run_constant_split(state)
    assert split["coverage"] == "partial"
    assert split["run_constant"] == []
    # The node keeps its retained ids; only the claim about EVERY experiment is withheld.
    assert "training/batch_size" in split["distinguishing"][3]


# --------------------------------------------------------------------------- the frame

def test_the_concepts_frame_publishes_the_split_and_bounds_it(v9, v8):
    from looplab.search.concept_lens import default_lenses
    from looplab.serve.concept_frame import build_frame

    def frame(state, run_id):
        return build_frame(state, run_id=run_id, requested_lens="is_a", lens_pack=default_lenses(),
                           generation="g", requested_seq=None, captured_seq=1, max_seq=1,
                           source_divergence=None)

    v9_frame = frame(v9, "v9")
    scope = v9_frame["run_scope"]
    assert scope["coverage"] == "complete" and len(scope["run_constant"]) == 5
    # Self-consistency: the frame may not name a run constant it did not itself publish. A bounded
    # projection announcing "carried by every experiment" about ids it truncated away is the class of
    # claim concept_frame's whole receipt apparatus exists to refuse.
    assert set(scope["run_constant"]) <= set(v9_frame["tree"]["nodes"])
    assert set(scope["run_constant"]) <= set(v9_frame["experiment_refs"])
    v8_scope = frame(v8, "v8")["run_scope"]
    assert v8_scope["run_constant"] == [] and v8_scope["no_distinguishing"] == []


def test_a_bounded_or_torn_frame_publishes_no_run_constant(v9):
    """The frame's claim is about the population IT published. A cap or a torn log makes that
    population inexact, so the split degrades to the empty shape with `bounded_frame` said — even
    though the underlying projection could make the claim."""
    from looplab.search.concept_lens import default_lenses
    from looplab.serve.concept_frame import build_frame

    torn = build_frame(v9, run_id="v9", requested_lens="is_a", lens_pack=default_lenses(),
                       generation="g", requested_seq=None, captured_seq=1, max_seq=1,
                       source_divergence={"corrupt_line": 3, "dropped_lines": 1})
    assert torn["run_scope"]["run_constant"] == []
    assert "bounded_frame" in torn["run_scope"]["reasons"]
    # An EDGE-lane reason is deliberately NOT blinding — it bears on no membership.
    from looplab.serve.concept_frame import RUN_SCOPE_BLIND_REASONS
    assert "invalid_edge" in RUN_SCOPE_BLIND_REASONS
    assert "membership_cap" not in RUN_SCOPE_BLIND_REASONS


# --------------------------------------------------------------------------- it is a READER

def test_nothing_new_is_written(v9):
    """The whole fix is a projection. If `run_constant` ever became a stamped field it would mean
    "constant among the nodes that existed when I fired" — undecidable at tag time, and a different
    answer for the same concept depending on when it was written. Assert the two writer-side surfaces
    stayed shut: no new event type, and the fold's own state gained no field."""
    from looplab.events import types
    assert not [name for name in dir(types)
                if name.startswith("EV_") and "RUN_CONSTANT" in name]
    assert not [field for field in type(v9).model_fields if "run_constant" in field]
