"""The per-operation claim→terminal receipt ledger for paid UI-side model calls (doc 25 SR-01).

`serve/paid_work.py` owns one half of the paid-work boundary — the generation-bound metering lease
that attributes every observed usage delta to an exact run generation without ever repeating the
provider call during recovery. This module owns the OTHER half its docstring hands off: the durable
*operation* idempotency ledger folded out of the run's own event log — claim before paid work,
exactly one terminal receipt after it, a generation fence around both, and a reconciliation that
fails closed on every storage capability gap.

It was five hand-rolled copies. Two are event-ledger copies and are what this module unifies:
`routers/boss.py`'s `report_refresh` and `routers/runs.py`'s concept lens. The other three are
FILE-ledger protocols over receipts, leases and fences on disk rather than over events
(`scope_report_store.py` + `scope_actions.py`, `trace_clear.py`, `deletion_service.py`); they share
the vocabulary but not the storage, and deliberately stay separate.

**The two ported protocols are not the same protocol, and this module must not pretend they are.**
Their fold skeletons are near-identical; their CONFLICT POLICY is not, and each half of the
difference is load-bearing money behaviour in the opposite direction:

* `report_refresh` is FIRST-TERMINAL-WINS. Its success terminal (`report_generated`) is appended by
  the worker OUTSIDE this ledger, so a terminal whose claim is unreadable is still a real, already
  paid report; dropping it would send the operator back through a second paid call. It has no
  request digest — its identity is the Idempotency-Key bound to the run + generation, and nothing
  about the request body is folded into it.
* the concept lens is FAIL-CLOSED. It binds `request_digest` into the claim, so a terminal that
  disagrees with its claim's digest is evidence of a damaged or forged ledger rather than of paid
  work, and admitting it would publish a lens the operator never asked for. Its terminals are all
  written THROUGH this ledger, so an unclaimed terminal cannot be a real receipt.

Flattening either into the other is a silent regression that reads as a cleanup, so the policy is
an explicit spec field with two named values and no default.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from looplab.core.atomicio import strict_fsync
from looplab.core.jsonutil import valid_digest_ref
from looplab.events.eventstore import EventStore
from looplab.events.types import (
    DIAGNOSTIC_EVENTS,
    EV_CONCEPT_LENS_COMPLETED, EV_CONCEPT_LENS_FAILED, EV_CONCEPT_LENS_STARTED,
    EV_REPORT_GENERATED, EV_REPORT_REFRESH_FAILED, EV_REPORT_REFRESH_STARTED,
)

# Domain ownership still requires explicit serve-side writer boundaries: a GENERIC append helper would
# let every future paid route reach the log through one function that asks no questions. So the parameterized claim
# and terminal types are allow-listed here and asserted at BOTH append sites, mirroring
# `events/types.py::BACKGROUND_APPENDABLE`.
#
# Almost every member is DIAGNOSTIC (fold-ignored, so its byte position cannot change any folded
# state) — asserted below rather than trusted, because a type moving out of DIAGNOSTIC_EVENTS is
# precisely the change that must not pass silently.
SERVE_PAID_LEDGER_DIAGNOSTIC_EVENTS: frozenset[str] = frozenset({
    EV_REPORT_REFRESH_STARTED, EV_REPORT_REFRESH_FAILED,
    EV_CONCEPT_LENS_STARTED, EV_CONCEPT_LENS_COMPLETED, EV_CONCEPT_LENS_FAILED,
})

# The ONE folded exception, and it is read-only here: `report_generated` is a terminal TYPE the
# report-refresh fold must recognize, but this module never appends it — `routers/boss.py`'s worker
# appends the paid report itself, deliberately outside the sequencer and outside this ledger, so
# that a generation change mid-flight cannot discard a report the operator has already paid for.
# `record_terminal` refuses to append it (see `_APPENDABLE_EVENTS`), which keeps that asymmetry from
# being erased by a later caller passing it as a terminal type.
SERVE_PAID_LEDGER_FOLDED_EVENTS: frozenset[str] = frozenset({EV_REPORT_GENERATED})

SERVE_PAID_LEDGER_EVENTS: frozenset[str] = (
    SERVE_PAID_LEDGER_DIAGNOSTIC_EVENTS | SERVE_PAID_LEDGER_FOLDED_EVENTS)

_APPENDABLE_EVENTS: frozenset[str] = SERVE_PAID_LEDGER_DIAGNOSTIC_EVENTS

assert SERVE_PAID_LEDGER_DIAGNOSTIC_EVENTS <= DIAGNOSTIC_EVENTS, (
    "a paid-ledger event left DIAGNOSTIC_EVENTS: its splice position now has folded meaning")
assert not (SERVE_PAID_LEDGER_FOLDED_EVENTS & DIAGNOSTIC_EVENTS), (
    "a declared folded exception is diagnostic; move it to the diagnostic set")


#: A terminal that arrives without a readable claim IS a receipt, and the first visible terminal for
#: an identity wins. For a protocol whose success terminal is written outside this ledger.
FIRST_TERMINAL_WINS = "first_terminal_wins"
#: A duplicate claim, an unclaimed terminal, a duplicate terminal or a terminal whose digest
#: disagrees with its claim's all mark the identity CONFLICTED, and a conflicted identity's terminal
#: is dropped. For a protocol that writes every terminal through this ledger.
FAIL_CLOSED = "fail_closed"

CONFLICT_POLICIES: frozenset[str] = frozenset({FIRST_TERMINAL_WINS, FAIL_CLOSED})


@dataclass(frozen=True)
class PaidLedgerSpec:
    """One paid operation's durable identity vocabulary and conflict policy.

    `digest_field` is None for a protocol whose identity is not request-bound. That is not a
    formatting difference: with a digest field the ledger additionally proves the terminal belongs
    to the exact request that claimed it, and `record_terminal` refuses an identity whose stored
    claim digest does not match.
    """

    claim_type: str
    terminal_types: frozenset[str]
    identity_field: str
    conflict_policy: str
    digest_field: Optional[str] = None

    def __post_init__(self) -> None:
        if self.conflict_policy not in CONFLICT_POLICIES:
            raise ValueError(f"unknown paid-ledger conflict policy: {self.conflict_policy!r}")
        declared = {self.claim_type} | set(self.terminal_types)
        unlisted = sorted(declared - SERVE_PAID_LEDGER_EVENTS)
        if unlisted:
            raise ValueError(
                "paid-ledger event types must be allow-listed in "
                f"SERVE_PAID_LEDGER_EVENTS: {unlisted}")

    @property
    def fails_closed(self) -> bool:
        return self.conflict_policy == FAIL_CLOSED


@dataclass(frozen=True)
class PaidLedgerView:
    """The folded ledger for ONE run generation.

    `claims` maps identity -> the claim's request digest (None when the spec has no digest field),
    `terminals` maps identity -> the winning terminal event, `unresolved` is the claims with no
    terminal — an orphaned claim is intentionally uncertain and can never be replayed into a second
    paid call — and `conflicts` is empty under FIRST_TERMINAL_WINS by construction.
    """

    claims: dict[str, Optional[str]]
    terminals: dict[str, Any]
    unresolved: set[str]
    conflicts: set[str]


def fold_paid_ledger(spec: PaidLedgerSpec, events, generation: str) -> PaidLedgerView:
    """Fold durable paid claims without trusting malformed or conflicting receipts."""
    claims: dict[str, Optional[str]] = {}
    terminals: dict[str, Any] = {}
    conflicts: set[str] = set()
    fails_closed = spec.fails_closed
    for event in events:
        data = event.data if isinstance(event.data, dict) else {}
        identity = data.get(spec.identity_field)
        # `valid_digest_ref` is the ONE reader of what `canonical_json_digest` mints (doc 25 EV-04):
        # a str, exactly 64 lowercase hex. Both ported copies hand-rolled that predicate — boss.py
        # already through this function, runs.py through a module-local `_SHA256_RE.fullmatch` — and
        # they accept and reject exactly the same strings.
        if not valid_digest_ref(identity) or data.get("generation") != generation:
            continue
        digest: Optional[str] = None
        if spec.digest_field is not None:
            digest = data.get(spec.digest_field)
            if not valid_digest_ref(digest):
                continue
        if event.type == spec.claim_type:
            if identity in claims or identity in terminals:
                if fails_closed:
                    conflicts.add(identity)
                    continue
            # FIRST_TERMINAL_WINS keeps a repeated claim a no-op, which is what the set of started
            # identities it replaces did; the claim after a terminal still records the claim, and
            # `unresolved` below subtracts the terminal again.
            claims.setdefault(identity, digest)
        elif event.type in spec.terminal_types:
            if not fails_closed:
                terminals.setdefault(identity, event)  # first visible terminal; replay confirms sync
                continue
            if identity not in claims:
                continue
            if identity in terminals or claims[identity] != digest:
                conflicts.add(identity)
            else:
                terminals[identity] = event
    for identity in conflicts:
        terminals.pop(identity, None)
    unresolved = set(claims) - set(terminals)
    return PaidLedgerView(
        claims=claims, terminals=terminals, unresolved=unresolved, conflicts=conflicts)


def confirm_terminal_receipt(path) -> bool:
    """Upgrade a visible terminal to a durable replay receipt before returning it.

    A strict append can write a complete line and then fail to confirm its fsync. Such a line is
    intentionally visible for same-key reconciliation, but it is not authoritative until a later
    sync succeeds. Windows requires a writable descriptor for ``fsync``/``_commit``.
    """
    try:
        with open(path, "r+b") as handle:
            strict_fsync(handle.fileno())
        return True
    except Exception:  # noqa: BLE001 - replay remains fail-closed on every storage capability gap
        return False


def _ledger_event_data(spec: PaidLedgerSpec, identity: str, generation: str,
                       request_digest: Optional[str], extra: Optional[dict]) -> dict:
    data: dict[str, Any] = {spec.identity_field: identity, "generation": generation}
    if spec.digest_field is not None:
        data[spec.digest_field] = request_digest
    data.update(extra or {})
    return data


def append_claim(store: EventStore, spec: PaidLedgerSpec, identity: str, generation: str, *,
                 request_digest: Optional[str] = None,
                 claim_fields: Optional[dict] = None):
    """Append the durable paid-work claim. The caller must already hold the run command sequencer.

    The event log is the restart-safe idempotency ledger: this lands BEFORE provider construction,
    and a success/failure terminal receipt closes it. An orphaned claim is intentionally uncertain
    and can never be replayed into a second paid call.
    """
    assert spec.claim_type in _APPENDABLE_EVENTS, (
        f"paid-ledger claim {spec.claim_type!r} is not serve-appendable (engine invariant #1)")
    return store.append(
        spec.claim_type,
        _ledger_event_data(spec, identity, generation, request_digest, claim_fields),
        require_lock=True, require_durable=True,
    )


def record_terminal(srv, spec: PaidLedgerSpec, run_dir, generation: str, identity: str,
                    event_type: str, terminal_fields: Optional[dict] = None, *,
                    request_digest: Optional[str] = None):
    """Append one terminal iff the exact claim is still unresolved; return the terminal or None.

    Provider work and explicit operator abandonment can finish in different processes.  The run
    command sequencer is therefore the only terminal commit point: a late worker observes and
    replays the winner instead of appending a conflicting second receipt.
    """
    assert event_type in spec.terminal_types, (
        f"{event_type!r} is not a terminal of {spec.claim_type!r}")
    assert event_type in _APPENDABLE_EVENTS, (
        f"paid-ledger terminal {event_type!r} is not serve-appendable (engine invariant #1)")
    try:
        with srv.commands.sequence(run_dir):
            canonical = srv.commands.validate_paths(run_dir)
            if srv.commands.run_generation(canonical) != generation:
                return None
            store = EventStore(canonical / "events.jsonl")
            view = fold_paid_ledger(spec, store.read_all(), generation)
            if spec.digest_field is not None:
                # Only a digest-bound protocol can prove the terminal belongs to the request that
                # claimed it. Asking a protocol without a digest field the same question would
                # compare None to None and add nothing; asking it of one that HAS the field is the
                # whole point of the field.
                if identity in view.conflicts or view.claims.get(identity) != request_digest:
                    return None
            if identity in view.terminals:
                return view.terminals[identity]
            if identity not in view.unresolved:
                return None
            return store.append(
                event_type,
                _ledger_event_data(spec, identity, generation, request_digest, terminal_fields),
                require_lock=True,
                require_durable=True,
            )
    except Exception:  # noqa: BLE001 - an unresolved paid claim must remain fail-closed
        return None
