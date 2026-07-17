"""Shared generation-bound accounting for paid UI-side model calls.

The caller owns its operation-specific idempotency ledger.  This module owns the other half of the
boundary: one model client is leased to an exact run generation and every observed usage delta is
durably attributed to that run without ever repeating the provider call during recovery.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
import sys
import threading
import unicodedata

from looplab.engine.costs import (
    bind_run_client_cost, reconcile_cost_accountants, reconcile_usage_outbox)
from looplab.events.eventstore import EventStore


class RunCostAccountingPending(RuntimeError):
    """A prior paid call cannot yet be durably attributed to this run."""


_PENDING_RUN_COST_INIT = threading.Lock()


def pending_run_cost_state(
        srv) -> tuple[threading.Lock, dict[str, list[dict]], dict[str, threading.Lock]]:
    """Return the app-owned registry for paid deltas awaiting a durable event append."""
    with _PENDING_RUN_COST_INIT:
        lock = getattr(srv, "_pending_run_cost_lock", None)
        pending = getattr(srv, "_pending_run_costs", None)
        flush_locks = getattr(srv, "_pending_run_cost_flush_locks", None)
        if lock is None or not isinstance(pending, dict) or not isinstance(flush_locks, dict):
            lock = threading.Lock()
            pending = {}
            flush_locks = {}
            setattr(srv, "_pending_run_cost_lock", lock)
            setattr(srv, "_pending_run_costs", pending)
            setattr(srv, "_pending_run_cost_flush_locks", flush_locks)
        return lock, pending, flush_locks


def pending_run_cost_key(run_dir) -> str:
    """Filesystem-aware identity shared by all paid calls for one run directory."""
    return os.path.normcase(str(run_dir.resolve()))


def _retain_pending_run_cost(srv, run_dir, generation: str, ledger, activity_ctx) -> None:
    lock, pending, _flush_locks = pending_run_cost_state(srv)
    entry = {
        "generation": generation,
        "ledger": ledger,
        "activity_ctx": activity_ctx,
    }
    with lock:
        pending.setdefault(pending_run_cost_key(run_dir), []).append(entry)


def _flush_durable_run_costs_unlocked(run_dir) -> bool:
    try:
        return reconcile_usage_outbox(EventStore(run_dir / "events.jsonl"))
    except Exception:  # noqa: BLE001 - destructive callers must fail closed
        return False


def flush_durable_run_costs(srv, run_dir) -> bool:
    """Drain the restart-safe usage outbox without inverting the command sequencer."""
    key = pending_run_cost_key(run_dir)
    lock, _pending, flush_locks = pending_run_cost_state(srv)
    with lock:
        flush_lock = flush_locks.setdefault(key, threading.Lock())
    if not flush_lock.acquire(blocking=False):
        return False
    try:
        return _flush_durable_run_costs_unlocked(run_dir)
    finally:
        flush_lock.release()


def flush_pending_run_costs(srv, run_dir) -> bool:
    """Retry retained same-ID deltas without issuing another provider request."""
    key = pending_run_cost_key(run_dir)
    lock, pending, flush_locks = pending_run_cost_state(srv)
    with lock:
        flush_lock = flush_locks.setdefault(key, threading.Lock())
    with flush_lock:
        if not _flush_durable_run_costs_unlocked(run_dir):
            return False
        with lock:
            entries = pending.pop(key, [])
        if not entries:
            return True

        retained = []
        for entry in entries:
            try:
                same_generation = srv.commands.run_generation(run_dir) == entry["generation"]
                durable = same_generation and reconcile_cost_accountants(entry["ledger"])
            except Exception:  # noqa: BLE001 - preserve known usage and its lease
                durable = False
            if not durable:
                retained.append(entry)
                continue
            entry["activity_ctx"].__exit__(None, None, None)

        if retained:
            with lock:
                pending.setdefault(key, []).extend(retained)
        with lock:
            return not pending.get(key)


@contextmanager
def metered_run_client(srv, settings, run_dir, generation):
    """Lease and meter one UI-side model client against an exact run generation."""
    if not flush_pending_run_costs(srv, run_dir):
        raise RunCostAccountingPending

    activity_ctx = srv.commands.run_activity(run_dir, "ui_llm", generation=generation)
    activity_ctx.__enter__()
    retained = False
    try:
        client = srv.make_llm_client(settings)
        ledger = bind_run_client_cost(client, EventStore(run_dir / "events.jsonl"))
        try:
            yield client
        finally:
            if not reconcile_cost_accountants(ledger):
                _retain_pending_run_cost(
                    srv, run_dir, generation, ledger, activity_ctx)
                retained = True
    finally:
        if not retained:
            activity_ctx.__exit__(None, None, None)


def run_directory_identity(run_dir) -> str:
    """Stable direct-child identity with desktop filesystem case/Unicode semantics."""
    identity = run_dir.name
    if os.name == "nt":
        return os.path.normcase(identity)
    if sys.platform == "darwin":
        return unicodedata.normalize("NFD", identity).casefold()
    return identity
