"""The shared serve-side paid claim→terminal ledger (doc 25 SR-01).

`serve/paid_ledger.py` unifies two hand-rolled protocols that guard MONEY: the boss report refresh
and the concept lens. The thing worth guarding is not that the code is shared — it is that sharing
it did NOT flatten the one place the two protocols deliberately disagree.

Measured before the extraction, over 600 randomized event sequences folded by both the pre-change
hand-rolled folds and the shared one: the shared fold reproduces both exactly (0 differences), while
giving the lens `FIRST_TERMINAL_WINS` changes 204/600 of those folds and giving report_refresh
`FAIL_CLOSED` changes 218/600. So "the two folds are near-identical" is true of their SKELETON and
false of their MEANING, and the tests below drive that difference on real event logs rather than
pinning it in source.

Every fold test here writes through a real `EventStore` and reads the real `events.jsonl` back, and
the at-most-once test drives the real `RunCommandService` sequencer from eight threads.
"""
from __future__ import annotations

import ast
import threading

import pytest

pytest.importorskip("fastapi")   # `routers/runs.py` imports it unguarded; skip before reaching it

from looplab.core.errors import OperatorRefusal  # noqa: E402
from looplab.events.eventstore import EventStore  # noqa: E402
from looplab.events.types import ALL_EVENT_TYPES, DIAGNOSTIC_EVENTS  # noqa: E402
from looplab.serve import paid_ledger  # noqa: E402
from looplab.serve.paid_ledger import (  # noqa: E402
    FAIL_CLOSED, FIRST_TERMINAL_WINS, PaidLedgerSpec, SERVE_PAID_LEDGER_DIAGNOSTIC_EVENTS,
    SERVE_PAID_LEDGER_EVENTS, SERVE_PAID_LEDGER_FOLDED_EVENTS, append_claim,
    confirm_terminal_receipt, fold_paid_ledger, record_terminal)
from looplab.serve.routers import boss as boss_router, runs as runs_router  # noqa: E402

from _source_scan import PKG, called_names, iter_trees  # noqa: E402

REFRESH = boss_router._REPORT_REFRESH_LEDGER
LENS = runs_router._CONCEPT_LENS_LEDGER

ID_A = "a" * 64
DIGEST_A = "1" * 64
DIGEST_B = "2" * 64


def _log(tmp_path, rows, name="events.jsonl"):
    """Write *rows* through a real EventStore and hand back the events it reads out again."""
    store = EventStore(tmp_path / name)
    for event_type, data in rows:
        store.append(event_type, data)
    return store, store.read_all()


def _generation(store) -> str:
    from looplab.serve.run_commands import run_generation_token

    return run_generation_token(store.read_all())


def _refresh_rows(kind, generation, *, identity=ID_A):
    """One claim/terminal row in the report-refresh vocabulary (no request digest)."""
    return (kind, {"refresh_id": identity, "generation": generation})


def _lens_rows(kind, generation, *, identity=ID_A, digest=DIGEST_A):
    """One claim/terminal row in the concept-lens vocabulary (request-digest bound)."""
    return (kind, {"lens_request_id": identity, "generation": generation,
                   "request_digest": digest})


# --------------------------------------------------------------- the asymmetry, driven both ways

def test_an_unclaimed_terminal_is_a_receipt_for_report_refresh_and_a_conflict_for_the_lens(
        tmp_path):
    """The single most expensive thing an over-eager abstraction could flatten.

    `report_generated` is appended by `_run_report_refresh_worker` OUTSIDE this ledger, so a terminal
    whose claim is unreadable is a report the operator has ALREADY PAID FOR — dropping it bills them
    twice. Every lens terminal is written THROUGH the ledger, so an unclaimed lens terminal is a
    damaged or forged receipt, and admitting it publishes a lens nobody asked for.
    """
    gen = "b" * 64
    _store, refresh_events = _log(
        tmp_path, [_refresh_rows("report_generated", gen)], name="refresh.jsonl")
    _store2, lens_events = _log(
        tmp_path, [_lens_rows("concept_lens_completed", gen)], name="lens.jsonl")

    refresh = fold_paid_ledger(REFRESH, refresh_events, gen)
    lens = fold_paid_ledger(LENS, lens_events, gen)

    assert ID_A in refresh.terminals, "report_refresh must replay a paid terminal it did not claim"
    assert refresh.conflicts == set()
    assert ID_A not in lens.terminals, "the lens must not publish an unclaimed terminal"
    assert lens.unresolved == set()


