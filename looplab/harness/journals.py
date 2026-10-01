"""Bounded journal reads which retain quarantine and non-absence I/O failures."""
from pathlib import Path

from fastapi import HTTPException

from looplab.core.jsonlio import read_jsonl_lenient_with_health

MAX_JOURNAL_BYTES = 16 * 1024 * 1024


def read_source(path: Path) -> tuple[list[dict], dict]:
    try:
        exists = path.exists()
        if exists and path.stat().st_size > MAX_JOURNAL_BYTES:
            raise HTTPException(503, {"code": "harness_history_too_large", "source": path.name})
        rows, health = read_jsonl_lenient_with_health(path)
        return rows, {**health, "file_present": exists}
    except OSError as exc:
        raise HTTPException(503, {"code": "harness_history_unavailable", "source": path.name}) from exc
