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


def test_experiments_digest_excludes_tombstoned_nodes():
    # §6.3: tombstoned (logically-deleted) nodes are invisible to selection; the always-on Researcher
    # context (fail count, failures list, theme rollup) must hide them too — else a deleted dead-end
    # keeps steering proposals. The winners path already excludes them via feasible_nodes().
    from looplab.events.digest import theme_rollup
    live_fail = _node(1, op="improve", status=NodeStatus.failed)
    dead_fail = _node(2, op="improve", status=NodeStatus.failed)
    dead_fail.tombstoned = True
    st = _state([_node(0, metric=1.0), live_fail, dead_fail])
    st.best_node_id = 0
    st.nodes[1].idea.theme = "beta"
    st.nodes[2].idea.theme = "beta"          # tombstoned -> must NOT add to the 'beta' rollup
    assert "1 failed" in experiments_digest(st).splitlines()[1]     # only the live failure counted
    assert theme_rollup(st).get("beta", {}).get("count") == 1       # tombstoned 'beta' node excluded


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


def test_consolidate_noted_never_retires_a_verdict():
    # WRITE-path neutrality of "noted" (matching the read-path test below): a newer untagged
    # reflection ("noted") must NOT become the merged verdict and zero the evidence of a real
    # "supported" row — pre-fix, newest-wins let one tag-noncompliant line retire the verdict.
    rows = [
        {"statement": "Deeper trees help", "outcome": "supported", "task_id": "t", "run_id": "R"},
        {"statement": "deeper trees help", "outcome": "noted", "task_id": "t", "run_id": "S"},
    ]
    out = consolidate_lessons(rows)
    assert len(out) == 1
    assert out[0]["outcome"] == "supported"       # the verdict survives the newer neutral duplicate
    assert out[0]["evidence_count"] == 1          # its evidence is kept, not zeroed
    # a group of ONLY noted rows keeps "noted" (there is no verdict to protect)
    only = [{"statement": "wider nets help", "outcome": "noted", "task_id": "t", "run_id": "R"},
            {"statement": "wider nets  help", "outcome": "noted", "task_id": "t", "run_id": "S"}]
    out2 = consolidate_lessons(only)
    assert out2[0]["outcome"] == "noted" and out2[0]["evidence_count"] == 2


def test_consolidate_legacy_and_unknown_outcomes_are_inert():
    # `_verdict_base` closes the legacy hole: a row with NO `outcome` at all (written before the
    # field existed) and an UNRECOGNIZED outcome string are both inert exactly like "noted" —
    # neither is a re-adjudication of the claim, so a NEWER one must not become the merged verdict
    # and zero the supported row's accumulated evidence.
    rows = [
        {"statement": "Deeper trees help", "outcome": "supported", "task_id": "t", "run_id": "R",
         "evidence_count": 2},
        {"statement": "deeper trees help", "task_id": "t", "run_id": "S"},             # legacy: no outcome
        {"statement": "deeper trees  help", "outcome": "inconclusive", "task_id": "t",  # unknown string
         "run_id": "U"},
    ]
    out = consolidate_lessons(rows)
    assert len(out) == 1
    assert out[0]["outcome"] == "supported"       # the verdict survives the newer inert rows
    assert out[0]["evidence_count"] == 2          # …with its evidence; inert rows add no support


def test_agentic_merge_base_skips_noted(monkeypatch):
    # The paraphrase-merge pass applies the same rule: the newest NON-noted member carries the
    # verdict/fields for the merged row (a newer "noted" paraphrase must not retire "supported").
    import looplab.search.hybrid_merge as hm
    from looplab.engine.memory import _agentic_merge_lessons
    rows = [
        {"statement": "raise the learning rate", "outcome": "supported", "task_id": "t",
         "evidence_count": 2},
        {"statement": "increase the learning rate", "outcome": "noted", "task_id": "t",
         "evidence_count": 1},
    ]
    monkeypatch.setattr(hm, "consolidate",
                        lambda texts, client, **kw: [{"members": [0, 1], "merged": "increase the LR"}])
    out = _agentic_merge_lessons(rows, client=object())
    assert len(out) == 1
    assert out[0]["outcome"] == "supported" and out[0]["statement"] == "increase the LR"
    assert out[0]["evidence_count"] == 2          # only members AGREEING with the verdict add support


