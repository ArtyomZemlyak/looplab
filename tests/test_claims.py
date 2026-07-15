"""PART IV cross-run Step 4 (§21.20) — claim_assessments: lessons + D8 claims -> evidence-grounded claims.

Pins the projection that turns the shipped lesson verdicts + D8 research-memo claims into verifiable
assertions with support/oppose evidence refs and an epistemic state — the "what does the evidence suggest,
and what contradicts it" read-model. Pure/deterministic; unifies the two shipped shapes, forks neither.
"""
from __future__ import annotations

from looplab.engine.claims import claim_assessments


def _lesson(statement, outcome, evidence, *, run_id="r1", task_id="t"):
    return {"statement": statement, "outcome": outcome, "evidence": evidence,
            "run_id": run_id, "task_id": task_id}


def test_supported_lesson_becomes_a_supported_claim():
    out = claim_assessments([_lesson("hard-neg mining helps recall", "supported", [3, 5])])
    assert len(out) == 1
    c = out[0]
    assert c["epistemic"] == "supported" and c["support"] == ["r1:3", "r1:5"] and c["oppose"] == []
    assert c["runs"] == ["r1"] and c["scopes"] == ["t"]


def test_negative_verdicts_map_to_oppose():
    for verdict in ("tested", "abandoned", "failed", "refuted"):
        out = claim_assessments([_lesson("X helps", verdict, [7])])
        assert out[0]["epistemic"] == "refuted" and out[0]["oppose"] == ["r1:7"]


def test_conflicting_verdicts_make_a_mixed_claim_not_newest_wins():
    # same statement, one run supports (nodes 1,2), another opposes (node 9) -> MIXED, both sides kept.
    out = claim_assessments([
        _lesson("mnr loss helps", "supported", [1, 2], run_id="rA"),
        _lesson("mnr loss helps", "tested", [9], run_id="rB"),
    ])
    assert len(out) == 1
    c = out[0]
    assert c["epistemic"] == "mixed"
    assert c["support"] == ["rA:1", "rA:2"] and c["oppose"] == ["rB:9"]
    assert c["runs"] == ["rA", "rB"]


def test_noted_is_neutral_but_still_registers_the_run():
    out = claim_assessments([_lesson("some observation", "noted", [4], run_id="rZ")])
    c = out[0]
    assert c["epistemic"] == "inconclusive" and c["support"] == [] and c["oppose"] == []
    assert c["runs"] == ["rZ"]                       # the run is recorded, but it takes no stance


def test_evidence_is_run_scoped_so_cross_run_corroboration_counts():
    # Two INDEPENDENT runs each support the same statement citing their own run-local nodes 0,1.
    # Bare node-ids would collapse ({0,1}) and read as a single run's worth of evidence; run-qualified
    # "run:node" refs keep all four distinct, so n_support reflects genuine cross-run corroboration.
    two_runs = claim_assessments([
        _lesson("dropout helps", "supported", [0, 1], run_id="rA"),
        _lesson("dropout helps", "supported", [0, 1], run_id="rB"),
    ])[0]
    one_run = claim_assessments([_lesson("dropout helps", "supported", [0, 1], run_id="rA")])[0]
    assert two_runs["n_support"] == 4 and one_run["n_support"] == 2   # distinguishable, not collapsed
    assert two_runs["support"] == ["rA:0", "rA:1", "rB:0", "rB:1"]
    assert two_runs["runs"] == ["rA", "rB"]


def test_research_claims_contribute_support_and_sources():
    out = claim_assessments(
        [],
        research_claims=[{"statement": "doc2query expands recall", "node_ids": [11, 12],
                          "urls": ["http://x"]}])
    c = out[0]
    assert c["epistemic"] == "supported" and c["support"] == ["?:11", "?:12"]
    assert c["sources"] == ["http://x"]


