"""PART IV Phase 2b — D3 graded novelty wired into the LIVE novelty gate + the D7 capability-expansion
forced-jump directive.

Locks in that: with `graded_novelty` on, the live gate GRADES a proposal over the concept graph and
ALLOWS a level-4 "same direction, different implementation" or a level-5 "re-opens a wrongly-abandoned
FAILED direction" — the two the flat LLM/semantic dedup gate gets wrong — short-circuiting the flat gate
and recording a `novelty_graded` audit event; levels 0/1/2/3 defer unchanged. And that, with
`capability_expansion` on, an `explore`-stance hint ESCALATES to "expand the action space / build infra"
once the concept cadence reports action-space lock-in. Both are opt-in, deterministic, and never touch
selection (the audit event folds audit-only; no-op for a task with no curated concept skeleton)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from looplab.core.config import Settings
from looplab.core.models import Idea, RunState
from looplab.engine.novelty import (
    NoveltyGateMixin,
    _canonical_idea_identity,
    _same_canonical_idea_identity,
)
from looplab.engine.orchestrator import Engine
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold


# --------------------------------------------------------------------------- #
# Fixtures — a run with a WON DCL/loss branch and a FAILED false-negative direction
# --------------------------------------------------------------------------- #

def _run(tmp_path, task_id="dense-retrieval") -> EventStore:
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": task_id, "goal": "g", "direction": "max"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {"temperature": 0.05},
                                       "theme": "dcl", "rationale": "decoupled contrastive loss with r-drop"}})
    s.append("node_evaluated", {"node_id": 0, "metric": 0.85})
    s.append("node_created", {"node_id": 1, "parent_ids": [0], "operator": "improve",
                              "idea": {"operator": "improve", "params": {"seed": 1.0}, "theme": "fn",
                                       "rationale": "loss-side false negative filtering that broke training"}})
    s.append("node_failed", {"node_id": 1, "error": "nan", "reason": "crash"})
    return s


class _GateEngine(NoveltyGateMixin):
    """Minimal host exposing the real mixin methods (`self` is the engine). `mode="off"` +
    `stance="balanced"` makes the flat gate below the precheck a pure no-op, so a deferral returns the
    idea unchanged and any short-circuit is unambiguously the D3 precheck's doing."""
    def __init__(self, store, *, graded=True, stance="balanced", mode="off"):
        self.store = store
        self._graded_novelty = graded
        self._novelty_stance = stance
        self._novelty_mode = mode


# --------------------------------------------------------------------------- #
# D3 precheck — the level-4/5 ALLOW override
# --------------------------------------------------------------------------- #

_LEVEL4 = Idea(operator="improve", params={"temperature": 0.5},
               rationale="decoupled contrastive with a listwise KL term")     # same DCL branch, new impl
_LEVEL5 = Idea(operator="improve", params={"seed": 9.0},
               rationale="data-side false negative filtering (nv-0.95)")      # re-opens the killed direction


def test_level4_same_direction_new_impl_is_allowed_and_audited(tmp_path):
    store = _run(tmp_path)
    eng = _GateEngine(store, graded=True)
    out = eng._graded_novelty_precheck(fold(store.read_all()), _LEVEL4)
    assert out is _LEVEL4                       # short-circuit ALLOW (idea returned unchanged)
    st = fold(store.read_all())
    assert len(st.novelty_grades) == 1
    g = st.novelty_grades[0]
    assert g["level"] == 4 and g["grade"] == "same_direction_new_impl" and g["recommendation"] == "allow"


def test_level5_wrongly_abandoned_is_allowed_and_audited(tmp_path):
    store = _run(tmp_path)
    eng = _GateEngine(store, graded=True)
    out = eng._graded_novelty_precheck(fold(store.read_all()), _LEVEL5)
    assert out is _LEVEL5
    st = fold(store.read_all())
    assert len(st.novelty_grades) == 1 and st.novelty_grades[0]["level"] == 5
    assert st.novelty_grades[0]["grade"] == "wrongly_abandoned"
    assert "negatives/false-neg-handling" in st.novelty_grades[0]["shared_concepts"]


