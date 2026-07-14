"""Owner-plane attention feed: bounded, observation-only, and redacted."""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Query

from looplab.events.eventstore import log_divergence
from looplab.serve.attention import (
    project_event_attention, project_runtime_attention, visible_event_attention)
from looplab.serve.engine_proc import _engine_liveness


_MAX_CACHE_ENTRIES = 8192  # exceeds the documented 5k-run operating target without unbounded growth


def build_router(srv) -> APIRouter:
    router = APIRouter()
    # run_id -> (reset-safe file signature, event projection). The live process probe is deliberately
    # not cached: engine liveness can change without appending an event.
    cache: dict[str, tuple[tuple, dict, bool]] = {}

    @router.get("/api/attention")
    def attention(
        limit: int = Query(default=100, ge=1, le=200),
        cursor: str | None = Query(default=None, pattern=r"^[0-9a-f]{64}$"),
    ):
        items: list[dict] = []
        present: set[str] = set()
        partial = False
        root = srv.root
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
            for item in visible_event_attention(cached[1], engine_running=verified_liveness):
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
                signature = (stat.st_dev, stat.st_ino, stat.st_ctime_ns,
                             stat.st_size, stat.st_mtime_ns)
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
                        before_sig = (before.st_dev, before.st_ino, before.st_ctime_ns,
                                      before.st_size, before.st_mtime_ns)
                        after_sig = (after.st_dev, after.st_ino, after.st_ctime_ns,
                                     after.st_size, after.st_mtime_ns)
                        if divergence is not None:
                            # `iter_jsonl` deliberately returns the valid prefix of a damaged log.
                            # That is useful for forensic reads, but an owner alert inferred from a
                            # truncated fold could be actively wrong, so omit this run and surface a
                            # partial-feed warning instead of caching the prefix as authoritative.
                            partial = True
                            break
                        if before_sig == after_sig:
                            signature, projection = after_sig, candidate
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
                    cache[rd.name] = (signature, projection, True)
                items.extend(visible_event_attention(projection, engine_running=alive))
                # Liveness is a derived in-app warning only. This probe never runs resume reconciliation
                # and never writes events/commands or starts a process.
                items.extend(project_runtime_attention(
                    rd.name, projection, engine_running=alive))
            except Exception:  # noqa: BLE001 - one partial/corrupt/unreadable run cannot hide the rest
                partial = True
                append_stale(rd.name)
                continue
        for stale in set(cache) - present:
            cache.pop(stale, None)
        items.sort(key=lambda item: (
            bool(item.get("active")), float(item.get("created") or 0), int(item.get("seq") or -1),
            str(item.get("id") or "")), reverse=True)
        start = 0
        if cursor is not None:
            position = next((index for index, item in enumerate(items)
                             if item.get("id") == cursor), None)
            if position is None:
                raise HTTPException(409, "attention cursor is stale; reload the first page")
            start = position + 1
        remaining = items[start:]
        page = remaining[:limit]
        truncated = len(remaining) > limit
        next_cursor = page[-1]["id"] if truncated and page else None
        return {"schema": 1, "generated_at": time.time(), "items": page,
                "truncated": truncated, "next_cursor": next_cursor, "partial": partial}

    return router
