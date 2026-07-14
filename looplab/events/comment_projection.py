"""Pure projection for versioned operator comments.

The reducer is shared by ``replay.fold`` and the bounded owner/reviewer APIs.  Supported writers
validate before append, but replay must remain total over legacy/hand-edited logs: malformed,
duplicated, stale, or out-of-order comment records are deterministic no-ops.
"""
from __future__ import annotations

import math
import re
import hashlib
from collections.abc import Iterable
from typing import Optional

from looplab.core.models import CommentState, Event
from looplab.events.types import (
    EV_ANNOTATION, EV_COMMENT_CREATED, EV_COMMENT_EDITED, EV_COMMENT_RESOLUTION_CHANGED)


COMMENT_TEXT_MAX_BYTES = 8 * 1024
COMMENT_MAX_PER_RUN = 500
COMMENT_MAX_PER_NODE_GENERATION = 100
COMMENT_MAX_VERSION = 50
COMMENT_ID_RE = re.compile(r"^cmt_[0-9a-f]{32}$")
ACTOR_LABELS = {
    "deployment_owner": "Deployment owner",
    "local_operator": "Local operator",
    "legacy_unknown": "Legacy note (unattributed)",
}
_GENERATION_RE = re.compile(r"^[0-9a-f]{64}$")


class CommentCursorError(ValueError):
    def __init__(self, message: str, *, stale: bool = False):
        super().__init__(message)
        self.stale = stale


def normalize_comment_text(value: object) -> str:
    """Return one non-empty, strict-UTF-8 comment bounded by encoded bytes."""
    if not isinstance(value, str):
        raise ValueError("text must be a string")
    text = value.strip()
    if not text:
        raise ValueError("text must be non-empty")
    try:
        encoded = text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("text must be valid UTF-8") from exc
    if len(encoded) > COMMENT_TEXT_MAX_BYTES:
        raise ValueError(f"text must be at most {COMMENT_TEXT_MAX_BYTES} UTF-8 bytes")
    return text


def _integer(value: object, *, minimum: int = 0) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return None
    return value