def test_lesson_and_research_claim_unify_on_the_same_statement():
    # a lesson OPPOSES while a D8 memo claim SUPPORTS the same statement -> one mixed claim (not two).
    # Identity reuses the shipped `normalize_statement` (whitespace+case), so casing/spacing unify...
    out = claim_assessments(
        [_lesson("Distillation  helps", "refuted", [2])],
        research_claims=[{"statement": "distillation helps", "node_ids": [8]}])
    assert len(out) == 1                              # normalized statement collapses them
    c = out[0]
    assert c["epistemic"] == "mixed" and c["support"] == ["?:8"] and c["oppose"] == ["r1:2"]


def test_identity_matches_the_shipped_lesson_normalizer_punctuation_is_significant():
    # ...but a trailing period is NOT stripped — identity is deliberately the SAME as the lesson store's
    # `normalize_statement` (we do not fork a divergent claim normalizer), so these stay two claims.
    out = claim_assessments([
        _lesson("distillation helps", "supported", [1]),
        _lesson("distillation helps.", "supported", [2]),
    ])
    assert len(out) == 2


def test_ranking_most_evidenced_and_contested_first():
    out = claim_assessments([
        _lesson("weak claim", "supported", [1]),
        _lesson("contested claim", "supported", [1, 2], run_id="rA"),
        _lesson("contested claim", "tested", [3], run_id="rB"),
    ])
    # contested (3 evidence, has opposition) ranks before the weak single-evidence claim
    assert out[0]["statement"] == "contested claim" and out[0]["epistemic"] == "mixed"


def test_urls_are_not_treated_as_node_evidence():
    out = claim_assessments(
        [], research_claims=[{"statement": "s", "node_ids": ["4", "bad", True], "urls": ["u"]}])
    # "4" coerces to node 4; "bad"/bool dropped; url goes to sources not support
    assert out[0]["support"] == ["?:4"] and out[0]["sources"] == ["u"]


def test_empty_input_is_empty():
    assert claim_assessments([]) == []
    assert claim_assessments([{"statement": "", "outcome": "supported", "evidence": [1]}]) == []


# --------------------------------------------------------------------------- #
# CLI  (`looplab claims`)
# --------------------------------------------------------------------------- #

def _write_lessons(path, lessons):
    import orjson
    path.write_bytes(b"\n".join(orjson.dumps(l) for l in lessons) + b"\n")


def test_cli_claims_lists_and_filters_contested(tmp_path):
    from typer.testing import CliRunner
    from looplab.cli import app
    _write_lessons(tmp_path / "lessons.jsonl", [
        _lesson("hard-neg helps", "supported", [1]),
        _lesson("mnr helps", "supported", [2], run_id="rA"),
        _lesson("mnr helps", "tested", [3], run_id="rB"),
    ])
    runner = CliRunner()
    res = runner.invoke(app, ["claims", str(tmp_path)])
    assert res.exit_code == 0 and "hard-neg helps" in res.stdout and "mnr helps" in res.stdout
    # --contested keeps only the mixed claim
    res2 = runner.invoke(app, ["claims", str(tmp_path), "--contested"])
    assert res2.exit_code == 0 and "mnr helps" in res2.stdout and "hard-neg helps" not in res2.stdout


def test_cli_claims_missing_file_is_clean_error(tmp_path):
    from typer.testing import CliRunner
    from looplab.cli import app
    res = CliRunner().invoke(app, ["claims", str(tmp_path)])
    assert res.exit_code == 1 and "no lessons" in res.stdout


# --------------------------------------------------------------------------- #
# Operator claim DECISIONS (§22.4) — the governance overlay
# --------------------------------------------------------------------------- #

def test_record_and_load_decisions(tmp_path):
    from looplab.engine.claims import load_claim_decisions, record_claim_decision
    record_claim_decision(str(tmp_path), statement="hard-neg helps", decision="ratified", note="proven")
    record_claim_decision(str(tmp_path), statement="bad idea", decision="rejected")
    d = load_claim_decisions(str(tmp_path))
    from looplab.engine.memory import normalize_statement
    assert d[normalize_statement("hard-neg helps")]["decision"] == "ratified"
    assert d[normalize_statement("bad idea")]["decision"] == "rejected"