class _IdeaReflectClient:
    """Fake reflect client the F2 agentic idea-tagger calls; returns a fixed grown concept id."""
    def __init__(self, ids): self.ids = ids
    def complete_tool(self, messages, json_schema): return {"concept_ids": self.ids}
    def complete_text(self, messages): return "x"


def test_f2_grades_from_the_cached_llm_tags_and_agentic_idea_tag(tmp_path):
    """F2 (§21.4): when node tags are cached as node_concepts (Feature 1) and a reflect client is wired, the
    grade uses the AGENTIC vocabulary — here a GROWN concept absent from any skeleton, so a level-4 ALLOW on
    it is only reachable via the cache + the LLM idea-tag, not the alias heuristic."""
    s = _run(tmp_path, task_id="totally-unknown-task")     # no curated skeleton -> vocab must come from cache
    # the agentic node tagger (Feature 1) assigned node 0 a grown concept the skeleton never had:
    s.append("node_concepts", {"node_id": 0, "concepts": ["encoderarch/adapter-tuning"], "mode": "llm"})
    # An explicit empty classifier result is still a complete receipt. The admission override must
    # never infer "no concepts" merely because a node has not reached the classifier cadence yet.
    s.append("node_concepts", {"node_id": 1, "concepts": [], "mode": "llm"})
    eng = _GateEngine(s, graded=True)
    eng._reflect_client = lambda: _IdeaReflectClient(["encoderarch/adapter-tuning"])
    # a materially-different proposal on that SAME grown direction (different params + rationale)
    idea = Idea(operator="improve", params={"rank": 8.0}, rationale="a different adapter bottleneck size")
    out = eng._graded_novelty_precheck(fold(s.read_all()), idea)
    assert out is idea                                     # level-4 ALLOW short-circuit
    replayed = fold(s.read_all())
    assert replayed.node_concept_provenance == {0: "classifier", 1: "classifier"}
    g = replayed.novelty_grades[-1]
    assert g["level"] == 4 and "encoderarch/adapter-tuning" in g["shared_concepts"]


def test_partial_classifier_cache_defers_instead_of_treating_new_nodes_as_concept_free(tmp_path):
    """A mixed classified/unknown snapshot cannot certify a graded-novelty admission override."""
    s = _run(tmp_path, task_id="totally-unknown-task")
    s.append("node_concepts", {"node_id": 0,
                                "concepts": ["encoderarch/adapter-tuning"], "mode": "llm"})
    state = fold(s.read_all())
    assert state.node_concept_provenance == {0: "classifier"}  # node 1 is still UNKNOWN

    eng = _GateEngine(s, graded=True)
    eng._reflect_client = lambda: _IdeaReflectClient(["encoderarch/adapter-tuning"])
    candidate = Idea(operator="improve", params={"rank": 8.0},
                     rationale="a different adapter bottleneck size")

    # CODEX AGENT: deferral preserves the ordinary novelty gate; an incomplete cadence may not hide
    # the newest duplicate/win and manufacture a level-4 or level-5 short-circuit.
    assert eng._graded_novelty_precheck(state, candidate) is None
    assert fold(s.read_all()).novelty_grades == []


def test_operator_edited_node_counts_as_covered_for_the_completeness_gate(tmp_path):
    """PART V cross-phase: an operator concept edit gives a node an authoritative tag set, so it counts
    as COVERED for the completeness gate — a single operator edit no longer disables the agentic
    graded-novelty path for the WHOLE run. The graded channel still reads ONLY classifier tags (the
    operator node's tags never enter the grade)."""
    s = _run(tmp_path, task_id="totally-unknown-task")
    s.append("node_concepts", {"node_id": 0,
                                "concepts": ["encoderarch/adapter-tuning"], "mode": "llm"})
    # node 1 (the FAILED experiment node) gets an authoritative OPERATOR tag rather than a classifier receipt
    s.append("concept_tag_edited", {"node_id": 1, "concepts": ["someaxis/hand-labeled"]})
    state = fold(s.read_all())
    assert state.node_concept_provenance == {0: "classifier", 1: "operator-edited"}

    eng = _GateEngine(s, graded=True)
    eng._reflect_client = lambda: _IdeaReflectClient(["encoderarch/adapter-tuning"])
    candidate = Idea(operator="improve", params={"rank": 8.0},
                     rationale="a different adapter bottleneck size")

    # coverage complete (classifier {0} ∪ operator {1} == experiments {0,1}) -> the precheck RUNS and
    # grades a level-4 ALLOW instead of deferring as in the partial-cadence case above.
    out = eng._graded_novelty_precheck(state, candidate)
    assert out is candidate
    g = fold(s.read_all()).novelty_grades[-1]
    assert g["level"] == 4 and "encoderarch/adapter-tuning" in g["shared_concepts"]
    # the operator node's tag never entered the graded vocabulary (grade reads classifier tags only)
    assert "someaxis/hand-labeled" not in g["shared_concepts"]