def _timestamp(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return number if math.isfinite(number) and number >= 0 else 0.0


def _actor(value: object, *, legacy: bool = False) -> Optional[str]:
    if legacy:
        return "legacy_unknown"
    return value if value in {"deployment_owner", "local_operator"} else None


def _history_row(comment: CommentState, event: Event, action: str) -> dict:
    return {
        "version": comment.version,
        "action": action,
        "text": comment.text,
        "resolved": comment.resolved,
        "actor_kind": comment.actor_kind,
        "actor_label": ACTOR_LABELS[comment.actor_kind],
        "updated_at": _timestamp(event.ts),
        "event_seq": event.seq,
    }


def apply_comment_event(comments: dict[str, CommentState], event: Event,
                        history: Optional[dict[str, list[dict]]] = None) -> Optional[dict]:
    """Apply one accepted comment event and optionally return/store its audit snapshot."""
    data = event.data if isinstance(event.data, dict) else {}
    seq = _integer(event.seq)
    if seq is None:
        return None
    ts = _timestamp(event.ts)

    if event.type == EV_ANNOTATION:
        # Old notes have no durable id, actor, or node-attempt identity.  Keep them visible under a
        # stable synthetic id but never fabricate attribution or permit edits/resolution.
        node_id = _integer(data.get("node_id"))
        try:
            text = normalize_comment_text(data.get("text"))
        except ValueError:
            return None
        if node_id is None:
            return None
        comment_id = f"legacy_{seq}"
        if comment_id in comments or len(comments) >= COMMENT_MAX_PER_RUN:
            return None
        comment = CommentState(
            comment_id=comment_id, node_id=node_id, node_generation=None, text=text,
            actor_kind="legacy_unknown", version=1, resolved=False,
            created_at=ts, updated_at=ts, created_seq=seq, updated_seq=seq,
            legacy=True, editable=False,
        )
        comments[comment_id] = comment
        row = _history_row(comment, event, "created")
    elif event.type == EV_COMMENT_CREATED:
        comment_id = data.get("comment_id")
        node_id = _integer(data.get("node_id"))
        node_generation = _integer(data.get("node_generation"))
        actor = _actor(data.get("actor_kind"))
        version = _integer(data.get("version"), minimum=1)
        try:
            text = normalize_comment_text(data.get("text"))
        except ValueError:
            return None
        per_subject = sum(
            1 for item in comments.values()
            if (not item.legacy and item.node_id == node_id
                and item.node_generation == node_generation))
        if (not isinstance(comment_id, str) or COMMENT_ID_RE.fullmatch(comment_id) is None
                or comment_id in comments or node_id is None or node_generation is None
                or actor is None or version != 1 or len(comments) >= COMMENT_MAX_PER_RUN
                or per_subject >= COMMENT_MAX_PER_NODE_GENERATION):
            return None
        comment = CommentState(
            comment_id=comment_id, node_id=node_id, node_generation=node_generation, text=text,
            actor_kind=actor, version=1, resolved=False,
            created_at=ts, updated_at=ts, created_seq=seq, updated_seq=seq,
        )
        comments[comment_id] = comment
        row = _history_row(comment, event, "created")
    elif event.type in {EV_COMMENT_EDITED, EV_COMMENT_RESOLUTION_CHANGED}:
        comment_id = data.get("comment_id")
        comment = comments.get(comment_id) if isinstance(comment_id, str) else None
        actor = _actor(data.get("actor_kind"))
        node_id = _integer(data.get("node_id"))
        node_generation = _integer(data.get("node_generation"))
        base_version = _integer(data.get("base_version"), minimum=1)
        version = _integer(data.get("version"), minimum=2)
        if (comment is None or not comment.editable or actor is None
                or node_id != comment.node_id or node_generation != comment.node_generation
                or base_version != comment.version or version != comment.version + 1
                or version > COMMENT_MAX_VERSION):
            return None
        if event.type == EV_COMMENT_EDITED:
            try:
                text = normalize_comment_text(data.get("text"))
            except ValueError:
                return None
            if text == comment.text:
                return None
            comment.text = text
            action = "edited"
        else:
            resolved = data.get("resolved")
            if not isinstance(resolved, bool) or resolved == comment.resolved:
                return None
            comment.resolved = resolved
            action = "resolved" if resolved else "reopened"
        comment.actor_kind = actor
        comment.version = version
        comment.updated_at = ts
        comment.updated_seq = seq
        row = _history_row(comment, event, action)
    else:
        return None

    if history is not None:
        history.setdefault(comment.comment_id, []).append(row)
    return row


def project_comments(events: Iterable[Event], *, include_history: bool = False
                     ) -> tuple[dict[str, CommentState], dict[str, list[dict]]]:
    comments: dict[str, CommentState] = {}
    history: dict[str, list[dict]] = {}
    for event in events:
        apply_comment_event(comments, event, history if include_history else None)
    return comments, history


def comment_item(comment: CommentState) -> dict:
    """Exact current-item wire allow-list shared by owner, reviewer, and node detail."""
    return {
        "comment_id": comment.comment_id,
        "node_id": comment.node_id,
        "node_generation": comment.node_generation,
        "text": comment.text,
        "actor_kind": comment.actor_kind,
        "actor_label": ACTOR_LABELS[comment.actor_kind],
        "version": comment.version,
        "resolved": comment.resolved,
        "created_at": comment.created_at,
        "updated_at": comment.updated_at,
        "legacy": comment.legacy,
        # ``editable`` is a caller-facing capability, not merely a schema marker.  A modern comment
        # at the audit cap is as immutable as a legacy note even though its projected model retains
        # the original provenance flag for precise version-limit diagnostics.
        "editable": bool(comment.editable and not comment.legacy
                         and comment.version < COMMENT_MAX_VERSION),
    }


def _scope_digest(scope: str) -> str:
    return hashlib.sha256(scope.encode("utf-8")).hexdigest()[:16]


def _cursor_boundary(cursor: object, *, kind: str, generation: str, scope: str) -> Optional[int]:
    if cursor is None or cursor == "":
        return None
    if not isinstance(cursor, str) or len(cursor) > 160:
        raise CommentCursorError("comment cursor is invalid")
    parts = cursor.split(".")
    if len(parts) != 4 or parts[0] != kind:
        raise CommentCursorError("comment cursor is invalid")
    if parts[1] != generation:
        raise CommentCursorError("comment cursor belongs to another run generation", stale=True)
    if parts[2] != _scope_digest(scope):
        raise CommentCursorError("comment cursor belongs to another filter scope", stale=True)
    try:
        boundary = int(parts[3], 16)
    except ValueError as exc:
        raise CommentCursorError("comment cursor is invalid") from exc
    if boundary < 0 or boundary > 2**63 - 1 or parts[3] != f"{boundary:x}":
        raise CommentCursorError("comment cursor is invalid")
    return boundary


def _next_cursor(kind: str, generation: str, scope: str, seq: int) -> str:
    return f"{kind}.{generation}.{_scope_digest(scope)}.{seq:x}"


def comments_page(comments: dict[str, CommentState], *, generation: str, limit: int,
                  cursor: object = None, node_id: Optional[int] = None,
                  node_generation: Optional[int] = None,
                  include_resolved: bool = True) -> dict:
    if _GENERATION_RE.fullmatch(generation or "") is None:
        raise CommentCursorError("run generation is unavailable", stale=True)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise CommentCursorError("comment page limit is invalid")
    if (node_id is None) != (node_generation is None):
        raise CommentCursorError(
            "node_id and node_generation must be supplied together")
    scope = (f"node={node_id if node_id is not None else 'all'};"
             f"generation={node_generation if node_generation is not None else 'all'};"
             f"resolved={int(include_resolved)}")
    boundary = _cursor_boundary(cursor, kind="c", generation=generation, scope=scope)
    rows = sorted(comments.values(), key=lambda item: item.created_seq, reverse=True)
    if node_id is not None:
        rows = [item for item in rows if item.node_id == node_id]
    if node_generation is not None:
        rows = [item for item in rows if item.node_generation == node_generation]
    if not include_resolved:
        rows = [item for item in rows if not item.resolved]
    if boundary is not None:
        # A cursor names the exact last row of the preceding page.  Reject an anchor that vanished
        # from this filter (resolution change, log rewrite, or forged seq) instead of silently
        # skipping an arbitrary slice of the discussion.
        if not any(item.created_seq == boundary for item in rows):
            raise CommentCursorError("comment cursor anchor is no longer available", stale=True)
        rows = [item for item in rows if item.created_seq < boundary]
    page = rows[:limit]
    has_more = len(rows) > len(page)
    next_cursor = (_next_cursor("c", generation, scope, page[-1].created_seq)
                   if has_more and page else None)
    return {
        "comments": [comment_item(item) for item in page],
        "next_cursor": next_cursor,
        "has_more": has_more,
        "run_generation": generation,
    }


def history_page(comment_id: str, versions: list[dict], *, generation: str, limit: int,
                 cursor: object = None) -> dict:
    if _GENERATION_RE.fullmatch(generation or "") is None:
        raise CommentCursorError("run generation is unavailable", stale=True)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise CommentCursorError("comment page limit is invalid")
    boundary = _cursor_boundary(
        cursor, kind="h", generation=generation, scope=comment_id)
    rows = sorted(versions, key=lambda item: int(item.get("event_seq", -1)), reverse=True)
    if boundary is not None:
        if not any(int(item.get("event_seq", -1)) == boundary for item in rows):
            raise CommentCursorError("comment history cursor anchor is unavailable", stale=True)
        rows = [item for item in rows if int(item.get("event_seq", -1)) < boundary]
    page = rows[:limit]
    has_more = len(rows) > len(page)
    next_cursor = (_next_cursor(
        "h", generation, comment_id, int(page[-1]["event_seq"]))
        if has_more and page else None)
    return {
        "comment_id": comment_id,
        "versions": page,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "run_generation": generation,
    }
