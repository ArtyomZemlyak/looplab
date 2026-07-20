"""PART IV cross-run Step 4 (§21.20) — claim_assessments: lessons + D8 claims -> evidence-grounded claims.

Pins the projection that turns the shipped lesson verdicts + D8 research-memo claims into verifiable
assertions with support/oppose evidence refs and an epistemic state — the "what does the evidence suggest,
and what contradicts it" read-model. Pure/deterministic; unifies the two shipped shapes, forks neither.
"""
from __future__ import annotations

import pytest

from looplab.engine.claims import claim_assessments


def _lesson(statement, outcome, evidence, *, run_id="r1", task_id="t", **extra):
    return {"statement": statement, "outcome": outcome, "evidence": evidence,
            "run_id": run_id, "task_id": task_id, **extra}


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


def test_explicit_claim_stance_separates_literal_truth_from_action_outcome():
    # The change is bad advice, but the evidence SUPPORTS the literal negative factual sentence.
    row = _lesson("raising LR regressed validation", "failed", [7], claim_stance="support")
    for structured in (False, True):
        claim = claim_assessments([row], structured=structured)[0]
        assert claim["epistemic"] == "supported"
        assert claim["support"] == ["r1:7"] and claim["oppose"] == []


def test_explicit_oppose_and_neutral_override_outcome_while_invalid_stance_is_quarantined():
    opposed = claim_assessments([
        _lesson("X helps", "supported", [1], claim_stance="oppose")])[0]
    neutral = claim_assessments([
        _lesson("Y helps", "supported", [2], claim_stance="neutral")])[0]
    malformed = claim_assessments([
        _lesson("Z helps", "supported", [3], claim_stance="definitely")])
    assert opposed["epistemic"] == "refuted" and opposed["oppose"] == ["r1:1"]
    assert neutral["epistemic"] == "inconclusive" and neutral["support"] == []
    assert malformed == []
    assert malformed.claim_source["source_complete"] is False
    assert malformed.claim_source["lessons"]["invalid_rows"] == 1


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
                          "urls": ["http://x"],
                          "verification": {"verdict": "supported", "method": "llm"}}])
    c = out[0]
    assert c["epistemic"] == "supported" and c["support"] == ["?:11", "?:12"]
    assert c["sources"] == ["http://x"]


def test_lesson_and_research_claim_unify_on_the_same_statement():
    # a lesson OPPOSES while a D8 memo claim SUPPORTS the same statement -> one mixed claim (not two).
    # Identity reuses the shipped `normalize_statement` (whitespace+case), so casing/spacing unify...
    out = claim_assessments(
        [_lesson("Distillation  helps", "refuted", [2])],
        research_claims=[{"statement": "distillation helps", "node_ids": [8],
                          "verification": {"verdict": "supported", "method": "llm"}}])
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


def test_numeric_string_node_ids_are_compatible_and_urls_are_not_node_evidence():
    out = claim_assessments(
        [], research_claims=[{"statement": "s", "node_ids": ["4", "-5"], "urls": ["u"],
                              "verification": {"verdict": "supported", "method": "llm"}}])
    # Legacy bounded integer strings still coerce exactly; a URL belongs only in sources.
    assert out[0]["support"] == ["?:-5", "?:4"] and out[0]["sources"] == ["u"]


def test_empty_input_is_empty():
    assert claim_assessments([]) == []
    assert claim_assessments([{"statement": "", "outcome": "supported", "evidence": [1]}]) == []


def test_unverified_or_unsupported_d8_citation_never_becomes_support():
    rows = [
        {"statement": "legacy citation", "run_id": "r1", "task_id": "t", "node_ids": [9]},
        {"statement": "verifier rejected", "run_id": "r2", "task_id": "t", "node_ids": [10],
         "verification": {"verdict": "unsupported", "method": "llm", "note": "does not establish it"}},
    ]
    out = {c["statement"]: c for c in claim_assessments([], research_claims=rows)}
    assert out["legacy citation"]["epistemic"] == "inconclusive"
    assert out["legacy citation"]["support"] == [] and out["legacy citation"]["unverified"] == ["r1:9"]
    assert out["verifier rejected"]["oppose"] == [] and out["verifier rejected"]["unverified"] == ["r2:10"]
    assert out["verifier rejected"]["runs"] == ["r2"] and out["verifier rejected"]["scopes"] == ["t"]


# --------------------------------------------------------------------------- #
# CLI  (`looplab claims`)
# --------------------------------------------------------------------------- #

def _write_lessons(path, lessons):
    import orjson
    path.write_bytes(b"\n".join(orjson.dumps(lesson) for lesson in lessons) + b"\n")


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


def test_every_persistable_research_statement_is_governable(tmp_path):
    import pytest
    from looplab.engine.claims import record_claim_decision
    statement = "x improves y " + ("z" * (4000 - len("x improves y ")))
    record = record_claim_decision(str(tmp_path), statement=statement, decision="pinned")
    assert record["statement"] == statement
    with pytest.raises(ValueError, match="statement exceeds 4000"):
        record_claim_decision(str(tmp_path), statement=statement + "z", decision="pinned")


