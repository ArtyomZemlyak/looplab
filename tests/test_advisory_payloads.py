"""Focused trust-ordering regressions for bounded advisory payloads."""

from looplab.core.advisory_payloads import (
    bounded_cross_run_advisory_receipt,
    sanitize_report_payload,
    sanitize_research_memo_payload,
)


def _cross_run_receipt() -> dict:
    segment = {
        "read_complete": True,
        "rows_total": 0,
        "rows_retained": 0,
        "rows_quarantined": 0,
        "malformed_rows": 0,
        "invalid_rows": 0,
    }
    return {
        "v": 2,
        "scope_task": "toy",
        "excluded_run": "run-before-restart",
        "n_lessons": 0,
        "n_capsules": 1,
        "n_research": 0,
        "concept_scope": {
            "scope_complete": True,
            "scope_unknown_capsules": 0,
            "scope_fingerprint_unknown_capsules": 0,
            "scope_fingerprint_items_omitted": 0,
            "scope_direction_unknown_capsules": 0,
        },
        "claim_source": {
            "v": 1,
            "receipt_known": True,
            "source_complete": True,
            "read_complete": True,
            "research_source_complete": True,
            "lessons": dict(segment),
            "research": dict(segment),
            "snapshot_digest": "a" * 64,
        },
        "corpus_digest": "b" * 64,
        "render_digest": "c" * 64,
    }


def test_cross_run_receipt_accepts_only_the_two_closed_current_shapes():
    available = _cross_run_receipt()
    unavailable = {
        "v": 2,
        "status": "unavailable",
        "complete": False,
        "governance": {
            "v": 1,
            "status": "unavailable",
            "complete": False,
            "code": "governance_ledger_unavailable",
            "ledger": "concept_aliases",
            "reason": "torn_tail",
        },
    }

    assert bounded_cross_run_advisory_receipt(available) == available
    assert bounded_cross_run_advisory_receipt(unavailable) == unavailable
    assert bounded_cross_run_advisory_receipt({
        **available,
        "status": "unavailable",
        "complete": False,
        "governance": unavailable["governance"],
    }) == {}


def test_cross_run_receipt_rejects_oversize_unknown_and_secret_bearing_replay_data():
    available = _cross_run_receipt()
    assert bounded_cross_run_advisory_receipt({
        **available,
        "scope_task": "x" * 501,
    }) == {}
    assert bounded_cross_run_advisory_receipt({
        **available,
        "claim_source": {
            **available["claim_source"],
            "api_key": "sk-this-must-not-be-forwarded",
        },
    }) == {}
    assert bounded_cross_run_advisory_receipt({
        **available,
        "concept_scope": {
            **available["concept_scope"],
            "future_counter": 1,
        },
    }) == {}
    assert bounded_cross_run_advisory_receipt({
        **available,
        "scope_task": "authorization: bearer forged-credential",
    }) == {}


def test_memo_sanitizer_preserves_late_unsupported_verdict_under_saturated_payload():
    verdicts = [
        {
            "statement": f"claim-{index}: " + "s" * 4_000,
            "verdict": "unsupported" if index == 63 else "supported",
            "note": f"reason-{index}: " + "n" * 1_000,
        }
        for index in range(64)
    ]
    clean = sanitize_research_memo_payload({
        "summary": "summary " + "s" * 10_000,
        "reasoning": "r" * 100_000,
        "findings": ["f" * 5_000 for _ in range(32)],
        "recommended_directions": ["d" * 5_000 for _ in range(16)],
        "proposed_ideas": [{"idea": "i" * 5_000} for _ in range(16)],
        "verification": {
            "verdicts": verdicts,
            "method": "deterministic",
            "unsupported": 0,
        },
    })

    projected = clean["verification"]
    assert len(projected["verdicts"]) == 64
    assert projected["verdicts"][-1]["verdict"] == "unsupported"
    assert projected["verdicts"][-1]["statement"].startswith("claim-63:")
    assert projected["verdicts"][-1]["note"].startswith("reason-63:")
    assert projected["unsupported"] == 1
    assert projected["total_verdicts"] == 64
    assert projected["omitted_verdicts"] == 0


def test_memo_sanitizer_persists_verifier_omissions_across_replay_sanitization():
    verdicts = [
        {"statement": f"claim-{index}", "verdict": "supported", "note": "checked"}
        for index in range(65)
    ]
    verdicts[-1]["verdict"] = "unsupported"

    written = sanitize_research_memo_payload({
        "verification": {"method": "llm", "verdicts": verdicts},
    })
    replayed = sanitize_research_memo_payload(written)

    for projected in (written["verification"], replayed["verification"]):
        assert len(projected["verdicts"]) == 64
        assert projected["unsupported"] == 0
        assert projected["total_verdicts"] == 65
        assert projected["omitted_verdicts"] == 1


def test_memo_sanitizer_ignores_inconsistent_verifier_omission_metadata():
    projected = sanitize_research_memo_payload({
        "verification": {
            "verdicts": [{"statement": "visible", "verdict": "supported"}],
            "total_verdicts": 1_000_000,
            "omitted_verdicts": 0,
        },
    })["verification"]

    assert projected["total_verdicts"] == 1
    assert projected["omitted_verdicts"] == 0


def test_memo_sanitizer_records_claim_and_evidence_omissions_idempotently():
    claims = [{
        "statement": f"claim-{index}",
        "node_ids": list(range(9)),
        "urls": [f"https://example.test/{item}" for item in range(5)],
    } for index in range(65)]

    written = sanitize_research_memo_payload({"claims": claims})
    replayed = sanitize_research_memo_payload(written)

    for projected in (written, replayed):
        assert len(projected["claims"]) == 64
        assert projected["claims_receipt"] == {
            "v": 1, "total": 65, "retained": 64, "omitted": 1, "complete": False,
        }
        claim = projected["claims"][0]
        assert claim["node_ids"] == list(range(8))
        assert len(claim["urls"]) == len(claim["url_identities"]) == 4
        assert claim["evidence_receipt"] == {
            "v": 1,
            "node_refs_total": 9, "node_refs_retained": 8, "node_refs_omitted": 1,
            "url_refs_total": 5, "url_refs_retained": 4, "url_refs_omitted": 1,
            "complete": False,
        }


def test_memo_sanitizer_rejects_negative_or_understated_omission_receipts():
    projected = sanitize_research_memo_payload({
        "claims": [{"statement": "visible"}],
        "claims_receipt": {
            "v": 1, "total": 0, "retained": 1, "omitted": -1, "complete": False,
        },
    })
    assert projected["claims_receipt"] == {
        "v": 1, "total": 1, "retained": 1, "omitted": 0, "complete": True,
    }


def test_report_sanitizer_reserves_shared_budget_for_caveats():
    clean = sanitize_report_payload({
        "what_worked": ["w" * 5_000 for _ in range(32)],
        "learnings": ["l" * 5_000 for _ in range(32)],
        "what_didnt": ["d" * 5_000 for _ in range(32)],
        "next_directions": ["n" * 5_000 for _ in range(32)],
        "caveats": ["critical advisory caveat"],
    })

    assert clean["caveats"] == ["critical advisory caveat"]
