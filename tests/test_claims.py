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