def test_claim_decision_revision_cas_and_idempotency_are_atomic_contracts(tmp_path):
    import pytest
    from looplab.engine.claims import (ClaimDecisionConflict, claim_governance_revision,
                                       record_claim_decision)
    first = record_claim_decision(str(tmp_path), statement="x improves y", decision="ratified",
                                  expected_revision=0, action_id="action-1", by="first-operator")
    assert (first["revision"] == 1 and first["by"] == "first-operator"
            and claim_governance_revision(str(tmp_path)) == 1)
    # Actor/timestamp are receipt metadata. A transport retry returns the durable first receipt even when
    # its server-derived actor changed and its old CAS revision is repeated.
    retry = record_claim_decision(str(tmp_path), statement="x improves y", decision="ratified",
                                  expected_revision=0, action_id="action-1", by="retry-operator")
    assert retry == first and retry["by"] == "first-operator"
    assert claim_governance_revision(str(tmp_path)) == 1
    assert len((tmp_path / "claim_decisions.jsonl").read_text().splitlines()) == 1
    with pytest.raises(ValueError, match="different claim decision"):
        record_claim_decision(str(tmp_path), statement="x improves y", decision="rejected",
                              expected_revision=1, action_id="action-1")
    with pytest.raises(ClaimDecisionConflict) as exc:
        record_claim_decision(str(tmp_path), statement="z helps", decision="pinned",
                              expected_revision=0, action_id="action-2")
    assert exc.value.current_revision == 1 and claim_governance_revision(str(tmp_path)) == 1


def test_claim_governance_append_survives_a_torn_jsonl_tail(tmp_path):
    from looplab.engine.claims import load_claim_decisions, record_claim_decision
    from looplab.engine.memory import normalize_statement

    path = tmp_path / "claim_decisions.jsonl"
    path.write_bytes(b'{"statement":"torn"')
    stored = record_claim_decision(
        str(tmp_path), statement="durable verdict", decision="pinned",
        expected_revision=0, action_id="after-torn",
    )
    assert stored["revision"] == 1
    physical_lines = path.read_bytes().splitlines()
    assert physical_lines[0] == b'{"statement":"torn"' and physical_lines[1].startswith(b"{")
    loaded = load_claim_decisions(str(tmp_path))
    assert loaded[normalize_statement("durable verdict")]["decision"] == "pinned"


def test_clear_only_tombstones_its_exact_scope_and_global_key_does_not_leak(tmp_path):
    from looplab.engine.claim_key import claim_uid
    from looplab.engine.claims import load_claim_decisions, record_claim_decision
    from looplab.engine.memory import normalize_statement
    statement = "adapter tuning improves recall"
    record_claim_decision(str(tmp_path), statement=statement, decision="ratified")
    record_claim_decision(str(tmp_path), statement=statement, decision="rejected",
                          scope="taskA", metric="recall")
    loaded = load_claim_decisions(str(tmp_path))
    assert loaded[normalize_statement(statement)]["decision"] == "ratified"
    scoped_uid = claim_uid(statement, scope="taskA", metric="recall")
    assert loaded[scoped_uid]["decision"] == "rejected"
    record_claim_decision(str(tmp_path), statement=statement, decision="clear",
                          scope="taskA", metric="recall")
    loaded = load_claim_decisions(str(tmp_path))
    assert scoped_uid not in loaded
    assert loaded[normalize_statement(statement)]["decision"] == "ratified"


def test_global_clear_retires_older_paraphrase_indexes_for_same_uid(tmp_path):
    from looplab.engine.claim_key import claim_uid
    from looplab.engine.claims import load_claim_decisions, record_claim_decision
    from looplab.engine.memory import normalize_statement
    original = "augmentation improves retrieval recall"
    paraphrase = "augmentation greatly improves retrieval recall"
    assert claim_uid(original) == claim_uid(paraphrase)
    record_claim_decision(str(tmp_path), statement=original, decision="ratified")
    record_claim_decision(str(tmp_path), statement=paraphrase, decision="clear")
    loaded = load_claim_decisions(str(tmp_path))
    assert claim_uid(original) not in loaded
    assert normalize_statement(original) not in loaded


