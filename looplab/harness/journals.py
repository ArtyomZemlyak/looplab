"""Bounded journal reads which retain quarantine and non-absence I/O failures."""
from pathlib import Path

import orjson
from fastapi import HTTPException

from looplab.core.jsonlio import read_jsonl_lenient_with_health

MAX_JOURNAL_BYTES = 16 * 1024 * 1024


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
