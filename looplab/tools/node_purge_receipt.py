"""The durable receipt for the agent-facing NODE PURGE (doc 34 D-01).

`run_control_tools.py::RunControlTools._purge_node_snapshot` is an irreversible multi-file
transaction: it rewrites the authoritative `events.jsonl` with renumbered `seq`, replaces
`spans.jsonl`, retires two derived projections and `rmtree`s node workdirs. Until 2026-09-08 it did
all of that inside a bare `try/finally` with an ad-hoc `events.jsonl.bak-del<N>` copy that no reader
knew about — no operation id, no phase, no crash-recovery record — while its three sibling
destructive operations (whole-run reset, whole-run deletion, node trace clear) all keep one.

What a death mid-flight used to leave: a renumbered event log whose `seq` values no longer match the
unfiltered spans sidecar, node directories still on disk, and NOTHING anywhere saying an operation
was in flight. The next purge would then re-enter the rewrite over that state.

This is the SCHEMA half; the protocol (the paranoid read, the validate/immutable/transition/publish/
confirm save) is `core/receipt.py`, shared with reset and deletion. Doc 34 asked four questions
before a receipt could be added here, and this module is the four answers:

  * **Who owns the operation id?** The turn's own durable mutation journal does.
    `turn_mutation_fence.py` already stages every assistant mutation intent BEFORE the run is
    touched and hands back an idempotency key that a recovered turn reconstructs byte-identically;
    `purge_operation_id` derives the id from that key exactly as `_deletion_operation_id` derives
    the whole-run deletion's. With no journal (standalone/embedder use) the id is a fresh uuid4 —
    still unique, just not reconstructible, which only costs the id its stability across a restart.

  * **What is the phase lattice?** A MONOTONIC index over the transaction's own irreversible steps,
    `PURGE_PHASES`, and nothing else. Not reset's unordered adjacency table — every rung here has
    already changed the filesystem, so a back edge is unrepresentable on purpose — and,
    deliberately, not deletion's absorbing `quarantine_ambiguous` either: that state exists because
    a Windows quarantine RENAME can report failure after the destination became visible, so its
    receipt genuinely cannot say which side the run is on. Every step here is a copy, an atomic
    replace or an `rmtree`, so the last COMPLETED phase is always a true statement and is strictly
    more informative than "unknown" — and an unresolved receipt is already a fail-closed fence on
    every later purge of this run, so a second terminal state would add no authority and would cost
    the phase.

  * **What does recovery DO?** It REFUSES, loudly, and says what to inspect. It does not resume:
    re-entering a destructive rewrite of a run's own event log on a resume is strictly worse than
    the state it would be repairing (doc 34), and there is no operator in the assistant's loop to
    adjudicate an ambiguous outcome mid-turn. So an unresolved receipt is a fail-closed fence on
    every later purge of that run, and `describe_purge_recovery` states — per phase — what is
    known to be on disk, which file restores it, and that a human must clear the receipt. That is
    the whole difference this change makes: an inconsistency that was silent is now stated.

  * **Should an agent be able to do this at all?** Unchanged and still open as a product question.
    A receipt does not widen the tool; it makes the tool's worst outcome legible.

The receipt lives INSIDE the run directory (unlike deletion's, which must outlive the directory it
describes) and is named for its operation, so a second purge never overwrites the first's record —
the same append-only reasoning the numbered `events.jsonl.bak-del<N>` backups already follow.
A `succeeded` receipt is KEPT: it is the durable record that this run's history was compacted, and
it blocks nothing.
"""
from __future__ import annotations

import hashlib
import math
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from looplab.core.receipt import (
    ReceiptProtocol, load_receipt, receipts_for_run, save_receipt)


PURGE_RECEIPT_PREFIX = ".looplab-purge-receipt-"
PURGE_RECEIPT_GLOB = f"{PURGE_RECEIPT_PREFIX}*.json"
PURGE_RECEIPT_VERSION = 1
# Small on purpose: the subtree is the only unbounded field and a subtree large enough to overflow
# this is a purge of a whole run, which is a different operation with a different transaction.
PURGE_RECEIPT_MAX_BYTES = 16 * 1024
PURGE_OPERATION_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# The transaction's irreversible steps, in the ONE order `_purge_node_snapshot` performs them. Each
# name is the state the filesystem is in once that step returned, so a receipt read after a crash
# says which steps definitely happened — never which one was interrupted, because that is exactly
# what a crashed process cannot report about itself.
PURGE_PHASES = (
    "prepared",            # receipt published; nothing destructive done yet
    "backed_up",           # events.jsonl copied to the backup this receipt names
    "log_rewritten",       # events.jsonl replaced, renumbered — the point of no return
    "trace_published",     # spans.jsonl replaced with the filtered snapshot (or there was none)
    "workdirs_removed",    # the purged nodes' workdirs are gone
    "succeeded",
)
_PHASE_INDEX = {phase: index for index, phase in enumerate(PURGE_PHASES)}
_RECEIPT_KEYS = frozenset({
    "version", "id", "run_id", "node_id", "subtree", "expected_generation", "expected_seq",
    "backup", "status", "phase", "created_at", "updated_at",
})
# `backup` is immutable but NOT part of the identity for a reason worth stating: the name is chosen
# before the copy is taken (the first free `events.jsonl.bak-del<N>` suffix), so it is a promise the
# receipt makes and then keeps. Letting it change would let a later attempt point recovery at a file
# that never held this run's pre-purge bytes.
_IMMUTABLE_FIELDS = frozenset({
    "version", "id", "run_id", "node_id", "subtree", "expected_generation", "expected_seq",
    "backup", "created_at",
})