def test_v1_decision_uid_is_migrated_from_durable_statement(tmp_path):
    import json
    from looplab.engine.claim_key import CLAIM_KEY_VERSION, claim_uid
    from looplab.engine.claims import load_claim_decisions
    statement = "teacher distillation improves student recall"
    legacy = {"statement": statement, "scope": "t", "metric": "recall", "decision": "rejected",
              "claim_key_version": 1, "claim_uid": "clm_old_collision"}
    (tmp_path / "claim_decisions.jsonl").write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    current_uid = claim_uid(statement, scope="t", metric="recall")
    loaded = load_claim_decisions(str(tmp_path))
    assert current_uid in loaded and "clm_old_collision" not in loaded
    assert loaded[current_uid]["claim_key_version"] == CLAIM_KEY_VERSION


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
    record_research_claims(str(tmp_path), run_id="r1", task_id="t", direction="max",
                           claims=[{"statement": "doc2query helps", "node_ids": [5], "urls": ["u"]}, {"statement": ""}])
    record_research_claims(str(tmp_path), run_id="r1", task_id="t", direction="max",  # replaces r1
                           claims=[{"statement": "doc2query helps", "node_ids": [7]}])
    rows = load_research_claims(str(tmp_path))
    assert len(rows) == 1 and rows[0]["run_id"] == "r1" and rows[0]["node_ids"] == [7]
    assert rows[0]["v"] == 3
    assert rows[0]["source_receipt"] == {
        "v": 1, "claims_total": 1, "claims_retained": 1,
        "claims_omitted": 0, "producer_complete": True,
    }


def test_research_claim_upsert_preserves_malformed_future_and_invalid_same_run_rows(tmp_path):
    import orjson
    from looplab.engine.claims import record_research_claims

    current = {
        "statement": "current claim", "node_ids": [1],
        "verification": {"verdict": "supported"},
    }
    record_research_claims(tmp_path, run_id="same", task_id="t", direction="max", claims=[current])
    record_research_claims(tmp_path, run_id="sibling", task_id="t", direction="max", claims=[{
        "statement": "sibling claim", "node_ids": [2],
    }])
    path = tmp_path / "research_claims.jsonl"
    malformed = b'{"v":3,"run_id":"same",BROKEN\n'
    future = {
        "v": 99, "run_id": "same", "task_id": "t", "statement": "future claim",
        "node_ids": [9], "future_contract": {"must": "survive"},
    }
    future_kind = {
        "v": 3, "record_kind": "future-claim-family", "run_id": "same", "task_id": "t",
        "statement": "same-version future family", "node_ids": [10],
        "source_receipt": {
            "v": 1, "claims_total": 1, "claims_retained": 1,
            "claims_omitted": 0, "producer_complete": True,
        },
    }
    invalid_current = {
        "v": 3, "run_id": "same", "task_id": "t", "statement": "invalid receipt",
        "node_ids": [8], "source_receipt": {
            "v": 1, "claims_total": 1, "claims_retained": 1,
            "claims_omitted": 0, "producer_complete": False,
        },
    }
    path.write_bytes(path.read_bytes() + malformed + orjson.dumps(future) + b"\n"
                     + orjson.dumps(future_kind) + b"\n"
                     + orjson.dumps(invalid_current) + b"\n")

    record_research_claims(tmp_path, run_id="same", task_id="t", direction="max", claims=[{
        **current, "node_ids": [7],
    }])

    raw = path.read_bytes()
    assert malformed in raw
    decoded = []
    for line in raw.split(b"\n"):
        try:
            decoded.append(orjson.loads(line))
        except orjson.JSONDecodeError:
            pass
    same = [row for row in decoded if row.get("run_id") == "same"]
    assert any(row.get("v") == 99 and row.get("future_contract") == {"must": "survive"}
               for row in same)
    assert future_kind in same
    assert invalid_current in same
    current_rows = [row for row in same
                    if row.get("v") == 3 and row.get("record_kind") == "claim"
                    and row.get("source_receipt", {}).get("producer_complete") is True]
    assert len(current_rows) == 1 and current_rows[0]["node_ids"] == [7]
    assert any(row.get("run_id") == "sibling" for row in decoded)


def test_nonempty_all_invalid_d8_source_persists_receipt_sentinel_without_indexing_claim(tmp_path):
    from looplab.engine.claims import (
        claim_assessments, load_research_claims, portfolio_atlas,
        record_research_claims, render_context_pack,
    )

    assert record_research_claims(
        tmp_path, run_id="invalid-only", task_id="t", direction="max",
        claims=[None, {"statement": ""}]) == 0
    rows = load_research_claims(tmp_path)
    assert rows == [{
        "v": 3,
        "record_kind": "source_receipt",
        "run_id": "invalid-only",
        "task_id": "t",
        "direction": "max",
        "source_receipt": {
            "v": 1, "claims_total": 2, "claims_retained": 0,
            "claims_omitted": 2, "producer_complete": False,
        },
    }]

    claims = claim_assessments([
        _lesson("lesson support", "supported", [1])
    ], research_claims=rows)
    assert len(claims) == 1 and claims[0]["statement"] == "lesson support"
    assert claims[0]["epistemic"] == "inconclusive"
    assert claims[0]["research_source"]["producer_claims_retained"] == 0
    assert claims[0]["research_source"]["producer_claims_omitted"] == 2
    refuted = claim_assessments([
        _lesson("lesson opposition", "refuted", [2])
    ], research_claims=rows)[0]
    assert refuted["oppose"] == ["r1:2"] and refuted["epistemic"] == "inconclusive"

    atlas = portfolio_atlas([], [], research_claims=rows)
    assert atlas["n_claims"] == 0
    assert atlas["research_source"]["source_complete"] is False
    assert atlas["context_pack"]["claims"] == []
    assert "2 claim(s) known omitted" in render_context_pack(atlas["context_pack"])