def test_decisions_last_write_wins(tmp_path):
    from looplab.engine.claims import load_claim_decisions, record_claim_decision
    record_claim_decision(str(tmp_path), statement="x", decision="ratified")
    record_claim_decision(str(tmp_path), statement="x", decision="rejected")   # operator changed their mind
    from looplab.engine.memory import normalize_statement
    assert load_claim_decisions(str(tmp_path))[normalize_statement("x")]["decision"] == "rejected"


def test_invalid_decision_raises(tmp_path):
    import pytest
    from looplab.engine.claims import record_claim_decision
    with pytest.raises(ValueError):
        record_claim_decision(str(tmp_path), statement="x", decision="bogus")
    with pytest.raises(ValueError):
        record_claim_decision(str(tmp_path), statement="", decision="ratified")


def test_maturity_overlay_on_assessments():
    from looplab.engine.claims import claim_assessments
    from looplab.engine.memory import normalize_statement
    lessons = [_lesson("hard-neg helps", "supported", [1]), _lesson("noise", "supported", [2])]
    dec = {normalize_statement("hard-neg helps"): {"decision": "ratified"}}
    out = {c["statement"]: c["maturity"] for c in claim_assessments(lessons, decisions=dec)}
    assert out["hard-neg helps"] == "operator-ratified" and out["noise"] == "machine-proposed"


def test_pack_drops_rejected_and_leads_with_ratified():
    from looplab.engine.claims import build_context_pack, claim_assessments
    from looplab.engine.memory import normalize_statement
    lessons = [
        _lesson("ratified claim", "supported", [1]),
        _lesson("rejected claim", "supported", [2]),
        _lesson("contested", "supported", [3], run_id="rA"),
        _lesson("contested", "tested", [4], run_id="rB"),
    ]
    dec = {normalize_statement("ratified claim"): {"decision": "ratified"},
           normalize_statement("rejected claim"): {"decision": "rejected"}}
    pack = build_context_pack(claim_assessments(lessons, decisions=dec), max_claims=5)
    stmts = [c["statement"] for c in pack["claims"]]
    assert stmts[0] == "ratified claim"                 # operator-ratified surfaced FIRST
    assert "rejected claim" not in stmts                # operator-rejected dropped from the pack


def test_reserved_caveat_slot_can_be_filled_by_a_ratified_caveat():
    from looplab.engine.claims import build_context_pack, claim_assessments
    from looplab.engine.memory import normalize_statement
    # 3 ratified positives (more evidence -> rank first) fill max_claims; a ratified MIXED caveat has less
    # evidence so it is pushed PAST the cutoff. The reserved caveat slot must still pull it in — before the
    # fix `caveats` looked only in the NON-ratified pool and this ratified caveat was starved (§20.5).
    lessons = [_lesson("pos0", "supported", [1, 2, 3]),
               _lesson("pos1", "supported", [1, 2, 3]),
               _lesson("pos2", "supported", [1, 2, 3]),
               _lesson("contested", "supported", [5], run_id="rA"),
               _lesson("contested", "tested", [6], run_id="rB")]        # mixed, lower evidence -> ranks last
    dec = {normalize_statement(s): {"decision": "ratified"} for s in ("pos0", "pos1", "pos2", "contested")}
    pack = build_context_pack(claim_assessments(lessons, decisions=dec), max_claims=3)
    assert len(pack["claims"]) == 3
    assert any(c["epistemic"] == "mixed" and c["statement"] == "contested" for c in pack["claims"])


def test_ratified_mixed_counts_as_contested_and_rejected_dropped_from_atlas():
    from looplab.engine.claims import build_context_pack, claim_assessments, portfolio_atlas
    from looplab.engine.memory import normalize_statement
    lessons = [_lesson("cA", "supported", [1], run_id="rA"), _lesson("cA", "tested", [2], run_id="rB"),  # mixed
               _lesson("cB", "supported", [1], run_id="rA"), _lesson("cB", "tested", [2], run_id="rB")]  # mixed
    dec = {normalize_statement("cA"): {"decision": "ratified"},   # a ratified contradiction is still contested
           normalize_statement("cB"): {"decision": "rejected"}}   # a rejected contradiction is NOT live
    pack = build_context_pack(claim_assessments(lessons, decisions=dec))
    assert pack["n_contested"] == 1                               # cA counts (ratified mixed); cB excluded
    atlas = portfolio_atlas(lessons, [], decisions=dec)
    stmts = [c["statement"] for c in atlas["contradictions"]]
    assert "cA" in stmts and "cB" not in stmts                    # operator-rejected contradiction dropped