@pytest.mark.parametrize(("event_type", "payload"), [
    ("node_concepts", {
        "node_id": 1, "concepts": [f"axis/c{i:03d}" for i in range(65)], "mode": "llm"}),
    ("node_concepts", {
        "node_id": 1, "concepts": ["valid/y", "bad!"], "mode": "llm"}),
    ("concept_tag_edited", {
        "node_id": 1, "concepts": [f"operator/c{i:03d}" for i in range(65)]}),
])
def test_partial_classifier_or_operator_cache_cannot_certify_admission(
        tmp_path, event_type, payload):
    s = _run(tmp_path, task_id="totally-unknown-task")
    s.append("node_concepts", {
        "node_id": 0, "concepts": ["encoderarch/adapter-tuning"], "mode": "llm"})
    s.append(event_type, payload)
    state = fold(s.read_all())
    assert state.node_concept_materialization_receipts[1]["status"] == "partial"

    eng = _GateEngine(s, graded=True)
    eng._reflect_client = lambda: _IdeaReflectClient(["encoderarch/adapter-tuning"])
    candidate = Idea(operator="improve", params={"rank": 8.0},
                     rationale="a different adapter bottleneck size")

    assert eng._graded_novelty_precheck(state, candidate) is None
    assert fold(s.read_all()).novelty_grades == []


def test_researcher_authored_concepts_cannot_certify_agentic_bypass(tmp_path):
    """The proposer cannot self-author the classifier evidence that lets its next proposal bypass dedup."""
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "unknown-task", "goal": "g",
                             "direction": "max"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {"rank": 4.0},
                                       "rationale": "baseline bottleneck",
                                       "concepts": ["encoderarch/adapter-tuning"]}})
    s.append("node_evaluated", {"node_id": 0, "metric": 0.7})
    state = fold(s.read_all())
    assert state.node_concept_provenance == {0: "researcher-authored"}

    eng = _GateEngine(s, graded=True)
    eng._reflect_client = lambda: _IdeaReflectClient(["encoderarch/adapter-tuning"])
    candidate = Idea(operator="improve", params={"rank": 8.0},
                     rationale="a materially different adapter bottleneck")

    assert eng._graded_novelty_precheck(state, candidate) is None
    assert fold(s.read_all()).novelty_grades == []


def test_candidate_authored_concepts_cannot_certify_heuristic_bypass(tmp_path):
    """A fresh proposal cannot self-assign the concept that would let it skip the ordinary flat gate."""
    store = _run(tmp_path)
    eng = _GateEngine(store, graded=True)
    candidate = Idea(
        operator="improve",
        params={"rank": 8.0},
        rationale="generic training adjustment with no independently classifiable method",
        concepts=["loss/decoupled-contrastive"],
    )

    assert eng._graded_novelty_precheck(fold(store.read_all()), candidate) is None
    assert fold(store.read_all()).novelty_grades == []


