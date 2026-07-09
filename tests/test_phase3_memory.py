"""Phase 3 (docs/12): operator-scoped memory, insight backpropagation, scaling digest,
memory hygiene (consolidation / contradiction quarantine), auto-distilled skills,
run-level ablation attribution."""
from __future__ import annotations

import json

import pytest

from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.events.digest import (ablation_attribution, ancestral_repair_chain, auto_char_cap,
                                   experiments_digest, lineage_lessons, sibling_digest)
from looplab.engine.memory import (consolidate_lessons, filter_contradicted, skill_slug,
                                   write_auto_skill)


def _node(nid, metric=None, op="draft", parents=(), status=NodeStatus.evaluated,
          rationale="", error="", error_reason=""):
    n = Node(id=nid, parent_ids=list(parents), operator=op,
             idea=Idea(operator=op, params={"x": float(nid)}, rationale=rationale),
             metric=metric, status=status)
    n.error, n.error_reason = error, error_reason
    return n


def _state(nodes, direction="max"):
    st = RunState(direction=direction)
    st.nodes = {n.id: n for n in nodes}
    return st


# --------------------------------------------------------------------------- #
# 3.1 operator-scoped memory
# --------------------------------------------------------------------------- #

def test_sibling_digest_for_improve_lists_other_children():
    p = _node(0, metric=0.5)
    a = _node(1, metric=0.6, op="improve", parents=(0,), rationale="tried deeper trees")
    b = _node(2, metric=None, op="improve", parents=(0,), status=NodeStatus.failed,
              rationale="tried a transformer", error_reason="oom")
    st = _state([p, a, b])
    out = sibling_digest(st, p)
    assert "push diversity" in out and "#1" in out and "#2" in out
    assert "tried deeper trees" in out


def test_sibling_digest_for_draft_lists_other_roots():
    a = _node(0, metric=0.6, rationale="baseline linear model with standard features")
    b = _node(1, metric=0.4, rationale="gradient boosting on raw features")
    st = _state([a, b])
    out = sibling_digest(st, None)
    assert "#0" in out and "#1" in out


def test_sibling_digest_empty_when_no_siblings():
    st = _state([_node(0, metric=0.5)])
    assert sibling_digest(st, st.nodes[0]) == ""


def test_ancestral_repair_chain_surfaces_prior_fixes():
    root = _node(0, metric=None, status=NodeStatus.failed, error="ImportError: no torch",
                 error_reason="crash")
    fix1 = _node(1, metric=0.5, op="debug", parents=(0,), error="ImportError: no torch")
    child = _node(2, metric=None, op="improve", parents=(1,), status=NodeStatus.failed,
                  error="ValueError: bad shape", error_reason="crash")
    st = _state([root, fix1, child])
    out = ancestral_repair_chain(st, child)
    assert "Prior repairs" in out and "#1 debug" in out and "fixed, metric=0.5" in out


# --------------------------------------------------------------------------- #
# 3.2 insight backpropagation
# --------------------------------------------------------------------------- #

def test_lineage_lessons_ranked_by_delta():
    p = _node(0, metric=0.5)
    win = _node(1, metric=0.9, op="improve", parents=(0,), rationale="added feature crosses")
    lose = _node(2, metric=0.45, op="improve", parents=(0,), rationale="dropped regularization")
    fail = _node(3, metric=None, op="improve", parents=(0,), status=NodeStatus.failed,
                 rationale="swapped to GPU kernel", error_reason="crash")
    st = _state([p, win, lose, fail])
    out = lineage_lessons(st, p)
    assert "Lessons from this lineage" in out
    assert out.index("#1") < out.index("#2")          # biggest |delta| first
    assert "improved" in out and "regressed" in out and "FAILED" in out


def test_lineage_lessons_empty_for_draft():
    assert lineage_lessons(_state([_node(0, metric=1.0)]), None) == ""


# --------------------------------------------------------------------------- #
# 3.3 scaling digest
# --------------------------------------------------------------------------- #

