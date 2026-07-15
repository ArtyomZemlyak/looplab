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

from looplab.core.config import Settings
from looplab.core.models import Idea, RunState
from looplab.engine.novelty import NoveltyGateMixin
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
    eng = _GateEngine(s, graded=True)
    eng._reflect_client = lambda: _IdeaReflectClient(["encoderarch/adapter-tuning"])
    # a materially-different proposal on that SAME grown direction (different params + rationale)
    idea = Idea(operator="improve", params={"rank": 8.0}, rationale="a different adapter bottleneck size")
    out = eng._graded_novelty_precheck(fold(s.read_all()), idea)
    assert out is idea                                     # level-4 ALLOW short-circuit
    g = fold(s.read_all()).novelty_grades[-1]
    assert g["level"] == 4 and "encoderarch/adapter-tuning" in g["shared_concepts"]


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


def test_structural_variant_with_empty_params_still_allowed(tmp_path):
    # empty params but a DIFFERENT rationale on the same branch is a genuine new implementation -> the text
    # guard does NOT fire (texts differ) -> level-4 ALLOW stands (the guard closes the dup hole without
    # over-restricting real structural variants).
    store = _run(tmp_path)
    eng = _GateEngine(store, graded=True)
    variant = Idea(operator="improve", params={},
                   rationale="decoupled contrastive loss with an added listwise KL distillation term")
    out = eng._graded_novelty_precheck(fold(store.read_all()), variant)
    assert out is variant and fold(store.read_all()).novelty_grades[0]["level"] == 4


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
    import os, subprocess, sys
    p = str(tmp_path / "events.jsonl")
    _run(tmp_path)                                        # writes the run's events.jsonl at `p`
    def _emit(seed: str) -> str:
        env = {**os.environ, "PYTHONHASHSEED": seed}
        # each subprocess writes graded events into its OWN copy of the log so the payloads compare cleanly
        import shutil
        cp = str(tmp_path / f"events-{seed}.jsonl"); shutil.copy(p, cp)
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


def test_settings_flags_default_off():
    assert Settings().graded_novelty is False and Settings().capability_expansion is False
