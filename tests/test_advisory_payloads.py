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


def test_report_sanitizer_reserves_shared_budget_for_caveats():
    clean = sanitize_report_payload({
        "what_worked": ["w" * 5_000 for _ in range(32)],
        "learnings": ["l" * 5_000 for _ in range(32)],
        "what_didnt": ["d" * 5_000 for _ in range(32)],
        "next_directions": ["n" * 5_000 for _ in range(32)],
        "caveats": ["critical advisory caveat"],
    })

    assert clean["caveats"] == ["critical advisory caveat"]