def test_auto_char_cap_scales_with_run():
    small = _state([_node(i, metric=float(i)) for i in range(5)])
    big = _state([_node(i, metric=float(i)) for i in range(150)])
    assert auto_char_cap(small) == 1200
    assert auto_char_cap(big) == 6000
    # digest honors the auto cap (no crash, bounded)
    out = experiments_digest(big)
    assert 0 < len(out) <= 6002


# --------------------------------------------------------------------------- #
# 3.6 ablation attribution
# --------------------------------------------------------------------------- #

def test_ablation_attribution_aggregates_across_probes():
    st = _state([_node(0, metric=1.0)])
    st.ablations = [{"parent_id": 0, "impacts": {"features": 0.3, "model": 0.1}},
                    {"parent_id": 1, "impacts": {"features": 0.2}}]
    attr = ablation_attribution(st)
    assert list(attr)[0] == "features"
    assert attr["features"]["impact"] == pytest.approx(0.5)
    assert attr["features"]["n"] == 2
    out = experiments_digest(st)
    assert "Component attribution" in out


# --------------------------------------------------------------------------- #
# D2 memory hygiene
# --------------------------------------------------------------------------- #

def test_consolidate_merges_duplicates_and_counts_evidence():
    rows = [
        {"statement": "Deeper trees help", "outcome": "supported", "task_id": "t"},
        {"statement": "deeper trees   help", "outcome": "supported", "task_id": "t"},
    ]
    out = consolidate_lessons(rows)
    assert len(out) == 1 and out[0]["evidence_count"] == 2


def test_consolidate_dedups_evidence_by_run_id():
    # A single run that re-reflects (a reopened + budget-extended run re-enters finalize and re-appends
    # its own lessons) must NOT inflate evidence_count: the same run agreeing with itself counts ONCE.
    rows = [
        {"statement": "Deeper trees help", "outcome": "supported", "task_id": "t", "run_id": "R"},
        {"statement": "deeper trees help", "outcome": "supported", "task_id": "t", "run_id": "R"},
    ]
    assert consolidate_lessons(rows)[0]["evidence_count"] == 1        # same run twice -> 1
    # two DISTINCT runs still accumulate genuine cross-run support
    rows2 = rows + [{"statement": "Deeper trees help", "outcome": "supported", "task_id": "t", "run_id": "S"}]
    assert consolidate_lessons(rows2)[0]["evidence_count"] == 2       # {R, S} -> 2
    # a pre-consolidated row (evidence_count>1) keeps its stored weight even sharing a run_id
    rows3 = [{"statement": "X", "outcome": "supported", "task_id": "t", "run_id": "R", "evidence_count": 3},
             {"statement": "X", "outcome": "supported", "task_id": "t", "run_id": "R"}]
    assert consolidate_lessons(rows3)[0]["evidence_count"] == 3       # 3 (folded) + deduped fresh R (0) = 3


def test_consolidate_newest_verdict_wins():
    rows = [
        {"statement": "Deeper trees help", "outcome": "supported", "task_id": "t"},
        {"statement": "Deeper trees help", "outcome": "abandoned", "task_id": "t"},
    ]
    out = consolidate_lessons(rows)
    assert len(out) == 1
    assert out[0]["outcome"] == "abandoned"           # forgetting: stale verdict retired
    assert out[0]["evidence_count"] == 1              # the old CONFLICTING one adds no support


def test_filter_contradicted_quarantines_reversed_claims():
    scored = [
        (0.9, 0, {"statement": "Deeper trees help", "outcome": "supported"}),
        (0.9, 5, {"statement": "deeper trees help", "outcome": "tested"}),   # newer, reversed
        (0.8, 2, {"statement": "More data helps", "outcome": "supported"}),
    ]
    kept = filter_contradicted(scored)
    stmts = [(o["statement"], o["outcome"]) for _, _, o in kept]
    assert ("Deeper trees help", "supported") not in stmts   # old positive quarantined
    assert ("deeper trees help", "tested") in stmts          # newer negative stays
    assert ("More data helps", "supported") in stmts


