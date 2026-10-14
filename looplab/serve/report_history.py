"""The run report's VERSION HISTORY and its duplicate rule — read models over `report_generated`.

Review 2026-10-14 section 6.3 / doc 80 §9 items 13, 14. The fold keeps ONE report (`RunState.report`, latest
wins, `events/replay_journals.py::_on_report_generated`), so every earlier version — what the agent
said three experiments ago, what the paid refresh replaced — was in the log and nowhere on screen.
And an agent re-sending the same report counted as a new one (Codex audit L-06): the report gate
accepted a copy.

Both are answered here without touching the fold:

* `report_versions` / `report_version` read every `report_generated` row through the SAME sanitizer
  the fold applies (`sanitize_report_payload(..., env={})`), so a version on screen is byte-for-byte
  what the fold would have published had it been the latest. A finish report is listed like any
  other row (its CAS chain decides only whether the fold PUBLISHES it, not whether it was written).
* `report_digest` is the identity of a report's CONTENT — everything but where and when it was
  published (`at_node`, `trigger`, `published_seq`, `published_at`). Two versions with one digest
  are the same report; the history marks the later one `duplicate_of`, and the command intake
  answers an identical re-send with `noop` (`serve/run_commands.py::_identical_report_duplicate`).
"""
from __future__ import annotations

from typing import Iterable, Optional

from looplab.core.advisory_payloads import sanitize_report_payload
from looplab.core.jsonutil import canonical_json_digest
from looplab.events.types import EV_REPORT_GENERATED

# Where/when a version was published — not what it says.
REPORT_PLACEMENT_KEYS = frozenset({"at_node", "trigger", "published_seq", "published_at"})
REPORT_VERSIONS_MAX = 200


def report_digest(content) -> Optional[str]:
    """The content identity of one sanitized report; None for a non-report."""
    if not isinstance(content, dict):
        return None
    return canonical_json_digest({k: v for k, v in content.items()
                                  if k not in REPORT_PLACEMENT_KEYS})


def _content(event) -> dict:
    data = event.data if isinstance(event.data, dict) else {}
    content = sanitize_report_payload(data.get("content") or data, env={})
    for key in ("at_node", "trigger"):
        if key in data:
            content[key] = sanitize_report_payload({key: data.get(key)}, env={})[key]
    return content


def report_versions(events: Iterable, *, current_seq: Optional[int] = None) -> dict:
    """Every published report, newest first, each with its digest and `duplicate_of`.

    `current_seq` is the fold's published version (`RunState.report.published_seq`), marked
    `current` so a reader can tell the version the Report tab shows from the ones it replaced (a
    finish report the fold refused is listed but never current).
    """
    rows: list[dict] = []
    first_by_digest: dict[str, int] = {}
    for event in events:
        if getattr(event, "type", None) != EV_REPORT_GENERATED:
            continue
        seq = event.seq if type(event.seq) is int else None
        content = _content(event)
        digest = report_digest(content)
        row = {
            "seq": seq,
            "ts": event.ts if isinstance(event.ts, (int, float)) else None,
            "at_node": content.get("at_node"),
            "trigger": content.get("trigger") or "",
            "headline": (content.get("headline") or content.get("summary") or "")[:300],
            "publisher": (content.get("publisher") or {}).get("kind") or "",
            "author_claim": (content.get("publisher") or {}).get("author_claim") or "",
            "has_body": bool(content.get("body_markdown")),
            "figures": len(content.get("figures") or []),
            "digest": digest,
        }
        if digest in first_by_digest:
            row["duplicate_of"] = first_by_digest[digest]
        elif digest is not None and seq is not None:
            first_by_digest[digest] = seq
        row["current"] = seq is not None and seq == current_seq
        rows.append(row)
    rows.reverse()
    return {"versions": rows[:REPORT_VERSIONS_MAX], "total": len(rows),
            "distinct": len(first_by_digest),
            "truncated": len(rows) > REPORT_VERSIONS_MAX}


def report_version(events: Iterable, seq: int) -> Optional[dict]:
    """The sanitized content of the report published at `seq`, or None."""
    for event in events:
        if getattr(event, "type", None) == EV_REPORT_GENERATED and event.seq == seq:
            content = _content(event)
            content["published_seq"] = seq
            return content
    return None
