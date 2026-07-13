"""Durable LLM usage accounting for one run.

``CostAccountant`` remains the synchronous in-process budget guard. This module binds its
post-commit delta callback to the append-only event log, so appended usage survives restart.
Only numeric counts are recorded: never prompts, responses, model URLs, or credentials.

Every delta carries a stable usage ID. Before the event append, the exact sanitized delta is written
atomically to a per-run outbox; failed or ambiguously acknowledged appends are retried with that same
ID and replay is first-write-wins, so recovery cannot double-charge. A kill before the outbox rename
completes and before the event append completes is the remaining explicitly documented
pre-first-persistence measurement gap and never turns into another paid provider request.
"""
from __future__ import annotations

import math
import os
import secrets
import stat
import sys
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import orjson

from looplab.core.atomicio import atomic_write_bytes
from looplab.events.types import EV_LLM_USAGE


_MAX_COUNTER = (1 << 63) - 1
_MAX_COST = sys.float_info.max
_COUNTER_KEYS = ("calls", "prompt_tokens", "completion_tokens", "total_tokens")
_OUTBOX_DIRNAME = ".llm-usage-outbox"
_OUTBOX_VERSION = 1
_ROOT_ATTRS = ("researcher", "developer", "strategist", "deep_researcher",
               "report_writer", "onboarder")
_CHILD_ATTRS = (
    "client", "inner", "fallback", "researcher", "developer", "strategist", "tools",
    "_pilot_client", "summary_client", "loop_opts", "_loop_opts", "stage_clients", "providers",
)


class _OutboxEvidenceError(OSError):
    """An existing record cannot safely be attributed or acknowledged."""


