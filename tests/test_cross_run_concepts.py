"""PART IV cross-run Step 2 (wiring) — capsule WRITE at finalize + prior SURFACE at the novelty gate.

The first *visible* cross-run win: at run end a per-run concept capsule is written to the shared
`memory_dir` (reusing the shipped `node_concepts` tags + `task_fingerprint`), and when a later SIMILAR
run proposes an idea whose concept was tried before, the gate records a `cross_run_prior` audit event
that SURFACES the earlier run + outcome — it never rejects (D3 level 3 defers to the flat gate). Both
halves are OPT-IN (`cross_run_concepts`) and audit-only; these tests pin the write, the surface, the
off-switch, and replay-safety.
"""
from __future__ import annotations

from types import SimpleNamespace

from looplab.core.config import Settings
from looplab.core.models import Idea
from looplab.engine.lessons import LessonMemory
from looplab.engine.memory import ConceptCapsuleStore, build_concept_capsule, task_fingerprint
from looplab.engine.novelty import NoveltyGateMixin
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold


# --------------------------------------------------------------------------- #
# WRITE — store_concept_capsule builds a capsule from node_concepts + best per-concept outcome
# --------------------------------------------------------------------------- #

def _fake_engine(memory_dir, *, goal="dense retrieval reviews"):
    return SimpleNamespace(
        memory_dir=str(memory_dir),
        task=SimpleNamespace(kind="dataset", metric="recall", id="t", goal=goal),
        _fingerprint_universal=True)


def test_store_concept_capsule_writes_best_per_concept_outcome(tmp_path):
    mem = tmp_path / "mem"; mem.mkdir()
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "r-now", "task_id": "t", "goal": "dense retrieval reviews",
                             "direction": "max"})
    # two nodes on the SAME concept; the better (max) metric must win the concept's outcome
    for nid, metric in ((0, 0.85), (1, 0.90)):
        s.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {"t": float(nid)}, "theme": "x"}})
        s.append("node_evaluated", {"node_id": nid, "metric": metric})
        s.append("node_concepts", {"node_id": nid, "concepts": ["data/hard-negative-mining"], "mode": "llm"})
    LessonMemory(_fake_engine(mem)).store_concept_capsule(fold(s.read_all()))

    caps = ConceptCapsuleStore(mem / "concept_capsules.jsonl").all()
    assert len(caps) == 1
    c = caps[0]
    assert c["run_id"] == "r-now" and c["direction"] == "max"
    assert c["concepts"] == ["data/hard-negative-mining"]
    assert c["best_metric"] == 0.90
    assert c["concept_outcomes"]["data/hard-negative-mining"] == 0.90   # best-of, not last-of


def test_store_concept_capsule_noop_without_tags(tmp_path):
    mem = tmp_path / "mem"; mem.mkdir()
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "theme": "x"}})
    s.append("node_evaluated", {"node_id": 0, "metric": 0.5})
    LessonMemory(_fake_engine(mem)).store_concept_capsule(fold(s.read_all()))
    assert not (mem / "concept_capsules.jsonl").exists()   # nothing tagged -> no capsule


# --------------------------------------------------------------------------- #
# SURFACE — the novelty gate records a cross_run_prior when a concept was tried in a similar prior run
# --------------------------------------------------------------------------- #

class _IdeaReflectClient:
    def __init__(self, ids): self.ids = ids
    def complete_tool(self, messages, json_schema): return {"concept_ids": self.ids}
    def complete_text(self, messages): return "x"


class _FPLessons:
    """Minimal stand-in for LessonMemory.task_fingerprint at the gate (universal, matches the prior fp)."""
    def task_fingerprint(self, state, best=None):
        return task_fingerprint("dataset", state.direction, state.goal, "recall", universal=True)


class _GateEngine(NoveltyGateMixin):
    def __init__(self, store, memory_dir):
        self.store = store
        self._graded_novelty = True
        self._novelty_stance = "balanced"
        self._novelty_mode = "off"
        self._cross_run_concepts = True
        self.memory_dir = str(memory_dir)
        self.lessons = _FPLessons()


def _run_with_cached_concept(tmp_path, concept):
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "r-now", "task_id": "unknown-task", "goal": "dense retrieval",
                             "direction": "max"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {"temperature": 0.05}, "theme": "x"}})
    s.append("node_evaluated", {"node_id": 0, "metric": 0.85})
    s.append("node_concepts", {"node_id": 0, "concepts": [concept], "mode": "llm"})
    return s


def _seed_prior(memory_dir, concept, *, goal="dense retrieval", metric=0.88, run_id="prior-1"):
    fp = task_fingerprint("dataset", "max", goal, "recall", universal=True)
    ConceptCapsuleStore(memory_dir / "concept_capsules.jsonl").add(build_concept_capsule(
        run_id=run_id, fingerprint=fp, direction="max", concepts=[concept], best_metric=metric,
        concept_outcomes={concept: metric}))


_CONCEPT = "data/hard-negative-mining"