def test_a_repeated_claim_is_a_no_op_for_report_refresh_and_a_conflict_for_the_lens(tmp_path):
    gen = "b" * 64
    _s1, refresh_events = _log(tmp_path, [
        _refresh_rows("report_refresh_started", gen),
        _refresh_rows("report_refresh_started", gen),
    ], name="refresh.jsonl")
    _s2, lens_events = _log(tmp_path, [
        _lens_rows("concept_lens_started", gen),
        _lens_rows("concept_lens_started", gen),
    ], name="lens.jsonl")

    refresh = fold_paid_ledger(REFRESH, refresh_events, gen)
    lens = fold_paid_ledger(LENS, lens_events, gen)

    assert refresh.unresolved == {ID_A} and refresh.conflicts == set()
    assert lens.conflicts == {ID_A}


def test_a_second_terminal_is_ignored_by_report_refresh_and_conflicts_the_lens(tmp_path):
    gen = "b" * 64
    _s1, refresh_events = _log(tmp_path, [
        _refresh_rows("report_refresh_started", gen),
        _refresh_rows("report_generated", gen),
        _refresh_rows("report_refresh_failed", gen),
    ], name="refresh.jsonl")
    _s2, lens_events = _log(tmp_path, [
        _lens_rows("concept_lens_started", gen),
        _lens_rows("concept_lens_completed", gen),
        _lens_rows("concept_lens_failed", gen),
    ], name="lens.jsonl")

    refresh = fold_paid_ledger(REFRESH, refresh_events, gen)
    lens = fold_paid_ledger(LENS, lens_events, gen)

    assert refresh.terminals[ID_A].type == "report_generated", "first visible terminal wins"
    assert refresh.conflicts == set()
    assert lens.conflicts == {ID_A} and ID_A not in lens.terminals


def test_only_the_lens_binds_the_request_digest_into_its_identity(tmp_path):
    """A terminal that disagrees with its claim's digest is a conflict — for the protocol that HAS
    a digest. report_refresh has none: its identity is the Idempotency-Key bound to run+generation,
    and a `request_digest` field would be folded into nothing."""
    gen = "b" * 64
    _s2, lens_events = _log(tmp_path, [
        _lens_rows("concept_lens_started", gen, digest=DIGEST_A),
        _lens_rows("concept_lens_completed", gen, digest=DIGEST_B),
    ], name="lens.jsonl")
    lens = fold_paid_ledger(LENS, lens_events, gen)
    assert lens.conflicts == {ID_A} and ID_A not in lens.terminals

    assert LENS.digest_field == "request_digest" and LENS.conflict_policy == FAIL_CLOSED
    assert REFRESH.digest_field is None and REFRESH.conflict_policy == FIRST_TERMINAL_WINS
    # A digest-less spec records `None` as every claim's digest, which is what makes
    # `record_terminal`'s digest guard a no-op there rather than an accidental extra refusal.
    _s1, refresh_events = _log(
        tmp_path, [_refresh_rows("report_refresh_started", gen)], name="refresh.jsonl")
    assert fold_paid_ledger(REFRESH, refresh_events, gen).claims == {ID_A: None}


def test_a_malformed_identity_or_digest_is_skipped_by_both_policies(tmp_path):
    """Uppercase hex, wrong length and non-strings are all rejected — the shared predicate
    `valid_digest_ref` replaced boss.py's copy of itself and runs.py's `_SHA256_RE.fullmatch`."""
    gen = "b" * 64
    rows = []
    for bad in ("A" * 64, "a" * 63, "a" * 65, "z" * 64, ""):
        rows.append(("report_refresh_started", {"refresh_id": bad, "generation": gen}))
        rows.append(("concept_lens_started", {
            "lens_request_id": ID_A, "generation": gen, "request_digest": bad}))
    _store, events = _log(tmp_path, rows)

    assert fold_paid_ledger(REFRESH, events, gen).claims == {}
    assert fold_paid_ledger(LENS, events, gen).claims == {}