def test_unknown_concept_provenance_fails_closed(tmp_path):
    """A legacy/direct state snapshot with memberships but no producer receipt is not classifier evidence."""
    s = _run(tmp_path, task_id="totally-unknown-task")
    s.append("node_concepts", {"node_id": 0, "concepts": ["encoderarch/adapter-tuning"]})
    replayed = fold(s.read_all())
    assert replayed.node_concept_provenance == {0: "classifier"}  # old event shape remains trusted
    state_without_receipt = replayed.model_copy(update={"node_concept_provenance": {}})
    eng = _GateEngine(s, graded=True)
    eng._reflect_client = lambda: _IdeaReflectClient(["encoderarch/adapter-tuning"])
    candidate = Idea(operator="improve", params={"rank": 8.0},
                     rationale="a materially different adapter bottleneck")

    assert eng._graded_novelty_precheck(state_without_receipt, candidate) is None
    assert fold(s.read_all()).novelty_grades == []


def test_offline_heuristic_membership_cannot_certify_agentic_bypass(tmp_path):
    """Offline aliases remain visible but cannot create an L4/L5 classifier-only admission override."""
    s = _run(tmp_path, task_id="totally-unknown-task")
    for node_id in (0, 1):
        s.append("node_concepts", {
            "node_id": node_id,
            "concepts": ["encoderarch/adapter-tuning"],
            "mode": "offline-heuristic",
        })
    state = fold(s.read_all())
    assert state.node_concept_provenance == {
        0: "offline-heuristic", 1: "offline-heuristic"}

    eng = _GateEngine(s, graded=True)
    eng._reflect_client = lambda: _IdeaReflectClient(["encoderarch/adapter-tuning"])
    candidate = Idea(operator="improve", params={"rank": 8.0},
                     rationale="a materially different adapter bottleneck")

    assert eng._graded_novelty_precheck(state, candidate) is None
    assert fold(s.read_all()).novelty_grades == []


def test_f2_no_cache_no_client_is_the_heuristic_fallback(tmp_path):
    """With no node_concepts cache and no reflect client, F2 degrades to the skeleton + heuristic path —
    byte-identical behaviour to pre-F2 (a level-4 on the curated DCL branch still fires)."""
    store = _run(tmp_path)                                 # dense-retrieval skeleton, no node_concepts
    eng = _GateEngine(store, graded=True)                  # no _reflect_client attribute -> client None
    out = eng._graded_novelty_precheck(fold(store.read_all()), _LEVEL4)
    assert out is _LEVEL4 and fold(store.read_all()).novelty_grades[-1]["level"] == 4


def test_verbatim_text_duplicate_with_empty_params_defers(tmp_path):
    # a VERBATIM rationale repeat of node 0 with EMPTY params grades level 4 (grade_novelty's param-based
    # dedup is structurally skipped for empty/key-disjoint params) — but it is a true duplicate, so the
    # text guard DEFERS it to the flat gate instead of wrongly short-circuiting an ALLOW.
    store = _run(tmp_path)
    eng = _GateEngine(store, graded=True)
    dup = Idea(operator="improve", params={},
               rationale="decoupled contrastive loss with r-drop")     # node 0's rationale, verbatim
    assert eng._graded_novelty_precheck(fold(store.read_all()), dup) is None
    assert fold(store.read_all()).novelty_grades == []                 # NOT recorded as a graded allow


@pytest.mark.parametrize("duplicate", [
    "DECOUPLED CONTRASTIVE LOSS WITH R-DROP",                         # case-fold
    "  decoupled\tcontrastive\nloss   with r-drop  ",                 # whitespace collapse
    "decoupled, contrastive loss with r—drop!!!",                     # punctuation separators
    "decoupled contrastive loss with rdrop",                          # punctuation deletion
    "ｄｅｃｏｕｐｌｅｄ contrastive loss with r-drop",                         # NFKC full-width form
])
def test_surface_only_duplicate_cannot_use_level4_bypass(tmp_path, duplicate):
    store = _run(tmp_path)
    eng = _GateEngine(store, graded=True)
    idea = Idea(operator="improve", params={}, rationale=duplicate)

    assert _same_canonical_idea_identity(
        _canonical_idea_identity(duplicate),
        _canonical_idea_identity("decoupled contrastive loss with r-drop"),
    )
    assert eng._graded_novelty_precheck(fold(store.read_all()), idea) is None
    assert fold(store.read_all()).novelty_grades == []


