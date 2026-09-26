"""A node of one run as ANOTHER run's operator inject: the ONE snapshot of what crosses runs.

Two producers import a node across runs, and until doc 67 67.2 only one existed: the server's
`import` action (`serve/control_validation.py::_import_cross_run_source`), which seeds a LIVE run
from a sibling's experiment, and now `Settings.seed_from_run`, which makes a prior run's champion
the first experiment of a NEW one (`engine/seed_from_run.py`). They must import the same thing —
the idea, the code, the files the node wrote and deleted, and a provenance receipt — so the snapshot
is spelled here once, as a pure function of the SOURCE run's fold. The server keeps its own
refusals (a 404 for a missing node, a 409 for a tombstoned or aborted one) and the new launch form
keeps its own; neither re-derives the payload.

The concept rule is the server's, moved verbatim: a source delta is relative to the SOURCE run's
base and DAG, so an effective membership is imported as an exact FULL set, and an unknown, partial
or unavailable one transports no envelope at all — the experiment and its code are still worth
importing; the taxonomy is left genuinely absent rather than reinterpreted against new parents.

The FILE-NAME rule is here for the same reason (critic 2026-09-26): what name an imported node may
carry — relative, portable, inside a node workspace on every host — was the server's
`_relative_file_name`, and the launch form re-spelled a weaker copy (no reserved device names, no
trailing dot or space, no control characters). `portable_relative_name` is the one predicate; each
producer phrases its own refusal.

Layering: `events`, so `core` only — the concept helpers and `durable_idea_payload` both live there.
"""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Optional

from looplab.core.concepts import (
    normalized_concept_materialization_receipt,
    normalized_concept_renames,
    resolve_concept_set,
)
from looplab.core.models import durable_idea_payload
from looplab.core.pathsafe import WINDOWS_RESERVED

# Why a name was refused: `not_text` (not a non-empty, bounded, printable string) or `escapes` (it
# could leave a node workspace, or names a different file on another host).
NAME_NOT_TEXT = "not_text"
NAME_ESCAPES = "escapes"


def portable_relative_name(value) -> tuple[Optional[str], Optional[str]]:
    """`(portable, None)` for a file name an imported node may carry — its forward-slash spelling —
    or `(None, defect)` naming why it may not (`NAME_NOT_TEXT` / `NAME_ESCAPES`). The rule the
    server's inject intake applied as `_relative_file_name`, moved here verbatim."""
    if (not isinstance(value, str) or not value or len(value) > 512
            or any(ord(ch) < 32 for ch in value)):
        return None, NAME_NOT_TEXT
    portable = value.replace("\\", "/")
    parsed = PurePosixPath(portable)
    raw_parts = portable.split("/")
    if (not parsed.parts or parsed.is_absolute() or ":" in portable
            or any(part in {"", ".", ".."} for part in raw_parts)
            or any(part.endswith((".", " ")) for part in raw_parts)
            or any(part.split(".", 1)[0].upper() in WINDOWS_RESERVED for part in raw_parts)):
        return None, NAME_ESCAPES
    return portable, None


def node_import_payload(source_state, node_id: int, source_run_id: str) -> dict:
    """The `inject_node` fields that import node `node_id` of a folded source run: `idea`, `code`,
    `files`, `deleted` and `origin`. The caller has already refused a missing, tombstoned or aborted
    node — this builds, it does not judge."""
    snode = source_state.nodes[node_id]
    sidea = durable_idea_payload(snode.idea)
    receipt = (getattr(source_state, "node_concept_materialization_receipts", None) or {}).get(
        node_id)
    receipt_valid = (receipt is None
                     or normalized_concept_materialization_receipt(receipt) is not None)
    membership_known = node_id in (getattr(source_state, "node_concepts", None) or {})
    effective: set[str] = set()
    membership_problem = None
    if receipt is None and receipt_valid and membership_known:
        effective, membership_problem = resolve_concept_set(
            source_state.node_concepts[node_id],
            normalized_concept_renames(getattr(source_state, "concept_consolidation", None)))
    if receipt is None and receipt_valid and membership_known and membership_problem is None:
        # a source delta is relative to the SOURCE base/DAG. Import its effective
        # snapshot as an exact full set so the target run cannot reinterpret it against new parents.
        sidea.update({"concept_mode": "full", "concepts": sorted(effective),
                      "concepts_added": [], "concepts_removed": []})
    else:
        # Unknown/partial/unavailable source membership must not transport a relative or future
        # envelope. The experiment/code import remains useful; taxonomy stays genuinely absent.
        for field in ("concept_mode", "concepts", "concepts_added", "concepts_removed"):
            sidea.pop(field, None)
    note = f"imported from run {source_run_id} #{node_id}"
    base = (sidea.get("rationale") or "").strip()
    sidea["rationale"] = f"{base} | {note}" if base else note
    return {
        "idea": sidea,
        "code": snode.code or None,
        "files": dict(snode.files),
        "deleted": list(snode.deleted),
        # ATTEMPT-STAMPED: a node id survives `node_reset`, so `(run_id, node_id)` alone stops
        # identifying the bytes that were actually imported the moment the source node is re-run —
        # the receipt and its UI link then point at a different experiment than the one this
        # snapshot came from. `attempt` is the source node's lifecycle generation at import time; it
        # is additive, so older receipts simply carry no `source_attempt` and read exactly as before.
        "origin": {"run_id": source_run_id, "node_id": node_id, "metric": snode.robust_metric,
                   "source_attempt": getattr(snode, "attempt", 0)},
    }
