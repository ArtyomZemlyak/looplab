"""The durable-operation kit shared by the whole-run reset and deletion transactions (doc 25 SC-06).

This is the RECEIPT tier of the same split `core/fence.py` made one layer down for the fence MARKERS
(doc 25 CO-01), and it is worth stating the relationship because the two are easy to confuse:

  * `core/fence.py` owns the *fence* protocol — the marker that blocks every other writer while an
    operation is unresolved. It is read by the engine, the CLI and five serve modules.
  * this module owns the *receipt* protocol — the operation's own durable state machine, read only by
    the transaction that wrote it: identity, phase, and the rules for advancing one.

`reset_transaction.py` and `deletion_transaction.py` are two receipts with genuinely different
schemas, phase lattices, error types and size budgets — but they implemented the same paranoid
read-then-verify load and the same validate/immutable/transition/publish/confirm save twice. A
rename-normalising diff of the two put the shared machinery at ~90 of ~500 combined code lines. The
regular-file probe was *byte-identical* after renaming (0 differing lines); the loads differed by
exactly the drift below; only the saves differed for a reason.

That is the wrong half to duplicate, and CO-01 already recorded what it costs: the deletion fence had
re-derived `atomicio.file_identity` as a local six-field lambda while importing the canonical one two
lines above. **The deletion RECEIPT still had that exact lambda when SC-06 was implemented** — the
same defect, in the second location, surviving the commit that fixed the first. One copy quietly
gaining a fix the other misses is not a hypothetical failure mode here; it is the observed one.

So the PROTOCOL lives here and the SCHEMAS stay with their owners. Each owner declares one
`ReceiptProtocol` naming its label, error class, size cap, validator, immutable identity fields and —
the asymmetry that must never be flattened — its own `check_transition`. Reset's phase lattice is an
unordered adjacency table (a launch can go back to `archived`); deletion's is a monotonic index with
three special terminal branches, one of which (`quarantine_ambiguous`) is deliberately NOT resumable
and demands manual storage recovery. Merging those into one "generic phase machine" would silently
make an ambiguous Windows quarantine move resumable, which is the difference between failing closed
and deleting a run twice.

The read is deliberately paranoid, in this order:

  lstat -> reject a reparse point or anything that is not a regular file -> reject an oversized file
  -> bounded read -> lstat AGAIN through the same rejection -> reject if the file identity changed.

The second lstat is the point: a receipt replaced mid-read would otherwise be parsed as a mix of two
operations. Unlike `core/fence.py`, the re-stat here goes back through the full regular-file check
rather than a bare `lstat`, so a receipt that became a symlink under the read is refused as "not a
regular service-owned file" rather than as a changed one. Both fail closed; the receipt tier keeps
the sharper message because its callers surface it to an operator who has to decide what to repair.

The second half of this module is the destructive-quiescence CHECKLIST, which the same finding names
as "four spellings". It is not four, and it is not one — see `refuse_unless_quiescent`.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Callable, NamedTuple, Optional

from fastapi import HTTPException

from looplab.core.atomicio import file_identity, strict_atomic_write_text
from looplab.core.pathsafe import is_reparse


class ReceiptProtocol(NamedTuple):
    """One owner's per-receipt facts. Everything here differs between reset and deletion.

    ``label`` is the noun every message in this module is built from, so an owner's error text stays
    byte-identical to what it raised before the protocol was shared (``"reset receipt cannot be
    read: …"``). ``validate`` returns the normalized receipt and raises the owner's own error with
    the owner's own message — a bool would have collapsed six distinct schema refusals into one.
    ``check_transition`` raises when ``value``'s phase may not follow ``current``'s.
    """

    label: str
    error_cls: type[Exception]
    max_bytes: int
    validate: Callable[..., dict[str, Any]]
    immutable: frozenset[str]
    check_transition: Callable[[dict[str, Any], dict[str, Any]], None]


def regular_receipt_stat(path: Path, protocol: ReceiptProtocol) -> Optional[os.stat_result]:
    """``lstat`` a receipt, or ``None`` when there is none; refuse anything not service-owned.

    An absent receipt is the one case that must not raise — every resume path distinguishes "no
    operation" from "an operation whose state cannot be read", and collapsing them would let a
    destructive transaction restart from scratch over its own unfinished work.
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise protocol.error_cls(f"{protocol.label} cannot be inspected: {exc}") from exc
    if is_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise protocol.error_cls(f"{protocol.label} is not a regular service-owned file")
    return info


def load_receipt(path: Path, protocol: ReceiptProtocol) -> Optional[dict[str, Any]]:
    """Read a receipt under the paranoid protocol above, or ``None`` when there is none."""
    before = regular_receipt_stat(path, protocol)
    if before is None:
        return None
    if before.st_size > protocol.max_bytes:
        raise protocol.error_cls(f"{protocol.label} exceeds its safety limit")
    try:
        with path.open("rb") as stream:
            raw = stream.read(protocol.max_bytes + 1)
        after = regular_receipt_stat(path, protocol)
    except OSError as exc:
        raise protocol.error_cls(f"{protocol.label} cannot be read: {exc}") from exc
    # `file_identity` is the canonical same-file-unchanged stat tuple. The deletion receipt used to
    # spell its own six-field lambda here — the very drift CO-01 removed from the deletion FENCE and
    # did not reach — so a change to the canonical tuple would have left this copy comparing a weaker
    # identity, with nothing to notice.
    if after is None or file_identity(before) != file_identity(after):
        raise protocol.error_cls(f"{protocol.label} changed while it was being read")
    # `st_size` is not a promise about what the read returned: the file can grow between the two
    # calls, and on an attribute-caching filesystem the stat can simply be stale. The pre-read guard
    # is what keeps an oversized receipt from being pulled into memory at all; this one is what
    # refuses it once it has been.
    if len(raw) > protocol.max_bytes:
        raise protocol.error_cls(f"{protocol.label} exceeds its safety limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise protocol.error_cls(f"{protocol.label} cannot be decoded: {exc}") from exc
    return protocol.validate(value, path=path)


def save_receipt(
        path: Path, value: dict[str, Any], protocol: ReceiptProtocol, *,
        load: Callable[[Path], Optional[dict[str, Any]]]) -> dict[str, Any]:
    """Validate, admit the phase move, write durably, then READ IT BACK and confirm it.

    ``load`` is the owner's own public loader rather than `load_receipt` directly: both halves of
    this function (the read-current that the immutable/transition checks judge, and the read-back)
    are decisions the owner is entitled to intercept, and routing them through its module-level name
    keeps that a live monkeypatch seam instead of one that resolves past the owner. `core/fence.py`
    passes its `confirm` callable for the same reason.

    The read-back is not belt-and-braces: a strict write can fail after the replacement became
    visible, callers retry the same operation, and that later read is the authority on every
    subsequent decision. Confirming here means a half-published receipt is a loud storage error
    rather than a phase nobody can advance.
    """
    value = protocol.validate(value, path=path)
    current = load(path)
    if current is not None:
        if any(current.get(key) != value.get(key) for key in protocol.immutable):
            raise protocol.error_cls(f"{protocol.label} immutable identity changed")
        protocol.check_transition(current, value)
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > protocol.max_bytes:
            raise ValueError(f"{protocol.label} exceeds its safety limit")
        strict_atomic_write_text(path, encoded)
    except (OSError, TypeError, ValueError) as exc:
        raise protocol.error_cls(
            f"{protocol.label} could not be published durably: {exc}") from exc
    confirmed = load(path)
    if confirmed != value:
        raise protocol.error_cls(f"{protocol.label} publication could not be confirmed")
    return confirmed


def receipts_for_run(
        root: Path, pattern: str,
        load: Callable[[Path], Optional[dict[str, Any]]]) -> list[tuple[Path, dict[str, Any]]]:
    """Every readable receipt matching one run's glob, as ``(path, receipt)``.

    Deliberately takes an already-derived ``root`` and ``pattern`` instead of ``(srv, rd)``: the two
    owners derive the run key from DIFFERENT authorities — reset from the command sequencer's file
    stem (having first proved that path sits in the canonical lock namespace), deletion from
    `run_deletion_key(rd)` cross-checked against that same stem — and those derivations are the
    identity binding each transaction rests on. Folding them into a shared "get the run key" would
    make one owner's binding silently answer for the other's.
    """
    found: list[tuple[Path, dict[str, Any]]] = []
    for path in root.glob(pattern):
        receipt = load(path)
        if receipt is not None:
            found.append((path, receipt))
    return found


# --------------------------------------------------------------- the destructive-quiescence ladder


def refuse_unless_quiescent(
        commands, rd: Path, *,
        active_command: Callable[[list[str]], HTTPException],
        engine_start: Callable[[], HTTPException],
        finalize_incomplete: Callable[[], HTTPException]) -> None:
    """The three-probe checklist a destructive whole-run mutation must pass, in its one order.

    SC-06 called this "four spellings of the same quiescence checklist". Measured against the tree it
    is THREE spellings of THIS ladder — `RunCommandService.destructive_guard`,
    `reset_route._admit_destructive_reset` and `deletion_service.begin_or_resume_run_deletion`'s
    fresh-preflight block, which ran the same three probes in the same order — plus a fourth,
    `RunCommandService.reject_if_active`, which is a DIFFERENT ladder and is deliberately not routed
    through here: it asks `_active_record` (this run's one authoritative command record) rather than
    `_active_command_ids` (a fail-CLOSED census that counts unreadable records and planted symlinks
    as active), it additionally refuses on `_unresolved_terminal_record`, and it checks finalize
    FIRST with an `allow_incomplete_finalize` opt-out that no destructive caller has. Those are
    answers to a different question — "may this legacy mutation overtake a durable intent?" versus
    "is it safe to destroy this run?" — and flattening them would have given every destructive path
    an opt-out from the finalize check.

    What is shared, and all that is shared, is the probe SET and its ORDER. The refusals are NOT: the
    three callers answer with three different HTTP bodies (a plain sentence, a `_detail` envelope
    with `retryable`, an `operation_id`-bearing conflict) and those are live contracts. So they stay
    at the call sites, passed in as builders — and because they are REQUIRED keyword arguments, a
    fourth probe added to this ladder cannot reach production without every caller supplying its own
    refusal for it. That is the failure this extraction exists to prevent: a destructive path that
    silently skips one rung races a live command and destroys a run out from under it.

    Each builder RETURNS its exception rather than raising, so the raise is here and the tracebacks
    all point at the one ladder.

    Lock precondition: the caller holds ``commands.sequence(rd)`` — every probe reads durable state a
    concurrent command would be mutating, and a check taken outside the sequencer answers about a
    moment that has already passed.
    """
    active = commands._active_command_ids(rd)
    if active:
        raise active_command(active)
    if commands._recent_spawn_claim(rd):
        raise engine_start()
    if commands._finalize_incomplete(rd):
        raise finalize_incomplete()


__all__ = [
    "ReceiptProtocol", "load_receipt", "receipts_for_run", "refuse_unless_quiescent",
    "regular_receipt_stat", "save_receipt",
]
