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
    mem = tmp_path / "mem"
    mem.mkdir()
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


def test_concept_capsule_records_direction_normalized_profit_signs():
    # PART V Phase 1: each concept gets a SCALE-FREE, direction-normalized sign vs the run's OWN median
    # outcome (+1 helped / 0 neutral / -1 hurt) — the per-run signal that legitimately aggregates cross-run.
    max_cap = build_concept_capsule(run_id="r", fingerprint=["k"], direction="max",
                                    concepts=["loss/a", "loss/b", "loss/c"],
                                    concept_outcomes={"loss/a": 0.9, "loss/b": 0.5, "loss/c": 0.3})
    assert max_cap["concept_signs"] == {"loss/a": 1, "loss/b": 0, "loss/c": -1}   # median 0.5, higher=better
    min_cap = build_concept_capsule(run_id="r2", fingerprint=["k"], direction="min",
                                    concepts=["loss/a", "loss/b", "loss/c"],
                                    concept_outcomes={"loss/a": 0.9, "loss/b": 0.5, "loss/c": 0.3})
    assert min_cap["concept_signs"] == {"loss/a": -1, "loss/b": 0, "loss/c": 1}   # direction flips the sign
    one = build_concept_capsule(run_id="r3", fingerprint=["k"], direction="max",
                                concepts=["loss/a"], concept_outcomes={"loss/a": 0.9})
    assert one["concept_signs"] == {}                                            # <2 outcomes -> no signal


def test_profit_signs_neutral_band_keeps_near_median_concepts_off_both_sides():
    # REVIEW: without a neutral band a median split forces ~half the concepts onto each side every run (a
    # weak, self-balancing signal). A concept within a fraction of the run's SPREAD around the median is 0.
    # Field: 0.0 .. 1.0 (spread 1.0, band 0.1); the two mid concepts at 0.48/0.52 sit inside the band.
    cap = build_concept_capsule(run_id="r", fingerprint=["k"], direction="max",
                                concepts=["hi", "midlo", "midhi", "lo"],
                                concept_outcomes={"hi": 1.0, "midhi": 0.52, "midlo": 0.48, "lo": 0.0})
    signs = cap["concept_signs"]
    assert signs["hi"] == 1 and signs["lo"] == -1                 # clearly better / worse half
    assert signs["midhi"] == 0 and signs["midlo"] == 0           # within the neutral band -> no forced side


def test_portfolio_overview_net_combines_opposite_signs_on_canonical_collapse():
    # REVIEW: when aliased raws collapse to ONE canonical in a run, opposite signs must COMBINE by net, not
    # let the sorted-first raw silently win. Two raws alias to loss/x; in r1 both help (net +) -> helped.
    from looplab.engine.memory import portfolio_concept_overview

    caps = [build_concept_capsule(run_id="r1", fingerprint=["k"], direction="max",
                                  concepts=["loss/a", "loss/b", "loss/lo"],
                                  concept_outcomes={"loss/a": 0.9, "loss/b": 0.8, "loss/lo": 0.1})]
    # alias loss/a and loss/b -> loss/x (both landed in the better half -> net helped)
    aliases = {"loss/a": "loss/x", "loss/b": "loss/x"}
    rows = {e["concept"]: e for e in portfolio_concept_overview(caps, aliases=aliases)["concepts"]}
    assert "loss/x" in rows and rows["loss/x"]["n_helped"] == 1 and rows["loss/x"]["n_hurt"] == 0


def test_portfolio_concept_graph_builds_cross_run_cooccurrence_and_paths():
    # PART V Phase 4/5: the GLOBAL cross-run concept map — nodes with run counts, is_a path spine, and
    # co_occurs edges weighted by DISTINCT runs a pair appeared together in (min_cooccurrence gate).
    from looplab.engine.memory import portfolio_concept_graph
    caps = [
        build_concept_capsule(run_id="r1", fingerprint=["k"], direction="max",
                              concepts=["loss/contrastive/dcl", "arch/moe"], concept_outcomes={}),
        build_concept_capsule(run_id="r2", fingerprint=["k"], direction="max",
                              concepts=["loss/contrastive/dcl", "arch/moe"], concept_outcomes={}),
        build_concept_capsule(run_id="r3", fingerprint=["k"], direction="max",
                              concepts=["loss/contrastive/dcl", "data/aug"], concept_outcomes={}),
    ]
    g = portfolio_concept_graph(caps, min_cooccurrence=2)
    cruns = {c["concept"]: c["n_runs"] for c in g["concepts"]}
    assert cruns["loss/contrastive/dcl"] == 3 and cruns["arch/moe"] == 2
    edges = {(e["src"], e["rel"], e["dst"]) for e in g["edges"]}
    assert ("loss/contrastive/dcl", "is_a", "loss/contrastive") in edges     # path spine
    assert ("loss/contrastive", "is_a", "loss") in edges                     # materialized ancestor
    assert ("arch/moe", "is_a", "arch") in edges
    cooc = {(e["src"], e["dst"]): e["n_runs"] for e in g["edges"] if e["rel"] == "co_occurs"}
    assert cooc[("arch/moe", "loss/contrastive/dcl")] == 2                   # co-occurred in r1 + r2 (sorted pair)
    assert ("data/aug", "loss/contrastive/dcl") not in cooc                  # only 1 run -> below min_cooccurrence