def test_surface_only_duplicate_cannot_use_level5_bypass(tmp_path):
    store = _run(tmp_path)
    eng = _GateEngine(store, graded=True)
    duplicate = Idea(
        operator="improve",
        params={},
        rationale="LOSS—SIDE, FALSE NEGATIVE FILTERING THAT BROKE TRAINING!!!",
    )

    assert eng._graded_novelty_precheck(fold(store.read_all()), duplicate) is None
    assert fold(store.read_all()).novelty_grades == []


def test_oversize_identity_defers_instead_of_trusting_a_truncated_comparison(tmp_path):
    store = _run(tmp_path)
    eng = _GateEngine(store, graded=True)
    oversized = Idea(
        operator="improve",
        params={},
        rationale=("decoupled contrastive loss with a distinct adapter " * 400),
    )

    identity, complete = _canonical_idea_identity(oversized.rationale)
    assert identity and complete is False
    assert eng._graded_novelty_precheck(fold(store.read_all()), oversized) is None
    assert fold(store.read_all()).novelty_grades == []


def test_prior_identity_scan_memoizes_immutable_node_text(tmp_path, monkeypatch):
    import looplab.engine.novelty as novelty_module

    store = _run(tmp_path)
    state = fold(store.read_all())
    eng = _GateEngine(store, graded=True)
    original = novelty_module._canonical_idea_identity
    calls = 0

    def counted(text):
        nonlocal calls
        calls += 1
        return original(text)

    monkeypatch.setattr(novelty_module, "_canonical_idea_identity", counted)
    assert eng._graded_novelty_precheck(state, _LEVEL4) is _LEVEL4
    assert eng._graded_novelty_precheck(state, _LEVEL4) is _LEVEL4
    # Candidate identity is intentionally recomputed; each of the two immutable priors is scanned once.
    assert calls == 4


def test_oversize_prior_is_fail_closed_but_warned_once(tmp_path, caplog):
    store = _run(tmp_path)
    store.append("node_created", {"node_id": 2, "parent_ids": [], "operator": "explore",
                                   "idea": {"operator": "explore", "params": {"huge": 1.0},
                                            "rationale": "x" * 17_000}})
    store.append("node_evaluated", {"node_id": 2, "metric": 0.1})
    state = fold(store.read_all())
    eng = _GateEngine(store, graded=True)

    with caplog.at_level("WARNING", logger="looplab.engine.novelty"):
        assert eng._graded_novelty_precheck(state, _LEVEL4) is None
        assert eng._graded_novelty_precheck(state, _LEVEL4) is None

    warnings = [r for r in caplog.records if "incomplete bounded idea identity" in r.message]
    assert len(warnings) == 1
    assert "node 2" in warnings[0].message


def test_prose_only_structural_variant_still_runs_the_flat_gate(tmp_path):
    # Different prose is not machine-evaluable proof of a different implementation. With no concrete
    # param/space delta the graded precheck must defer instead of trusting the proposal's own assertion.
    store = _run(tmp_path)
    eng = _GateEngine(store, graded=True)
    variant = Idea(operator="improve", params={},
                   rationale="decoupled contrastive loss with an added listwise KL distillation term")
    assert eng._graded_novelty_precheck(fold(store.read_all()), variant) is None
    assert fold(store.read_all()).novelty_grades == []


def test_identical_and_near_dup_defer_to_flat_gate(tmp_path):
    # levels 1 (identical) and 2 (near-dup) must NOT be short-circuited — the flat gate legitimately dedups.
    store = _run(tmp_path)
    eng = _GateEngine(store, graded=True)
    st = fold(store.read_all())
    identical = Idea(operator="improve", params={"temperature": 0.05},
                     rationale="decoupled contrastive r-drop")                # same params as node 0
    near_dup = Idea(operator="improve", params={"temperature": 0.055},
                    rationale="decoupled contrastive loss with r-drop")       # same concepts, close params
    assert eng._graded_novelty_precheck(st, identical) is None
    assert eng._graded_novelty_precheck(st, near_dup) is None
    assert fold(store.read_all()).novelty_grades == []                        # nothing audited on a defer


