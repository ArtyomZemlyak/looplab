"""The two at-most-once PAID-curation protocols, and the four row schemas they must keep readable.

Doc 25 EM-03 called these "two independently designed at-most-once protocols writing the SAME three
ledgers" and asked for one writer. Only half of that survived adjudication. The finalize transaction
moved out of `lessons.py` into `engine/curation_protocol.py`, beside its sibling
`engine/steward_invocation.py`, and the kind -> ledger-file vocabulary they share is now derived
from one table. The WRITERS stay separate, and this file is where that decision is executable
rather than merely written down:

* both protocols must survive a crash between their claim and their terminal without re-paying —
  and they must do it DIFFERENTLY, because they are answering different questions (finalize closes
  the semantic key forever; on-demand leaves the operator's claim open so the operator can decide);
* a v2 finalize row physically cannot carry the on-demand path's fields, which is why "converge new
  writes on the v2 shape" would have needed a FIFTH schema rather than removing four;
* every schema generation an old ledger can contain still reads, and the one that carries an
  `action_id` still deduplicates a paid on-demand call rather than reading as a cache miss.

Money is the reason these are worth their length: each of these calls buys one provider invocation.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from looplab.core.models import RunState
from looplab.engine.curation_protocol import CurationProtocolMixin
from looplab.engine.governance_health import (
    GovernanceLedgerUnavailable,
    curation_ledger_file,
    curation_ledger_scope,
    curation_source_key,
    read_curation_rows,
)
from looplab.engine.lessons import LessonMemory

from _source_scan import called_names
from test_steward_semantic_identity import _ToolClient, _engine, _seed_claim

CLAIM_OK = {"decisions": [{"statement": "reranking helps", "decision": "ratified",
                           "why": "consistently supported across runs"}]}
_KINDS = ("concept", "claim", "facets")


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --- the crash window each protocol defends, and how they differ ------------------------------

def test_a_finalize_crash_between_claim_and_terminal_closes_the_key_without_re_paying(tmp_path):
    """Finalize's answer: the next attempt CLOSES the semantic key and never buys it again.

    The provider call is not part of the JSONL transaction, so a process that dies after the model
    answered leaves a durable claim and no receipt. Re-running the same snapshot must observe the
    claim, write the ambiguous terminal from the CLAIM's own identity, and spend nothing.
    """
    _seed_claim(tmp_path)
    first_client = _ToolClient(CLAIM_OK, model="model-a")
    first = LessonMemory(_engine(tmp_path, first_client))

    def die_after_the_paid_call(*_args, **_kwargs):
        raise SystemExit("process died before the terminal reached disk")

    first._append_curation_once = die_after_the_paid_call
    with pytest.raises(SystemExit):
        first.store_claim_curation(RunState(run_id="run-a", task_id="task", last_finish_seq=7))
    assert first_client.calls == 1, "the harness must actually reach the paid call"
    assert list((tmp_path / ".curation_invocations").glob("*.json")), (
        "the paid claim is the only durable record of an ambiguous attempt")

    second_client = _ToolClient(CLAIM_OK, model="model-b")
    second = LessonMemory(_engine(tmp_path, second_client))
    outcome = second.store_claim_curation(
        RunState(run_id="run-b", task_id="task", last_finish_seq=9))

    assert second_client.calls == 0, "a replay bought the same snapshot a second time"
    # The DRIVER reports "already-resolved" — it did not run a steward — while the LEDGER records
    # what actually happened to the lost attempt. The durable row is the one that matters here.
    assert outcome == "already-resolved"
    rows = _rows(tmp_path / "claim_curation_log.jsonl")
    assert [r["outcome"] for r in rows] == ["prior_attempt_incomplete_not_replayed"]
    assert rows[0]["run_id"] == "run-a" and rows[0]["model"] == "model-a", (
        "the recovery terminal must carry the LOST attempt's identity, not the retrying run's")

    # And the key is now closed: a third finalize of the same snapshot neither pays nor appends.
    third_client = _ToolClient(CLAIM_OK, model="model-c")
    third = LessonMemory(_engine(tmp_path, third_client))
    assert third.store_claim_curation(
        RunState(run_id="run-c", task_id="task")) == "already-resolved"
    assert third_client.calls == 0
    assert len(_rows(tmp_path / "claim_curation_log.jsonl")) == 1


def test_an_on_demand_crash_between_begun_and_terminal_replays_the_claim_without_re_paying(
        tmp_path):
    """The on-demand answer to the SAME crash: report the open claim, never a replacement call.

    Deliberately unlike finalize: no terminal is invented. The `action_id` was chosen by an operator,
    so only that operator can decide whether the work should be bought again — with a NEW id. This
    asymmetry is why the two writers were not merged (doc 25 EM-03).
    """
    from looplab.engine.steward_invocation import run_steward_invocation

    calls: list[str] = []

    def crash(_prepared):
        calls.append("paid")
        raise KeyboardInterrupt("process died after the provider answered")

    with pytest.raises(KeyboardInterrupt):
        run_steward_invocation(
            tmp_path, "concept", "operator-action-1", actor="op", at="2026-08-05T00:00:00Z",
            prepare=lambda: object(), invoke=crash,
            safe_error=lambda exc, phase: f"{phase}:failed")
    assert calls == ["paid"]

    path = tmp_path / "concept_curation_log.jsonl"
    assert [r["action"] for r in _rows(path)] == ["steward-invocation-begun"]

    record, replayed = run_steward_invocation(
        tmp_path, "concept", "operator-action-1", actor="op", at="2026-08-05T00:01:00Z",
        prepare=lambda: pytest.fail("a claimed action id must not rebuild the client"),
        invoke=lambda _prepared: pytest.fail("a claimed action id must never be paid again"),
        safe_error=lambda exc, phase: f"{phase}:failed")

    assert replayed is True and record["action"] == "steward-invocation-begun"
    assert calls == ["paid"], "the same action id purchased a second provider call"
    assert [r["action"] for r in _rows(path)] == ["steward-invocation-begun"], (
        "the on-demand protocol must leave the claim OPEN — inventing a terminal here would "
        "silently retire an ambiguous paid attempt the operator has not seen")

    # A different action id is the operator's explicit decision to buy it again — and must work.
    record, replayed = run_steward_invocation(
        tmp_path, "concept", "operator-action-2", actor="op", at="2026-08-05T00:02:00Z",
        prepare=lambda: object(), invoke=lambda _prepared: {"proposals": {}, "receipt": None},
        safe_error=lambda exc, phase: f"{phase}:failed")
    assert replayed is False and record["action"] == "steward-invocation"


# --- why the writers were not converged on the v2 shape -----------------------------------------

@pytest.mark.parametrize("extra", [
    {"action_id": "operator-action-1"},
    {"by": "operator"},
    {"at": "2026-08-05T00:00:00Z"},
    {"receipt": {"applied": []}},
    {"request_digest": "b" * 64},
])
def test_a_v2_finalize_row_cannot_carry_the_on_demand_paths_fields(tmp_path, extra):
    """The finding's recommendation was to converge new writes on the v2 semantic shape.

    It cannot be done without changing that shape: the v2 validator pins an EXACT field set, and
    every field the on-demand protocol needs is outside it. Widening v2 to admit them would add a
    fifth schema instead of retiring four — and would let an operator's on-demand review write a row
    that the finalize gate reads as "this snapshot is already curated", silently skipping the next
    unattended curation. So this stays a red line, not a migration path.
    """
    path = tmp_path / "concept_curation_log.jsonl"
    path.write_text(json.dumps({**_v2_row(), **extra}) + "\n", encoding="utf-8")
    with pytest.raises(GovernanceLedgerUnavailable) as caught:
        read_curation_rows(path)
    assert caught.value.reason == "invalid_record"


# --- one ledger vocabulary, two protocols -------------------------------------------------------

@pytest.mark.parametrize("kind", _KINDS)
def test_both_protocols_resolve_the_same_ledger_from_one_table(tmp_path, kind):
    """A kind paired with the wrong log is not a crash, it is a split paid history: the finalize
    gate and the on-demand gate would each deduplicate against a file the other never writes."""
    from looplab.engine.steward_invocation import _log_path

    name = curation_ledger_file(kind)
    assert _log_path(tmp_path, kind).name == name
    assert curation_ledger_scope(name)[0] == kind        # the inverse round-trips
    for unknown in ("", "lessons", "Concept"):
        with pytest.raises(ValueError):
            curation_ledger_file(unknown)
        with pytest.raises(ValueError):
            _log_path(tmp_path, unknown)


def test_neither_protocol_derives_a_ledger_name_of_its_own():
    """AST, not text: both call sites must REACH the shared table. A comment naming it does not."""
    from looplab.engine.steward_invocation import _log_path

    assert "curation_ledger_file" in called_names(_log_path)
    assert "curation_ledger_file" in called_names(
        CurationProtocolMixin._run_finalize_steward)


def test_the_finalize_steward_writes_the_ledger_the_table_names(tmp_path):
    _seed_claim(tmp_path)
    memory = LessonMemory(_engine(tmp_path, _ToolClient(CLAIM_OK)))
    assert memory.store_claim_curation(RunState(run_id="r", task_id="task")) == "proposed"
    assert (tmp_path / curation_ledger_file("claim")).exists()


# --- every schema an old ledger can contain still reads -----------------------------------------

def _v2_row(**overrides) -> dict:
    digest = "a" * 64
    row = {
        "v": 2, "curation_key": f"concept:v2:{digest}",
        "source_key": curation_source_key(run_id="r", task_id="t", finish_seq=3),
        "run_id": "r", "task_id": "t", "finish_seq": 3, "input_digest": digest,
        "input_schema": "finalize-concept-curation/v3", "model": "m",
        "parser": "tool_call_once", "outcome": "proposed", "auto": False,
        "auto_requested": False,
        "proposals": {"merges": [], "splits": [], "purges": []}, "receipt": None,
    }
    row.update(overrides)
    return row


# The four coexisting generations, each with the writer that shipped it. `_validate_curation_row`
# is a reader of durable history: a branch removed here does not simplify a ledger already on disk,
# it makes that ledger unreadable (and, for the two action-id shapes, re-payable).
_SCHEMAS = {
    # written TODAY by curation_protocol.py::_append_curation_once
    "v2-semantic-finalize": [_v2_row()],
    # written TODAY by steward_invocation.py
    "v1-on-demand-begun-and-terminal": [
        {"v": 1, "action": "steward-invocation-begun", "from": "concept",
         "invocation_id": "act-1", "request_digest": "b" * 64, "by": "op", "at": "t",
         "outcome": "begun"},
        {"v": 1, "action": "steward-invocation", "from": "concept", "action_id": "act-1",
         "request_digest": "b" * 64, "by": "op", "at": "t", "outcome": "empty",
         "proposals": {}, "receipt": None, "begun_revision": 1},
    ],
    # the shipped finalize writer from f1e15c79 until 8471f407 keyed finalize by semantic work
    "legacy-run-keyed-finalize": [
        {"v": 1, "run_id": "old-run", "task_id": "t", "outcome": "proposed",
         "auto": False, "auto_requested": False, "proposals": {}, "receipt": None},
    ],
    # the oldest on-demand audit projection: an action id with no discriminator at all
    "undiscriminated-action-id": [
        {"action_id": "old-http-1", "proposals": {"merges": [{"from_concept": "a"}]},
         "receipt": None},
    ],
}


@pytest.mark.parametrize("generation", sorted(_SCHEMAS))
def test_every_shipped_row_generation_still_reads(tmp_path, generation):
    path = tmp_path / "concept_curation_log.jsonl"
    rows = _SCHEMAS[generation]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert len(read_curation_rows(path)) == len(rows)


def test_the_undiscriminated_action_id_row_still_blocks_a_second_paid_invocation(tmp_path):
    """The reason that branch cannot be retired, stated as money rather than as a schema.

    The row carries an `action_id` but no `action` discriminator, so `read_curation_rows` normalizes
    it into a terminal. Without that normalization the on-demand cache lookup misses and the same id
    is paid for a second time; without the validator branch the whole ledger fails closed instead.
    """
    from looplab.engine.steward_invocation import run_steward_invocation

    path = tmp_path / "concept_curation_log.jsonl"
    path.write_text(json.dumps(_SCHEMAS["undiscriminated-action-id"][0]) + "\n", encoding="utf-8")

    normalized = read_curation_rows(path)[0]
    assert normalized["action"] == "steward-invocation" and normalized["outcome"] == "proposed"

    record, replayed = run_steward_invocation(
        tmp_path, "concept", "old-http-1", actor="op", at="2026-08-05T00:00:00Z",
        prepare=lambda: pytest.fail("a known action id must not rebuild the client"),
        invoke=lambda _prepared: pytest.fail("a known action id must never be paid again"),
        safe_error=lambda exc, phase: f"{phase}:failed")
    assert replayed is True and record["action_id"] == "old-http-1"


def test_the_pre_outcome_finalize_row_fails_closed_rather_than_reading_as_a_decision(tmp_path):
    """A FIFTH shape exists in history — the very first finalize row (9761a429), which carried
    `run_id`/`auto`/`proposals` and no `outcome`. It is deliberately not admitted: an unknown
    decision must stop the ledger, not be projected as one of the four known outcomes."""
    path = tmp_path / "concept_curation_log.jsonl"
    path.write_text(json.dumps({
        "run_id": "r0", "auto": False, "proposals": {}, "receipt": None}) + "\n", encoding="utf-8")
    with pytest.raises(GovernanceLedgerUnavailable) as caught:
        read_curation_rows(path)
    assert caught.value.reason == "invalid_record"


# --- the extraction itself ----------------------------------------------------------------------

_PROTOCOL_MEMBERS = (
    "_already_curated", "_curation_finish_seq", "_curation_source_key",
    "_portfolio_curation_key", "_facets_curation_key", "_diagnostic_curation_key",
    "_curation_provenance", "_curation_claim_path", "_legacy_curation_claim_path",
    "_legacy_curation_terminal", "_write_curation_claim", "_read_curation_claim",
    "_curation_decision_lock", "_prune_curation_scratch", "_paid_curation_attempt_locked",
    "_recover_curation_claim_locked", "_curation_attempt_already_resolved_locked",
    "_paid_curation_attempt", "_append_curation_once", "_run_finalize_steward",
    "store_concept_curation", "store_claim_curation", "store_task_facets",
)


@pytest.mark.parametrize("member", _PROTOCOL_MEMBERS)
def test_the_paid_curation_transaction_lives_beside_its_sibling_protocol(member):
    """It is governance money-handling, not lessons (doc 25 EM-03) — but it must stay reachable
    under its original `LessonMemory` name, because every one of these is a live patch seam."""
    attribute = getattr(LessonMemory, member)
    function = getattr(attribute, "__func__", attribute)
    assert function.__module__ == "looplab.engine.curation_protocol", member
    # …and it must be the SAME function object the mixin defines: a copy left behind in `lessons.py`
    # would satisfy the module check above through inheritance while shadowing nothing.
    mixin = getattr(CurationProtocolMixin, member)
    assert getattr(mixin, "__func__", mixin) is function
