"""GPU inventory, footprint clamping, and lifecycle-scoped resource reservations.

The scheduler deals in *logical* GPU ids (the ordinals visible to the engine), while child
processes and Docker need the corresponding physical ids from ``CUDA_VISIBLE_DEVICES``.  Memory
inventory is deliberately optional: if the nvidia-smi inventory cannot be joined losslessly to the
visible logical set, admission falls back to count-only instead of guessing.
"""
from __future__ import annotations

import os
import threading
from typing import Optional

import anyio

from looplab.core.hardware import detect_gpus
from looplab.core.models import normalize_researcher_footprint


def detect_gpu_inventory(logical_ids: list[int]) -> tuple[dict[int, str], dict[int, int]]:
    """Return ``(logical -> physical, logical -> free MiB)`` for the visible GPU set.

    ``CUDA_VISIBLE_DEVICES=3,7`` makes the engine see logical devices ``0,1``; subprocess pinning
    must nevertheless write ``3`` or ``7`` back into a fresh child environment.  UUID tokens are
    valid physical selectors too, but the current nvidia-smi inventory is indexed numerically, so
    their memory join intentionally degrades to count-only.
    """
    ids = list(logical_ids or [])
    cvd = os.environ.get("CUDA_VISIBLE_DEVICES")
    if cvd is not None:
        tokens = [token.strip() for token in cvd.split(",") if token.strip()]
        # _detect_gpu_ids derives the same count.  A mismatch means one of the probes changed or the
        # environment was malformed; retain safe logical identity and do not invent a memory join.
        if len(tokens) != len(ids):
            return ({logical: str(logical) for logical in ids}, {})
        physical = {logical: tokens[pos] for pos, logical in enumerate(ids)}
    else:
        physical = {logical: str(logical) for logical in ids}

    try:
        rows = detect_gpus()
    except Exception:  # noqa: BLE001 -- capability detection is best-effort by contract
        return physical, {}
    by_physical: dict[int, int] = {}
    try:
        for row in rows:
            index = row.get("index")
            free = row.get("mem_free_mib")
            if type(index) is not int or type(free) is not int or free < 0 or index in by_physical:
                return physical, {}
            by_physical[index] = free
        joined: dict[int, int] = {}
        for logical in ids:
            token = physical[logical]
            if not token.isdigit() or int(token) not in by_physical:
                return physical, {}
            joined[logical] = by_physical[int(token)]
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return physical, {}
    # An empty/partial inventory is count-only.  Memory-aware fitting is enabled only when every
    # visible device has a trustworthy row, preventing a missing device from becoming a false 0 MiB.
    return (physical, joined) if len(joined) == len(ids) else (physical, {})