def test_novel_region_defers_not_short_circuits(tmp_path):
    # level 0 (novel) also defers — the flat gate passes it anyway; no graded event is recorded.
    store = _run(tmp_path)
    eng = _GateEngine(store, graded=True)
    novel = Idea(operator="improve", params={"seed": 3.0},
                 rationale="synthetic query generation via doc2query")
    assert eng._graded_novelty_precheck(fold(store.read_all()), novel) is None
    assert fold(store.read_all()).novelty_grades == []


# --------------------------------------------------------------------------- #
# Opt-in + universality guards
# --------------------------------------------------------------------------- #

def test_flag_off_is_a_no_op(tmp_path):
    store = _run(tmp_path)
    eng = _GateEngine(store, graded=False)
    assert eng._graded_novelty_precheck(fold(store.read_all()), _LEVEL4) is None
    assert fold(store.read_all()).novelty_grades == []


def test_no_skeleton_task_is_a_no_op(tmp_path):
    store = _run(tmp_path, task_id="some-tabular-task")                       # no curated skeleton
    eng = _GateEngine(store, graded=True)
    assert eng._graded_novelty_precheck(fold(store.read_all()), _LEVEL4) is None
    assert fold(store.read_all()).novelty_grades == []


def test_empty_run_is_a_no_op(tmp_path):
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "dr", "goal": "g", "direction": "max"})
    eng = _GateEngine(s, graded=True)
    assert eng._graded_novelty_precheck(fold(s.read_all()), _LEVEL4) is None


def test_apply_gate_short_circuits_on_a_level4(tmp_path):
    # the whole gate returns the idea WITHOUT reaching the flat LLM path (mode='llm' would need a client).
    store = _run(tmp_path)
    eng = _GateEngine(store, graded=True, mode="llm")
    assert eng._apply_novelty_gate(fold(store.read_all()), _LEVEL4) is _LEVEL4


# --------------------------------------------------------------------------- #
# Replay-safety of the audit event
# --------------------------------------------------------------------------- #

def test_graded_event_folds_audit_only(tmp_path):
    store = _run(tmp_path)
    store.append("novelty_graded", {"node_id": 2, "level": 4, "grade": "same_direction_new_impl",
                                    "recommendation": "allow", "near_node": 0, "shared_concepts": ["x"]})
    st = fold(store.read_all())
    assert len(st.novelty_grades) == 1 and st.novelty_grades[0]["node_id"] == 2
    assert st.best_node_id == 0                                               # selection unchanged


def test_old_logs_fold_without_the_field(tmp_path):
    st = fold(_run(tmp_path).read_all())
    assert st.novelty_grades == []


def test_recorded_grade_payload_is_sorted(tmp_path):
    # the persisted shared_concepts must be deterministically ordered (sorted) — the ONLY place a
    # frozenset iteration order could leak into the event bytes. Inspect the actual recorded payload.
    store = _run(tmp_path)
    _GateEngine(store, graded=True)._graded_novelty_precheck(fold(store.read_all()), _LEVEL5)
    sc = fold(store.read_all()).novelty_grades[0]["shared_concepts"]
    assert sc == sorted(sc) and sc                       # sorted, non-empty


# The grade payload must be byte-identical across PYTHONHASHSEED values (a frozenset/dict iteration order
# leaking into `shared_concepts` would only surface across processes — same-process f(x)==f(x) can't catch it).
_PAYLOAD_SNIPPET = """
import json
from looplab.core.models import Idea
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.engine.novelty import NoveltyGateMixin

class E(NoveltyGateMixin):
    def __init__(self, store):
        self.store = store; self._graded_novelty = True; self._novelty_stance = "balanced"

store = EventStore({path!r})
eng = E(store)
idea = Idea(operator="improve", params={{"seed": 9.0}},
            rationale="data-side false negative filtering (nv-0.95)")
eng._graded_novelty_precheck(fold(store.read_all()), idea)
print(json.dumps(fold(store.read_all()).novelty_grades[0], sort_keys=True))
"""