@pytest.mark.parametrize("field,value", [
    ("statement", "forged support"),
    ("metric", "recall"),
    ("node_ids", [7]),
    ("urls", ["https://example.invalid"]),
    ("verification", {"verdict": "supported", "method": "forged", "note": ""}),
    ("fingerprint", ["scope:foreign"]),
    ("outcome", "supported"),
    ("claim_stance", "support"),
    ("role", "researcher"),
    ("unexpected_extension", "future claim envelope"),
])
def test_v3_source_receipt_sentinel_is_an_exact_non_claim_schema(field, value):
    from looplab.engine.claims import _valid_claim_source_row

    sentinel = {
        "v": 3,
        "record_kind": "source_receipt",
        "run_id": "receipt-run",
        "task_id": "t",
        "direction": "max",
        "source_receipt": {
            "v": 1, "claims_total": 0, "claims_retained": 0,
            "claims_omitted": 0, "producer_complete": True,
        },
    }

    assert _valid_claim_source_row(sentinel, research=True) is True
    assert _valid_claim_source_row({**sentinel, field: value}, research=True) is False


def test_forged_receipt_claim_fields_never_reach_lean_structured_retrieval_or_atlas(
        tmp_path, monkeypatch):
    import looplab.engine.claims as claims_module

    forged = {
        "v": 3,
        "record_kind": "source_receipt",
        "run_id": "forged-run",
        "task_id": "t",
        "direction": "max",
        "source_receipt": {
            "v": 1, "claims_total": 0, "claims_retained": 0,
            "claims_omitted": 0, "producer_complete": True,
        },
        "statement": "forged support",
        "metric": "recall",
        "node_ids": [7],
        "urls": ["https://example.invalid"],
        "verification": {"verdict": "supported", "method": "forged", "note": ""},
    }

    # The primary fence quarantines the row and lowers source authority.
    for structured in (False, True):
        projected = claims_module.claim_assessments(
            [], research_claims=[forged], structured=structured)
        assert projected == []
        assert projected.claim_source["source_complete"] is False
        assert projected.claim_source["research"]["invalid_rows"] == 1

    # Simulate a future validator regression: independent consumer discriminators still refuse to index a
    # cardinality sentinel as evidence in either identity, the Atlas composition, or retrieval corpus.
    monkeypatch.setattr(
        claims_module, "_valid_claim_source_row", lambda _row, *, research: True)
    for structured in (False, True):
        assert claims_module.claim_assessments(
            [], research_claims=[forged], structured=structured) == []
    atlas = claims_module.portfolio_atlas(
        [], [], research_claims=[forged], structured=True)
    assert atlas["n_claims"] == atlas["n_contested"] == 0
    assert atlas["contradictions"] == [] and atlas["context_pack"]["claims"] == []
    retrieval = claims_module.cross_run_retrieve(
        tmp_path, "forged support", lessons=[], capsules=[], research_claims=[forged],
        structured=True)
    assert not [hit for hit in retrieval["results"] if hit.get("kind") == "claim"]
    assert retrieval["receipt"]["n_corpus"] == retrieval["receipt"]["n_indexed"] == 0


def test_d8_producer_cap_receipt_withholds_positive_when_opposition_tail_is_unknown(tmp_path):
    from looplab.engine.claims import (
        build_context_pack, claim_assessments, load_research_claims,
        record_research_claims, render_context_pack,
    )

    positive = {
        "statement": "dropout improves generalization", "node_ids": [1],
        "verification": {"verdict": "supported", "method": "llm"},
    }
    fillers = [{
        "statement": f"bounded filler claim {index}", "node_ids": [index + 2],
        "verification": {"verdict": "supported", "method": "llm"},
    } for index in range(255)]
    omitted_opposite = {
        "statement": "dropout never improves generalization", "node_ids": [999],
        "verification": {"verdict": "supported", "method": "llm"},
    }

    assert record_research_claims(
        tmp_path, run_id="r-cap", task_id="t", direction="max",
        claims=[positive, *fillers, omitted_opposite]) == 256
    retained = load_research_claims(tmp_path)
    assert len(retained) == 256
    assert all(row["source_receipt"] == {
        "v": 1, "claims_total": 257, "claims_retained": 256,
        "claims_omitted": 1, "producer_complete": False,
    } for row in retained)
    assert omitted_opposite["statement"] not in {row["statement"] for row in retained}

    claims = claim_assessments([], research_claims=retained, structured=True)
    target = next(row for row in claims if row["statement"] == positive["statement"])
    assert target["support"] == ["r-cap:1"]
    assert target["epistemic"] == "inconclusive"
    assert {key: target["research_source"][key] for key in (
        "source_complete", "producer_receipt_known", "producer_complete", "producer_runs",
        "producer_partial_runs", "producer_unknown_runs", "producer_claims_total",
        "producer_claims_retained", "producer_claims_omitted",
    )} == {
        "source_complete": False,
        "producer_receipt_known": True,
        "producer_complete": False,
        "producer_runs": 1,
        "producer_partial_runs": 1,
        "producer_unknown_runs": 0,
        "producer_claims_total": 257,
        "producer_claims_retained": 256,
        "producer_claims_omitted": 1,
    }
    assert target["research_source"]["read_complete"] is True
    assert len(target["research_source"]["snapshot_digest"]) == 64
    rendered = render_context_pack(build_context_pack([target]))
    assert "D8 research-claim source is PARTIAL/UNKNOWN" in rendered
    assert "exact one-sided states are withheld" in rendered