def test_cli_claim_decide_and_reflect(tmp_path):
    import orjson
    from typer.testing import CliRunner
    from looplab.cli import app
    (tmp_path / "lessons.jsonl").write_bytes(orjson.dumps(_lesson("some finding", "supported", [1])) + b"\n")
    r = CliRunner().invoke(app, ["claim-decide", str(tmp_path), "some finding", "--reject", "--note", "wrong"])
    assert r.exit_code == 0 and "rejected" in r.stdout
    # the claims view now marks it REJECTED
    out = CliRunner().invoke(app, ["claims", str(tmp_path)])
    assert "[REJECTED]" in out.stdout


def test_cli_claim_decide_requires_exactly_one(tmp_path):
    from typer.testing import CliRunner
    from looplab.cli import app
    r = CliRunner().invoke(app, ["claim-decide", str(tmp_path), "x"])   # none chosen
    assert r.exit_code == 2


# --------------------------------------------------------------------------- #
# D8 research claims persisted cross-run (makes `contested` reachable)
# --------------------------------------------------------------------------- #

def test_record_and_load_research_claims_upsert(tmp_path):
    from looplab.engine.claims import load_research_claims, record_research_claims
    record_research_claims(str(tmp_path), run_id="r1", task_id="t",
                           claims=[{"statement": "doc2query helps", "node_ids": [5], "urls": ["u"]}, {"statement": ""}])
    record_research_claims(str(tmp_path), run_id="r1", task_id="t",     # re-run replaces r1's rows
                           claims=[{"statement": "doc2query helps", "node_ids": [7]}])
    rows = load_research_claims(str(tmp_path))
    assert len(rows) == 1 and rows[0]["run_id"] == "r1" and rows[0]["node_ids"] == [7]


def test_d8_claim_contests_a_lesson_verdict(tmp_path):
    # a D8 research claim SUPPORTS a statement a lesson REFUTED -> the portfolio now has a CONTESTED claim
    # (unreachable from consolidated lessons alone, which carry one verdict per statement).
    from looplab.engine.claims import claims_for_memory, record_research_claims
    _write_lessons(tmp_path / "lessons.jsonl", [_lesson("distillation helps", "refuted", [2], run_id="rL")])
    record_research_claims(str(tmp_path), run_id="rR", task_id="t",
                           claims=[{"statement": "distillation helps", "node_ids": [9]}])
    out = {c["statement"]: c for c in claims_for_memory(str(tmp_path))}
    c = out["distillation helps"]
    assert c["epistemic"] == "mixed"                       # now contested
    assert c["support"] == ["rR:9"] and c["oppose"] == ["rL:2"]


def test_claims_for_memory_applies_decisions_too(tmp_path):
    from looplab.engine.claims import claims_for_memory, record_claim_decision
    _write_lessons(tmp_path / "lessons.jsonl", [_lesson("x helps", "supported", [1])])
    record_claim_decision(str(tmp_path), statement="x helps", decision="ratified")
    out = claims_for_memory(str(tmp_path))
    assert out[0]["maturity"] == "operator-ratified"


# --------------------------------------------------------------------------- #
# CR1b — opt-in fuzzy/paraphrase claim merge (off by default)
# --------------------------------------------------------------------------- #

def test_fuzzy_off_is_default_no_merge():
    lessons = [_lesson("hard negative mining improves recall", "supported", [1]),
               _lesson("hard-negative mining boosts recall performance", "supported", [2])]
    out = claim_assessments(lessons)                     # fuzzy defaults off
    assert len(out) == 2                                 # two distinct normalized statements