class NodePurgeReceiptError(RuntimeError):
    """A node-purge receipt is unavailable, malformed, or changed while being read."""


def purge_operation_id(key: str) -> str:
    """This purge's operation id, derived from the turn journal's idempotency key.

    Deliberately the same derivation shape as `run_command_adapter._deletion_operation_id`, with its
    own domain separator: the two ids name different operations over the same run and must never
    collide, and a recovered turn that reconstructs the same journal key reconstructs the same id.
    """
    if not key:
        return str(uuid.uuid4())
    digest = hashlib.sha256(("looplab-purge-v1\0" + key).encode("utf-8")).hexdigest()
    return f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"


def purge_receipt_path(rd: Path, operation_id: str) -> Path:
    if PURGE_OPERATION_RE.fullmatch(operation_id) is None:
        raise ValueError("invalid purge operation id")
    return Path(rd) / f"{PURGE_RECEIPT_PREFIX}{operation_id}.json"


def _validate_receipt(value: Any, *, path: Path) -> dict[str, Any]:
    if (not isinstance(value, dict) or set(value) != _RECEIPT_KEYS
            or value.get("version") != PURGE_RECEIPT_VERSION
            or not isinstance(value.get("id"), str)
            or PURGE_OPERATION_RE.fullmatch(value["id"]) is None
            or not isinstance(value.get("run_id"), str) or not value["run_id"]
            or Path(value["run_id"]).name != value["run_id"]
            or value["run_id"] in {".", ".."}
            or type(value.get("node_id")) is not int or value["node_id"] < 0
            or not isinstance(value.get("subtree"), list)
            or any(type(n) is not int or n < 0 for n in value["subtree"])
            or sorted(value["subtree"]) != value["subtree"]
            or value["node_id"] not in value["subtree"]
            or not isinstance(value.get("expected_generation"), str)
            or _SHA256_RE.fullmatch(value["expected_generation"]) is None
            or type(value.get("expected_seq")) is not int or value["expected_seq"] < -1
            # The backup is a NAME inside the run directory, never a path: recovery reads it beside
            # the receipt, and a receipt that could name `../../etc` would be a traversal primitive
            # handed to whoever repairs the run.
            or not isinstance(value.get("backup"), str) or not value["backup"]
            or Path(value["backup"]).name != value["backup"]
            or value["backup"] in {".", ".."}
            or value.get("phase") not in _PHASE_INDEX
            or value.get("status") != (
                "succeeded" if value.get("phase") == "succeeded" else "pending")
            or isinstance(value.get("created_at"), bool)
            or not isinstance(value.get("created_at"), (int, float))
            or not math.isfinite(value["created_at"])
            or isinstance(value.get("updated_at"), bool)
            or not isinstance(value.get("updated_at"), (int, float))
            or not math.isfinite(value["updated_at"])
            or value["updated_at"] < value["created_at"]):
        raise NodePurgeReceiptError("node purge receipt is malformed")
    if path.name != f"{PURGE_RECEIPT_PREFIX}{value['id']}.json":
        raise NodePurgeReceiptError("node purge receipt path and identity disagree")
    return value


def _check_transition(current: dict[str, Any], value: dict[str, Any]) -> None:
    """Purge's phase lattice: a MONOTONIC index, one rung at a time, and nothing else.

    Every rung has already changed the filesystem, so there is no back edge to admit — that is the
    difference from reset's unordered adjacency table, and the absence of deletion's absorbing
    `quarantine_ambiguous` is the other half of the same reasoning (see the module docstring). It is
    written out rather than reused precisely so neither asymmetry can be flattened by accident.
    """
    if current["phase"] == "succeeded":
        raise NodePurgeReceiptError("a succeeded node purge receipt is terminal")
    old_phase = _PHASE_INDEX[current["phase"]]
    new_phase = _PHASE_INDEX[value["phase"]]
    if new_phase < old_phase or new_phase > old_phase + 1:
        raise NodePurgeReceiptError(
            f"invalid node purge receipt transition {current['phase']} -> {value['phase']}")


