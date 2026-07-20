"""Fail-closed health contract for Part IV/V operator-governance ledgers."""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event

import pytest
from typer.testing import CliRunner

from looplab.cli import app
from looplab.engine.claims import (
    atlas_for_memory,
    cross_run_retrieve,
    load_claim_lessons,
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
    project_governed_sources,
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


def test_permission_error_cannot_be_laundered_into_missing_governance():
    secret = "C:/private/operator-policy.jsonl"

    class _SuppressedExistsPath:
        def exists(self):
            return False

        def read_bytes(self):
            raise PermissionError(secret)

    with pytest.raises(GovernanceLedgerUnavailable) as exc:
        from looplab.engine.governance_health import read_governance_rows
        read_governance_rows(
            _SuppressedExistsPath(), ledger="claim_decisions", validate=lambda _row: None)

    assert exc.value.reason == "storage_unreadable"
    assert secret not in str(exc.value)


def test_permission_error_cannot_be_laundered_into_missing_evidence(tmp_path, monkeypatch):
    target = tmp_path / "lessons.jsonl"
    secret = "C:/private/evidence.jsonl"
    original_read_bytes = Path.read_bytes

    def denied(self, *args, **kwargs):
        if self == target:
            raise PermissionError(secret)
        return original_read_bytes(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", denied)

    with pytest.raises(PermissionError, match="private"):
        load_claim_lessons(tmp_path)


@pytest.mark.parametrize("project", [
    lambda base: project_governed_sources(base, lambda governance: governance),
    concept_governance_snapshot,
])
def test_permission_error_cannot_be_laundered_into_missing_memory_dir(
        tmp_path, monkeypatch, project):
    secret = "C:/private/cross-run-memory"
    original_stat = Path.stat

    def denied(self, *args, **kwargs):
        if self == tmp_path:
            raise PermissionError(secret)
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)

    with pytest.raises(PermissionError, match="private"):
        project(tmp_path)


def test_duplicate_json_member_cannot_select_an_ambiguous_governance_action(tmp_path):
    (tmp_path / "concept_aliases.jsonl").write_bytes(
        b'{"v":1,"action":"purge","action":"clear","from":"data/hn","to":""}\n')

    with pytest.raises(GovernanceLedgerUnavailable) as exc:
        load_concept_aliases(tmp_path)

    assert exc.value.reason == "malformed_json"


def _task_facets_curation_row(**overrides):
    row = {
        "v": 2,
        "run_id": "run-a",
        "task_id": "task-a",
        "finish_seq": 7,
        "input_digest": "c" * 64,
        "input_schema": "finalize-task-facets/v1",
        "model": "model-a",
        "parser": "tool_call_once",
        "outcome": "already-governed",
        "auto": False,
        "auto_requested": True,
        "proposals": {"task_id": "task-a", "facets": {"domain": "retrieval"}},
        "receipt": None,
    }
    row.update(overrides)
    encoded_source = json.dumps({
        "v": 1, "run_id": row["run_id"], "task_id": row["task_id"],
        "finish_seq": row["finish_seq"],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    row.setdefault("source_key", "source:v1:" + hashlib.sha256(encoded_source).hexdigest())
    encoded_key = json.dumps({
        "v": 2, "kind": "facets", "task_id": row["task_id"],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    row.setdefault("curation_key", "facets:v2:" + hashlib.sha256(encoded_key).hexdigest())
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


@pytest.mark.parametrize("mutation", [
    {"action_id": ["not-an-http-receipt"]},
    {"source_key": "source:v1:" + "0" * 64},
    {"curation_key": "facets:v2:" + "0" * 64},
    {"input_digest": "not-a-digest"},
    {"input_schema": "future-or-forged"},
    {"model": ""},
    {"parser": "tool_call"},
    {"auto": True},
    {"receipt": {"forged": True}},
])
def test_v2_curation_semantic_identity_fails_closed(tmp_path, mutation):
    path = tmp_path / "task_facets_curation_log.jsonl"
    _write_rows(path, [_task_facets_curation_row(**mutation)])

    with pytest.raises(GovernanceLedgerUnavailable) as exc:
        read_curation_rows(path)

    assert exc.value.reason == "invalid_record"


@pytest.mark.parametrize("row", [
    {
        "v": 1, "run_id": "legacy-run", "task_id": "task", "outcome": "proposed",
        "auto": False, "auto_requested": False, "proposals": {}, "receipt": None,
        "curation_key": "concept:v2:" + "a" * 64,
    },
    {
        "v": 1, "action": "steward-invocation", "from": "concept",
        "action_id": "http-action", "by": "operator", "at": "now", "outcome": "empty",
        "proposals": {}, "receipt": None, "curation_key": "concept:v2:" + "a" * 64,
    },
])
def test_v1_curation_rows_cannot_forge_v2_semantic_identity(tmp_path, row):
    path = tmp_path / "concept_curation_log.jsonl"
    _write_rows(path, [row])

    with pytest.raises(GovernanceLedgerUnavailable) as exc:
        read_curation_rows(path)

    assert exc.value.reason == "invalid_record"


def test_v2_curation_accepts_distinct_unavailable_sources_then_one_terminal(tmp_path):
    path = tmp_path / "task_facets_curation_log.jsonl"
    rows = [
        _task_facets_curation_row(
            run_id="run-a", outcome="unavailable",
            proposals={"task_id": "task-a", "facets": {}}),
        _task_facets_curation_row(
            run_id="run-b", outcome="unavailable",
            proposals={"task_id": "task-a", "facets": {}}),
        _task_facets_curation_row(run_id="run-c"),
    ]
    _write_rows(path, rows)

    assert read_curation_rows(path) == rows


@pytest.mark.parametrize(("rows", "reason"), [
    ([
        _task_facets_curation_row(
            outcome="unavailable", proposals={"task_id": "task-a", "facets": {}}),
        _task_facets_curation_row(
            outcome="unavailable", proposals={"task_id": "task-a", "facets": {}}),
    ], "duplicate_action_id"),
    ([
        _task_facets_curation_row(),
        _task_facets_curation_row(run_id="run-b"),
    ], "revision_collision"),
    ([
        _task_facets_curation_row(),
        _task_facets_curation_row(
            run_id="run-b", outcome="unavailable",
            proposals={"task_id": "task-a", "facets": {}}),
    ], "revision_collision"),
])
def test_v2_curation_rejects_impossible_semantic_sequences(tmp_path, rows, reason):
    path = tmp_path / "task_facets_curation_log.jsonl"
    _write_rows(path, rows)

    with pytest.raises(GovernanceLedgerUnavailable) as exc:
        read_curation_rows(path)

    assert exc.value.reason == reason


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


@pytest.mark.parametrize("mutation", [
    {"statement": "x\n improves recall"},
    {"scope": "task\nscope"},
    {"metric": "recall\tmetric"},
    {"key": "forged-key"},
    {"claim_uid": "clm_" + "0" * 32},
])
def test_current_claim_decision_identity_must_be_exactly_canonical(tmp_path, mutation):
    path = tmp_path / "claim_decisions.jsonl"
    record_claim_decision(
        tmp_path, statement="x improves recall", decision="pinned",
        scope="task", metric="recall", action_id="canonical")
    row = json.loads(path.read_text(encoding="utf-8"))
    row.update(mutation)
    _write_rows(path, [row])

    with pytest.raises(GovernanceLedgerUnavailable) as exc:
        load_claim_decisions(tmp_path)

    assert exc.value.reason == "invalid_record"


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


def test_governed_source_projection_uses_one_canonical_lock_order(tmp_path, monkeypatch):
    import looplab.events.eventstore as eventstore_module

    entered: list[str] = []
    active: list[str] = []

    @contextmanager
    def observed_lock(path, *, required=False):
        assert required is True
        name = path.name
        entered.append(name)
        active.append(name)
        try:
            yield
        finally:
            assert active.pop() == name

    monkeypatch.setattr(eventstore_module, "_interprocess_lock", observed_lock)

    def project(governance):
        assert active == [
            "concept_governance.lock",
            "claim_decisions.jsonl.lock",
            "concept_capsules.jsonl.lock",
            "external_evidence.jsonl.lock",
            "lessons.jsonl.lock",
            "research_claims.jsonl.lock",
        ]
        return governance

    snapshot = project_governed_sources(
        tmp_path, project, include_concepts=True,
        # Deliberately unsorted: the transaction owns deterministic source ordering.
        source_names=(
            "research_claims.jsonl", "lessons.jsonl", "concept_capsules.jsonl"),
        source_paths=(tmp_path / "external_evidence.jsonl",),
    )

    assert entered == [
        "concept_governance.lock", "claim_decisions.jsonl.lock",
        "concept_capsules.jsonl.lock", "external_evidence.jsonl.lock", "lessons.jsonl.lock",
        "research_claims.jsonl.lock",
    ]
    assert snapshot["claim_revision"] == snapshot["concept_governance_revision"] == 0


def test_governed_source_projection_rejects_external_and_policy_paths(tmp_path):
    for source in (
            tmp_path.parent / "outside.jsonl",
            tmp_path / "claim_decisions.jsonl",
            tmp_path / "nested" / "evidence.jsonl"):
        with pytest.raises(ValueError, match="governed source path"):
            project_governed_sources(
                tmp_path, lambda governance: governance, source_paths=(source,))


def test_cross_run_cli_payloads_are_built_inside_one_governed_source_snapshot(
        tmp_path, monkeypatch):
    import looplab.engine.claims as claims_module
    import looplab.engine.memory as memory_module
    import looplab.events.eventstore as eventstore_module

    _seed_cross_run(tmp_path)
    active: list[str] = []
    phase = {"name": ""}
    observed: list[str] = []
    expected = {
        "concepts": [
            "concept_governance.lock", "claim_decisions.jsonl.lock",
            "concept_capsules.jsonl.lock",
        ],
        "digest": [
            "concept_governance.lock", "claim_decisions.jsonl.lock",
            "concept_capsules.jsonl.lock",
        ],
        "atlas": [
            "concept_governance.lock", "claim_decisions.jsonl.lock",
            "concept_capsules.jsonl.lock", "lessons.jsonl.lock",
            "research_claims.jsonl.lock",
        ],
        "pack": [
            "concept_governance.lock", "claim_decisions.jsonl.lock",
            "concept_capsules.jsonl.lock", "lessons.jsonl.lock",
            "research_claims.jsonl.lock",
        ],
    }

    @contextmanager
    def observed_lock(path, *, required=False):
        assert required is True
        active.append(path.name)
        try:
            yield
        finally:
            assert active.pop() == path.name

    monkeypatch.setattr(eventstore_module, "_interprocess_lock", observed_lock)

    def wrap(target, name):
        def checked(*args, **kwargs):
            if phase["name"] == name:
                assert active == expected[name]
                observed.append(name)
            return target(*args, **kwargs)
        return checked

    monkeypatch.setattr(
        memory_module, "portfolio_concept_overview",
        wrap(memory_module.portfolio_concept_overview, "concepts"))
    monkeypatch.setattr(
        memory_module, "portfolio_digest",
        wrap(memory_module.portfolio_digest, "digest"))
    monkeypatch.setattr(
        claims_module, "portfolio_atlas",
        wrap(claims_module.portfolio_atlas, "atlas"))
    monkeypatch.setattr(
        claims_module, "build_context_pack",
        wrap(claims_module.build_context_pack, "pack"))

    commands = {
        "concepts": ["cross-run-concepts", str(tmp_path), "--json"],
        "digest": ["cross-run-digest", str(tmp_path), "--json"],
        "atlas": ["atlas", str(tmp_path), "--json"],
        "pack": ["claims", str(tmp_path), "--pack", "--json"],
    }
    for name, command in commands.items():
        phase["name"] = name
        result = CliRunner().invoke(app, command)
        assert result.exit_code == 0, result.output
        assert active == []

    assert observed == ["concepts", "digest", "atlas", "pack"]


def test_claims_cli_locks_the_exact_explicit_evidence_file(tmp_path, monkeypatch):
    import looplab.engine.claims as claims_module
    import looplab.events.eventstore as eventstore_module

    evidence = tmp_path / "custom-evidence.txt"
    evidence.write_text(json.dumps({
        "statement": "explicit evidence remains supported", "outcome": "supported",
        "evidence": [1], "run_id": "r-explicit", "task_id": "t",
    }) + "\n", encoding="utf-8")
    active: list[str] = []
    observed = []

    @contextmanager
    def observed_lock(path, *, required=False):
        assert required is True
        active.append(path.name)
        try:
            yield
        finally:
            assert active.pop() == path.name

    monkeypatch.setattr(eventstore_module, "_interprocess_lock", observed_lock)
    original = claims_module.claim_assessments

    def checked(*args, **kwargs):
        assert active == [
            "claim_decisions.jsonl.lock", "custom-evidence.txt.lock",
            "research_claims.jsonl.lock",
        ]
        observed.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(claims_module, "claim_assessments", checked)

    result = CliRunner().invoke(app, ["claims", str(evidence), "--json"])

    assert result.exit_code == 0, result.output
    assert "explicit evidence remains supported" in result.output
    assert observed == [True]
    assert active == []


def test_capsule_stat_failure_is_unknown_and_cli_surfaces_are_bounded(tmp_path, monkeypatch):
    _seed_cross_run(tmp_path)
    original_stat = Path.stat
    secret = f"permission denied: {tmp_path / 'concept_capsules.jsonl'}"

    def guarded_stat(path, *args, **kwargs):
        if path.name == "concept_capsules.jsonl":
            raise PermissionError(secret)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", guarded_stat)

    with pytest.raises(PermissionError, match="permission denied"):
        atlas_for_memory(tmp_path)

    for command in (
            ["cross-run-concepts", str(tmp_path)],
            ["cross-run-digest", str(tmp_path)],
            ["atlas", str(tmp_path)],
            ["claims", str(tmp_path), "--pack"]):
        result = CliRunner().invoke(app, command)
        assert result.exit_code == 2, result.output
        assert "ledger=cross_run_sources" in result.output
        assert "reason=storage_unreadable" in result.output
        assert secret not in result.output and str(tmp_path) not in result.output


def test_governed_source_projection_fences_policy_and_evidence_writers(tmp_path):
    from looplab.events.eventstore import _interprocess_lock

    lessons_path = tmp_path / "lessons.jsonl"
    lessons_path.write_text(json.dumps({
        "statement": "old evidence", "outcome": "supported", "evidence": [1],
        "run_id": "r1", "task_id": "t",
    }) + "\n", encoding="utf-8")
    original_lessons = lessons_path.read_bytes()
    entered, release = Event(), Event()
    policy_started, policy_done = Event(), Event()
    evidence_started, evidence_done = Event(), Event()

    def project(governance):
        rows = [json.loads(line) for line in lessons_path.read_text().splitlines()]
        entered.set()
        assert release.wait(5)
        return {"revision": governance["claim_revision"], "lessons": rows}

    def write_policy():
        policy_started.set()
        record_claim_decision(
            tmp_path, statement="new policy", decision="pinned",
            expected_revision=0, action_id="concurrent-policy")
        policy_done.set()

    def write_evidence():
        evidence_started.set()
        with _interprocess_lock(
                tmp_path / "lessons.jsonl.lock", required=True):
            with lessons_path.open("ab") as handle:
                handle.write(json.dumps({
                    "statement": "new evidence", "outcome": "supported",
                    "evidence": [2], "run_id": "r2", "task_id": "t",
                }).encode("utf-8") + b"\n")
        evidence_done.set()

    with ThreadPoolExecutor(max_workers=3) as executor:
        snapshot_future = executor.submit(
            project_governed_sources, tmp_path, project,
            source_names=("lessons.jsonl",))
        assert entered.wait(5)
        policy_future = executor.submit(write_policy)
        evidence_future = executor.submit(write_evidence)
        assert policy_started.wait(5) and evidence_started.wait(5)
        assert not policy_done.wait(0.1)
        assert not evidence_done.wait(0.1)
        assert lessons_path.read_bytes() == original_lessons
        assert not (tmp_path / "claim_decisions.jsonl").exists()
        release.set()
        snapshot = snapshot_future.result(timeout=5)
        policy_future.result(timeout=5)
        evidence_future.result(timeout=5)

    assert snapshot == {
        "revision": 0,
        "lessons": [{
            "statement": "old evidence", "outcome": "supported", "evidence": [1],
            "run_id": "r1", "task_id": "t",
        }],
    }
    assert policy_done.is_set() and evidence_done.is_set()


def test_absent_governed_source_bootstrap_cannot_mix_new_evidence_with_revision_zero(tmp_path):
    """A missing memory root must join the ordinary lock order before its callback can read files."""
    from looplab.events.eventstore import _interprocess_lock

    memory = tmp_path / "not-created-yet"
    entered, release = Event(), Event()
    writer_started, writer_done = Event(), Event()

    def project(governance):
        entered.set()
        assert release.wait(5)
        lessons = memory / "lessons.jsonl"
        rows = [] if not lessons.exists() else lessons.read_text(encoding="utf-8").splitlines()
        return {"revision": governance["claim_revision"], "rows": rows}

    def write_new_memory():
        writer_started.set()
        record_claim_decision(
            memory, statement="new policy", decision="pinned",
            expected_revision=0, action_id="bootstrap-policy")
        path = memory / "lessons.jsonl"
        with _interprocess_lock(Path(str(path) + ".lock"), required=True):
            path.write_text(json.dumps({
                "statement": "new evidence", "outcome": "supported",
                "evidence": [1], "run_id": "r", "task_id": "t",
            }) + "\n", encoding="utf-8")
        writer_done.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        snapshot_future = executor.submit(
            project_governed_sources, memory, project,
            source_names=("lessons.jsonl",))
        assert entered.wait(5)
        writer_future = executor.submit(write_new_memory)
        assert writer_started.wait(5)
        assert not writer_done.wait(0.1)
        release.set()
        snapshot = snapshot_future.result(timeout=5)
        writer_future.result(timeout=5)

    # CODEX AGENT: either era is valid, but revision-zero policy plus later evidence never is. By
    # deliberately pausing the first snapshot, this test pins the empty era deterministically.
    assert snapshot == {"revision": 0, "rows": []}
    assert writer_done.is_set()


def test_require_existing_concept_write_fences_capsule_snapshot_through_commit(
        tmp_path, monkeypatch):
    import looplab.engine.concept_registry as registry_module

    capsule_path = tmp_path / "concept_capsules.jsonl"
    ConceptCapsuleStore(capsule_path).add(build_concept_capsule(
        run_id="same-run", task_id="task", fingerprint=["task"], direction="max",
        concepts=["loss/a", "loss/b"], concept_outcomes={},
    ))
    snapshot_read, release = Event(), Event()
    writer_started, writer_done = Event(), Event()
    original_snapshot = registry_module._observed_concept_snapshot

    def paused_snapshot(*args, **kwargs):
        snapshot = original_snapshot(*args, **kwargs)
        snapshot_read.set()
        assert release.wait(5)
        return snapshot

    def replace_capsule():
        writer_started.set()
        ConceptCapsuleStore(capsule_path).add(build_concept_capsule(
            run_id="same-run", task_id="task", fingerprint=["task"], direction="max",
            concepts=["loss/b"], concept_outcomes={},
        ))
        writer_done.set()

    monkeypatch.setattr(registry_module, "_observed_concept_snapshot", paused_snapshot)
    with ThreadPoolExecutor(max_workers=2) as executor:
        policy = executor.submit(
            record_concept_alias, tmp_path, from_concept="loss/a", to_concept="loss/b",
            require_existing=True)
        assert snapshot_read.wait(5)
        writer = executor.submit(replace_capsule)
        assert writer_started.wait(5)
        assert not writer_done.wait(0.1)
        release.set()
        receipt = policy.result(timeout=5)
        writer.result(timeout=5)

    assert receipt["concept_snapshot_count"] == 2
    assert load_concept_aliases(tmp_path) == {"loss/a": "loss/b"}
    assert writer_done.is_set()


def test_require_existing_concept_write_refuses_quarantined_capsule_source(tmp_path):
    capsule_path = tmp_path / "concept_capsules.jsonl"
    valid = build_concept_capsule(
        run_id="valid", task_id="task", fingerprint=["task"], direction="max",
        concepts=["loss/a", "loss/b"], concept_outcomes={},
    )
    capsule_path.write_text(json.dumps(valid) + "\n{not-json}\n", encoding="utf-8")

    with pytest.raises(GovernanceLedgerUnavailable) as exc:
        record_concept_alias(
            tmp_path, from_concept="loss/a", to_concept="loss/b", require_existing=True)

    assert exc.value.ledger == "concept_capsules"
    assert exc.value.reason == "invalid_record"
    assert not (tmp_path / "concept_aliases.jsonl").exists()


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


def test_cli_unreadable_cross_run_source_is_bounded_exit_two(tmp_path, monkeypatch):
    import looplab.engine.claims as claims_module

    secret = f"permission denied: {tmp_path / 'private-lessons.jsonl'}"
    monkeypatch.setattr(
        claims_module, "cross_run_retrieve",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError(secret)),
    )

    cli = CliRunner().invoke(app, ["cross-run-search", str(tmp_path), "query"])

    assert cli.exit_code == 2
    assert "ledger=cross_run_sources" in cli.output
    assert "reason=storage_unreadable" in cli.output
    assert secret not in cli.output and str(tmp_path) not in cli.output
