"""Fail-closed health contract for Part IV/V operator-governance ledgers."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from looplab.cli import app
from looplab.engine.claims import (
    atlas_for_memory,
    cross_run_retrieve,
    load_claim_decisions,
    record_claim_decision,
)
from looplab.engine.concept_registry import (
    concept_governance_snapshot,
    load_concept_aliases,
    load_concept_splits,
    record_concept_alias,
)
from looplab.engine.concept_steward import concept_curation_snapshot
from looplab.engine.governance_health import (
    GovernanceLedgerUnavailable,
    cross_run_governance_snapshot,
    read_curation_rows,
)
from looplab.engine.memory import ConceptCapsuleStore, build_concept_capsule
from looplab.tools.concept_tools import ConceptGovernanceTools
from looplab.tools.cross_run_tools import CrossRunTools


def _write_rows(path, rows) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _seed_cross_run(memory_dir) -> None:
    memory_dir.mkdir(parents=True, exist_ok=True)
    ConceptCapsuleStore(memory_dir / "concept_capsules.jsonl").add(build_concept_capsule(
        run_id="r1", task_id="t", fingerprint=["task:t"], direction="max",
        concepts=["data/hn", "loss/mnr"], concept_outcomes={"data/hn": 0.8},
    ))
    (memory_dir / "lessons.jsonl").write_text(json.dumps({
        "statement": "hard negatives improve recall", "outcome": "supported",
        "evidence": [1], "run_id": "r1", "task_id": "t",
    }) + "\n", encoding="utf-8")


@pytest.mark.parametrize(("filename", "payload", "reader", "reason"), [
    ("concept_aliases.jsonl", b'{"action":"purge"', load_concept_aliases, "torn_tail"),
    ("concept_aliases.jsonl", b'[]\n', load_concept_aliases, "non_object"),
    ("concept_splits.jsonl", b'{not-json}\n', load_concept_splits, "malformed_json"),
    ("claim_decisions.jsonl", b'\n', load_claim_decisions, "blank_row"),
])
def test_physical_governance_damage_is_unavailable_not_an_empty_policy(
        tmp_path, filename, payload, reader, reason):
    (tmp_path / filename).write_bytes(payload)

    with pytest.raises(GovernanceLedgerUnavailable) as exc:
        reader(tmp_path)

    assert exc.value.reason == reason
    assert str(tmp_path) not in str(exc.value)


def test_duplicate_json_member_cannot_select_an_ambiguous_governance_action(tmp_path):
    (tmp_path / "concept_aliases.jsonl").write_bytes(
        b'{"v":1,"action":"purge","action":"clear","from":"data/hn","to":""}\n')

    with pytest.raises(GovernanceLedgerUnavailable) as exc:
        load_concept_aliases(tmp_path)

    assert exc.value.reason == "malformed_json"


def _task_facets_curation_row(**overrides):
    row = {
        "v": 2,
        "curation_key": "facets:v2:" + "a" * 64,
        "source_key": "source:v1:" + "b" * 64,
        "run_id": "run-a",
        "task_id": "task-a",
        "finish_seq": 7,
        "input_digest": "c" * 64,
        "input_schema": "finalize-task-facets/v1",
        "model": "model-a",
        "parser": "tool-call-v1",
        "outcome": "already-governed",
        "auto": False,
        "auto_requested": True,
        "proposals": {"task_id": "task-a", "facets": {"domain": "retrieval"}},
        "receipt": None,
    }
    row.update(overrides)
    return row


def test_task_facets_paid_history_accepts_known_finalize_schema(tmp_path):
    path = tmp_path / "task_facets_curation_log.jsonl"
    expected = _task_facets_curation_row()
    _write_rows(path, [expected])

    assert read_curation_rows(path) == [expected]


@pytest.mark.parametrize(("tail", "reason"), [
    ({"v": 999}, "unsupported_schema"),
    ({"v": 2, "outcome": "future-auto-apply"}, "invalid_record"),
])
def test_task_facets_paid_history_fails_closed_after_a_valid_terminal(
        tmp_path, tail, reason):
    path = tmp_path / "task_facets_curation_log.jsonl"
    _write_rows(path, [_task_facets_curation_row(), tail])

    with pytest.raises(GovernanceLedgerUnavailable) as exc:
        read_curation_rows(path)

    assert exc.value.ledger == "task_facets_curation"
    assert exc.value.reason == reason
    assert exc.value.public_receipt()["complete"] is False


@pytest.mark.parametrize(("filename", "row", "reader", "reason"), [
    ("concept_aliases.jsonl",
     {"v": 999, "action": "purge", "from": "data/hn", "to": ""},
     load_concept_aliases, "unsupported_schema"),
    ("concept_aliases.jsonl",
     {"v": 1, "action": "future-unpurge", "from": "data/hn"},
     load_concept_aliases, "unknown_action"),
    ("concept_splits.jsonl",
     {"v": 1, "action": "set", "from": "data/hn",
      "rules": [{"to": "data/a", "when_any": ["a"]}, "poison"], "default": ""},
     load_concept_splits, "invalid_record"),
    ("claim_decisions.jsonl",
     {"claim_key_version": 999, "statement": "x helps", "decision": "rejected"},
     load_claim_decisions, "unsupported_schema"),
    ("claim_decisions.jsonl",
     {"statement": "x helps", "decision": "future-reconsider"},
     load_claim_decisions, "unknown_action"),
])
def test_unknown_or_partial_governance_schema_fails_closed(
        tmp_path, filename, row, reader, reason):
    _write_rows(tmp_path / filename, [row])

    with pytest.raises(GovernanceLedgerUnavailable) as exc:
        reader(tmp_path)

    assert exc.value.reason == reason


@pytest.mark.parametrize("collision", [False, True])
def test_duplicate_action_id_is_unavailable_even_when_payload_is_identical(tmp_path, collision):
    first = {
        "v": 1, "action": "set", "from": "a", "to": "b",
        "action_id": "same-action",
    }
    second = {**first, "to": "c" if collision else "b"}
    _write_rows(tmp_path / "concept_aliases.jsonl", [first, second])

    with pytest.raises(GovernanceLedgerUnavailable) as exc:
        concept_governance_snapshot(tmp_path)

    assert exc.value.reason == "duplicate_action_id"


def test_local_and_cross_ledger_revision_collisions_are_unavailable(tmp_path):
    _write_rows(tmp_path / "concept_aliases.jsonl", [{
        "v": 1, "action": "set", "from": "a", "to": "b",
        "revision": 2, "governance_revision": 1,
    }])
    with pytest.raises(GovernanceLedgerUnavailable) as local:
        concept_governance_snapshot(tmp_path)
    assert local.value.reason == "revision_mismatch"

    _write_rows(tmp_path / "concept_aliases.jsonl", [{
        "v": 1, "action": "set", "from": "a", "to": "b",
        "revision": 1, "governance_revision": 1,
    }])
    _write_rows(tmp_path / "concept_splits.jsonl", [{
        "v": 1, "action": "set", "from": "coarse",
        "rules": [{"to": "fine", "when_any": ["match"]}], "default": "",
        "revision": 1, "governance_revision": 1,
    }])
    with pytest.raises(GovernanceLedgerUnavailable) as global_collision:
        concept_governance_snapshot(tmp_path)
    assert global_collision.value.reason == "revision_collision"

    # A modern explicit global revision cannot reuse the implicit prefix occupied by a
    # legacy row merely because it lives in the sibling physical ledger.
    _write_rows(tmp_path / "concept_aliases.jsonl", [{
        "v": 1, "from": "legacy-a", "to": "legacy-b",
    }])
    _write_rows(tmp_path / "concept_splits.jsonl", [{
        "v": 1, "action": "set", "from": "coarse",
        "rules": [{"to": "fine", "when_any": ["match"]}], "default": "",
        "revision": 1, "governance_revision": 1,
    }])
    with pytest.raises(GovernanceLedgerUnavailable) as implicit_collision:
        concept_governance_snapshot(tmp_path)
    assert implicit_collision.value.reason == "revision_collision"


def test_combined_snapshot_binds_all_policy_maps_to_matching_revisions(tmp_path):
    alias = record_concept_alias(
        tmp_path, from_concept="a", to_concept="b", expected_revision=0,
        expected_governance_revision=0, action_id="alias-one")
    claim = record_claim_decision(
        tmp_path, statement="b improves recall", decision="pinned",
        expected_revision=0, action_id="claim-one")

    snapshot = cross_run_governance_snapshot(tmp_path)

    assert snapshot["complete"] is True and snapshot["aliases"] == {"a": "b"}
    assert snapshot["alias_revision"] == alias["revision"] == 1
    assert snapshot["concept_governance_revision"] == alias["governance_revision"] == 1
    assert snapshot["claim_revision"] == claim["revision"] == 1
    assert any(row["decision"] == "pinned" for row in snapshot["decisions"].values())
    atlas = atlas_for_memory(tmp_path, lessons=[], capsules=[], research_claims=[])
    assert atlas["governance"]["revisions"] == {
        "claims": 1, "concept_aliases": 1, "concept_splits": 0,
        "concept_governance": 1,
    }
    retrieval = cross_run_retrieve(
        tmp_path, "recall", lessons=[], capsules=[], research_claims=[])
    assert retrieval["receipt"]["governance_complete"] is True
    assert retrieval["receipt"]["claim_governance_revision"] == 1


def test_idempotent_alias_retry_still_refuses_an_unhealthy_sibling_ledger(tmp_path):
    receipt = record_concept_alias(
        tmp_path, from_concept="a", to_concept="b", expected_revision=0,
        expected_governance_revision=0, action_id="alias-retry")
    split_path = tmp_path / "concept_splits.jsonl"
    split_path.write_bytes(b'{"action":"clear"')
    alias_before = (tmp_path / "concept_aliases.jsonl").read_bytes()

    with pytest.raises(GovernanceLedgerUnavailable, match="concept_splits"):
        record_concept_alias(
            tmp_path, from_concept="a", to_concept="b", expected_revision=0,
            expected_governance_revision=0, action_id="alias-retry")

    assert receipt["revision"] == 1
    assert (tmp_path / "concept_aliases.jsonl").read_bytes() == alias_before


def test_corrupt_alias_policy_reaches_atlas_retrieval_curation_tools_and_cli(tmp_path):
    _seed_cross_run(tmp_path)
    poisoned = "SECRET_ROW_MUST_NOT_LEAK"
    (tmp_path / "concept_aliases.jsonl").write_text(
        '{"action":"purge","from":"data/hn"}\n' + poisoned + "\n",
        encoding="utf-8",
    )

    for project in (
        lambda: atlas_for_memory(tmp_path),
        lambda: cross_run_retrieve(tmp_path, "hard negatives"),
        lambda: concept_curation_snapshot(tmp_path),
    ):
        with pytest.raises(GovernanceLedgerUnavailable):
            project()

    assert CrossRunTools(tmp_path).execute("cross_run_atlas", {}) == "(cross-run tool unavailable)"
    assert ConceptGovernanceTools(tmp_path).execute(
        "concept_taxonomy", {}) == "(concept governance unavailable: ledger health failure)"

    cli = CliRunner().invoke(app, ["cross-run-concepts", str(tmp_path)])
    assert cli.exit_code == 2
    assert "governance unavailable" in cli.output
    assert poisoned not in cli.output and str(tmp_path) not in cli.output


def test_corrupt_claim_policy_reaches_claims_cli_without_leaking_content(tmp_path):
    _seed_cross_run(tmp_path)
    poisoned = "RAW_CLAIM_POLICY_MUST_NOT_LEAK"
    (tmp_path / "claim_decisions.jsonl").write_text(poisoned + "\n", encoding="utf-8")

    with pytest.raises(GovernanceLedgerUnavailable):
        atlas_for_memory(tmp_path)
    with pytest.raises(GovernanceLedgerUnavailable):
        cross_run_retrieve(tmp_path, "recall")
    assert CrossRunTools(tmp_path).execute("cross_run_claims", {}) == "(cross-run tool unavailable)"

    cli = CliRunner().invoke(app, ["claims", str(tmp_path), "--structured"])
    assert cli.exit_code == 2
    assert "ledger=claim_decisions" in cli.output
    assert poisoned not in cli.output and str(tmp_path) not in cli.output