def test_filter_contradicted_neutral_noted_never_quarantines():
    # "noted" (an UNTAGGED reflection line, parse_credit_lessons) is neutral by design: a newer
    # "noted" duplicate must NOT quarantine an older "supported" claim (pre-fix, untagged lines
    # defaulted to "tested" ∈ _NEGATIVE and could), and a newer "supported" keeps the noted row too.
    scored = [
        (0.9, 0, {"statement": "Deeper trees help", "outcome": "supported"}),
        (0.9, 5, {"statement": "deeper trees help", "outcome": "noted"}),    # newer but NEUTRAL
    ]
    stmts = [(o["statement"], o["outcome"]) for _, _, o in filter_contradicted(scored)]
    assert ("Deeper trees help", "supported") in stmts       # NOT quarantined by the neutral row
    assert ("deeper trees help", "noted") in stmts


# --------------------------------------------------------------------------- #
# M4 auto-skills
# --------------------------------------------------------------------------- #

def test_auto_skill_candidate_then_promoted(tmp_path):
    fp_a = ["kind:dataset", "dir:max", "churn"]
    fp_b = ["kind:repo", "dir:min", "latency"]      # a DIFFERENT task fingerprint
    p1 = write_auto_skill(tmp_path, "Target-encode categoricals", "body A", fp_a, "task-a")
    assert p1 and p1.exists()
    text1 = p1.read_text(encoding="utf-8")
    assert "status: candidate" in text1 and "provenance: auto" in text1
    p2 = write_auto_skill(tmp_path, "Target-encode categoricals", "body B", fp_b, "task-b")
    text2 = p2.read_text(encoding="utf-8")
    assert p2 == p1 and "status: promoted" in text2   # won on a 2nd distinct fingerprint


def test_auto_skill_same_fingerprint_stays_candidate(tmp_path):
    fp = ["kind:dataset", "dir:max", "churn"]
    write_auto_skill(tmp_path, "Use early stopping", "body", fp, "t")
    p = write_auto_skill(tmp_path, "Use early stopping", "body", fp, "t")
    assert "status: candidate" in p.read_text(encoding="utf-8")


def test_skill_slug_stable():
    assert skill_slug("Target-encode categoricals!") == skill_slug(" target-encode   categoricals")


# --------------------------------------------------------------------------- #
# End-to-end: run-end reflection writes hygienic lessons + auto-skill
# --------------------------------------------------------------------------- #

def test_reflection_writes_consolidated_lessons_and_skill(tmp_path):
    import anyio

    from looplab.engine.orchestrator import Engine
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.search.policy import GreedyTree

    class _T:
        id = "mem-task"; goal = "maximize accuracy on churn"; direction = "max"; kind = "dataset"
        def model_dump(self, mode="json"):
            return {"id": self.id}

    class _R:
        def __init__(self):
            self.i = 0
        def propose(self, state, parent):
            self.i += 1
            return Idea(operator="draft", params={"x": float(self.i)},
                        rationale=f"attempt {self.i}",
                        hypothesis="deeper trees capture the interaction structure")

    class _D:
        def implement(self, idea):
            return f"import json; print(json.dumps({{'metric': {0.5 + 0.1 * idea.params['x']}}}))"

    mem = tmp_path / "memory"
    eng = Engine(tmp_path / "run", task=_T(), researcher=_R(), developer=_D(),
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=3),
                 memory_dir=str(mem), reflection_priors=True)
    anyio.run(eng.run)
    lessons = [json.loads(line) for line in (mem / "lessons.jsonl").read_text().splitlines()]
    assert lessons and all("run_id" in lz for lz in lessons)          # D2 provenance
    hyp = [lz for lz in lessons if "deeper trees" in lz["statement"]]
    assert hyp and hyp[0].get("evidence")                             # node ids recorded
    skills = list((mem / "skills").glob("auto-*.md"))
    assert skills, "supported hypothesis with positive delta should distill an auto-skill"