def test_fuzzy_merges_paraphrases():
    lessons = [_lesson("hard negative mining improves recall", "supported", [1], run_id="rA"),
               _lesson("hard negative mining improves recall greatly", "tested", [2], run_id="rB"),
               _lesson("learning rate warmup stabilizes training", "supported", [3], run_id="rC")]
    out = claim_assessments(lessons, fuzzy=True)
    # the two hard-negative paraphrases merge into one (contested: rA supports, rB refutes); the warmup
    # claim stays separate.
    hn = [c for c in out if "hard negative" in c["statement"].lower()]
    assert len(hn) == 1 and hn[0]["epistemic"] == "mixed"
    assert set(hn[0]["support"]) == {"rA:1"} and set(hn[0]["oppose"]) == {"rB:2"}
    assert "merged_from" in hn[0] and len(hn[0]["merged_from"]) == 2
    assert any("warmup" in c["statement"] for c in out)


def test_cli_claims_fuzzy_flag(tmp_path):
    from typer.testing import CliRunner
    from looplab.cli import app
    _write_lessons(tmp_path / "lessons.jsonl", [
        _lesson("distillation improves retrieval recall", "supported", [1]),
        _lesson("distillation improves retrieval recall a lot", "supported", [2])])
    r = CliRunner().invoke(app, ["claims", str(tmp_path), "--fuzzy"])
    assert r.exit_code == 0


# --------------------------------------------------------------------------- #
# Structured semantic claim key (§21.20.13, full CR of the lean fuzzy merge)
# --------------------------------------------------------------------------- #

def test_structured_merges_paraphrases_within_a_scope():
    out = claim_assessments([
        _lesson("hard negative mining improves recall", "supported", [1], run_id="rA"),
        _lesson("hard negative mining improved recall greatly", "supported", [2], run_id="rB"),
    ], structured=True)
    assert len(out) == 1 and out[0]["n_support"] == 2 and out[0]["runs"] == ["rA", "rB"]


def test_structured_does_not_merge_opposite_polarity_it_contradicts():
    out = claim_assessments([
        _lesson("dropout improves generalization", "supported", [1], run_id="rA"),
        _lesson("dropout never improves generalization", "supported", [2], run_id="rB"),
    ], structured=True)
    assert len(out) == 2                              # two SEPARATE assertions, not one merged claim
    # each is marked contested and names the other as a contradiction (unreachable from the lean merge)
    assert all(c["epistemic"] == "mixed" and c["contradicts"] for c in out)


def test_structured_scope_separates_same_words_across_tasks():
    out = claim_assessments([
        _lesson("distillation helps", "supported", [1], run_id="rA", task_id="retrieval"),
        _lesson("distillation helps", "refuted", [2], run_id="rB", task_id="classification"),
    ], structured=True)
    assert len(out) == 2                              # different tasks => different claims (not a mixed merge)
    assert {c["epistemic"] for c in out} == {"supported", "refuted"}


def test_structured_governance_is_scope_precise(tmp_path):
    from looplab.engine.claims import claims_for_memory, record_claim_decision
    _write_lessons(tmp_path / "lessons.jsonl", [
        _lesson("adapter tuning helps", "supported", [1], run_id="rA", task_id="taskA"),
        _lesson("adapter tuning helps", "supported", [2], run_id="rB", task_id="taskB")])
    # reject the claim ONLY in taskA
    record_claim_decision(str(tmp_path), statement="adapter tuning helps", decision="rejected", scope="taskA")
    out = {c["scopes"][0]: c["maturity"] for c in claims_for_memory(str(tmp_path), structured=True)}
    assert out["taskA"] == "operator-rejected" and out["taskB"] == "machine-proposed"


def test_scopeless_decision_applies_in_structured_mode(tmp_path):
    # mega-review regression: the DEFAULT `claim-decide` (no --scope) must still overlay in structured mode,
    # applying to every scope of the statement — but a SCOPED decision must NOT leak across tasks.
    from looplab.engine.claims import claims_for_memory, record_claim_decision
    _write_lessons(tmp_path / "lessons.jsonl", [
        _lesson("dense retrieval helps", "supported", [1], run_id="rA", task_id="taskA"),
        _lesson("dense retrieval helps", "supported", [2], run_id="rB", task_id="taskB")])
    record_claim_decision(str(tmp_path), statement="dense retrieval helps", decision="rejected")  # NO scope
    out = {c["scopes"][0]: c["maturity"] for c in claims_for_memory(str(tmp_path), structured=True)}
    assert out["taskA"] == "operator-rejected" and out["taskB"] == "operator-rejected"   # applies everywhere