def test_portfolio_concept_graph_honors_alias_governance_and_is_deterministic():
    from looplab.engine.memory import portfolio_concept_graph
    caps = [build_concept_capsule(run_id=f"r{i}", fingerprint=["k"], direction="max",
                                  concepts=["loss/a", "loss/b"], concept_outcomes={}) for i in range(3)]
    aliases = {"loss/b": "loss/a"}                          # merge b into a -> the pair collapses to one node
    g = portfolio_concept_graph(caps, aliases=aliases, min_cooccurrence=2)
    names = {c["concept"] for c in g["concepts"]}
    assert "loss/a" in names and "loss/b" not in names     # aliased away
    # deterministic: same input -> byte-identical result
    assert g == portfolio_concept_graph(caps, aliases=aliases, min_cooccurrence=2)


def test_portfolio_overview_rolls_up_help_hurt_counts_across_runs():
    from looplab.engine.memory import portfolio_concept_overview
    caps = [
        build_concept_capsule(run_id="r1", fingerprint=["k"], direction="max",
                              concepts=["loss/a", "loss/b", "loss/c"],
                              concept_outcomes={"loss/a": 0.9, "loss/b": 0.5, "loss/c": 0.3}),
        build_concept_capsule(run_id="r2", fingerprint=["k"], direction="max",
                              concepts=["loss/a", "loss/b", "loss/c"],
                              concept_outcomes={"loss/a": 0.8, "loss/b": 0.5, "loss/c": 0.2}),
    ]
    rows = {e["concept"]: e for e in portfolio_concept_overview(caps)["concepts"]}
    assert rows["loss/a"]["n_helped"] == 2 and rows["loss/a"]["n_hurt"] == 0     # consistent helper
    assert rows["loss/c"]["n_hurt"] == 2 and rows["loss/c"]["n_helped"] == 0     # consistent hurter
    assert rows["loss/b"]["n_neutral"] == 2                                      # always the median


def test_context_pack_surfaces_consistent_help_hurt_tendency_advisory_only():
    from looplab.engine.claims import build_context_pack, render_context_pack
    overview = {"n_runs": 2, "n_concepts": 3, "concepts": [
        {"concept": "loss/a", "n_runs": 2, "n_helped": 2, "n_neutral": 0, "n_hurt": 0},
        {"concept": "loss/c", "n_runs": 2, "n_helped": 0, "n_neutral": 0, "n_hurt": 2},
        {"concept": "loss/b", "n_runs": 2, "n_helped": 1, "n_neutral": 0, "n_hurt": 1},  # mixed -> neither
    ]}
    pack = build_context_pack([], concept_overview=overview)
    # E3: helps/hurts carry the run count (n_helped/n_hurt) so the tendency's strength is visible.
    assert pack["coverage"]["helps"] == ["loss/a (n=2)"] and pack["coverage"]["hurts"] == ["loss/c (n=2)"]
    text = render_context_pack(pack)
    assert "RANK BETTER" in text and "loss/a" in text
    assert "RANK WORSE" in text and "loss/c" in text
    assert "advisory, NOT a rule" in text                   # never a selection input


def test_capsule_validation_guards_profit_signs():
    from looplab.engine.memory import _valid_capsule_record
    good = build_concept_capsule(run_id="r", fingerprint=["k"], direction="max",
                                 concepts=["a", "b"], concept_outcomes={"a": 1.0, "b": 0.0})
    assert good["concept_signs"] == {"a": 1, "b": -1} and _valid_capsule_record(good)
    legacy = {k: v for k, v in good.items() if k != "concept_signs"}    # old v2 capsule, no field
    assert _valid_capsule_record(legacy)                                # additive -> still valid
    assert not _valid_capsule_record({**good, "concept_signs": {"a": 2}})      # out of range
    assert not _valid_capsule_record({**good, "concept_signs": {"a": True}})   # bool (int subclass)
    assert not _valid_capsule_record({**good, "concept_signs": "nope"})        # not a dict