class ResourceSchedulingMixin:
    """Resource-manager methods inherited by :class:`looplab.engine.orchestrator.Engine`."""

    def _ensure_resource_state(self) -> None:
        """Backstop for focused tests that construct an Engine via ``__new__``."""
        if not hasattr(self, "_gpu_ids"):
            self._gpu_ids = []
        if not hasattr(self, "_gpu_physical_ids"):
            self._gpu_physical_ids = {gpu: str(gpu) for gpu in self._gpu_ids}
        if not hasattr(self, "_gpu_mem"):
            self._gpu_mem = {}
        if not hasattr(self, "_free_gpus"):
            self._free_gpus = list(self._gpu_ids)
        if not hasattr(self, "_gpu_lock"):
            self._gpu_lock = threading.Lock()
        if not hasattr(self, "_gpu_condition"):
            self._gpu_condition = threading.Condition(self._gpu_lock)
        if not hasattr(self, "_gpu_epoch"):
            self._gpu_epoch = 0
        if not hasattr(self, "_eval_gpu_reservations"):
            self._eval_gpu_reservations = {}

    def _memory_envelope(self, gpu_count: int) -> Optional[int]:
        """Largest per-GPU request for which ``gpu_count`` devices can fit."""
        mem = list(getattr(self, "_gpu_mem", {}).values())
        if not mem:
            return None
        n = max(1, min(int(gpu_count or 1), len(mem)))
        return sorted(mem, reverse=True)[n - 1]

    def _clamp_resource_footprint(self, footprint) -> dict | None:
        """Normalize and clamp quantitative footprint fields to the detected pool envelope.

        This is intentionally synchronous and does not reserve anything.  Developer-finalization and
        Card receipts use it to persist the *effective* quantities that admission will consume.
        When memory inventory is unavailable the requested memory is retained and admission degrades
        to count-only; silently fabricating a memory ceiling would be less honest than that fallback.
        """
        clean = normalize_researcher_footprint(footprint)
        if clean is None:
            return None
        total = len(getattr(self, "_gpu_ids", []) or [])
        out = dict(clean)
        if "gpus" in out:
            out["gpus"] = min(out["gpus"], total)
        if isinstance(out.get("gpu_mem_mib"), int):
            requested_gpus = out.get("gpus", 1)
            envelope = self._memory_envelope(requested_gpus)
            if envelope is not None:
                out["gpu_mem_mib"] = min(out["gpu_mem_mib"], envelope)
        return out

    def _resource_request_for_node(self, node) -> dict:
        """Translate a node footprint into the effective admission request.

        UNSPECIFIED preserves the historical split: a serial eval remains unpinned and can see the
        whole box; a parallel eval reserves one device.  Explicit ``gpus=0`` is a CPU request and
        bypasses the GPU queue.  Explicit positive counts are clamped, never allowed to wedge it.
        """
        raw = normalize_researcher_footprint(getattr(getattr(node, "idea", None), "footprint", None))
        effective = self._clamp_resource_footprint(raw)
        declared = raw is not None and "gpus" in raw
        cpu_only = bool(declared and raw.get("gpus") == 0)
        pool_size = len(getattr(self, "_gpu_ids", []) or [])
        parallel = max(1, int(getattr(self, "_eval_parallel",
                                      getattr(self, "max_parallel", 1)) or 1))
        if cpu_only:
            count = 0
        elif declared:
            count = min(int((effective or {}).get("gpus", 0)), pool_size)
        elif pool_size and parallel > 1:
            count = 1
        else:
            count = 0
        memory = (effective or {}).get("gpu_mem_mib")
        return {
            "count": count,
            "gpu_mem_mib": memory if isinstance(memory, int) else None,
            "cpu_only": cpu_only,
            "unspecified": not declared,
            "pin": bool(count > 0),
            "footprint": effective,
        }

    def _acquire_gpus(self, n: int, mem: Optional[int] = None) -> Optional[list[int]]:
        """Atomically reserve the first ``n`` fitting logical devices.

        ``None`` means a populated pool is currently too busy and the caller should wait/re-scan;
        ``[]`` is an immediate no-GPU reservation (CPU request or a machine with no detected GPUs).
        Counts and memory are clamped to the pool envelope so an over-declaration cannot deadlock.
        """
        self._ensure_resource_state()
        total = len(self._gpu_ids)
        try:
            count = max(0, int(n))
        except (TypeError, ValueError, OverflowError):
            count = 0
        count = min(count, total)
        if count == 0 or total == 0:
            return []
        try:
            requested_mem = max(0, int(mem)) if mem is not None else None
        except (TypeError, ValueError, OverflowError):
            requested_mem = None
        envelope = self._memory_envelope(count)
        if requested_mem is not None and envelope is not None:
            requested_mem = min(requested_mem, envelope)
        with self._gpu_condition:
            fitting = [gpu for gpu in self._free_gpus
                       if requested_mem is None or not self._gpu_mem
                       or self._gpu_mem.get(gpu, -1) >= requested_mem]
            if len(fitting) < count:
                return None
            chosen = fitting[:count]
            chosen_set = set(chosen)
            self._free_gpus[:] = [gpu for gpu in self._free_gpus if gpu not in chosen_set]
            self._gpu_epoch += 1
            return chosen

    def _release_gpus(self, gpu_ids) -> None:
        self._ensure_resource_state()
        released = [gpu for gpu in (gpu_ids or []) if gpu in self._gpu_ids]
        if not released:
            return
        with self._gpu_condition:
            for gpu in released:
                if gpu not in self._free_gpus:
                    self._free_gpus.append(gpu)
            order = {gpu: pos for pos, gpu in enumerate(self._gpu_ids)}
            self._free_gpus.sort(key=lambda gpu: order[gpu])
            self._gpu_epoch += 1
            self._gpu_condition.notify_all()

    # Back-compat for integrations/tests that exercised the old single-GPU primitive directly.  The
    # dispatcher itself uses the multi-GPU API and never relies on this non-blocking wrapper.
    def _acquire_gpu(self) -> Optional[int]:
        if max(1, int(getattr(self, "max_parallel", 1) or 1)) <= 1:
            return None
        got = self._acquire_gpus(1)
        return got[0] if got else None

    def _release_gpu(self, gpu_id: Optional[int]) -> None:
        self._release_gpus([] if gpu_id is None else [gpu_id])

    def _gpu_pool_epoch(self) -> int:
        self._ensure_resource_state()
        with self._gpu_condition:
            return self._gpu_epoch

    def _wait_for_gpu_change(self, expected: int) -> None:
        self._ensure_resource_state()
        with self._gpu_condition:
            if self._gpu_epoch == expected:
                # A finite wait lets an abandoned anyio worker exit promptly after cancellation.
                self._gpu_condition.wait(timeout=0.5)

    def _try_reserve_node_resources(self, node) -> Optional[dict]:
        request = self._resource_request_for_node(node)
        gpu_ids = self._acquire_gpus(request["count"], request["gpu_mem_mib"])
        # Legacy parallel nodes had no resource declaration: once every detected GPU was already
        # handed out, `_acquire_gpu` returned an unpinned eval instead of blocking.  Preserve that
        # branch byte-for-byte; only an explicit footprint participates in strict admission.
        if gpu_ids is None and request["unspecified"]:
            gpu_ids = []
            request["pin"] = False
        if gpu_ids is None:
            return None
        return {**request, "gpu_ids": gpu_ids}

    async def _wait_reserve_node_resources(self, node) -> dict:
        while True:
            epoch = self._gpu_pool_epoch()
            reservation = self._try_reserve_node_resources(node)
            if reservation is not None:
                return reservation
            await anyio.to_thread.run_sync(self._wait_for_gpu_change, epoch,
                                           abandon_on_cancel=True)

    def _register_eval_resource_reservation(self, node_id: int, generation: int,
                                            reservation: dict) -> None:
        self._ensure_resource_state()
        with self._gpu_condition:
            self._eval_gpu_reservations[(int(node_id), int(generation))] = dict(reservation)

    def _eval_resource_reservation(self, node_id: int, generation: int) -> Optional[dict]:
        self._ensure_resource_state()
        with self._gpu_condition:
            value = self._eval_gpu_reservations.get((int(node_id), int(generation)))
            return dict(value) if value is not None else None

    def _clear_eval_resource_reservation(self, node_id: int, generation: int) -> None:
        self._ensure_resource_state()
        with self._gpu_condition:
            self._eval_gpu_reservations.pop((int(node_id), int(generation)), None)

    def _physical_gpu_ids(self, logical_ids) -> list[str]:
        mapping = getattr(self, "_gpu_physical_ids", {})
        return [str(mapping.get(gpu, gpu)) for gpu in (logical_ids or [])]

    def _resource_eval_env(self, reservation: Optional[dict], *, base: Optional[dict] = None,
                           inherit_host: bool = False) -> Optional[dict]:
        """Build the child env for a reservation without changing the unpinned legacy branch."""
        if not reservation or (not reservation.get("cpu_only") and not reservation.get("gpu_ids")):
            return dict(base) if base is not None else None
        env = ({**os.environ, **(base or {})} if inherit_host else dict(base or {}))
        env["CUDA_VISIBLE_DEVICES"] = (
            "" if reservation.get("cpu_only")
            else ",".join(self._physical_gpu_ids(reservation.get("gpu_ids"))))
        return env