def test_scoped_decision_still_does_not_leak_via_legacy_key(tmp_path):
    from looplab.engine.claims import claims_for_memory, record_claim_decision
    _write_lessons(tmp_path / "lessons.jsonl", [
        _lesson("mnr helps", "supported", [1], run_id="rA", task_id="taskA"),
        _lesson("mnr helps", "supported", [2], run_id="rB", task_id="taskB")])
    record_claim_decision(str(tmp_path), statement="mnr helps", decision="rejected", scope="taskA")
    out = {c["scopes"][0]: c["maturity"] for c in claims_for_memory(str(tmp_path), structured=True)}
    assert out["taskA"] == "operator-rejected" and out["taskB"] == "machine-proposed"   # no leak to taskB


def test_scoped_decision_does_not_leak_in_LEAN_mode(tmp_path):
    # mega-review HIGH regression: a decision scoped to taskA must NOT govern a same-worded taskB claim in
    # the LEAN (default) read path — a task-bound reader passes only its own lessons.
    from looplab.engine.claims import claims_for_memory, record_claim_decision
    record_claim_decision(str(tmp_path), statement="reranking helps", decision="rejected", scope="taskA")
    # a taskB-bound reader (only taskB lessons)
    taskb = claims_for_memory(str(tmp_path), lessons=[_lesson("reranking helps", "supported", [1],
                                                              run_id="rB", task_id="taskB")])
    assert taskb[0]["maturity"] == "machine-proposed"          # taskA's reject does NOT reach taskB
    # a taskA-bound reader (only taskA lessons) DOES see the reject
    taska = claims_for_memory(str(tmp_path), lessons=[_lesson("reranking helps", "supported", [2],
                                                              run_id="rA", task_id="taskA")])
    assert taska[0]["maturity"] == "operator-rejected"


def test_global_decision_survives_a_later_scoped_one_in_LEAN_mode(tmp_path):
    # regression: a GLOBAL reject then a LATER scoped ratify overwrites the legacy key last-wins; the global
    # verdict must still apply to EVERY OTHER scope in the lean (default) path (companion to the structured
    # fix — the lean scope-guard shared the same bug and dropped the global decision).
    from looplab.engine.claims import claims_for_memory, record_claim_decision
    record_claim_decision(str(tmp_path), statement="distillation helps", decision="rejected")           # GLOBAL
    record_claim_decision(str(tmp_path), statement="distillation helps", decision="ratified", scope="taskA")  # scoped
    taska = claims_for_memory(str(tmp_path), lessons=[_lesson("distillation helps", "supported", [1],
                                                              run_id="rA", task_id="taskA")])
    assert taska[0]["maturity"] == "operator-ratified"          # taskA: the specific scoped verdict wins
    taskb = claims_for_memory(str(tmp_path), lessons=[_lesson("distillation helps", "supported", [2],
                                                              run_id="rB", task_id="taskB")])
    assert taskb[0]["maturity"] == "operator-rejected"          # taskB: the GLOBAL reject still applies


def test_global_decision_survives_a_later_scoped_decision_in_structured_mode(tmp_path):
    # mega-review HIGH regression: a portfolio-wide (scope-less) decision must keep applying to OTHER
    # scopes even after a later SCOPED decision on the same statement is recorded. The scoped decision
    # overwrites the legacy statement key last-wins, so the structured fallback reads the global verdict
    # from its distinct global index, not the (now-scoped) legacy key.
    from looplab.engine.claims import claims_for_memory, record_claim_decision
    record_claim_decision(str(tmp_path), statement="dropout helps", decision="rejected")            # global
    record_claim_decision(str(tmp_path), statement="dropout helps", decision="ratified", scope="taskA")
    outB = claims_for_memory(str(tmp_path), structured=True,
                             lessons=[_lesson("dropout helps", "supported", [0], run_id="rB", task_id="taskB")])
    assert outB[0]["maturity"] == "operator-rejected", outB          # taskB keeps the GLOBAL rejection
    outA = claims_for_memory(str(tmp_path), structured=True,
                             lessons=[_lesson("dropout helps", "supported", [1], run_id="rA", task_id="taskA")])
    assert outA[0]["maturity"] == "operator-ratified", outA          # taskA keeps its own scoped ratify