def test_the_generation_fence_hides_another_generations_rows_from_both_policies(tmp_path):
    gen, other = "b" * 64, "c" * 64
    _store, events = _log(tmp_path, [
        _refresh_rows("report_refresh_started", other),
        _refresh_rows("report_generated", other),
        _lens_rows("concept_lens_started", other),
        _lens_rows("concept_lens_completed", other),
    ])

    for spec in (REFRESH, LENS):
        view = fold_paid_ledger(spec, events, gen)
        assert (view.claims, view.terminals, view.unresolved, view.conflicts) == ({}, {}, set(), set())


# ----------------------------------------------------------------- at-most-once, really driven

def _seed_run(root, name="demo"):
    root.mkdir(parents=True, exist_ok=True)
    rd = root / name
    rd.mkdir()
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {
        "run_id": name, "task_id": "toy", "goal": "g", "direction": "min"})
    return rd, store


def test_eight_racing_workers_write_exactly_one_terminal_and_all_observe_the_same_receipt(
        tmp_path):
    """The money property: a claim resolves once, no matter how many late workers report in.

    Driven through the REAL `RunCommandService` sequencer against a real run directory, then read
    back off disk — not asserted from the return values alone, because a protocol that returned the
    right object while appending a second paid receipt would still have double-billed.
    """
    from looplab.serve.server import make_app

    root = tmp_path / "runs"
    rd, store = _seed_run(root)
    srv = make_app(root).state.looplab
    generation = _generation(store)
    append_claim(store, REFRESH, ID_A, generation)

    results, errors = [], []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        try:
            results.append(record_terminal(
                srv, REFRESH, rd, generation, ID_A, "report_refresh_failed",
                {"error_kind": "provider_error"}))
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    written = [e for e in EventStore(rd / "events.jsonl").read_all()
               if e.type in {"report_refresh_failed", "report_generated"}]
    assert len(written) == 1, f"{len(written)} paid terminals landed on disk"
    assert {r.seq for r in results} == {written[0].seq}, "a late worker must replay the winner"

    view = fold_paid_ledger(REFRESH, EventStore(rd / "events.jsonl").read_all(), generation)
    assert view.unresolved == set() and view.terminals[ID_A].seq == written[0].seq


def test_a_worker_from_a_replaced_generation_cannot_write_its_receipt_into_the_live_run(tmp_path):
    """The generation fence, isolated: the fold alone does NOT cover it.

    A run can be reset while a paid worker is in flight. That worker still holds the generation it
    claimed under, and its claim row is still readable — so the ledger fold happily reports the claim
    unresolved and would append a paid terminal into a run the operator has since replaced. Only
    `record_terminal`'s live-generation check stops it, which is why this test seeds a claim under a
    generation that is NOT the log's own.
    """
    from looplab.serve.server import make_app

    root = tmp_path / "runs"
    rd, store = _seed_run(root)
    srv = make_app(root).state.looplab
    live = _generation(store)
    stale = "d" * 64
    assert stale != live
    store.append("report_refresh_started", {"refresh_id": ID_A, "generation": stale})
    before = len(store.read_all())

    # The fold DOES see the claim as unresolved under the stale generation...
    assert fold_paid_ledger(REFRESH, store.read_all(), stale).unresolved == {ID_A}
    # ...and the fence is the only thing that refuses it.
    assert record_terminal(
        srv, REFRESH, rd, stale, ID_A, "report_refresh_failed", {"error_kind": "internal"}) is None
    assert len(EventStore(rd / "events.jsonl").read_all()) == before