def test_grade_payload_is_hashseed_independent(tmp_path):
    import os
    import subprocess
    import sys
    p = str(tmp_path / "events.jsonl")
    _run(tmp_path)                                        # writes the run's events.jsonl at `p`
    def _emit(seed: str) -> str:
        env = {**os.environ, "PYTHONHASHSEED": seed}
        # each subprocess writes graded events into its OWN copy of the log so the payloads compare cleanly
        import shutil
        cp = str(tmp_path / f"events-{seed}.jsonl")
        shutil.copy(p, cp)
        return subprocess.check_output(
            [sys.executable, "-c", _PAYLOAD_SNIPPET.format(path=cp)], env=env, text=True).strip()
    assert _emit("0") == _emit("987654") != ""


# --------------------------------------------------------------------------- #
# D7 capability-expansion forced-jump directive (the explore-stance escalation)
# --------------------------------------------------------------------------- #

def _explore_state(tmp_path, *, locked_axis="loss", streak=7) -> RunState:
    # `streak` here drives `current_streak` (what the capability-expansion directive gates on — the
    # CURRENT lock-in, not the longest-ever); recent_axis names the currently-locked axis.
    st = fold(_run(tmp_path).read_all())
    st.concept_coverage_snapshots.append(
        {"at_node": 2, "fired": True, "uncovered_key": ["negatives/external-mining"],
         "directive": "0 coverage in {negatives/external-mining} — go there",
         "locked_axis": locked_axis, "recent_axis": locked_axis,
         "streak": streak, "current_streak": streak})
    return st


def _fake_engine(*, concept_pivot=False, capability_expansion=False):
    return SimpleNamespace(_concept_pivot=concept_pivot,
                           _capability_expansion=capability_expansion, researcher=SimpleNamespace())


def test_capability_expansion_escalates_on_lock_in(tmp_path):
    eng = _fake_engine(capability_expansion=True)
    Engine._stamp_novelty_hint(eng, _explore_state(tmp_path), "explore")
    hint = eng.researcher._novelty_hint
    assert "Capability expansion" in hint and "EXPAND THE ACTION SPACE" in hint
    assert "'loss'" in hint and "7 consecutive" in hint                      # names the saturated lever


def test_capability_expansion_off_is_unchanged(tmp_path):
    eng = _fake_engine(capability_expansion=False)
    Engine._stamp_novelty_hint(eng, _explore_state(tmp_path), "explore")
    assert "Capability expansion" not in eng.researcher._novelty_hint


def test_capability_expansion_needs_lock_in(tmp_path):
    # a short streak (below the fire threshold) does NOT escalate
    eng = _fake_engine(capability_expansion=True)
    Engine._stamp_novelty_hint(eng, _explore_state(tmp_path, streak=2), "explore")
    assert "Capability expansion" not in eng.researcher._novelty_hint


def test_capability_expansion_needs_a_locked_axis(tmp_path):
    eng = _fake_engine(capability_expansion=True)
    Engine._stamp_novelty_hint(eng, _explore_state(tmp_path, locked_axis=None), "explore")
    assert "Capability expansion" not in eng.researcher._novelty_hint


def test_capability_expansion_only_on_explore(tmp_path):
    eng = _fake_engine(capability_expansion=True)
    Engine._stamp_novelty_hint(eng, _explore_state(tmp_path), "exploit")
    assert "Capability expansion" not in eng.researcher._novelty_hint


def test_capability_expansion_composes_with_pivot(tmp_path):
    # both levers on: the pivot names the uncovered region AND capability-expansion adds the build-infra jump
    eng = _fake_engine(concept_pivot=True, capability_expansion=True)
    Engine._stamp_novelty_hint(eng, _explore_state(tmp_path), "explore")
    hint = eng.researcher._novelty_hint
    assert "Concept-graph pivot" in hint and "Capability expansion" in hint


def test_settings_flags_defaults():
    # graded_novelty ships ON by default (Part IV/V; heuristic tagger, audit-only, never rejects);
    # capability_expansion stays OFF (separate operator, not part of the Part IV/V default bundle).
    assert Settings().graded_novelty is True and Settings().capability_expansion is False