def test_scopeless_decision_applies_in_lean_mode(tmp_path):
    from looplab.engine.claims import claims_for_memory, record_claim_decision
    record_claim_decision(str(tmp_path), statement="warmup helps", decision="ratified")   # no scope
    out = claims_for_memory(str(tmp_path), lessons=[_lesson("warmup helps", "supported", [1], task_id="anyTask")])
    assert out[0]["maturity"] == "operator-ratified"           # scope-less applies everywhere in lean too


def test_research_claims_are_scoped_by_task(tmp_path):
    # mega-review HIGH regression: claims_for_memory(scope_task=) must filter D8 research to the bound task.
    from looplab.engine.claims import claims_for_memory, record_research_claims
    record_research_claims(str(tmp_path), run_id="rX", task_id="taskB",
                           claims=[{"statement": "other task secret finding", "node_ids": [9]}])
    scoped = claims_for_memory(str(tmp_path), lessons=[_lesson("local", "supported", [1], task_id="taskA")],
                               scope_task="taskA")
    assert all("secret" not in c["statement"] for c in scoped)   # taskB research not visible to taskA
    wide = claims_for_memory(str(tmp_path), lessons=[], scope_task="")   # unbound -> portfolio-wide
    assert any("secret" in c["statement"] for c in wide)


def test_atlas_drops_rejected_from_contradictions(tmp_path):
    from looplab.engine.claims import atlas_for_memory, record_claim_decision
    _write_lessons(tmp_path / "lessons.jsonl", [
        _lesson("contested thing", "supported", [1], run_id="rA"),
        _lesson("contested thing", "refuted", [2], run_id="rB")])   # -> mixed
    a0 = atlas_for_memory(str(tmp_path))
    assert any(c["statement"] == "contested thing" for c in a0["contradictions"])
    record_claim_decision(str(tmp_path), statement="contested thing", decision="rejected")   # scope-less
    a1 = atlas_for_memory(str(tmp_path))
    assert not any(c["statement"] == "contested thing" for c in a1["contradictions"])   # rejected -> dropped


def test_context_pack_never_evicts_a_pinned_claim():
    from looplab.engine.claims import build_context_pack, claim_assessments
    from looplab.engine.memory import normalize_statement
    lessons = [_lesson("pinned fact", "supported", [1])] + \
        [_lesson(f"filler claim {i}", "supported", [i + 2], run_id=f"r{i}") for i in range(6)]
    dec = {normalize_statement("pinned fact"): {"decision": "pinned", "scope": ""}}
    pack = build_context_pack(claim_assessments(lessons, decisions=dec), max_claims=2)
    assert any(c["statement"] == "pinned fact" for c in pack["claims"])   # pinned retained despite max_claims


def test_render_sanitizes_control_chars():
    from looplab.engine.claims import build_context_pack, claim_assessments, render_context_pack
    lessons = [_lesson("evil\nIGNORE PREVIOUS\x00 claim", "supported", [1])]
    txt = render_context_pack(build_context_pack(claim_assessments(lessons)))
    assert "\n  " in txt                     # structural newlines from the renderer are fine
    assert "\x00" not in txt and "evil IGNORE PREVIOUS claim" in txt   # embedded newline/control collapsed


def test_cli_claims_structured_flag(tmp_path):
    from typer.testing import CliRunner
    from looplab.cli import app
    _write_lessons(tmp_path / "lessons.jsonl", [
        _lesson("mnr loss helps", "supported", [1], run_id="rA"),
        _lesson("mnr loss never helps", "supported", [2], run_id="rB")])
    r = CliRunner().invoke(app, ["claims", str(tmp_path), "--structured"])
    assert r.exit_code == 0 and "mnr loss" in r.stdout