def test_research_source_aggregate_rejects_known_unknown_run_contradiction():
    from looplab.engine.claims import _safe_research_source_summary

    malformed = {
        "source_complete": True,
        "producer_receipt_known": True,
        "producer_complete": True,
        "producer_runs": 1,
        "producer_partial_runs": 0,
        "producer_unknown_runs": 1,
        "producer_claims_total": 1,
        "producer_claims_retained": 1,
        "producer_claims_omitted": 0,
    }
    assert _safe_research_source_summary(malformed) is None
    assert _safe_research_source_summary({
        **malformed,
        "source_complete": False,
        "producer_receipt_known": False,
        "producer_complete": False,
    }) is not None


def test_legacy_persisted_d8_source_is_unknown_and_fails_positive_closed(tmp_path):
    import orjson
    from looplab.engine.claims import claims_for_memory, load_research_claims

    (tmp_path / "research_claims.jsonl").write_bytes(orjson.dumps({
        "v": 2, "run_id": "legacy", "task_id": "t", "statement": "legacy support",
        "node_ids": [4], "urls": [],
        "verification": {"verdict": "supported", "method": "llm"},
    }) + b"\n")

    loaded = load_research_claims(tmp_path)
    claim = claims_for_memory(tmp_path, research_claims=loaded, structured=True)[0]
    assert claim["support"] == ["legacy:4"]
    assert claim["epistemic"] == "inconclusive"
    assert claim["research_source"]["producer_receipt_known"] is False
    assert claim["research_source"]["producer_unknown_runs"] == 1


def test_d8_claim_contests_a_lesson_verdict(tmp_path):
    # a D8 research claim SUPPORTS a statement a lesson REFUTED -> the portfolio now has a CONTESTED claim
    # (unreachable from consolidated lessons alone, which carry one verdict per statement).
    from looplab.engine.claims import claims_for_memory, record_research_claims
    _write_lessons(tmp_path / "lessons.jsonl", [_lesson("distillation helps", "refuted", [2], run_id="rL")])
    record_research_claims(str(tmp_path), run_id="rR", task_id="t", direction="max",
                           claims=[{"statement": "distillation helps", "node_ids": [9],
                                    "verification": {"verdict": "supported", "method": "llm"}}])
    out = {c["statement"]: c for c in claims_for_memory(str(tmp_path))}
    c = out["distillation helps"]
    assert c["epistemic"] == "mixed"                       # now contested
    assert c["support"] == ["rR:9"] and c["oppose"] == ["rL:2"]


def test_claim_sources_quarantine_malformed_future_and_incomplete_v3_rows(tmp_path):
    import orjson

    from looplab.engine.claims import claims_for_memory, load_research_claims, record_research_claims

    lesson = _lesson("stable lesson", "supported", [1], run_id="lesson-run")
    (tmp_path / "lessons.jsonl").write_bytes(
        orjson.dumps(lesson) + b"\n{broken\n" + orjson.dumps({**lesson, "v": 99}) + b"\n")
    record_research_claims(
        tmp_path, run_id="research-run", task_id="t", direction="min",
        claims=[{"statement": "stable research", "node_ids": [2],
                 "verification": {"verdict": "supported", "method": "test"}}],
    )
    path = tmp_path / "research_claims.jsonl"
    valid = orjson.loads(path.read_bytes().splitlines()[0])
    with path.open("ab") as f:
        f.write(orjson.dumps({k: v for k, v in valid.items() if k != "direction"}) + b"\n")
        f.write(orjson.dumps({**valid, "record_kind": "future-kind"}) + b"\n")
        f.write(orjson.dumps({**valid, "v": 99}) + b"\n{also-broken\n")

    research = load_research_claims(tmp_path)
    assert len(research) == 1 and research[0]["statement"] == "stable research"
    claims = claims_for_memory(tmp_path, structured=True)
    by_statement = {row["statement"]: row for row in claims}
    assert by_statement["stable lesson"]["epistemic"] == "inconclusive"
    assert by_statement["stable research"]["epistemic"] == "inconclusive"
    source = claims.claim_source
    assert source["source_complete"] is False and source["read_complete"] is False
    assert source["lessons"] == {
        "read_complete": False, "rows_total": 3, "rows_retained": 1,
        "rows_quarantined": 2, "malformed_rows": 1, "invalid_rows": 1,
    }
    assert source["research"] == {
        "read_complete": False, "rows_total": 5, "rows_retained": 1,
        "rows_quarantined": 4, "malformed_rows": 1, "invalid_rows": 3,
    }
    assert len(source["snapshot_digest"]) == 64
    assert claims.research_source["producer_receipt_known"] is True
    assert claims.research_source["read_complete"] is False


