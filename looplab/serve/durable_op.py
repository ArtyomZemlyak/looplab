"""The durable-operation kit shared by the whole-run reset and deletion transactions (doc 25 SC-06).

This is the RECEIPT tier of the same split `core/fence.py` made one layer down for the fence MARKERS
(doc 25 CO-01), and it is worth stating the relationship because the two are easy to confuse:

  * `core/fence.py` owns the *fence* protocol — the marker that blocks every other writer while an
    operation is unresolved. It is read by the engine, the CLI and five serve modules.
  * `core/receipt.py` owns the *receipt* protocol — the operation's own durable state machine, read
    only by the transaction that wrote it: identity, phase, and the rules for advancing one. It
    lived HERE until 2026-09-08 and is re-exported below unchanged; see the import comment for why
    it moved down a layer.

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

So the PROTOCOL lives in one place and the SCHEMAS stay with their owners. Each owner declares one
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

from pathlib import Path
from typing import Callable

from fastapi import HTTPException

# The RECEIPT tier itself MOVED DOWN to `core/receipt.py` on 2026-09-08 (doc 34 D-01), unchanged,
# and is re-exported here so both owners' imports and their `durable_op.load_receipt` monkeypatch
# seam are untouched. It moved because a THIRD owner appeared below `serve`: the agent-facing node
# purge (`tools/run_control_tools.py`) is an irreversible multi-file transaction, `tools` may not
# import `serve`, and the only other way to give it a receipt was a fourth hand-rolled protocol —
# the exact duplication SC-06 extracted this kit to end. What stays here is the quiescence LADDER,
# which raises `HTTPException` and is therefore the server's.
from looplab.core.receipt import (
    ReceiptProtocol, load_receipt, receipts_for_run, regular_receipt_stat, save_receipt)

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
