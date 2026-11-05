"""Pure applicability checks for operator-authored memory; no HTTP/UI dependency."""
from __future__ import annotations

from looplab.engine.memory import task_fingerprint


def scope_fingerprint(task) -> list[str]:
    return task_fingerprint(getattr(task, "kind", ""), getattr(task, "direction", ""),
                            getattr(task, "goal", ""), metric=str(getattr(task, "metric", "") or ""),
                            universal=True)


def valid_operator_note(row) -> bool:
    return (isinstance(row, dict) and row.get("record_kind") == "operator_note"
            and isinstance(row.get("note_id"), str) and row["note_id"]
            and isinstance(row.get("note"), str) and row["note"]
            and isinstance(row.get("task_family"), str) and row["task_family"]
            and row.get("direction") in ("min", "max")
            and isinstance(row.get("fingerprint"), list)
            and all(isinstance(token, str) and token for token in row["fingerprint"]))