def test_an_orphaned_claim_never_becomes_a_second_paid_call(tmp_path):
    """An unresolved claim is deliberately uncertain: the ledger refuses to resolve one whose
    identity it cannot see as claimed, and refuses to resolve one already terminal."""
    from looplab.serve.server import make_app

    root = tmp_path / "runs"
    rd, store = _seed_run(root)
    srv = make_app(root).state.looplab
    generation = _generation(store)

    # Never claimed -> nothing to close, and nothing appended.
    before = len(store.read_all())
    assert record_terminal(
        srv, REFRESH, rd, generation, ID_A, "report_refresh_failed",
        {"error_kind": "internal"}) is None
    assert len(EventStore(rd / "events.jsonl").read_all()) == before

    # Claimed and closed -> the second close replays the winner rather than appending.
    append_claim(store, REFRESH, ID_A, generation)
    first = record_terminal(
        srv, REFRESH, rd, generation, ID_A, "report_refresh_failed", {"error_kind": "internal"})
    again = record_terminal(
        srv, REFRESH, rd, generation, ID_A, "report_refresh_failed", {"error_kind": "credentials"})
    assert first is not None and again is not None and again.seq == first.seq
    terminals = [e for e in EventStore(rd / "events.jsonl").read_all()
                 if e.type == "report_refresh_failed"]
    assert len(terminals) == 1 and terminals[0].data["error_kind"] == "internal"


def test_confirm_terminal_receipt_is_false_when_the_storage_cannot_sync(tmp_path, monkeypatch):
    """Fail-closed on every storage capability gap — an unconfirmed line is visible, not durable."""
    path = tmp_path / "events.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    assert confirm_terminal_receipt(path) is True

    monkeypatch.setattr(paid_ledger, "strict_fsync", lambda _fd: (_ for _ in ()).throw(
        OSError("sync unsupported")))
    assert confirm_terminal_receipt(path) is False
    assert confirm_terminal_receipt(tmp_path / "absent.jsonl") is False


# ------------------------------------------------------------- engine invariant #1 at the seam

def test_the_shared_ledger_refuses_to_append_the_report_success_terminal(tmp_path):
    """`report_generated` is FOLDED, and the ledger recognizes it as a terminal TYPE but must never
    WRITE it: the worker appends the paid report outside the sequencer so a generation change
    mid-flight cannot discard a report the operator has already paid for."""
    from looplab.serve.server import make_app

    root = tmp_path / "runs"
    rd, store = _seed_run(root)
    srv = make_app(root).state.looplab
    generation = _generation(store)
    append_claim(store, REFRESH, ID_A, generation)
    before = len(store.read_all())

    with pytest.raises(AssertionError, match="serve-appendable"):
        record_terminal(srv, REFRESH, rd, generation, ID_A, "report_generated", {"content": {}})
    assert len(EventStore(rd / "events.jsonl").read_all()) == before


def test_a_spec_outside_the_serve_allow_list_or_with_an_unknown_policy_refuses(tmp_path):
    with pytest.raises(ValueError, match="SERVE_PAID_LEDGER_EVENTS"):
        PaidLedgerSpec(claim_type="node_created", terminal_types=frozenset({"node_evaluated"}),
                       identity_field="refresh_id", conflict_policy=FIRST_TERMINAL_WINS)
    with pytest.raises(ValueError, match="conflict policy"):
        PaidLedgerSpec(claim_type="report_refresh_started",
                       terminal_types=frozenset({"report_refresh_failed"}),
                       identity_field="refresh_id", conflict_policy="whatever")

    # Both are DEFECTS in code a developer just wrote, not refusals about an operator's input, so
    # they must keep their traceback rather than wearing the OperatorRefusal marker.
    for exc_type in (ValueError,):
        assert not issubclass(exc_type, OperatorRefusal)
    try:
        PaidLedgerSpec(claim_type="report_refresh_started",
                       terminal_types=frozenset({"report_refresh_failed"}),
                       identity_field="refresh_id", conflict_policy="whatever")
    except ValueError as exc:
        assert not isinstance(exc, OperatorRefusal)