def test_agentic_merge_legacy_outcomeless_row_is_inert(monkeypatch):
    # The paraphrase pass shares `_verdict_base` too: a NEWER legacy member without `outcome` must
    # not carry the merged row (pre-fix, the `outcome != "noted"` scan let it win and drop the
    # "supported" verdict + its evidence).
    import looplab.search.hybrid_merge as hm
    from looplab.engine.memory import _agentic_merge_lessons
    rows = [
        {"statement": "raise the learning rate", "outcome": "supported", "task_id": "t",
         "evidence_count": 2},
        {"statement": "increase the learning rate", "task_id": "t", "evidence_count": 1},
    ]
    monkeypatch.setattr(hm, "consolidate",
                        lambda texts, client, **kw: [{"members": [0, 1], "merged": "increase the LR"}])
    out = _agentic_merge_lessons(rows, client=object())
    assert len(out) == 1
    assert out[0]["outcome"] == "supported" and out[0]["evidence_count"] == 2


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
    # Lessons are LLM-authored ONLY now: with no LLM client wired, this toy run writes the offline
    # winner record — NOT the raw hypothesis dumped verbatim (that produced look-alike-hypothesis
    # noise; a real run's LLM reflection consolidates the hypothesis+Δ record into one lesson/theme).
    # The supported hypothesis is still captured as an auto-SKILL (keys off h.statement, unchanged).
    skills = list((mem / "skills").glob("auto-*.md"))
    assert skills, "supported hypothesis with positive delta should distill an auto-skill"
    assert any("deeper trees" in s.read_text() for s in skills)       # the hypothesis technique captured


def test_lessons_route_by_role(tmp_path):
    # §role-split: the Researcher's prior gets only R&D (+legacy) lessons; the Developer's only its own
    # code-fix (+legacy) lessons — the two contexts stay separate. Untagged (legacy) lessons are shared.
    from looplab.engine.orchestrator import Engine
    from looplab.engine.lessons import LESSON_ROLE_DEVELOPER, LESSON_ROLE_RESEARCHER
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.search.policy import GreedyTree

    class _T:
        id = "role-task"; goal = "g"; direction = "max"; kind = "dataset"
        def model_dump(self, mode="json"): return {"id": self.id}

    class _R:
        def propose(self, state, parent): return Idea(operator="draft", params={"x": 1.0})

    class _D:
        def implement(self, idea): return "import json; print(json.dumps({'metric': 0.5}))"

    mem = tmp_path / "memory"
    mem.mkdir()
    rows = [
        {"task_id": "role-task", "fingerprint": [], "statement": "deeper trees help",
         "outcome": "supported", "run_id": "r1", "role": LESSON_ROLE_RESEARCHER},
        {"task_id": "role-task", "fingerprint": [], "statement": "guard empty input fixed the crash",
         "outcome": "supported", "run_id": "r1", "role": LESSON_ROLE_DEVELOPER},
        {"task_id": "role-task", "fingerprint": [], "statement": "a legacy shared lesson",
         "outcome": "supported", "run_id": "r1"},                      # untagged -> shared
    ]
    (mem / "lessons.jsonl").write_text("".join(json.dumps(o) + "\n" for o in rows))
    eng = Engine(tmp_path / "run", task=_T(), researcher=_R(), developer=_D(),
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                 memory_dir=str(mem), reflection_priors=True)
    res = eng._load_reflection_priors(role=LESSON_ROLE_RESEARCHER)
    assert "deeper trees" in res and "legacy shared" in res           # researcher + shared
    assert "guard empty input" not in res                            # NOT the developer's
    dev = eng._load_reflection_priors(role=LESSON_ROLE_DEVELOPER)
    assert "guard empty input" in dev and "legacy shared" in dev      # developer + shared
    assert "deeper trees" not in dev                                 # NOT the researcher's
    # and the developer lessons ride the idea handed to the Developer (via _directed_idea), never the
    # researcher's — while the Researcher's proposal prior carries the R&D ones (hint += _prior_note_text)
    from looplab.core.models import RunState
    eng._dev_prior_note_text = dev
    di = eng._directed_idea(Idea(operator="draft", params={}, rationale="base"), RunState())
    assert "guard empty input" in di.rationale and "deeper trees" not in di.rationale