_PROTOCOL = ReceiptProtocol(
    label="node purge receipt",
    error_cls=NodePurgeReceiptError,
    max_bytes=PURGE_RECEIPT_MAX_BYTES,
    validate=_validate_receipt,
    immutable=_IMMUTABLE_FIELDS,
    check_transition=_check_transition,
)


def load_purge_receipt(path: Path) -> Optional[dict[str, Any]]:
    return load_receipt(path, _PROTOCOL)


def save_purge_receipt(path: Path, value: dict[str, Any]) -> dict[str, Any]:
    return save_receipt(path, value, _PROTOCOL, load=load_purge_receipt)


def prepare_purge_receipt(
        rd: Path, *, operation_id: str, node_id: int, subtree: set[int],
        expected_generation: str, expected_seq: int, backup: str) -> dict[str, Any]:
    """The `prepared` receipt: published after the last step that can still REFUSE and before the
    first one the transaction cannot take back, so a refused purge leaves no record and fences
    nothing. The only thing already done at this phase is the retirement of the two derived trace
    projections, which rebuild themselves."""
    now = time.time()
    return {
        "version": PURGE_RECEIPT_VERSION,
        "id": operation_id,
        "run_id": Path(rd).name,
        "node_id": int(node_id),
        "subtree": sorted(int(n) for n in subtree),
        "expected_generation": expected_generation,
        "expected_seq": int(expected_seq),
        "backup": backup,
        "status": "pending",
        "phase": "prepared",
        "created_at": now,
        "updated_at": now,
    }


def advance_purge_receipt(path: Path, receipt: dict[str, Any], phase: str) -> dict[str, Any]:
    if phase not in _PHASE_INDEX:
        raise ValueError("invalid node purge phase")
    return save_purge_receipt(path, {
        **receipt,
        "phase": phase,
        "status": "succeeded" if phase == "succeeded" else "pending",
        "updated_at": time.time(),
    })


def unresolved_purge_receipts(rd: Path) -> list[tuple[Path, dict[str, Any]]]:
    """Every purge receipt in *rd* that is not `succeeded`, as `(path, receipt)`.

    An UNREADABLE receipt raises rather than being skipped, and that is the whole safety property:
    "there is no receipt" and "there is a receipt I cannot read" must never collapse, or a crashed
    transaction whose record is corrupt reads as a clean run and gets rewritten again.
    """
    found = receipts_for_run(Path(rd), PURGE_RECEIPT_GLOB, load_purge_receipt)
    return [(path, receipt) for path, receipt in found if receipt.get("phase") != "succeeded"]


# What is known to be on disk at each pending phase, and which file undoes it. Stated per rung
# because "some of a purge happened" is not something a human can act on, and the receipt exists
# precisely so they do not have to reconstruct it from mtimes.
_PHASE_STATE = {
    "prepared": ("nothing was rewritten yet, though the derived trace projections may have been "
                 "retired (they rebuild themselves)"),
    "backed_up": "the event log was copied but not yet rewritten",
    "log_rewritten": ("the event log IS rewritten and renumbered while the trace sidecar is not — "
                      "the two disagree"),
    "trace_published": "the event log and the trace sidecar are both rewritten; workdirs may remain",
    "workdirs_removed": "every step completed but the operation was never marked succeeded",
}


def describe_purge_recovery(receipt: dict[str, Any]) -> str:
    """One bounded sentence naming the operation, what is on disk, and what a human must do.

    Read by the tool's refusal and by nothing that decides: the decision is always the same (refuse),
    because auto-resuming a destructive rewrite of a run's own event log is worse than the state it
    would repair.
    """
    phase = str(receipt.get("phase") or "prepared")
    state = _PHASE_STATE.get(phase, "its state is unknown")
    nodes = receipt.get("subtree") or [receipt.get("node_id")]
    return (
        f"purge {receipt.get('id')} of node(s) {nodes} stopped at {phase} — {state}. "
        f"Restore from {receipt.get('backup')} (or accept the compaction), then delete "
        f"{PURGE_RECEIPT_PREFIX}{receipt.get('id')}.json to clear this fence."
    )


__all__ = [
    "NodePurgeReceiptError", "PURGE_PHASES", "PURGE_RECEIPT_GLOB", "PURGE_RECEIPT_PREFIX",
    "advance_purge_receipt", "describe_purge_recovery", "load_purge_receipt",
    "prepare_purge_receipt", "purge_operation_id", "purge_receipt_path",
    "save_purge_receipt", "unresolved_purge_receipts",
]