def test_explicit_empty_d8_snapshot_persists_authoritative_zero_sentinel(tmp_path):
    from looplab.engine.claims import load_research_claims, record_research_claims, _research_source_summary

    record_research_claims(
        tmp_path, run_id="r", task_id="t", direction="max",
        claims=[{"statement": "stale", "node_ids": [1],
                 "verification": {"verdict": "supported", "method": "test"}}],
    )
    assert record_research_claims(
        tmp_path, run_id="r", task_id="t", direction="max", claims=[]) == 0
    rows = load_research_claims(tmp_path)
    assert len(rows) == 1 and rows[0]["record_kind"] == "source_receipt"
    assert rows[0]["source_receipt"] == {
        "v": 1, "claims_total": 0, "claims_retained": 0,
        "claims_omitted": 0, "producer_complete": True,
    }
    source = _research_source_summary(rows)
    assert source["producer_runs"] == 1
    assert source["producer_claims_total"] == 0
    assert source["producer_complete"] is True


def test_v3_writer_rejects_unknown_direction_without_replacing_prior_snapshot(tmp_path):
    from looplab.engine.claims import load_research_claims, record_research_claims

    claim = {"statement": "orientation matters", "node_ids": [1]}
    assert record_research_claims(
        tmp_path, run_id="r", task_id="t", direction="min", claims=[claim]) == 1
    before = (tmp_path / "research_claims.jsonl").read_bytes()
    assert record_research_claims(
        tmp_path, run_id="r", task_id="t", direction="", claims=[]) == 0
    assert (tmp_path / "research_claims.jsonl").read_bytes() == before
    assert load_research_claims(tmp_path)[0]["direction"] == "min"


def test_claim_snapshot_digest_is_stable_and_detects_same_count_rewrite(tmp_path):
    import orjson

    from looplab.engine.claims import claims_for_memory

    path = tmp_path / "lessons.jsonl"
    path.write_bytes(orjson.dumps(_lesson("first", "supported", [1])) + b"\n")
    first = claims_for_memory(tmp_path).claim_source["snapshot_digest"]
    assert len(first) == 64 and first == claims_for_memory(tmp_path).claim_source["snapshot_digest"]
    path.write_bytes(orjson.dumps(_lesson("second", "supported", [1])) + b"\n")
    second = claims_for_memory(tmp_path).claim_source["snapshot_digest"]
    assert second != first


def test_research_source_read_health_extension_is_atomic_and_receipt_invariants_hold():
    from looplab.engine.claims import _research_source_summary, _safe_research_source_summary

    current = _research_source_summary([])
    assert _safe_research_source_summary(current) == current
    partial = dict(current)
    partial.pop("invalid_rows")
    assert _safe_research_source_summary(partial) is None
    contradictory = dict(current)
    contradictory.update({
        "producer_receipt_known": True, "producer_unknown_runs": 1,
        "producer_runs": 1, "producer_complete": True, "source_complete": True,
    })
    assert _safe_research_source_summary(contradictory) is None


def test_claims_for_memory_applies_decisions_too(tmp_path):
    from looplab.engine.claims import claims_for_memory, record_claim_decision
    _write_lessons(tmp_path / "lessons.jsonl", [_lesson("x helps", "supported", [1])])
    record_claim_decision(str(tmp_path), statement="x helps", decision="ratified")
    out = claims_for_memory(str(tmp_path))
    assert out[0]["maturity"] == "operator-ratified"


def test_memory_helpers_scope_lessons_research_and_capsules_together(tmp_path):
    from looplab.engine.claims import atlas_for_memory, claims_for_memory
    from looplab.engine.memory import ConceptCapsuleStore, build_concept_capsule
    _write_lessons(tmp_path / "lessons.jsonl", [
        _lesson("visible lesson", "supported", [1], task_id="taskA"),
        _lesson("secret lesson", "supported", [2], task_id="taskB")])
    research = [
        {"statement": "visible research", "task_id": "taskA", "run_id": "rrA", "node_ids": [3]},
        {"statement": "secret research", "task_id": "taskB", "run_id": "rrB", "node_ids": [4]},
    ]
    store = ConceptCapsuleStore(tmp_path / "concept_capsules.jsonl")
    store.add(build_concept_capsule(run_id="cA", task_id="taskA", fingerprint=["a"],
                                    direction="max", concepts=["visible-concept"]))
    store.add(build_concept_capsule(run_id="cB", task_id="taskB", fingerprint=["b"],
                                    direction="max", concepts=["secret-concept"]))
    claims = claims_for_memory(str(tmp_path), research_claims=research, scope_task="taskA")
    assert {c["statement"] for c in claims} == {"visible lesson", "visible research"}
    atlas = atlas_for_memory(str(tmp_path), research_claims=research, scope_task="taskA")
    assert {e["concept"] for e in atlas["explored"]} == {"visible-concept"}
    assert all("secret" not in c["statement"] for c in atlas["context_pack"]["claims"])


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
    assert all(c["evidence_digest"].startswith("cev_") for c in out)


