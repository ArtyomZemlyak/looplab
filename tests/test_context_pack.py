"""PART IV cross-run Step 5 (§21.20.5) — build_context_pack / render_context_pack.

A token-bounded cross-run context pack for a proposing agent that carries BOTH support and
counter-evidence. Pins the design's hard rules: contested claims lead, a caveat slot is reserved so
positives can never crowd out opposition, the pack is bounded, and it's pure/silent (structured data
only — never auto-injected). Composes Step 4 claims + Step 3 concept overview.
"""
from __future__ import annotations

from looplab.engine.claims import build_context_pack, claim_assessments, render_context_pack


def _claim(statement, epistemic, n_support, n_oppose):
    return {"statement": statement, "epistemic": epistemic, "n_support": n_support,
            "n_oppose": n_oppose, "support": list(range(n_support)), "oppose": list(range(n_oppose))}


def test_pack_is_bounded_by_max_claims():
    claims = [_claim(f"c{i}", "supported", 5 - (i % 3), 0) for i in range(20)]
    pack = build_context_pack(claims, max_claims=3)
    assert len(pack["claims"]) == 3 and pack["n_claims_total"] == 20


def test_pack_has_a_hard_cap_even_when_caller_requests_an_unbounded_window():
    claims = [_claim(f"claim {index}", "supported", 1, 0) for index in range(100)]
    pack = build_context_pack(claims, max_claims=10 ** 1000)

    assert len(pack["claims"]) == 64
    assert pack["n_claims_total"] == 100


def test_contested_claims_lead():
    claims = [
        _claim("strong support", "supported", 9, 0),
        _claim("contested", "mixed", 2, 2),
    ]
    pack = build_context_pack(claims, max_claims=5)
    assert pack["claims"][0]["statement"] == "contested"     # the counter-argument leads
    assert pack["n_contested"] == 1


def test_caveat_slot_reserved_positives_never_crowd_out_opposition():
    # 5 strong supported claims + 1 refuted; with max_claims=5 the naive top-5 would be all-positive.
    claims = ([_claim(f"pos{i}", "supported", 10 - i, 0) for i in range(5)]
              + [_claim("the caveat", "refuted", 0, 3)])
    pack = build_context_pack(claims, max_claims=5)
    states = {c["epistemic"] for c in pack["claims"]}
    assert "refuted" in states                                # the caveat displaced the weakest positive
    assert any(c["statement"] == "the caveat" for c in pack["claims"])
    assert len(pack["claims"]) == 5


def test_no_caveat_swap_when_none_exist():
    claims = [_claim(f"pos{i}", "supported", 3, 0) for i in range(3)]
    pack = build_context_pack(claims, max_claims=2)
    assert len(pack["claims"]) == 2 and all(c["epistemic"] == "supported" for c in pack["claims"])


def test_caveat_slot_never_evicts_an_all_pinned_cutoff():
    pins = [
        {**_claim(f"pinned {index}", "supported", 5 - index, 0), "maturity": "operator-pinned"}
        for index in range(2)
    ]
    caveat = _claim("lower-priority caveat", "refuted", 0, 3)
    pack = build_context_pack([*pins, caveat], max_claims=2)
    assert [claim["statement"] for claim in pack["claims"]] == ["pinned 0", "pinned 1"]


def test_pin_overflow_is_explicit_in_pack_and_rendering():
    pins = [
        {**_claim(f"pinned {index}", "supported", 9 - index, 0),
         "maturity": "operator-pinned"}
        for index in range(6)
    ]
    pack = build_context_pack(pins, max_claims=5)
    assert len(pack["claims"]) == 5
    assert pack["n_pinned_total"] == 6 and pack["n_pinned_omitted"] == 1
    rendered = render_context_pack(pack)
    assert "1 operator-pinned claim(s) omitted" in rendered
    assert "full claims ledger" in rendered


def test_coverage_block_from_concept_overview():
    ov = {"n_runs": 4, "n_concepts": 7,
          "concepts": [{"concept": "hard-neg"}, {"concept": "distillation"}]}
    pack = build_context_pack([_claim("c", "supported", 1, 0)], concept_overview=ov, max_claims=5)
    assert pack["coverage"]["n_runs"] == 4 and pack["coverage"]["n_concepts"] == 7
    assert pack["coverage"]["top_concepts"] == ["hard-neg", "distillation"]
    assert pack["coverage"]["source_complete"] is False
    assert pack["coverage"]["source_unknown_capsules"] == 4
    assert "unknown totals" in render_context_pack(pack)