def test_cross_run_prior_surfaces_as_audit_without_changing_grade(tmp_path):
    mem = tmp_path / "mem"; mem.mkdir()
    _seed_prior(mem, _CONCEPT)
    s = _run_with_cached_concept(tmp_path, _CONCEPT)               # node 0 tagged _CONCEPT
    eng = _GateEngine(s, mem)
    eng._reflect_client = lambda: _IdeaReflectClient([_CONCEPT])   # idea tags to the SAME concept
    idea = Idea(operator="improve", params={"rank": 8.0}, rationale="a materially different hard-neg scheme")

    out = eng._graded_novelty_precheck(fold(s.read_all()), idea)
    # the idea shares _CONCEPT with node 0 -> a level-4 same-direction ALLOW; cross-run NEVER changes that.
    assert out is idea
    st = fold(s.read_all())
    # ...but the cross-run prior is SURFACED as a separate audit event alongside the (unchanged) grade.
    assert len(st.cross_run_priors) == 1
    cp = st.cross_run_priors[0]
    assert _CONCEPT in cp["matched_concepts"]
    assert cp["prior_runs"][0]["run_id"] == "prior-1"
    assert cp["prior_runs"][0]["outcomes"][_CONCEPT] == 0.88
    assert "similarity" in cp["prior_runs"][0]        # receipt carries the ranking similarity
    assert cp["stance"] == "balanced"


def test_cross_run_prior_never_changes_the_gate_decision(tmp_path):
    # §21.7 / CODEX #13: the SELECTION decision must be byte-identical whether or not a matching prior
    # capsule exists — cross-run is audit-only. Same run + idea; only the presence of a prior differs.
    s = _run_with_cached_concept(tmp_path, _CONCEPT)
    idea = Idea(operator="improve", params={"rank": 8.0}, rationale="a materially different hard-neg scheme")

    def _decide(with_prior):
        mem = tmp_path / ("w" if with_prior else "wo"); mem.mkdir()
        if with_prior:
            _seed_prior(mem, _CONCEPT)
        eng = _GateEngine(s, mem)
        eng._reflect_client = lambda: _IdeaReflectClient([_CONCEPT])
        return eng._graded_novelty_precheck(fold(s.read_all()), idea)

    assert _decide(with_prior=True) is _decide(with_prior=False)   # decision independent of the prior


def test_cross_run_flag_off_is_a_no_op(tmp_path):
    mem = tmp_path / "mem"; mem.mkdir()
    _seed_prior(mem, _CONCEPT)
    s = _run_with_cached_concept(tmp_path, _CONCEPT)
    eng = _GateEngine(s, mem)
    eng._cross_run_concepts = False                  # the off-switch
    eng._reflect_client = lambda: _IdeaReflectClient([_CONCEPT])
    idea = Idea(operator="improve", params={"rank": 8.0}, rationale="a materially different hard-neg scheme")
    eng._graded_novelty_precheck(fold(s.read_all()), idea)
    assert fold(s.read_all()).cross_run_priors == []   # no surface when off


def test_dissimilar_prior_does_not_surface(tmp_path):
    mem = tmp_path / "mem"; mem.mkdir()
    # prior capsule with the concept but a WILDLY different fingerprint (goal keywords disjoint + different
    # metric) -> below the similarity floor -> not loaded -> no surface.
    fp = task_fingerprint("dataset", "min", "forecast electricity demand timeseries", "rmse", universal=True)
    ConceptCapsuleStore(mem / "concept_capsules.jsonl").add(build_concept_capsule(
        run_id="unrelated", fingerprint=fp, direction="min", concepts=[_CONCEPT], best_metric=1.0))
    s = _run_with_cached_concept(tmp_path, _CONCEPT)
    eng = _GateEngine(s, mem)
    eng._reflect_client = lambda: _IdeaReflectClient([_CONCEPT])
    idea = Idea(operator="improve", params={"rank": 8.0}, rationale="hard-neg scheme")
    eng._graded_novelty_precheck(fold(s.read_all()), idea)
    assert fold(s.read_all()).cross_run_priors == []


# --------------------------------------------------------------------------- #
# Replay-safety of the audit event
# --------------------------------------------------------------------------- #

def test_cross_run_prior_event_folds_audit_only(tmp_path):
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "dr", "goal": "g", "direction": "max"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {"t": 1.0}, "theme": "x"}})
    s.append("node_evaluated", {"node_id": 0, "metric": 0.7})
    s.append("cross_run_prior", {"node_id": 1, "matched_concepts": ["c"],
                                 "prior_runs": [{"run_id": "p", "best_metric": 0.9, "concepts": ["c"]}]})
    st = fold(s.read_all())
    assert len(st.cross_run_priors) == 1 and st.cross_run_priors[0]["matched_concepts"] == ["c"]
    assert st.best_node_id == 0                       # selection untouched by the audit event


def test_old_logs_fold_without_the_cross_run_field(tmp_path):
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "t", "task_id": "dr", "goal": "g", "direction": "max"})
    assert fold(s.read_all()).cross_run_priors == []


def test_settings_flag_defaults_off():
    assert Settings().cross_run_concepts is False
