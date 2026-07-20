"""Finalize-boundary coverage for durable D8 research-source receipts."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from looplab.core.advisory_payloads import sanitize_research_memo_payload
from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.engine.claims import (
    claim_assessments,
    load_research_claims,
    record_research_claims,
)
from looplab.engine.lessons import LessonMemory
from looplab.trust.verify import verify_memo


def _node(node_id: int, *, generation: int = 0, tombstoned: bool = False) -> Node:
    return Node(
        id=node_id, operator="draft", idea=Idea(operator="draft"), metric=1.0,
        status=NodeStatus.evaluated, attempt=generation, tombstoned=tombstoned,
    )


def _finalize_claims(tmp_path, research, *, run_id: str = "run-finalize", nodes=(),
                     aborted=()) -> list[dict]:
    engine = SimpleNamespace(memory_dir=str(tmp_path))
    final = SimpleNamespace(
        research=research,
        run_id=run_id,
        task_id="task-finalize",
        direction="max",
        nodes={node.id: node for node in nodes},
        aborted_nodes=list(aborted),
    )
    LessonMemory(engine).store_research_claims(final)
    return load_research_claims(tmp_path)


def test_finalize_preserves_invalid_claim_slots_but_legacy_support_is_unverified(tmp_path):
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
    assert rows[0]["verification"]["verdict"] == "unverified"
    assert rows[0]["source_receipt"] == {
        "v": 2,
        "claims_total": 3,
        "claims_retained": 1,
        "claims_omitted": 2,
        "claims_receipt_known": False,
        "evidence_complete": False,
        "producer_complete": False,
    }
    # The retained positive citation stays drillable, but an omitted producer tail cannot establish a
    # one-sided durable verdict (CODEX AGENT).
    claim = claim_assessments([], research_claims=rows, structured=True)[0]
    assert claim["support"] == []
    assert claim["unverified"] == ["run-finalize:7"]
    assert claim["epistemic"] == "inconclusive"


def test_finalize_all_invalid_claims_emit_incomplete_source_sentinel(tmp_path):
    rows = _finalize_claims(tmp_path, [{"claims": [None, "not-a-claim"]}])

    assert len(rows) == 1
    assert rows[0]["record_kind"] == "source_receipt"
    assert "statement" not in rows[0]
    assert rows[0]["source_receipt"] == {
        "v": 2,
        "claims_total": 2,
        "claims_retained": 0,
        "claims_omitted": 2,
        "claims_receipt_known": False,
        "evidence_complete": True,
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
        receipt = rows[0]["source_receipt"]
        assert receipt == {
            "v": 2,
            "claims_total": 0,
            "claims_retained": 0,
            "claims_omitted": 0,
            "claims_receipt_known": index == 0,
            "evidence_complete": True,
            "producer_complete": index == 0,
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


def _verified_memo(state: RunState, *, count: int = 1) -> dict:
    raw = {"claims": [
        {"statement": f"verified claim {index}", "node_ids": [0]}
        for index in range(count)
    ]}
    memo = sanitize_research_memo_payload(raw)
    verification = verify_memo(memo, state, client=None)
    for verdict in verification["verdicts"]:
        verdict["verdict"] = "supported"
    return sanitize_research_memo_payload({**memo, "verification": verification})


def test_finalize_persists_only_generation_bound_verified_evidence(tmp_path):
    node = _node(0)
    state = RunState(run_id="run-finalize", task_id="task-finalize", direction="max")
    state.nodes = {0: node}
    memo = _verified_memo(state)

    rows = _finalize_claims(tmp_path, [memo], nodes=[node])

    assert rows[0]["verification"]["verdict"] == "supported"
    assert rows[0]["node_ids"] == [0]
    assert rows[0]["node_refs"] == [{"node_id": 0, "generation": 0}]
    assert rows[0]["source_receipt"] == {
        "v": 2, "claims_total": 1, "claims_retained": 1, "claims_omitted": 0,
        "claims_receipt_known": True, "evidence_complete": True,
        "producer_complete": True,
    }
    claim = claim_assessments([], research_claims=rows, structured=True)[0]
    assert claim["support"] == ["run-finalize:0"]


@pytest.mark.parametrize("lifecycle", ["reset", "tombstone", "abort"])
def test_finalize_downgrades_verification_when_node_lifecycle_changes(tmp_path, lifecycle):
    verified_node = _node(0)
    state = RunState(run_id="run-finalize", task_id="task-finalize", direction="max")
    state.nodes = {0: verified_node}
    memo = _verified_memo(state)

    final_node = _node(0, generation=1 if lifecycle == "reset" else 0,
                       tombstoned=lifecycle == "tombstone")
    rows = _finalize_claims(
        tmp_path, [memo], nodes=[final_node], aborted=[0] if lifecycle == "abort" else [])

    assert rows[0]["verification"]["verdict"] == "unverified"
    assert rows[0]["verification"]["note"] == "verification evidence lifecycle is stale"
    assert rows[0]["source_receipt"]["producer_complete"] is False
    claim = claim_assessments([], research_claims=rows, structured=True)[0]
    assert claim["support"] == []
    assert claim["unverified"] == ["run-finalize:0"]


def test_finalize_rejects_supported_verdict_with_subset_identity_receipt(tmp_path):
    urls = ["https://example.test/first", "https://example.test/second"]
    memo = sanitize_research_memo_payload({
        "claims": [{"statement": "both papers establish the result", "urls": urls}],
        "sources": [{"url": url, "title": url.rsplit("/", 1)[-1]} for url in urls],
    })
    identities = memo["claims"][0]["url_identities"]
    memo = sanitize_research_memo_payload({
        **memo,
        "verification": {
            "method": "llm",
            "verdicts": [{
                "statement": "both papers establish the result",
                "verdict": "supported",
                "note": "only the first citation was retained",
                # Simulate a legacy/custom event that asserted completeness over a strict subset.
                "evidence": {
                    "v": 1, "node_refs": [], "url_identities": identities[:1], "complete": True,
                },
            }],
        },
    })

    rows = _finalize_claims(tmp_path, [memo])

    assert rows[0]["verification"]["verdict"] == "unverified"
    assert rows[0]["verification"]["note"] \
        == "verification evidence identity does not cover the complete claim"
    assert rows[0]["source_receipt"]["producer_complete"] is False
    assert claim_assessments([], research_claims=rows, structured=True)[0]["support"] == []


def test_finalize_carries_more_than_64_claims_into_authoritative_omission_receipt(tmp_path):
    node = _node(0)
    state = RunState(run_id="run-finalize", task_id="task-finalize", direction="max")
    state.nodes = {0: node}
    memo = _verified_memo(state, count=65)

    rows = _finalize_claims(tmp_path, [memo], nodes=[node])

    assert len(rows) == 64
    assert rows[0]["source_receipt"] == {
        "v": 2, "claims_total": 65, "claims_retained": 64, "claims_omitted": 1,
        "claims_receipt_known": True, "evidence_complete": True,
        "producer_complete": False,
    }
    assert all(row["source_receipt"] == rows[0]["source_receipt"] for row in rows)
