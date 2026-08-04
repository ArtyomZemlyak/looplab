"""Owner-plane attention feed: bounded, observation-only, and redacted."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import secrets
import threading
import time

from fastapi import APIRouter, HTTPException, Query

from looplab.core.atomicio import file_identity
from looplab.events.eventstore import log_divergence
from looplab.serve.attention import (
    ATTENTION_NEEDS_ACTION_KINDS, project_event_attention, project_runtime_attention,
    visible_event_attention)
from looplab.serve.engine_proc import _engine_liveness


_MAX_CACHE_ENTRIES = 8192  # exceeds the documented 5k-run operating target without unbounded growth
# Refresh ahead of the owner's 8-second poll without calling an ordinary SWR refresh an outage. Only
# the hard age (or a recorded refresh failure) marks the served last-safe snapshot stale.
_SNAPSHOT_SOFT_REFRESH_SECONDS = 6.0
_SNAPSHOT_HARD_STALE_SECONDS = 20.0
_REFRESH_FAILURE_COOLDOWN_SECONDS = 8.0
_CURSOR_TTL_SECONDS = 300.0
_MAX_CURSOR_ENTRIES = 4096
_MAX_CURSOR_SNAPSHOTS = 8
_SEVERITY_PRIORITY = {"danger": 4, "action": 3, "warning": 2, "success": 1}


@dataclass(frozen=True)
class _AttentionSnapshot:
    snapshot_id: str
    cursor_scope: str
    generated_at: float
    completed_at: float
    items: tuple[dict, ...]
    active_action_count: int
    partial: bool


@dataclass(frozen=True)
class _CursorRecord:
    snapshot_scope: str
    offset: int
    expires_at: float


def build_router(srv) -> APIRouter:
    router = APIRouter()
    # run_id -> (reset-safe file signature, event projection). The live process probe is deliberately
    # not cached: engine liveness can change without appending an event.
    cache: dict[str, tuple[tuple, dict, bool]] = {}
    # This route is a sync `def` → FastAPI runs concurrent polls on threadpool threads. Guard every
    # mutation/iteration of the shared cache: an unlocked `set(cache)` racing a concurrent insert
    # raises "dictionary changed size during iteration" → 500 (the sibling trace_view cache locks for
    # exactly this). Reads via `.get()` stay lock-free (GIL-atomic; a slightly stale entry is fine).
    cache_lock = threading.Lock()
    snapshot_lock = threading.Lock()
    current_snapshot: _AttentionSnapshot | None = None
    refresh_running = False
    refresh_failed = False
    refresh_not_before = 0.0
    cursor_records: OrderedDict[str, _CursorRecord] = OrderedDict()
    cursor_snapshots: OrderedDict[str, _AttentionSnapshot] = OrderedDict()
    cursor_counts: dict[str, int] = {}

    def scan_items() -> tuple[tuple[dict, ...], bool]:
        items: list[dict] = []
        present: set[str] = set()
        partial = False
        root = srv.root
        try:
            labels = (srv.projects.load().get("labels") or {})
        except Exception:  # noqa: BLE001 — labels are optional display metadata
            labels = {}

        def contextualized(rows, projection, run_id):
            def _bounded(value):
                text = str(value or "")
                return (text if len(text) <= 255
                        and not any(ord(char) < 32 or ord(char) == 127 for char in text) else "")
            task_id = _bounded((projection or {}).get("task_id"))
            run_label = _bounded(labels.get(run_id))
            return [{**item, **({"task_id": task_id} if task_id else {}),
                     **({"run_label": run_label} if run_label else {})} for item in rows]
        try:
            if not root.is_dir():
                raise OSError("run root is not a readable directory")
            run_dirs = sorted(root.iterdir())
        except OSError as exc:
            # Returning an authoritative empty inbox would erase the client's last safe snapshot
            # during a transient unmount. Let Promise.allSettled preserve it and mark this source stale.
            raise HTTPException(503, "run root is temporarily unavailable") from exc

        def append_stale(run_id: str) -> None:
            cached = cache.get(run_id)
            if cached is None:
                return
            # Keep the last verified event-derived signal visible, but never notify or synthesize
            # liveness from a projection whose newer log could not be trusted.
            # A terminal card is safe to retain only if this cache already observed an exact engine
            # release. It remains explicitly stale/non-browser because a corrupt newer tail may hide
            # a subsequent resume; an unverified terminal projection still stays suppressed.
            verified_liveness = False if cached[2] else None
            for item in contextualized(
                    visible_event_attention(cached[1], engine_running=verified_liveness),
                    cached[1], run_id):
                items.append({**item, "browser": False, "stale": True})

        for rd in run_dirs:
            try:
                log = rd / "events.jsonl"
                if not rd.is_dir() or not log.is_file():
                    if rd.name in cache:
                        # A reset/replace can briefly leave the directory visible without its new
                        # log. Preserve the prior verified projection as stale; only disappearance of
                        # the directory entry itself authoritatively retires the cached run below.
                        present.add(rd.name)
                        partial = True
                        append_stale(rd.name)
                    continue
                present.add(rd.name)
                # Match the command plane's direct-child/symlink boundary even though this route is
                # read-only: a filesystem alias must not make the owner inbox inspect material outside
                # the configured run root.
                validator = getattr(getattr(srv, "commands", None), "validate_paths", None)
                if callable(validator):
                    rd = validator(rd)
                    log = rd / "events.jsonl"
                stat = log.stat()
                signature = file_identity(stat)
                cached = cache.get(rd.name)
                if cached is not None and cached[0] == signature:
                    projection = cached[1]
                    terminal_released = cached[2]
                else:
                    # A concurrent append is safe as a prefix read, but do not cache it under the old
                    # signature. Retry once; a continuously moving/corrupt run is omitted and marked
                    # partial rather than blocking the rest of the owner inbox.
                    projection = None
                    terminal_released = False
                    for _attempt in range(2):
                        before = log.stat()
                        candidate = project_event_attention(rd.name, srv.events(rd))
                        divergence = log_divergence(log)
                        after = log.stat()
                        before_sig = file_identity(before)
                        after_sig = file_identity(after)
                        if divergence is not None:
                            # `iter_jsonl` deliberately returns the valid prefix of a damaged log.
                            # That is useful for forensic reads, but an owner alert inferred from a
                            # truncated fold could be actively wrong, so omit this run and surface a
                            # partial-feed warning instead of caching the prefix as authoritative.
                            partial = True
                            break
                        if before_sig == after_sig:
                            signature, projection = after_sig, candidate
                            with cache_lock:
                                if rd.name in cache or len(cache) < _MAX_CACHE_ENTRIES:
                                    cache[rd.name] = (signature, projection, False)
                            break
                    if projection is None:
                        partial = True
                        append_stale(rd.name)
                        continue
                # Once a fully-finalized terminal run is definitively released, any reopen/resume
                # must append and change the signature. Avoid thousands of redundant OS lock probes
                # across unchanged historical runs while preserving live checks everywhere else.
                alive = False if terminal_released else _engine_liveness(rd)
                flags = projection.get("runtime") or {}
                if (alive is False and flags.get("finished")
                        and not flags.get("finalization_pending") and rd.name in cache):
                    with cache_lock:
                        cache[rd.name] = (signature, projection, True)
                items.extend(contextualized(
                    visible_event_attention(projection, engine_running=alive), projection, rd.name))
                # Liveness is a derived in-app warning only. This probe never runs resume reconciliation
                # and never writes events/commands or starts a process.
                items.extend(contextualized(project_runtime_attention(
                    rd.name, projection, engine_running=alive), projection, rd.name))
            except Exception:  # noqa: BLE001 - one partial/corrupt/unreadable run cannot hide the rest
                partial = True
                append_stale(rd.name)
                continue
        with cache_lock:
            for stale in set(cache) - present:
                cache.pop(stale, None)

        def _seq_key(item) -> int:
            # A genuine seq==0 must not collapse to -1 (`0 or -1 == -1`), which would mis-order an
            # item anchored at the run's first event relative to its timestamp peers.
            seq = item.get("seq")
            return seq if isinstance(seq, int) and not isinstance(seq, bool) else -1

        items.sort(key=lambda item: (
            str(item.get("kind") or "") in ATTENTION_NEEDS_ACTION_KINDS,
            bool(item.get("active")),
            _SEVERITY_PRIORITY.get(str(item.get("severity") or ""), 0),
            float(item.get("created") or 0), _seq_key(item),
            str(item.get("id") or "")), reverse=True)
        return tuple(items), bool(partial)

    def build_snapshot() -> _AttentionSnapshot:
        items, partial = scan_items()
        active_action_count = sum(
            1 for item in items
            if item.get("active") is True
            and str(item.get("kind") or "") in ATTENTION_NEEDS_ACTION_KINDS
        )
        normalized = json.dumps(
            {
                "items": items,
                "partial": partial,
                "active_action_count": active_action_count,
            },
            sort_keys=True, separators=(",", ":"),
            ensure_ascii=True, allow_nan=False,
        ).encode("ascii")
        return _AttentionSnapshot(
            snapshot_id=hashlib.sha256(normalized).hexdigest(),
            cursor_scope=secrets.token_hex(32),
            generated_at=time.time(),
            completed_at=time.monotonic(),
            items=items,
            active_action_count=active_action_count,
            partial=partial,
        )

    def drop_cursor_locked(token: str) -> None:
        record = cursor_records.pop(token, None)
        if record is None:
            return
        remaining = cursor_counts.get(record.snapshot_scope, 0) - 1
        if remaining > 0:
            cursor_counts[record.snapshot_scope] = remaining
        else:
            cursor_counts.pop(record.snapshot_scope, None)
            cursor_snapshots.pop(record.snapshot_scope, None)

    def expire_cursors_locked(now: float) -> None:
        while cursor_records:
            token, record = next(iter(cursor_records.items()))
            if record.expires_at > now:
                break
            drop_cursor_locked(token)

    def register_cursor_locked(snapshot: _AttentionSnapshot, offset: int, now: float) -> str:
        scope = snapshot.cursor_scope
        if scope not in cursor_snapshots:
            cursor_snapshots[scope] = snapshot
            cursor_counts[scope] = 0
        while len(cursor_snapshots) > _MAX_CURSOR_SNAPSHOTS:
            stale_scope = next(iter(cursor_snapshots))
            for token, record in list(cursor_records.items()):
                if record.snapshot_scope == stale_scope:
                    drop_cursor_locked(token)
            cursor_snapshots.pop(stale_scope, None)
            cursor_counts.pop(stale_scope, None)
        token = hashlib.sha256(f"{scope}:{offset}".encode("ascii")).hexdigest()
        if token in cursor_records:
            return token
        while len(cursor_records) >= _MAX_CURSOR_ENTRIES:
            drop_cursor_locked(next(iter(cursor_records)))
        cursor_records[token] = _CursorRecord(
            snapshot_scope=scope, offset=offset, expires_at=now + _CURSOR_TTL_SECONDS)
        cursor_counts[scope] = cursor_counts.get(scope, 0) + 1
        return token

    def snapshot_page(snapshot: _AttentionSnapshot, start: int, limit: int, *, stale: bool) -> dict:
        end = min(start + limit, len(snapshot.items))
        truncated = end < len(snapshot.items)
        next_cursor = None
        if truncated:
            now = time.monotonic()
            with snapshot_lock:
                expire_cursors_locked(now)
                next_cursor = register_cursor_locked(snapshot, end, now)
        return {
            "schema": 1,
            "snapshot_id": snapshot.snapshot_id,
            "generated_at": snapshot.generated_at,
            "stale": bool(stale),
            "active_action_count": snapshot.active_action_count,
            "items": list(snapshot.items[start:end]),
            "truncated": truncated,
            "next_cursor": next_cursor,
            "partial": snapshot.partial,
        }

    def refresh_in_background() -> None:
        nonlocal current_snapshot, refresh_failed, refresh_not_before, refresh_running
        try:
            built = build_snapshot()
        except Exception:  # noqa: BLE001 - retain the last complete snapshot and cool down retries
            with snapshot_lock:
                refresh_failed = True
                refresh_not_before = time.monotonic() + _REFRESH_FAILURE_COOLDOWN_SECONDS
                refresh_running = False
            return
        with snapshot_lock:
            current_snapshot = built
            refresh_failed = False
            refresh_not_before = 0.0
            refresh_running = False

    def start_background_refresh() -> None:
        nonlocal refresh_failed, refresh_not_before, refresh_running
        try:
            threading.Thread(
                target=refresh_in_background, name="looplab-attention-refresh", daemon=True,
            ).start()
        except RuntimeError:
            with snapshot_lock:
                refresh_failed = True
                refresh_not_before = time.monotonic() + _REFRESH_FAILURE_COOLDOWN_SECONDS
                refresh_running = False

    @router.get("/api/attention")
    def attention(
        limit: int = Query(default=100, ge=1, le=200),
        cursor: str | None = Query(default=None, pattern=r"^[0-9a-f]{64}$"),
    ):
        nonlocal current_snapshot, refresh_failed, refresh_not_before, refresh_running
        now = time.monotonic()
        if cursor is not None:
            with snapshot_lock:
                expire_cursors_locked(now)
                record = cursor_records.get(cursor)
                snapshot = (cursor_snapshots.get(record.snapshot_scope)
                            if record is not None else None)
                latest = current_snapshot
                failed = refresh_failed
            if record is None or snapshot is None:
                raise HTTPException(409, "attention cursor is stale; reload the first page")
            # A routine soft refresh does not make an immutable page stale. A content mismatch, the
            # matching latest snapshot's hard age, or an actual refresh failure does. Use the latest
            # completion time when content ids match so an unchanged successful refresh renews the
            # cursor's freshness instead of flashing a false stale state halfway through pagination.
            stale = (latest is None or snapshot.snapshot_id != latest.snapshot_id
                     or now - latest.completed_at >= _SNAPSHOT_HARD_STALE_SECONDS or failed)
            return snapshot_page(snapshot, record.offset, limit, stale=stale)

        launch_refresh = False
        cold_builder = False
        with snapshot_lock:
            expire_cursors_locked(now)
            snapshot = current_snapshot
            age = now - snapshot.completed_at if snapshot is not None else 0.0
            stale = snapshot is not None and (
                age >= _SNAPSHOT_HARD_STALE_SECONDS or refresh_failed)
            if snapshot is None:
                if refresh_running or now < refresh_not_before:
                    wait = max(1, int(refresh_not_before - now + 0.999))
                    raise HTTPException(
                        503, "attention snapshot is being prepared",
                        headers={"Retry-After": str(wait)},
                    )
                refresh_running = True
                cold_builder = True
            elif ((age >= _SNAPSHOT_SOFT_REFRESH_SECONDS or refresh_failed)
                  and not refresh_running and now >= refresh_not_before):
                refresh_running = True
                launch_refresh = True

        if snapshot is not None:
            if launch_refresh:
                start_background_refresh()
            return snapshot_page(snapshot, 0, limit, stale=stale)

        if not cold_builder:  # defensive: the state machine above always chooses one path
            raise HTTPException(503, "attention snapshot is temporarily unavailable",
                                headers={"Retry-After": "1"})
        try:
            snapshot = build_snapshot()
        except Exception as exc:  # noqa: BLE001 - cold scans fail closed; no empty authoritative inbox
            with snapshot_lock:
                refresh_failed = True
                refresh_not_before = time.monotonic() + _REFRESH_FAILURE_COOLDOWN_SECONDS
                refresh_running = False
            raise HTTPException(
                503, "attention snapshot is temporarily unavailable",
                headers={"Retry-After": str(int(_REFRESH_FAILURE_COOLDOWN_SECONDS))},
            ) from exc
        with snapshot_lock:
            current_snapshot = snapshot
            refresh_failed = False
            refresh_not_before = 0.0
            refresh_running = False
        return snapshot_page(snapshot, 0, limit, stale=False)

    return router
