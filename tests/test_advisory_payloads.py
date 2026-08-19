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


# ============================== the 2026-08-18 review finding: one join, three populations
#
# The verdict join is POSITIONAL — `verdicts[i]` belongs to `claims[i]` — so every reader of it has
# to enumerate exactly what the writer enumerated. `trust/memo_verify.py::_check_claims` emits one
# row per claim, dict-coercing a non-dict and filtering nothing; `sanitize_research_memo_payload`
# keeps a whitespace-only statement verbatim (`redact_persisted_text(" ") == " "`). Two readers
# filtered it out, each by its own rule, and each shifted the join by one for every blank above.

def _memo_with_a_blank_claim():
    return {
        "claims": [{"statement": " "},
                   {"statement": "A", "node_ids": [1]},
                   {"statement": "B", "node_ids": [2]}],
        "verification": {"method": "engine", "verdicts": [
            {"statement": "", "verdict": "unsupported", "note": "no evidence cited"},
            {"statement": "A", "verdict": "supported", "note": "node 1 exists"},
            {"statement": "B", "verdict": "supported", "note": "node 2 exists"}]},
    }


def test_a_blank_claim_does_not_shift_every_verdict_after_it():
    """Driven at the shape the sanitizer really produces. Before the fix both real claims came back
    `unverified` with "verification alignment mismatch", their true verdicts were counted as
    unmatched rows, and that false tally went on into `verdict_tally` / `memo_verdict_cue` prompts."""
    from looplab.core.advisory_payloads import memo_verification_view

    view = memo_verification_view(_memo_with_a_blank_claim())
    assert [row["statement"] for row in view["rows"]] == ["A", "B"]
    assert [row["verdict"] for row in view["rows"]] == ["supported", "supported"]
    assert all(row["aligned"] for row in view["rows"])
    assert view["counts"]["unverified"] == 0
    assert view["counts"]["unmatched_verdicts"] == 0     # the blank consumed its position
    assert view["counts"]["claims"] == 2                 # ...and is not a claim anyone counts
    assert view["counts"]["total_verdicts"] == 3         # the block's own receipt is untouched


def test_a_block_genuinely_longer_than_its_claims_still_says_so():
    """The blank claim must not become a way to hide a real mismatch: a verdict row with no claim at
    all is still counted and still reported."""
    from looplab.core.advisory_payloads import memo_verification_view

    memo = _memo_with_a_blank_claim()
    memo["verification"]["verdicts"].append({"statement": "C", "verdict": "supported"})
    view = memo_verification_view(memo)
    assert view["counts"]["unmatched_verdicts"] == 1
    assert [row["statement"] for row in view["rows"]] == ["A", "B"]


def test_the_rendered_memo_tags_each_claim_with_its_own_verdict():
    """The third population: `run_tools` filtered claims with a TRUTHY test, so `" "` was kept there
    and dropped in the view — and `_cite` reads `view["rows"][index]` positionally, rendering "A"
    under "B"'s verdict and leaving "B" untagged."""
    from looplab.core.models import RunState
    from looplab.tools.run_tools import RunTools

    state = RunState()
    state.research = [_memo_with_a_blank_claim()]
    text = RunTools(lambda: state)._research_memo(state)

    claim_lines = [line.strip() for line in text.splitlines()
                   if line.strip().startswith("- ") and line.rstrip().endswith(("A", "B"))
                   or ("] A" in line or "] B" in line)]
    assert len(claim_lines) == 2, text
    assert all(line.startswith("- [SUPPORTED]") for line in claim_lines), text
    assert "evidence: #1" in text and "evidence: #2" in text, text
