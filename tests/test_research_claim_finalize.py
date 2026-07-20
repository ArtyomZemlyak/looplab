"""Finalize-boundary coverage for durable D8 research-source receipts."""
from __future__ import annotations

from types import SimpleNamespace

from looplab.engine.claims import (
    claim_assessments,
    load_research_claims,
    record_research_claims,
)
from looplab.engine.lessons import LessonMemory


def _finalize_claims(tmp_path, research, *, run_id: str = "run-finalize") -> list[dict]:
    engine = SimpleNamespace(memory_dir=str(tmp_path))
    final = SimpleNamespace(
        research=research,
        run_id=run_id,
        task_id="task-finalize",
        direction="max",
    )
    LessonMemory(engine).store_research_claims(final)
    return load_research_claims(tmp_path)


def test_finalize_preserves_invalid_claim_slots_and_verifier_alignment(tmp_path):
    statement = "hard negatives improve retrieval"
    rows = _finalize_claims(tmp_path, [{
        "claims": [None, {"statement": statement, "node_ids": [7]}, {"statement": ""}],
        "verification": {
            "method": "deterministic",
            "verdicts": [
                {"statement": "invalid slot", "verdict": "unsupported"},
                {"statement": statement, "verdict": "supported", "note": "node agrees"},
                {"statement": "", "verdict": "supported"},
            ],
        },
    }])

    assert len(rows) == 1
    assert rows[0]["statement"] == statement
    assert rows[0]["verification"]["verdict"] == "supported"
    assert rows[0]["source_receipt"] == {
        "v": 1,
        "claims_total": 3,
        "claims_retained": 1,
        "claims_omitted": 2,
        "producer_complete": False,
    }
    # The retained positive citation stays drillable, but an omitted producer tail cannot establish a
    # one-sided durable verdict (CODEX AGENT).
    claim = claim_assessments([], research_claims=rows, structured=True)[0]
    assert claim["support"] == ["run-finalize:7"]
    assert claim["epistemic"] == "inconclusive"


def test_finalize_all_invalid_claims_emit_incomplete_source_sentinel(tmp_path):
    rows = _finalize_claims(tmp_path, [{"claims": [None, "not-a-claim"]}])

    assert len(rows) == 1
    assert rows[0]["record_kind"] == "source_receipt"
    assert "statement" not in rows[0]
    assert rows[0]["source_receipt"] == {
        "v": 1,
        "claims_total": 2,
        "claims_retained": 0,
        "claims_omitted": 2,
        "producer_complete": False,
    }


def test_finalize_malformed_memo_claim_shapes_are_opaque_omitted_slots(tmp_path):
    rows = _finalize_claims(tmp_path, [
        None,
        {"summary": "legacy memo without the D8 field"},
        {"claims": []},
        {"claims": None},
        {"claims": {"statement": "scalar dict must not be trusted"}},
        {"claims": "character iteration must not manufacture cardinality"},
        {"claims": 7},
    ])

    assert len(rows) == 1
    assert rows[0]["record_kind"] == "source_receipt"
    assert rows[0]["source_receipt"]["claims_total"] == 5
    assert rows[0]["source_receipt"]["claims_omitted"] == 5
    assert rows[0]["source_receipt"]["producer_complete"] is False


def test_finalize_malformed_outer_research_shape_is_not_walked_as_memos(tmp_path):
    rows = _finalize_claims(tmp_path, {
        "claims": [{"statement": "nested claim must not escape a malformed outer shape"}],
    })

    assert len(rows) == 1
    assert rows[0]["record_kind"] == "source_receipt"
    assert rows[0]["source_receipt"]["claims_total"] == 1
    assert rows[0]["source_receipt"]["claims_retained"] == 0


def test_finalize_explicit_empty_d8_snapshot_replaces_stale_rows_with_zero_receipt(tmp_path):
    for index, research in enumerate(([], [{"claims": []}])):
        memory = tmp_path / str(index)
        record_research_claims(
            memory,
            run_id="run-finalize",
            task_id="task-finalize",
            claims=[{"statement": "stale claim", "node_ids": [1]}],
            direction="max",
        )

        rows = _finalize_claims(memory, research)
        # CODEX AGENT: an authoritative zero is durable evidence, not the absence of a D8 producer.
        # Finalize must clear stale claims while retaining the zero-row denominator used by fail-closed
        # claim-source projections.
        assert len(rows) == 1
        assert rows[0]["record_kind"] == "source_receipt"
        assert "statement" not in rows[0]
        assert rows[0]["source_receipt"] == {
            "v": 1,
            "claims_total": 0,
            "claims_retained": 0,
            "claims_omitted": 0,
            "producer_complete": True,
        }


def test_finalize_absent_or_legacy_d8_source_does_not_erase_existing_rows(tmp_path):
    for index, research in enumerate((None, [{"summary": "pre-D8 memo"}])):
        memory = tmp_path / str(index)
        record_research_claims(
            memory,
            run_id="run-finalize",
            task_id="task-finalize",
            claims=[{"statement": "retained claim", "node_ids": [1]}],
            direction="max",
        )

        rows = _finalize_claims(memory, research)
        assert [row.get("statement") for row in rows] == ["retained claim"]