def test_the_paid_ledger_allow_list_covers_exactly_the_shipped_specs_and_is_registry_true():
    """The two-way registry check: nothing in the allow-list is unused, and no shipped spec names a
    type the allow-list does not carry — so a third paid route must extend it deliberately."""
    declared = set()
    for spec in (REFRESH, LENS):
        declared.add(spec.claim_type)
        declared |= set(spec.terminal_types)
    assert declared == set(SERVE_PAID_LEDGER_EVENTS)

    assert SERVE_PAID_LEDGER_DIAGNOSTIC_EVENTS <= DIAGNOSTIC_EVENTS
    assert not (SERVE_PAID_LEDGER_FOLDED_EVENTS & DIAGNOSTIC_EVENTS)
    assert SERVE_PAID_LEDGER_EVENTS <= ALL_EVENT_TYPES
    # The appendable half is exactly the diagnostic half: appending a FOLDED event from a generic
    # serve-side helper is the unguarded path around engine invariant #1.
    assert paid_ledger._APPENDABLE_EVENTS == SERVE_PAID_LEDGER_DIAGNOSTIC_EVENTS


def test_append_claim_refuses_a_claim_type_that_is_not_serve_appendable(tmp_path):
    store = EventStore(tmp_path / "events.jsonl")
    forged = PaidLedgerSpec.__new__(PaidLedgerSpec)     # bypass __post_init__ to reach the site
    object.__setattr__(forged, "claim_type", "report_generated")
    object.__setattr__(forged, "terminal_types", frozenset({"report_refresh_failed"}))
    object.__setattr__(forged, "identity_field", "refresh_id")
    object.__setattr__(forged, "conflict_policy", FIRST_TERMINAL_WINS)
    object.__setattr__(forged, "digest_field", None)

    with pytest.raises(AssertionError, match="serve-appendable"):
        append_claim(store, forged, ID_A, "b" * 64)
    assert store.read_all() == []


# ------------------------------------------------------------------------ the patch-seam rule

def _module_tree(relative):
    for path, tree in iter_trees():
        if path == PKG / relative:
            return tree
    raise AssertionError(f"{relative} is not in the package walk")


def _calls_in(tree):
    return {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}


@pytest.mark.parametrize("relative", ["serve/routers/boss.py", "serve/routers/runs.py"])
def test_neither_paid_router_still_binds_strict_fsync(relative):
    """The fsync-confirm seam moved, and a router that re-binds `strict_fsync` silently un-moves it.

    `tests/test_report.py` injects an unsyncable storage through
    `looplab.serve.paid_ledger.strict_fsync`. If a router imported the name again and confirmed a
    terminal with its own copy, that injection would still SUCCEED and simply reach nothing — the
    measured failure mode where the suite stays green with the property fully broken. So the rule is
    stated over the binding, not over the test.
    """
    module = {"serve/routers/boss.py": boss_router, "serve/routers/runs.py": runs_router}[relative]
    assert not hasattr(module, "strict_fsync"), (
        f"{relative} re-bound strict_fsync; the confirm seam must stay paid_ledger's")
    assert "confirm_terminal_receipt" in _calls_in(_module_tree(relative)), (
        f"{relative} no longer calls the shared confirm — a comment cannot satisfy this")


def test_the_shared_confirm_is_the_one_that_actually_syncs():
    assert hasattr(paid_ledger, "strict_fsync")
    assert "strict_fsync" in called_names(confirm_terminal_receipt)


def test_the_paid_report_success_terminal_is_appended_outside_the_ledger():
    """The fact FIRST_TERMINAL_WINS exists FOR: the worker publishes `report_generated` itself."""
    calls = called_names(boss_router._run_report_refresh_worker)
    assert "EventStore().append" in calls, "the worker must append the paid report itself"
    assert "record_terminal" not in calls, (
        "routing the success terminal through the ledger would let a generation change discard a "
        "report the operator already paid for")
    # ...and the FAILURE terminal does go through the ledger.
    assert "record_terminal" in called_names(boss_router._record_report_refresh_failure)