def test_structured_evidence_digest_changes_with_proof_not_governance(tmp_path):
    from looplab.engine.claims import claims_for_memory, record_claim_decision
    statement = "dropout improves generalization"
    path = tmp_path / "lessons.jsonl"
    _write_lessons(path, [_lesson(statement, "supported", [1], run_id="r1")])
    first = claims_for_memory(tmp_path, structured=True)[0]["evidence_digest"]
    record_claim_decision(tmp_path, statement=statement, scope="t", decision="pinned")
    governed_row = claims_for_memory(tmp_path, structured=True)[0]
    governed = governed_row["evidence_digest"]
    assert governed == first
    # Direct CLI decisions predate the HTTP evidence fence, so freshness is explicitly unknown.
    assert governed_row["decision_fresh"] is None
    _write_lessons(path, [
        _lesson(statement, "supported", [1], run_id="r1"),
        _lesson(statement, "supported", [2], run_id="r2"),
    ])
    assert claims_for_memory(tmp_path, structured=True)[0]["evidence_digest"] != first


def test_rejecting_an_opposite_changes_live_contradiction_not_evidence_digest(tmp_path):
    from looplab.engine.claims import claims_for_memory, record_claim_decision
    positive = "dropout improves generalization"
    negative = "dropout never improves generalization"
    _write_lessons(tmp_path / "lessons.jsonl", [
        _lesson(positive, "supported", [1], run_id="r1"),
        _lesson(negative, "supported", [2], run_id="r2"),
    ])
    before = {row["statement"]: row for row in claims_for_memory(tmp_path, structured=True)}
    record_claim_decision(tmp_path, statement=negative, scope="t", decision="rejected",
                          evidence_digest=before[negative]["evidence_digest"])
    after = {row["statement"]: row for row in claims_for_memory(tmp_path, structured=True)}
    assert after[positive]["contradicts"] == []
    assert after[positive]["evidence_digest"] == before[positive]["evidence_digest"]
    assert after[negative]["evidence_digest"] == before[negative]["evidence_digest"]
    assert after[negative]["decision_fresh"] is True


def test_structured_scope_separates_same_words_across_tasks():
    out = claim_assessments([
        _lesson("distillation helps", "supported", [1], run_id="rA", task_id="retrieval"),
        _lesson("distillation helps", "refuted", [2], run_id="rB", task_id="classification"),
    ], structured=True)
    assert len(out) == 2                              # different tasks => different claims (not a mixed merge)
    assert {c["epistemic"] for c in out} == {"supported", "refuted"}


def test_structured_metric_identity_separates_same_task_claims():
    out = claim_assessments([
        _lesson("adapter tuning improves score", "supported", [1], run_id="rA",
                fingerprint=["metric:recall"]),
        _lesson("adapter tuning improves score", "refuted", [2], run_id="rB",
                fingerprint=["metric:precision"]),
    ], structured=True)
    assert len(out) == 2
    assert {(c["metric"], c["epistemic"]) for c in out} == {
        ("recall", "supported"), ("precision", "refuted")}


def test_rejected_opposite_does_not_poison_live_structured_claim(tmp_path):
    from looplab.engine.claims import claims_for_memory, record_claim_decision
    positive = "augmentation improves retrieval recall"
    negative = "augmentation degrades retrieval recall"
    _write_lessons(tmp_path / "lessons.jsonl", [
        _lesson(positive, "supported", [1], run_id="rA", fingerprint=["metric:recall"]),
        _lesson(negative, "supported", [2], run_id="rB", fingerprint=["metric:recall"]),
    ])
    record_claim_decision(str(tmp_path), statement=negative, decision="rejected",
                          scope="t", metric="recall")
    out = {c["statement"]: c for c in claims_for_memory(str(tmp_path), structured=True)}
    assert out[positive]["epistemic"] == "supported" and out[positive]["contradicts"] == []
    assert out[negative]["maturity"] == "operator-rejected"
    assert out[negative]["epistemic"] == "supported" and out[negative]["contradicts"] == []