def test_concept_outcomes_use_selection_eligible_nodes_but_keep_attempt_coverage(tmp_path):
    mem = tmp_path / "mem"
    mem.mkdir()
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {
        "run_id": "r", "task_id": "t", "goal": "g", "direction": "max", "trust_gate": "gate",
    })
    concepts = {
        0: ["shared", "valid-only"],
        1: ["shared", "flagged-only"],
        2: ["tombstoned-only"],
        3: ["aborted-only"],
        4: ["infeasible-only"],
    }
    for node_id, metric in enumerate((0.5, 0.99, 0.98, 0.97, 0.96)):
        store.append("node_created", {
            "node_id": node_id, "parent_ids": [], "operator": "draft",
            "idea": {"operator": "draft", "params": {"x": node_id}, "theme": "x"},
        })
        store.append("node_evaluated", {
            "node_id": node_id, "metric": metric,
            "violations": ([{"name": "budget"}] if node_id == 4 else []),
        })
        store.append("node_concepts", {
            "node_id": node_id, "concepts": concepts[node_id], "mode": "llm",
        })
    store.append("reward_hack_suspected", {
        "node_id": 1, "signals": [{"signal": "grader_access"}],
    })
    store.append("node_tombstoned", {"node_ids": [2]})
    store.append("node_abort", {"node_id": 3})

    LessonMemory(_fake_engine(mem)).store_concept_capsule(fold(store.read_all()))
    capsule = ConceptCapsuleStore(mem / "concept_capsules.jsonl").all()[0]
    all_concepts = set().union(*map(set, concepts.values()))
    assert all_concepts <= set(capsule["concepts"])
    assert capsule["concept_outcomes"] == {"shared": 0.5, "valid-only": 0.5}


def test_store_concept_capsule_noop_without_tags(tmp_path):
    mem = tmp_path / "mem"
    mem.mkdir()
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {}, "theme": "x"}})
    s.append("node_evaluated", {"node_id": 0, "metric": 0.5})
    LessonMemory(_fake_engine(mem)).store_concept_capsule(fold(s.read_all()))
    assert not (mem / "concept_capsules.jsonl").exists()   # nothing tagged -> no capsule


def test_store_concept_capsule_ignores_researcher_authored_claims(tmp_path):
    mem = tmp_path / "mem"
    mem.mkdir()
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})
    s.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "concepts": ["claimed/breakthrough"]},
    })
    s.append("node_evaluated", {"node_id": 0, "metric": 0.5})

    LessonMemory(_fake_engine(mem)).store_concept_capsule(fold(s.read_all()))

    assert not (mem / "concept_capsules.jsonl").exists()


def test_store_concept_capsule_ignores_offline_heuristic_memberships(tmp_path):
    """A local alias match is useful UI taxonomy, not independent cross-run evidence."""
    mem = tmp_path / "mem"
    mem.mkdir()
    s = EventStore(tmp_path / "events.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "max"})
    s.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {}, "rationale": "contrastive temperature"},
    })
    s.append("node_evaluated", {"node_id": 0, "metric": 0.5})
    s.append("node_concepts", {
        "node_id": 0, "concepts": ["claimed/by-alias"], "mode": "offline-heuristic",
    })

    state = fold(s.read_all())
    assert state.node_concepts[0] == ["claimed/by-alias"]
    LessonMemory(_fake_engine(mem)).store_concept_capsule(state)

    assert not (mem / "concept_capsules.jsonl").exists()


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
    mem = tmp_path / "mem"
    mem.mkdir()
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
        mem = tmp_path / ("w" if with_prior else "wo")
        mem.mkdir()
        if with_prior:
            _seed_prior(mem, _CONCEPT)
        eng = _GateEngine(s, mem)
        eng._reflect_client = lambda: _IdeaReflectClient([_CONCEPT])
        return eng._graded_novelty_precheck(fold(s.read_all()), idea)

    assert _decide(with_prior=True) is _decide(with_prior=False)   # decision independent of the prior


