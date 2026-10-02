"""Bounded journal reads which retain quarantine and non-absence I/O failures."""
from pathlib import Path

import orjson
from fastapi import HTTPException

from looplab.core.jsonlio import read_jsonl_lenient_with_health

MAX_JOURNAL_BYTES = 16 * 1024 * 1024

_RECEIPT_FIELDS = {"generation": str, "run_uid": str, "phase_id": str,
                   "at_node": int, "evidence_revision": str, "decision": str,
                   "reason": str, "action_id": str}


def receipt_records(rows: list[dict], health: dict, *, decisions: bool) -> list[dict]:
    """Shared display/write shape contract; bool is never an integer stamp.

    Display retains accepted rows with an incomplete health receipt. Approval or
    acknowledgement readers must refuse that incomplete source, because a skipped
    record could be the original action or a conflicting use of its identity.
    """
    fields = {**_RECEIPT_FIELDS, **({"idea_sha256": str} if decisions else {})}
    valid = [row for row in rows if all(type(row.get(key)) is value
                                       for key, value in fields.items())]
    health["invalid_record_rows"] = len(rows) - len(valid)
    health["read_complete"] &= len(valid) == len(rows)
    return valid


def read_receipts(path: Path, *, decisions: bool) -> list[dict]:
    """Require a complete bounded source before asserting replay or approval.

    Missing files are ordinary empty journals. Call under the existing ledger
    lock when publishing; this reader never quarantines, rewrites or repairs.
    """
    rows, health = read_source(path)
    rows = receipt_records(rows, health, decisions=decisions)
    if not health["read_complete"]:
        raise HTTPException(503, {"code": "harness_history_incomplete", "source": path.name,
                                  "message": "ask the operator to recover the journal before retrying"})
    return rows


def same_receipt_request(saved: dict, proposed: dict) -> bool:
    """Compare canonical client content, retaining scope and action identity.

    at_node/evidence_revision are assigned on the FIRST publication, not authored
    inputs. An exact retry after new outcomes must acknowledge that old receipt,
    never replace its evidence stamp or satisfy the new review/admission window.
    Comparing existing row projections also supports receipts from older servers.
    """
    context = {"at_node", "evidence_revision"}
    def authored(row):
        return orjson.dumps({key: value for key, value in row.items() if key not in context},
                            option=orjson.OPT_SORT_KEYS)
    return authored(saved) == authored(proposed)


def read_source(path: Path) -> tuple[list[dict], dict]:
    try:
        exists = path.exists()
        if exists and path.stat().st_size > MAX_JOURNAL_BYTES:
            raise HTTPException(503, {"code": "harness_history_too_large", "source": path.name})
        rows, health = read_jsonl_lenient_with_health(path)
        return rows, {**health, "file_present": exists}
    except OSError as exc:
        raise HTTPException(503, {"code": "harness_history_unavailable", "source": path.name}) from exc