def test_refuted_or_unverified_opposite_is_not_live_contradictory_evidence():
    positive = "augmentation improves retrieval recall"
    negative = "augmentation degrades retrieval recall"
    out = {c["statement"]: c for c in claim_assessments([
        _lesson(positive, "supported", [1], run_id="rA"),
        _lesson(negative, "refuted", [2], run_id="rB"),
    ], research_claims=[{"statement": negative, "task_id": "t", "run_id": "rC", "node_ids": [3]}],
        structured=True)}
    assert out[positive]["epistemic"] == "supported" and out[positive]["contradicts"] == []
    assert out[negative]["epistemic"] == "refuted"


def test_structured_atlas_and_context_pack_expose_mutation_identity(tmp_path):
    from looplab.engine.claims import atlas_for_memory
    _write_lessons(tmp_path / "lessons.jsonl", [
        _lesson("augmentation improves retrieval recall", "supported", [1], run_id="rA",
                task_id="taskA", fingerprint=["metric:recall"])])
    atlas = atlas_for_memory(str(tmp_path), structured=True)
    claim = atlas["context_pack"]["claims"][0]
    assert claim["claim_uid"].startswith("clm_")
    assert claim["scope"] == "taskA" and claim["metric"] == "recall" and claim["polarity"] == 1


def test_metric_decision_precedence_exact_then_scope_then_global(tmp_path):
    from looplab.engine.claims import claims_for_memory, record_claim_decision
    statement = "adapter tuning improves score"
    _write_lessons(tmp_path / "lessons.jsonl", [
        _lesson(statement, "supported", [1], run_id="rA", task_id="taskA",
                fingerprint=["metric:recall"]),
        _lesson(statement, "supported", [2], run_id="rB", task_id="taskA",
                fingerprint=["metric:precision"]),
        _lesson(statement, "supported", [3], run_id="rC", task_id="taskB",
                fingerprint=["metric:recall"]),
        _lesson(statement, "supported", [4], run_id="rD", task_id="taskB",
                fingerprint=["metric:precision"]),
    ])
    record_claim_decision(str(tmp_path), statement=statement, decision="ratified")
    record_claim_decision(str(tmp_path), statement=statement, decision="pinned", metric="recall")
    record_claim_decision(str(tmp_path), statement=statement, decision="pinned", scope="taskA")
    record_claim_decision(str(tmp_path), statement=statement, decision="rejected",
                          scope="taskA", metric="recall")
    out = {(c["scopes"][0], c["metric"]): c["maturity"]
           for c in claims_for_memory(str(tmp_path), structured=True)}
    assert out[("taskA", "recall")] == "operator-rejected"
    assert out[("taskA", "precision")] == "operator-pinned"
    assert out[("taskB", "recall")] == "operator-pinned"
    assert out[("taskB", "precision")] == "operator-ratified"


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
    record_research_claims(str(tmp_path), run_id="rX", task_id="taskB", direction="max",
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


def test_claim_projection_caps_nested_evidence_but_keeps_full_counts_and_digest():
    lessons = [
        _lesson("bounded claim", "supported", [index], run_id=f"run-{index:03}")
        for index in range(80)
    ]

    projected = claim_assessments(lessons, structured=True)[0]
    complete = claim_assessments(lessons, structured=True, bounded=False)[0]

    assert projected["n_support"] == complete["n_support"] == 80
    assert len(projected["support"]) == len(projected["runs"]) == 64
    assert projected["nested_omitted"] == {"support": 16, "runs": 16}
    assert len(complete["support"]) == len(complete["runs"]) == 80
    assert projected["evidence_digest"] == complete["evidence_digest"]


def test_claim_readers_quarantine_malformed_persisted_rows(tmp_path):
    from looplab.engine.claims import claims_for_memory
    import orjson

    lessons = [
        {"statement": {"nested": "not identity"}, "outcome": "supported", "evidence": [1]},
        {"statement": "oversized evidence", "outcome": "supported", "evidence": list(range(257))},
        {"statement": "huge numeric id is quarantined", "outcome": "supported",
         "evidence": ["9" * 10_000], "run_id": "r-huge", "task_id": "t"},
        _lesson("usable lesson", "supported", [7], run_id="r-good"),
    ]
    _write_lessons(tmp_path / "lessons.jsonl", lessons)
    research = [
        {"statement": "bad verification", "node_ids": [1], "verification": []},
        {"statement": "usable research", "node_ids": [8], "run_id": "rr", "task_id": "t",
         "verification": {"verdict": "supported"}},
    ]
    (tmp_path / "research_claims.jsonl").write_bytes(
        b"[]\n" + b"\n".join(orjson.dumps(row) for row in research) + b"\n")

    rows = claims_for_memory(tmp_path, structured=True)
    by_statement = {row["statement"]: row for row in rows}

    assert set(by_statement) == {"usable lesson", "usable research"}
    assert by_statement["usable lesson"]["support"] == ["r-good:7"]
    assert by_statement["usable research"]["support"] == ["rr:8"]
    assert rows.claim_source["source_complete"] is False
    assert rows.claim_source["lessons"]["invalid_rows"] == 3