def test_partial_capsule_coverage_is_explicit_in_pack_and_rendering():
    ov = {
        "n_runs": 1, "n_concepts": 256, "concepts": [{"concept": "axis/a"}],
        "source_complete": False, "partial_capsules": 1,
        "source_concepts_omitted": 44, "source_outcomes_omitted": 44,
    }

    pack = build_context_pack([], concept_overview=ov, max_claims=5)
    rendered = render_context_pack(pack)

    assert pack["coverage"]["source_complete"] is False
    assert "source is PARTIAL" in rendered
    assert "44 concept(s)" in rendered and "returned observations only" in rendered


def test_coverage_helps_hurts_carry_run_counts():
    # E3: the profit tendency must surface the run COUNT (n_helped/n_hurt), not just the concept name,
    # so the Researcher can weigh a strong tendency (n=5) against a thin one (n=2).
    ov = {"n_runs": 6, "n_concepts": 3, "concepts": [
        {"concept": "loss/contrastive", "n_helped": 5, "n_neutral": 0, "n_hurt": 1},
        {"concept": "regularization/rdrop", "n_helped": 0, "n_neutral": 1, "n_hurt": 3},
    ]}
    pack = build_context_pack([_claim("c", "supported", 1, 0)], concept_overview=ov, max_claims=5)
    assert pack["coverage"]["helps"] == ["loss/contrastive (n=5)"]
    assert pack["coverage"]["hurts"] == ["regularization/rdrop (n=3)"]
    txt = render_context_pack(pack)
    assert "(n=5)" in txt and "RANK BETTER" in txt and "(n=3)" in txt


def test_support_and_oppose_refs_are_bounded():
    pack = build_context_pack([_claim("c", "mixed", 20, 20)], max_claims=5)
    assert len(pack["claims"][0]["support"]) == 6 and len(pack["claims"][0]["oppose"]) == 6


def test_render_leads_with_evidence_and_is_empty_when_empty():
    assert render_context_pack({"claims": [], "n_claims_total": 0}) == ""
    txt = render_context_pack(build_context_pack([_claim("mnr helps", "mixed", 2, 1)]))
    assert "counter-evidence" in txt and "mnr helps" in txt and "⚖" in txt


def test_render_names_structured_opposite_assertion():
    from looplab.engine.claims import claim_assessments
    claims = claim_assessments([
        {"statement": "dropout improves generalization", "outcome": "supported",
         "evidence": [1], "run_id": "r1", "task_id": "t"},
        {"statement": "dropout never improves generalization", "outcome": "supported",
         "evidence": [2], "run_id": "r2", "task_id": "t"},
    ], structured=True)
    text = render_context_pack(build_context_pack(claims, max_claims=1))
    assert "contradicts=" in text


def test_cli_pack_renders_contested_first(tmp_path):
    import orjson
    from typer.testing import CliRunner
    from looplab.cli import app
    lessons = [
        {"statement": "hard-neg helps", "outcome": "supported", "evidence": [1], "run_id": "rA", "task_id": "t"},
        {"statement": "mnr helps", "outcome": "supported", "evidence": [2], "run_id": "rB", "task_id": "t"},
        {"statement": "mnr helps", "outcome": "tested", "evidence": [3], "run_id": "rC", "task_id": "t"},
    ]
    (tmp_path / "lessons.jsonl").write_bytes(b"\n".join(orjson.dumps(row) for row in lessons) + b"\n")
    res = CliRunner().invoke(app, ["claims", str(tmp_path), "--pack"])
    assert res.exit_code == 0 and "counter-evidence" in res.stdout and "mnr helps" in res.stdout


def test_end_to_end_from_lessons():
    # real path: lessons -> claim_assessments -> pack, with a contested claim surfaced first.
    lessons = [
        {"statement": "hard-neg helps", "outcome": "supported", "evidence": [1], "run_id": "rA", "task_id": "t"},
        {"statement": "mnr helps", "outcome": "supported", "evidence": [2], "run_id": "rB", "task_id": "t"},
        {"statement": "mnr helps", "outcome": "refuted", "evidence": [3], "run_id": "rC", "task_id": "t"},
    ]
    pack = build_context_pack(claim_assessments(lessons))
    assert pack["claims"][0]["statement"] == "mnr helps" and pack["claims"][0]["epistemic"] == "mixed"
    assert pack["n_contested"] == 1
