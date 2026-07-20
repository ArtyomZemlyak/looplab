"""Durable at-most-once transactions for on-demand paid governance stewards.

The provider call cannot participate in the JSONL transaction. A durable ``begun`` row therefore claims
an action id before dispatch, and a terminal row closes it afterwards. A crash between those rows is
intentionally ambiguous: retrying the same id reports the claim and never purchases a replacement call.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from looplab.trust.cross_run import cross_run_identity_text, cross_run_text, sanitize_cross_run_projection


_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}


def _thread_lock(path: Path) -> threading.Lock:
    key = str(path.absolute())
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


def _log_path(memory_dir, kind: str) -> Path:
    names = {
        "concept": "concept_curation_log.jsonl",
        "claim": "claim_curation_log.jsonl",
        "facets": "task_facets_curation_log.jsonl",
    }
    try:
        name = names[kind]
    except KeyError as exc:
        raise ValueError("unknown steward kind") from exc
    if not memory_dir:
        raise ValueError("no memory_dir")
    return Path(memory_dir) / name


def _action_id(value: str) -> str:
    raw = str(value or "")
    bounded = cross_run_identity_text(raw, max_chars=160).strip()
    if not bounded or bounded != raw:
        raise ValueError("action_id must be a non-empty bounded identity")
    return bounded


def _public_text(value, maximum: int) -> str:
    return cross_run_text(value, max_chars=maximum, single_line=True, entropy=True)


def _read(path: Path, *, kind: str) -> list[dict]:
    from looplab.engine.governance_health import curation_ledger_scope, read_curation_rows

    expected_kind, ledger = curation_ledger_scope(path.name)
    if expected_kind != kind:
        raise ValueError("steward kind does not match curation ledger")
    return read_curation_rows(path, kind=kind, ledger=ledger)


def _cached(path: Path, *, kind: str, action_id: str) -> dict | None:
    begun = terminal = None
    for row in _read(path, kind=kind):
        if (row.get("action") == "steward-invocation"
                and str(row.get("action_id") or "") == action_id):
            terminal = row
        elif (row.get("action") == "steward-invocation-begun"
              and str(row.get("invocation_id") or "") == action_id):
            begun = row
    return terminal or begun


def _append(path: Path, *, kind: str, record: dict) -> dict:
    from looplab.engine.concept_registry import _append_governance

    path.parent.mkdir(parents=True, exist_ok=True)
    return _append_governance(
        path, record, require_durable=True,
        read_rows=lambda current: _read(current, kind=kind),
    )


def _has_proposals(kind: str, proposals: dict) -> bool:
    if kind == "concept":
        return any(proposals.get(field) for field in ("merges", "splits", "purges"))
    if kind == "claim":
        return bool(proposals.get("decisions"))
    return bool(proposals.get("facets"))


def _begin(path: Path, *, kind: str, action_id: str, actor: str, at: str) -> dict:
    return _append(path, kind=kind, record={
        "v": 1, "action": "steward-invocation-begun", "from": kind,
        # ``action_id`` is reserved for the terminal row; the validator binds this invocation id to
        # exactly one later terminal ``begun_revision``.
        "invocation_id": action_id, "by": actor, "at": at, "outcome": "begun",
    })


def _finish(path: Path, *, kind: str, action_id: str, actor: str, at: str,
            proposals: dict | None = None, receipt: dict | None = None,
            error: str = "", begun_revision: int | None = None) -> dict:
    safe_proposals = sanitize_cross_run_projection(
        proposals or {}, max_chars=64_000, max_items=128, max_total_items=2_048)
    record = {
        "v": 1, "action": "steward-invocation", "from": kind,
        "action_id": action_id, "by": actor, "at": at,
        "outcome": ("error" if error else
                    ("proposed" if _has_proposals(kind, safe_proposals) else "empty")),
        "proposals": safe_proposals,
        "receipt": sanitize_cross_run_projection(
            receipt, max_chars=32_000, max_items=128, max_total_items=1_024),
    }
    if begun_revision is not None:
        record["begun_revision"] = begun_revision
    if error:
        record["error"] = _public_text(error, 500)
    return _append(path, kind=kind, record=record)


@contextmanager
def _invocation_guard(path: Path):
    from looplab.events.eventstore import _interprocess_lock

    path.parent.mkdir(parents=True, exist_ok=True)
    invoke_lock = path.parent / f"{path.name}.invoke.lock"
    with _thread_lock(invoke_lock):
        with _interprocess_lock(invoke_lock, required=True):
            yield


def run_steward_invocation(
        memory_dir, kind: str, action_id: str, *, actor: str, at: str,
        prepare: Callable[[], Any], invoke: Callable[[Any], dict],
        safe_error: Callable[[Exception, str], str]) -> tuple[dict, bool]:
    """Run or replay one paid steward action, returning ``(durable_record, replayed)``.

    ``prepare`` constructs the provider client and runs before the begun claim because construction itself
    is not paid. ``invoke`` starts only after the begun row is durably published. Every exception becomes
    a redacted terminal error; a terminal-write failure leaves the begun row as the recovery fence.
    """
    path = _log_path(memory_dir, kind)
    action_id = _action_id(action_id)
    actor = _public_text(actor or "operator", 120)
    at = cross_run_identity_text(str(at or ""), max_chars=120)
    with _invocation_guard(path):
        existing = _cached(path, kind=kind, action_id=action_id)
        if existing is not None:
            # CODEX AGENT: terminal replay happens before provider setup; an unresolved begun claim is
            # equally final for this identity because replacing an unknown paid outcome can double-charge.
            # Reconfirm the file and its directory before acknowledging a row whose original response may
            # have failed during fsync after bytes reached the page cache.
            from looplab.engine.governance_health import confirm_governance_durable
            confirm_governance_durable(path)
            return existing, True
        try:
            prepared = prepare()
        except Exception as exc:  # noqa: BLE001 - persisted only through caller's redacted classifier
            return _finish(
                path, kind=kind, action_id=action_id, actor=actor, at=at,
                error=safe_error(exc, "client")), False
        begun = _begin(
            path, kind=kind, action_id=action_id, actor=actor, at=at)
        try:
            output = invoke(prepared)
            if not isinstance(output, dict):
                raise ValueError("invalid steward result")
        except Exception as exc:  # noqa: BLE001 - persisted only through caller's redacted classifier
            return _finish(
                path, kind=kind, action_id=action_id, actor=actor, at=at,
                error=safe_error(exc, "steward"),
                begun_revision=begun.get("revision")), False
        return _finish(
            path, kind=kind, action_id=action_id, actor=actor, at=at,
            proposals=output.get("proposals"), receipt=output.get("receipt"),
            begun_revision=begun.get("revision")), False