def _safe_counter(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if 0 <= value <= _MAX_COUNTER else 0


def _safe_cost(value: Any) -> float:
    if value is None or isinstance(value, (bool, str, bytes, bytearray)):
        return 0.0
    try:
        out = float(value)
    except (OverflowError, TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) and out >= 0.0 else 0.0


def sanitize_usage_delta(data: Any) -> dict[str, int | float]:
    """Return the only payload shape allowed into the durable usage ledger."""
    try:
        raw = dict(data) if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - hostile provider telemetry degrades to zero
        raw = {}
    return {
        "cost": _safe_cost(raw.get("cost")),
        "calls": _safe_counter(raw.get("calls")),
        "prompt_tokens": _safe_counter(raw.get("prompt_tokens")),
        "completion_tokens": _safe_counter(raw.get("completion_tokens")),
        "total_tokens": _safe_counter(raw.get("total_tokens")),
    }


def _snapshot(accountant: object) -> dict[str, int | float]:
    def read() -> dict[str, int | float]:
        return sanitize_usage_delta({
            "cost": getattr(accountant, "spent", 0.0),
            "calls": getattr(accountant, "calls", 0),
            "prompt_tokens": getattr(accountant, "prompt_tokens", 0),
            "completion_tokens": getattr(accountant, "completion_tokens", 0),
            "total_tokens": getattr(accountant, "total_tokens", 0),
        })

    # CostAccountant commits all counters under this lock. Read them under the same lock when
    # available so reconciliation cannot manufacture a torn cross-field delta mid-call.
    lock = getattr(accountant, "_lock", None)
    if lock is not None and hasattr(lock, "__enter__") and hasattr(lock, "__exit__"):
        try:
            with lock:
                return read()
        except Exception:  # noqa: BLE001 - a third-party lock/field may be surprising
            pass
    return read()


def _zero() -> dict[str, int | float]:
    return {"cost": 0.0, "calls": 0, "prompt_tokens": 0,
            "completion_tokens": 0, "total_tokens": 0}


def _children(obj: object) -> Iterable[object]:
    if isinstance(obj, dict):
        yield from obj.values()
        return
    if isinstance(obj, (list, tuple, set, frozenset)):
        yield from obj
        return
    for attr in _CHILD_ATTRS:
        try:
            child = getattr(obj, attr, None)
        except Exception:  # noqa: BLE001 - accounting introspection must not break a run
            continue
        if child is not None and child is not obj:
            yield child


def find_cost_accountants(engine: object) -> list[object]:
    """Walk every LLM-bearing role/wrapper and identity-dedupe its accountants."""
    stack: list[object] = []
    for attr in _ROOT_ATTRS:
        try:
            root = getattr(engine, attr, None)
        except Exception:  # noqa: BLE001
            root = None
        if root is not None:
            stack.append(root)

    seen_objects: set[int] = set()
    accountants: dict[int, object] = {}
    while stack:
        obj = stack.pop()
        oid = id(obj)
        if oid in seen_objects:
            continue
        seen_objects.add(oid)
        try:
            accountant = getattr(obj, "accountant", None)
        except Exception:  # noqa: BLE001
            accountant = None
        if accountant is not None:
            accountants.setdefault(id(accountant), accountant)
        stack.extend(child for child in _children(obj) if child is not None)
    return list(accountants.values())


def _tracker(engine: object) -> tuple[dict[int, dict[str, Any]], threading.RLock]:
    bindings = getattr(engine, "_llm_cost_bindings", None)
    lock = getattr(engine, "_llm_cost_lock", None)
    if not isinstance(bindings, dict):
        bindings = {}
        setattr(engine, "_llm_cost_bindings", bindings)
    if lock is None:
        lock = threading.RLock()
        setattr(engine, "_llm_cost_lock", lock)
    return bindings, lock


def _has_value(delta: dict[str, int | float]) -> bool:
    return bool(delta["cost"] or any(delta[key] for key in _COUNTER_KEYS))


def _record(binding: dict[str, Any], clean: dict[str, int | float]) -> None:
    recorded = binding["recorded"]
    recorded["cost"] = min(_MAX_COST, float(recorded["cost"]) + float(clean["cost"]))
    for key in _COUNTER_KEYS:
        recorded[key] = min(_MAX_COUNTER, int(recorded[key]) + int(clean[key]))


def _outbox_dir(engine: object) -> Path | None:
    """Return the run-local outbox without assuming a concrete EventStore implementation."""
    try:
        path = getattr(getattr(engine, "store"), "path", None)
    except Exception:  # noqa: BLE001 - fake/third-party stores keep the memory-only fallback
        return None
    if path is None:
        return None
    try:
        return Path(path).parent / _OUTBOX_DIRNAME
    except (TypeError, ValueError):
        return None


def _valid_usage_id(usage_id: object) -> bool:
    return (isinstance(usage_id, str) and len(usage_id) == 32
            and all(char in "0123456789abcdef" for char in usage_id))


def _outbox_record(usage_id: str, clean: dict[str, int | float]) -> dict[str, Any]:
    return {"version": _OUTBOX_VERSION, "usage_id": usage_id, "delta": dict(clean)}


def _decode_outbox(path: Path) -> tuple[str, dict[str, int | float]]:
    """Read one self-authenticating record; malformed pending usage must fail closed."""
    if path.is_symlink():
        raise ValueError("usage outbox record must not be a symlink")
    if not path.is_file():
        raise ValueError("usage outbox record must be a regular file")
    raw = orjson.loads(path.read_bytes())
    if not isinstance(raw, dict) or set(raw) != {"version", "usage_id", "delta"}:
        raise ValueError("invalid usage outbox envelope")
    usage_id = raw.get("usage_id")
    if raw.get("version") != _OUTBOX_VERSION or not _valid_usage_id(usage_id):
        raise ValueError("invalid usage outbox identity")
    if path.name != f"{usage_id}.json":
        raise ValueError("usage outbox filename does not match its identity")
    delta = raw.get("delta")
    if not isinstance(delta, dict) or set(delta) != {"cost", *_COUNTER_KEYS}:
        raise ValueError("invalid usage outbox delta")
    # Reject rather than coerce a damaged record. These are locally-written values, so any
    # difference from the sanitizer means the exact known delta can no longer be proven.
    clean = sanitize_usage_delta(delta)
    if (isinstance(delta.get("cost"), bool)
            or not isinstance(delta.get("cost"), (int, float))
            or any(isinstance(delta.get(key), bool) or not isinstance(delta.get(key), int)
                   for key in _COUNTER_KEYS)
            or any(delta.get(key) != clean[key] for key in ("cost", *_COUNTER_KEYS))):
        raise ValueError("unsafe usage outbox values")
    return usage_id, clean


def _persist_outbox(engine: object, usage_id: str,
                    clean: dict[str, int | float]) -> bool:
    """Atomically retain one exact delta; return false only for stores without a run path."""
    directory = _outbox_dir(engine)
    if directory is None:
        return False
    if directory.is_symlink():
        raise _OutboxEvidenceError("usage outbox directory must not be a symlink")
    path = directory / f"{usage_id}.json"
    expected = _outbox_record(usage_id, clean)
    # `Path.exists()` follows links and is false for a broken symlink. Treat any directory entry as
    # evidence: atomic replace must never erase an uninspectable same-ID record.
    if path.is_symlink() or os.path.lexists(path):
        try:
            existing_id, existing = _decode_outbox(path)
        except (OSError, ValueError, orjson.JSONDecodeError) as exc:
            raise _OutboxEvidenceError("unreadable existing usage outbox record") from exc
        if existing_id != usage_id or existing != clean:
            raise _OutboxEvidenceError("conflicting usage outbox identity")
        return True
    atomic_write_bytes(path, orjson.dumps(expected, option=orjson.OPT_SORT_KEYS))
    # Verify the renamed record before treating it as the durable recovery boundary.
    try:
        stored_id, stored = _decode_outbox(path)
    except (OSError, ValueError, orjson.JSONDecodeError) as exc:
        raise _OutboxEvidenceError("unreadable committed usage outbox record") from exc
    if stored_id != usage_id or stored != clean:
        raise _OutboxEvidenceError("usage outbox verification failed")
    return True


def _outbox_entry_present(engine: object, usage_id: str) -> bool:
    """Whether this exact ID still has evidence, including a broken symlink/non-file."""
    directory = _outbox_dir(engine)
    if directory is None:
        return False
    try:
        return os.path.lexists(directory / f"{usage_id}.json")
    except OSError:
        return True


def _forget_outbox(engine: object, usage_id: str,
                   clean: dict[str, int | float]) -> bool:
    """Acknowledge only the exact expected record; never erase conflicting pending evidence."""
    directory = _outbox_dir(engine)
    if directory is None:
        return True
    path = directory / f"{usage_id}.json"
    try:
        if not os.path.lexists(path):
            return True
        stored_id, stored = _decode_outbox(path)
        if stored_id != usage_id or stored != clean:
            return False
        path.unlink(missing_ok=True)
    except (OSError, ValueError, orjson.JSONDecodeError):
        return False
    return True


def _drain_outbox(engine: object,
                  persisted: dict[str, dict[str, int | float]]) -> bool:
    """Replay every prior-process delta with its original ID before allowing a summary."""
    directory = _outbox_dir(engine)
    if directory is None:
        return True
    try:
        # Query the directory entry itself. `exists()`/`lexists()` collapse some access errors into
        # false, which would make inaccessible evidence look clean; only FileNotFoundError proves
        # actual absence. A broken symlink is visible to lstat and therefore fails closed below.
        directory_stat = directory.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        if stat.S_ISLNK(directory_stat.st_mode):
            return False
        if not stat.S_ISDIR(directory_stat.st_mode):
            return False
        # Include malformed directories/special entries carrying our record suffix. `_decode_outbox`
        # rejects them fail-closed; filtering to regular files here would silently hide evidence.
        paths = sorted(path for path in directory.iterdir() if path.suffix == ".json")
    except OSError:
        return False

    complete = True
    for path in paths:
        try:
            usage_id, clean = _decode_outbox(path)
        except (OSError, ValueError, orjson.JSONDecodeError):
            complete = False
            continue
        if usage_id in persisted:
            # First-write-wins means a same-ID event with different telemetry is authoritative, but
            # it does not prove this pending record was acknowledged. Retain the conflict for repair.
            if persisted[usage_id] != clean or not _forget_outbox(engine, usage_id, clean):
                complete = False
            continue
        try:
            engine.store.append(EV_LLM_USAGE, _payload(usage_id, clean))
        except Exception:  # append may have committed before surfacing an error
            try:
                durable = _event_usage_deltas(engine.store.read_all()).get(usage_id) == clean
            except Exception:  # noqa: BLE001 - keep the outbox for a later process
                durable = False
            if not durable:
                complete = False
                continue
        else:
            try:
                durable = _event_usage_deltas(engine.store.read_all()).get(usage_id) == clean
            except Exception:  # noqa: BLE001 - never acknowledge an unverified first writer
                durable = False
            if not durable:
                complete = False
                continue
        persisted[usage_id] = clean
        if not _forget_outbox(engine, usage_id, clean):
            complete = False
    return complete


def _queue(binding: dict[str, Any], clean: dict[str, int | float]) -> str:
    pending = binding["pending"]
    usage_id = secrets.token_hex(16)
    while usage_id in pending:  # practically impossible, but make the identity contract total
        usage_id = secrets.token_hex(16)
    pending[usage_id] = dict(clean)
    return usage_id


def _payload(usage_id: str, clean: dict[str, int | float]) -> dict[str, int | float | str]:
    return {**clean, "usage_id": usage_id}


def _event_usage_deltas(events: Iterable[object]) -> dict[str, dict[str, int | float]]:
    deltas: dict[str, dict[str, int | float]] = {}
    for event in events:
        if getattr(event, "type", None) != EV_LLM_USAGE:
            continue
        data = getattr(event, "data", None)
        usage_id = data.get("usage_id") if isinstance(data, dict) else None
        if isinstance(usage_id, str) and usage_id:
            deltas.setdefault(usage_id, sanitize_usage_delta(data))
    return deltas


def bind_cost_accountants(engine: object, *, include_existing: bool = False) -> list[object]:
    """Bind every currently reachable accountant exactly once.

    Construction and an explicit role swap use current counters as their baseline: usage from
    before attachment belongs elsewhere. Finalization sets ``include_existing`` for a role that
    became reachable without the explicit swap seam; zero is safer there than dropping paid calls.
    """
    bindings, lock = _tracker(engine)
    found = find_cost_accountants(engine)
    with lock:
        for accountant in found:
            aid = id(accountant)
            if aid in bindings:
                continue
            binding: dict[str, Any] = {
                "accountant": accountant, "baseline": _zero(), "recorded": _zero(),
                "pending": {}, "sink": None, "durable_sink": False,
            }
            bindings[aid] = binding

            setter = getattr(accountant, "set_sink", None)
            binder = getattr(accountant, "bind_sink", None)
            if not callable(setter) and not callable(binder):
                # Legacy/fake accountant: finalization reconciles its aggregate counters.
                binding["baseline"] = _zero() if include_existing else _snapshot(accountant)
                continue

            def make_sink(previous, *, _binding=binding):
                # Reusing an accountant in another Engine transfers future ownership. An in-flight
                # add already captured the old sink and still finishes in the old ledger; later adds
                # use only this one. Preserve only a caller-owned callback.
                if getattr(previous, "_looplab_cost_sink", False):
                    previous = None

                def sink(delta, *, _previous=previous):
                    clean = sanitize_usage_delta(delta)
                    append_error: Exception | None = None
                    if _has_value(clean):
                        with lock:
                            usage_id = _queue(_binding, clean)
                            outbox_error: Exception | None = None
                            try:
                                _persist_outbox(engine, usage_id, clean)
                            except Exception as exc:  # memory retry remains valid in this process
                                outbox_error = exc
                            if isinstance(outbox_error, _OutboxEvidenceError):
                                # A same-ID file that we cannot prove is ours is stronger evidence
                                # than this new callback. Never append over or erase that ambiguity.
                                append_error = outbox_error
                            else:
                                try:
                                    engine.store.append(EV_LLM_USAGE, _payload(usage_id, clean))
                                except Exception as exc:  # append may have committed before raise
                                    try:
                                        persisted = _event_usage_deltas(engine.store.read_all())
                                    except Exception:  # noqa: BLE001 - retain the pending retry
                                        persisted = {}
                                    if persisted.get(usage_id) == clean:
                                        _record(_binding, clean)
                                        _binding["pending"].pop(usage_id, None)
                                        if not _forget_outbox(engine, usage_id, clean):
                                            append_error = _OutboxEvidenceError(
                                                "usage event is durable but outbox acknowledgement "
                                                "conflicts")
                                    else:
                                        append_error = outbox_error or exc
                                else:
                                    try:
                                        persisted = _event_usage_deltas(
                                            engine.store.read_all())
                                    except Exception:  # noqa: BLE001 - fail closed on an unknown winner
                                        persisted = {}
                                    if persisted.get(usage_id) != clean:
                                        append_error = _OutboxEvidenceError(
                                            "usage event identity was won by conflicting telemetry")
                                    else:
                                        _record(_binding, clean)
                                        _binding["pending"].pop(usage_id, None)
                                        if not _forget_outbox(engine, usage_id, clean):
                                            append_error = _OutboxEvidenceError(
                                                "usage event is durable but outbox acknowledgement "
                                                "conflicts")
                    callback_error: Exception | None = None
                    if callable(_previous):
                        try:
                            _previous(dict(clean))
                        except Exception as exc:
                            callback_error = exc
                    if append_error is not None:
                        raise append_error
                    if callback_error is not None:
                        raise callback_error

                setattr(sink, "_looplab_cost_sink", True)
                _binding["sink"] = sink
                return sink

            if callable(binder):
                # CostAccountant gives us one exact counter/sink ownership boundary under its lock.
                boundary = sanitize_usage_delta(binder(make_sink))
            else:
                # Compatibility path for a third-party accountant. It lacks an atomic bind seam, so
                # only its quiescent construction/finalization boundary is supported.
                previous = getattr(accountant, "on_delta", None)
                boundary = _snapshot(accountant)
                setter(make_sink(previous))
            binding["durable_sink"] = True
            binding["baseline"] = _zero() if include_existing else boundary
            if include_existing and _has_value(boundary):
                _queue(binding, boundary)
    return found


def reconcile_cost_accountants(engine: object) -> bool:
    """Retry pending usage IDs and return whether every known delta is durable.

    Sink-capable accountants are never inferred from a live aggregate snapshot: ``add`` commits its
    counters immediately before invoking the sink, and comparing that transient state would race a
    delayed callback and double-charge. Legacy accountants without a sink remain quiescent-snapshot
    compatible at finalization.
    """
    try:
        bind_cost_accountants(engine, include_existing=True)
    except Exception:  # noqa: BLE001 - caller must not mark finalization complete
        return False
    bindings, lock = _tracker(engine)
    with lock:
        try:
            persisted = _event_usage_deltas(engine.store.read_all())
        except Exception:  # noqa: BLE001
            return False
        complete = _drain_outbox(engine, persisted)
        for binding in list(bindings.values()):
            if not binding["durable_sink"]:
                current = _snapshot(binding["accountant"])
                baseline = binding["baseline"]
                recorded = binding["recorded"]
                # A failed append leaves its exact delta queued for a same-ID retry.  The legacy
                # snapshot path must reserve those pending counters too: otherwise every reconcile
                # pass infers the same aggregate gap again and queues another logical charge before
                # the original retry has had a chance to become durable.
                pending_total = _zero()
                for pending_delta in binding["pending"].values():
                    pending_total["cost"] = min(
                        _MAX_COST,
                        float(pending_total["cost"]) + float(pending_delta["cost"]),
                    )
                    for key in _COUNTER_KEYS:
                        pending_total[key] = min(
                            _MAX_COUNTER,
                            int(pending_total[key]) + int(pending_delta[key]),
                        )
                missing: dict[str, int | float] = {
                    "cost": max(0.0, float(current["cost"]) - float(baseline["cost"])
                                - float(recorded["cost"]) - float(pending_total["cost"])),
                }
                for key in _COUNTER_KEYS:
                    missing[key] = max(
                        0, int(current[key]) - int(baseline[key]) - int(recorded[key])
                        - int(pending_total[key]))
                if float(missing["cost"]) < 1e-12:
                    missing["cost"] = 0.0
                clean = sanitize_usage_delta(missing)
                if _has_value(clean):
                    _queue(binding, clean)

            for usage_id, clean in list(binding["pending"].items()):
                if usage_id in persisted:
                    if persisted[usage_id] != clean:
                        complete = False
                        continue
                    _record(binding, clean)
                    binding["pending"].pop(usage_id, None)
                    if not _forget_outbox(engine, usage_id, clean):
                        complete = False
                    continue
                # `_drain_outbox` already made this ID's one retry for the current reconcile pass.
                # If its record remains, do not immediately append the same ID a second time; the
                # next reconcile gets the next retry opportunity.
                if _outbox_entry_present(engine, usage_id):
                    complete = False
                    continue
                try:
                    _persist_outbox(engine, usage_id, clean)
                except _OutboxEvidenceError:
                    complete = False
                    continue
                except Exception:
                    # Still try events.jsonl: a successful append is itself the durable boundary.
                    pass
                try:
                    engine.store.append(EV_LLM_USAGE, _payload(usage_id, clean))
                except Exception:  # append may have committed before surfacing an error
                    try:
                        now_persisted = _event_usage_deltas(engine.store.read_all())
                    except Exception:  # noqa: BLE001
                        now_persisted = {}
                    if now_persisted.get(usage_id) != clean:
                        complete = False
                        continue
                else:
                    try:
                        now_persisted = _event_usage_deltas(engine.store.read_all())
                    except Exception:  # noqa: BLE001
                        now_persisted = {}
                    if now_persisted.get(usage_id) != clean:
                        complete = False
                        continue
                _record(binding, clean)
                binding["pending"].pop(usage_id, None)
                persisted[usage_id] = clean
                if not _forget_outbox(engine, usage_id, clean):
                    complete = False
        return complete and not any(binding["pending"] for binding in bindings.values())


class _RunOutboxOwner:
    """Minimal owner for draining known usage before a destructive run operation."""

    def __init__(self, store: object):
        self.store = store


def reconcile_usage_outbox(store: object) -> bool:
    """Durably drain a run's prior-process usage without requiring a live Engine.

    Reset/delete callers use this as a fail-closed generation boundary: either every exact pending
    delta reaches the current events log before it is archived, or the destructive operation stops.
    """
    owner = _RunOutboxOwner(store)
    try:
        persisted = _event_usage_deltas(store.read_all())
    except Exception:  # noqa: BLE001 - an unreadable source log cannot accept safe recovery
        return False
    return _drain_outbox(owner, persisted)


def in_memory_cost_total(engine: object) -> dict[str, int | float] | None:
    """Compatibility fallback for fake/legacy engines that never installed the tracker."""
    accountants = find_cost_accountants(engine)
    if not accountants:
        return None
    total = _zero()
    for accountant in accountants:
        snap = _snapshot(accountant)
        total["cost"] = min(_MAX_COST, float(total["cost"]) + float(snap["cost"]))
        for key in _COUNTER_KEYS:
            total[key] = min(_MAX_COUNTER, int(total[key]) + int(snap[key]))
    return total if _has_value(total) else None


class _RunClientLedger:
    """Minimal Engine-shaped owner for one server-side client attributed to a run."""

    def __init__(self, client: object, store: object):
        self.researcher = client
        self.store = store


def bind_run_client_cost(client: object, store: object) -> object:
    """Attach one UI/server LLM client to a run's event store and return its flush handle."""
    ledger = _RunClientLedger(client, store)
    bind_cost_accountants(ledger)
    return ledger
