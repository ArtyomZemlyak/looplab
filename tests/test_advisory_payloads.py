"""Focused trust-ordering regressions for bounded advisory payloads."""

from looplab.core.advisory_payloads import (
    sanitize_report_payload,
    sanitize_research_memo_payload,
)


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