def test_cross_run_flag_off_is_a_no_op(tmp_path):
    mem = tmp_path / "mem"
    mem.mkdir()
    _seed_prior(mem, _CONCEPT)
    s = _run_with_cached_concept(tmp_path, _CONCEPT)
    eng = _GateEngine(s, mem)
    eng._cross_run_concepts = False                  # the off-switch
    eng._reflect_client = lambda: _IdeaReflectClient([_CONCEPT])
    idea = Idea(operator="improve", params={"rank": 8.0}, rationale="a materially different hard-neg scheme")
    eng._graded_novelty_precheck(fold(s.read_all()), idea)
    assert fold(s.read_all()).cross_run_priors == []   # no surface when off


def test_dissimilar_prior_does_not_surface(tmp_path):
    mem = tmp_path / "mem"
    mem.mkdir()
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


def test_cross_run_direction_gate_suppresses_opposite_direction_prior(tmp_path):
    # The HARD direction gate (novelty._cross_run_prior) in ISOLATION: a prior with the SAME goal/metric/
    # kind but the OPPOSITE optimization direction differs only in the `dir:` fingerprint token, so it still
    # clears the fuzzy Jaccard floor — yet a min/rmse outcome is NOT comparable to this max/recall run, so
    # the gate must drop it. Same run + concept; ONLY the prior's direction differs, and that alone flips
    # surface on/off (test_dissimilar_prior_does_not_surface can't pin this — it fails the sim floor first).
    def _n_surfaced(prior_direction):
        d = tmp_path / prior_direction
        d.mkdir()
        mem = d / "mem"
        mem.mkdir()
        fp = task_fingerprint("dataset", prior_direction, "dense retrieval", "recall", universal=True)
        ConceptCapsuleStore(mem / "concept_capsules.jsonl").add(build_concept_capsule(
            run_id="prior-dir", fingerprint=fp, direction=prior_direction, concepts=[_CONCEPT],
            best_metric=0.9, concept_outcomes={_CONCEPT: 0.9}))
        s = _run_with_cached_concept(d, _CONCEPT)                  # run direction = "max"
        eng = _GateEngine(s, mem)
        eng._reflect_client = lambda: _IdeaReflectClient([_CONCEPT])
        idea = Idea(operator="improve", params={"rank": 8.0}, rationale="hard-neg scheme")
        eng._graded_novelty_precheck(fold(s.read_all()), idea)
        return len(fold(s.read_all()).cross_run_priors)

    assert _n_surfaced("max") == 1                                 # same direction -> surfaces (control)
    assert _n_surfaced("min") == 0                                 # opposite direction -> HARD gate drops it


def test_poisoned_capsule_row_is_quarantined_not_fatal(tmp_path):
    # ConceptCapsuleStore._valid_capsule (memory.py) drops schema-poisoned rows instead of letting one bad
    # row disable the feature: a STRING `concepts` would otherwise iterate into CHARACTER concepts, and a
    # non-string/empty run_id or non-list fingerprint can poison retrieval. The VALID row must still load.
    import orjson
    path = tmp_path / "concept_capsules.jsonl"
    good = build_concept_capsule(run_id="good", fingerprint=task_fingerprint("dataset", "max", "g", "m"),
                                 direction="max", concepts=["data/hard-neg"], concept_outcomes={})
    rows = [
        {"run_id": "", "fingerprint": [], "concepts": [], "concept_outcomes": {}},   # empty run_id -> drop
        {"run_id": "poison", "fingerprint": 7, "concepts": ["ok"], "concept_outcomes": {}},  # int fp -> drop
        {"run_id": "poison2", "fingerprint": [], "concepts": "hardneg", "concept_outcomes": {}},  # str concepts -> drop
        good,
    ]
    path.write_bytes(b"\n".join(orjson.dumps(r) for r in rows) + b"\n")
    caps = ConceptCapsuleStore(path).all()
    assert [c["run_id"] for c in caps] == ["good"]                 # only the valid row survives
    assert caps[0]["concepts"] == ["data/hard-neg"]               # not poisoned into character concepts


def test_capsule_store_rejects_unknown_schema_and_bad_live_write(tmp_path):
    p = tmp_path / "concept_capsules.jsonl"
    store = ConceptCapsuleStore(p)
    unknown = build_concept_capsule(run_id="future", fingerprint=["k"], direction="max", concepts=["c"])
    unknown["v"] = 999
    assert store.add(unknown) is False and not p.exists()
    assert store.add({"run_id": "bad", "fingerprint": [], "concepts": "characters"}) is False


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


def test_settings_flag_defaults_on():
    # Part IV/V ships ON by default (concept capsules + cross-run prior audit; never rejects).
    assert Settings().cross_run_concepts is True
