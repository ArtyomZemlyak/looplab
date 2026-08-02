"""One finalize-steward protocol, three configurations — guarded end to end (doc 25 EM-02).

The concept, claim and task-facet stewards used to be three copy-pasted ~90-line bodies of the same
at-most-once protocol: cross-run gate, semantic decision lock, already-resolved check, fast paths
that must not race a paid attempt, the paid attempt, and TWO error terminals. Every protocol fix had
to be applied three times in step, or the three ledgers would disagree about what happened during
the same finalize.

They now share `LessonMemory._run_finalize_steward` and differ only in data. This file is what makes
that safe to keep: it drives all three through every terminal — proposed, empty input, unavailable
client, provider error, and the replay of each — and compares the ledgers they WRITE, not just the
strings they return. The same harness run against the pre-extraction tree produced byte-identical
output, which is the evidence the extraction preserved behaviour rather than merely staying green.

The ledger bytes matter specifically because the extraction moved row construction into a shared
`row()` helper, which changes JSON key ORDER. Nothing digests a row today; this proves it.
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from looplab.core.models import RunState
from looplab.engine.lessons import LessonMemory

from test_steward_semantic_identity import _ToolClient, _engine, _seed_claim, _seed_concept

# Payloads that produce a genuinely NON-EMPTY proposal. Shapes matter: the stewards validate and
# drop entries they cannot match, so a plausible-looking payload with the wrong field names settles
# as "empty" and the `proposed` terminal below goes untested. That is exactly what the first draft
# of this file did — `{"concept": ..., "reason": ...}` instead of `{"from_concept": ...}` — and the
# terminal assertion is what caught it.
# The concept steward's non-empty path needs the OPAQUE record ids its prompt mints (persisted
# labels are untrusted evidence and never round-trip as instructions), and purges additionally
# require a complete source receipt. Constructing that here would couple this file to
# `_concept_prompt_payload`'s internals for no gain: `test_concept_steward.py` covers the proposal
# validator directly, and the `proposed` TERMINAL — the only part this file is about — is exercised
# through the claim steward below. So the concept steward is driven to empty here, deliberately.
CONCEPT_EMPTY = {"merges": [], "splits": [], "purges": []}
CLAIM_OK = {"decisions": [{"statement": "reranking helps", "decision": "ratified",
                           "why": "consistently supported across runs"}]}
FACETS_OK = {"facets": {"domain": "retrieval"}}


class _Boom:
    """A client whose provider call fails after the paid claim is durable — the error terminal."""

    model = "review-model"

    def complete_tool(self, _messages, _schema):
        raise RuntimeError("provider exploded")

    def complete_text(self, _messages):
        raise RuntimeError("provider exploded")


def _state(task_id="task"):
    return RunState(run_id="r1", task_id=task_id,
                    goal="improve retrieval recall on the benchmark")


CASES = {
    "concept/empty-input": (None, lambda: _ToolClient(CONCEPT_EMPTY), "concept", "empty"),
    "concept/empty-proposal": (_seed_concept, lambda: _ToolClient(CONCEPT_EMPTY),
                               "concept", "empty"),
    "concept/unavailable": (_seed_concept, lambda: None, "concept", "unavailable"),
    "concept/error": (_seed_concept, _Boom, "concept", "error"),
    "claim/proposed": (_seed_claim, lambda: _ToolClient(CLAIM_OK), "claim", "proposed"),
    "claim/empty-input": (None, lambda: _ToolClient(CLAIM_OK), "claim", "empty"),
    "claim/unavailable": (_seed_claim, lambda: None, "claim", "unavailable"),
    "claim/error": (_seed_claim, _Boom, "claim", "error"),
    "facets/proposed": (None, lambda: _ToolClient(FACETS_OK), "facets", "proposed"),
    "facets/unavailable": (None, lambda: None, "facets", "unavailable"),
    "facets/error": (None, _Boom, "facets", "error"),
}
_DRIVERS = {
    "concept": lambda memory, state: memory.store_concept_curation(state),
    "claim": lambda memory, state: memory.store_claim_curation(state),
    "facets": lambda memory, state: memory.store_task_facets(state),
}
_LOGS = {"concept": "concept_curation_log.jsonl", "claim": "claim_curation_log.jsonl",
         "facets": "task_facets_curation_log.jsonl"}


def _run(case, *, replay=False):
    seed, make_client, kind, _expected = CASES[case]
    root = Path(tempfile.mkdtemp())
    try:
        if seed is not None:
            seed(root)
        client = make_client()
        outcomes = [_DRIVERS[kind](LessonMemory(_engine(root, client)), _state())]
        if replay:
            outcomes.append(_DRIVERS[kind](LessonMemory(_engine(root, client)), _state()))
        rows = [json.loads(line) for line in
                (root / _LOGS[kind]).read_text(encoding="utf-8").splitlines() if line.strip()]
        return outcomes, rows
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.parametrize("case", sorted(CASES))
def test_every_steward_terminal_is_reached_and_logged_exactly_once(case):
    """Each configuration must reach its own terminal AND leave exactly one durable row for it.

    A shared driver makes it cheap to get one of these subtly wrong for one steward only — the
    return string can look right while the ledger records a different outcome, or none at all.
    """
    _seed, _client, _kind, expected = CASES[case]
    outcomes, rows = _run(case)
    assert outcomes == [expected], f"{case} returned {outcomes}, expected [{expected!r}]"
    assert len(rows) == 1, f"{case} wrote {len(rows)} ledger rows, expected exactly one"
    assert rows[0]["outcome"] == expected, (
        f"{case} returned {expected!r} but logged {rows[0]['outcome']!r} — the ledger is the "
        "durable record, so a divergence here is the operator being told something untrue")
    assert rows[0]["auto"] is False, "finalize never applies a proposal"
    assert rows[0]["auto_requested"] is False
    assert "proposals" in rows[0] and "receipt" in rows[0]


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_second_finalize_never_re_pays_or_double_logs(case):
    """The whole point of the protocol. A replay must observe the existing decision, not spend
    again — and must not append a second terminal for the same semantic key."""
    _seed, _client, _kind, expected = CASES[case]
    outcomes, rows = _run(case, replay=True)
    assert outcomes[0] == expected
    assert outcomes[1] == "already-resolved", (
        f"{case} replayed to {outcomes[1]!r}; a resolved attempt must never be re-run")
    assert len(rows) == 1, f"{case} appended {len(rows)} rows across two finalizes"


def test_a_task_with_no_id_settles_before_the_lock_and_writes_nothing():
    """The facets steward's one pre-lock short-circuit: no task id means there is nothing to key a
    once-per-task decision on, so it must not create a ledger at all."""
    root = Path(tempfile.mkdtemp())
    try:
        memory = LessonMemory(_engine(root, _ToolClient(FACETS_OK)))
        assert memory.store_task_facets(_state(task_id="")) == "empty"
        assert not (root / _LOGS["facets"]).exists(), (
            "an unkeyable facets decision wrote a ledger row it can never deduplicate")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_an_already_governed_task_settles_without_touching_the_provider():
    """The facets steward's in-lock fast path. It must run BEFORE `reflect_client`, or an operator
    decision that is already recorded still costs a provider call every finalize."""
    from looplab.engine.task_facets import record_task_facets

    root = Path(tempfile.mkdtemp())
    try:
        record_task_facets(root, task_id="task", facets={"domain": "retrieval"})
        memory = LessonMemory(_engine(root, _ToolClient(FACETS_OK)))
        memory.reflect_client = lambda: pytest.fail("a governed task must not reach the provider")
        assert memory.store_task_facets(_state()) == "already-governed"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_the_disabled_gate_is_checked_before_anything_else():
    root = Path(tempfile.mkdtemp())
    try:
        engine = _engine(root, _ToolClient(CONCEPT_EMPTY))
        engine._cross_run_curation = False
        memory = LessonMemory(engine)
        for driver in _DRIVERS.values():
            assert driver(memory, _state()) == "disabled"
        assert not list(root.rglob("*.jsonl")), "a disabled steward wrote to the portfolio"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_each_steward_keeps_its_own_empty_proposal_SHAPE():
    """The one thing a shared driver could silently homogenise. Each ledger's readers know their
    steward's schema, so a concept row carrying `{"decisions": []}` is a corrupt row, not a tidy one.
    """
    for case, expected_keys in (("concept/unavailable", {"merges", "splits", "purges"}),
                                ("claim/unavailable", {"decisions"}),
                                ("facets/unavailable", {"task_id", "facets"})):
        _outcomes, rows = _run(case)
        assert set(rows[0]["proposals"]) == expected_keys, (
            f"{case} logged proposals {sorted(rows[0]['proposals'])}, expected "
            f"{sorted(expected_keys)}")


def test_the_empty_proposal_shape_is_a_fresh_object_per_call():
    """A shared mutable default would let one steward's row alias another's — the classic Python
    trap, and the reason the driver takes a FACTORY rather than a dict."""
    _outcomes, first = _run("concept/unavailable")
    _outcomes, second = _run("concept/unavailable")
    assert first[0]["proposals"] == second[0]["proposals"]
    assert first[0]["proposals"] is not second[0]["proposals"]


def test_an_already_governed_task_wins_over_an_empty_goal():
    """Fast-path ORDER, which a shared driver makes trivially swappable. A task the operator already
    governs must report `already-governed` even when this run's goal is empty — swapping the two
    checks reports `empty` instead, which reads as "there was nothing to propose" and erases the
    fact that a human decision already exists."""
    from looplab.engine.task_facets import record_task_facets

    root = Path(tempfile.mkdtemp())
    try:
        record_task_facets(root, task_id="task", facets={"domain": "retrieval"})
        memory = LessonMemory(_engine(root, _ToolClient(FACETS_OK)))
        state = RunState(run_id="r1", task_id="task", goal="")
        assert memory.store_task_facets(state) == "already-governed"
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.parametrize("case", ["claim/proposed", "claim/error", "concept/error", "facets/error"])
def test_every_terminal_after_a_paid_call_is_written_durably(case):
    """The one protocol property whose loss is invisible until a crash.

    Once the provider has been invoked the money is spent, so the row recording that MUST be fsynced
    before the process can die — otherwise a restart sees no terminal, replays, and pays twice. The
    non-paid terminals (empty / unavailable / already-governed) deliberately do NOT require
    durability: nothing was spent, so a lost row costs a re-read, not a re-bill.
    """
    seed, make_client, kind, _expected = CASES[case]
    root = Path(tempfile.mkdtemp())
    try:
        if seed is not None:
            seed(root)
        memory = LessonMemory(_engine(root, make_client()))
        durable_flags = []
        original = memory._append_curation_once

        def spy(*args, **kwargs):
            durable_flags.append(bool(kwargs.get("require_durable")))
            return original(*args, **kwargs)

        memory._append_curation_once = spy
        _DRIVERS[kind](memory, _state())
        assert durable_flags, f"{case} wrote no ledger row at all"
        assert all(durable_flags), (
            f"{case} appended a post-provider terminal without require_durable=True: {durable_flags}. "
            "A crash before that row reaches disk makes the next finalize replay a paid call.")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.parametrize("case", ["concept/empty-input", "claim/unavailable", "facets/unavailable"])
def test_terminals_that_spent_nothing_do_not_demand_durability(case):
    """The other half, so the assertion above cannot be satisfied by making everything durable —
    which would put an fsync on the common no-op finalize path."""
    seed, make_client, kind, _expected = CASES[case]
    root = Path(tempfile.mkdtemp())
    try:
        if seed is not None:
            seed(root)
        memory = LessonMemory(_engine(root, make_client()))
        durable_flags = []
        original = memory._append_curation_once

        def spy(*args, **kwargs):
            durable_flags.append(bool(kwargs.get("require_durable")))
            return original(*args, **kwargs)

        memory._append_curation_once = spy
        _DRIVERS[kind](memory, _state())
        assert durable_flags == [False], (
            f"{case} spent nothing but demanded an fsync: {durable_flags}")
    finally:
        shutil.rmtree(root, ignore_errors=True)
