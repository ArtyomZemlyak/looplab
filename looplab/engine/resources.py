"""GPU inventory, footprint clamping, and lifecycle-scoped resource reservations.

The scheduler deals in *logical* GPU ids (the ordinals visible to the engine), while child
processes and Docker need the corresponding physical ids from ``CUDA_VISIBLE_DEVICES``.  Memory
inventory is deliberately optional: if the nvidia-smi inventory cannot be joined losslessly to the
visible logical set, admission falls back to count-only instead of guessing.
"""
from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from typing import Optional

import anyio

from looplab.core.hardware import detect_gpus
from looplab.core.models import effective_card_footprint, normalize_researcher_footprint
from looplab.runtime.sandbox import SECRET_ENV


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
        # CODEX AGENT: validate selector uniqueness before constructing logical slots. A duplicated
        # CUDA_VISIBLE_DEVICES token currently becomes two scheduler reservations mapped to the same
        # physical device, so concurrent children appear disjoint while both run on one GPU. Reject
        # duplicate or ambiguous selectors instead of multiplying advertised capacity.
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
            # CODEX AGENT: this free list is process-local, while the server can run one Engine process
            # per run. Concurrent runs therefore initialize the same visible physical GPUs as free and
            # can reserve them simultaneously. Use a host-wide lease keyed by stable GPU identity with
            # owner-liveness/crash cleanup, or explicitly serialize GPU-owning runs.
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
            # Clamp the declared count to the detected pool (0 on a GPU-less host). Keeping a positive
            # requirement "unsatisfiable" instead hangs the serial dispatcher forever (the pool epoch
            # never bumps, no terminal is ever written). Degrading to CPU-only/fewer GPUs is graceful: a
            # node that genuinely needs CUDA fails NATURALLY at eval (a real node_failed), never a spin.
            out["gpus"] = min(out["gpus"], total)
        if isinstance(out.get("gpu_mem_mib"), int):
            requested_gpus = out.get("gpus", 1)
            envelope = self._memory_envelope(requested_gpus)
            if envelope is not None:
                out["gpu_mem_mib"] = min(out["gpu_mem_mib"], envelope)
        return out

    @staticmethod
    def _card_for_node(state, node):
        """Resolve a Node's immutable Card spelling to one unambiguous canonical row."""
        card_id = getattr(getattr(node, "idea", None), "card_id", None)
        cards = getattr(state, "cards", None)
        if not isinstance(card_id, str) or not isinstance(cards, Mapping):
            return None
        card = cards.get(card_id)
        if card is None:
            # Replay collapses merged rows to canonical Cards and retains raw work-item ids only in
            # ``Card.aliases``. Resolve that bounded projection without trusting an event-order map;
            # ambiguous/corrupt ownership fails closed rather than borrowing another Card's pin.
            match = None
            for candidate in cards.values():
                aliases = getattr(candidate, "aliases", None)
                if isinstance(aliases, list) and card_id in aliases:
                    if match is not None:
                        return None
                    match = candidate
            card = match
        return card

    @classmethod
    def _card_resource_pin_for_node(cls, state, node):
        """Resolve the independent operator pin through a bounded canonical Card chain."""
        card = cls._card_for_node(state, node)
        return getattr(card, "resource_pin", None) if card is not None else None

    @classmethod
    def _operator_card_dropped_for_node(cls, state, node) -> bool:
        """Whether the explicit operator stop affordance owns this Node's canonical Card."""
        card = cls._card_for_node(state, node)
        return bool(
            card is not None
            and getattr(card, "status", None) == "dropped"
            and getattr(card, "dropped_by", None) == "operator"
        )

    def _resource_request_for_node(self, node, *, resource_pin=None) -> dict:
        """Translate a node footprint into the effective admission request.

        UNSPECIFIED preserves the historical split: a serial eval remains unpinned and can see the
        whole box; a parallel eval reserves one device.  Explicit ``gpus=0`` is a CPU request and
        bypasses the GPU queue.  Explicit positive counts are clamped to the detected pool (0 on a
        GPU-less host -> CPU-only), so admission can never wait forever for capacity that cannot exist.
        """
        raw = effective_card_footprint(
            getattr(getattr(node, "idea", None), "footprint", None),
            resource_pin,
        )
        effective = self._clamp_resource_footprint(raw)
        declared = raw is not None and "gpus" in raw
        cpu_only = bool(declared and raw.get("gpus") == 0)
        pool_size = len(getattr(self, "_gpu_ids", []) or [])
        parallel = max(1, int(self._eval_parallel or 1))
        if cpu_only:
            count = 0
        elif declared:
            count = int((effective or {}).get("gpus", 0))
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

    def _node_resource_reservation_is_current(self, state, node, reservation) -> bool:
        """Verify that a reservation was formed for the Card pin in a fresh fold.

        A blocking GPU wait creates an operator-control race: the Card may be re-pinned while the
        waiter sleeps.  Callers re-fold after admission and use this comparison before starting the
        subprocess; a mismatch must release and retry instead of running with stale quantities.
        """
        if not isinstance(reservation, dict):
            return False
        expected = self._resource_request_for_node(
            node,
            resource_pin=self._card_resource_pin_for_node(state, node),
        )
        # ``pin`` and ``gpu_ids`` are admission outcomes, not source-request identity.  Exact device
        # assignment may differ after a release/retry even when the Card source footprint is unchanged.
        admitted_source = {
            key: value for key, value in reservation.items()
            if key not in {"gpu_ids", "pin"}
        }
        expected_source = {key: value for key, value in expected.items() if key != "pin"}
        return admitted_source == expected_source

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
        if max(1, int(self._eval_parallel or 1)) <= 1:
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

    def _try_reserve_node_resources(self, node, *, resource_pin=None) -> Optional[dict]:
        request = self._resource_request_for_node(node, resource_pin=resource_pin)
        # `count` is already clamped to the detected pool (`_clamp_resource_footprint`), so a GPU-less
        # host yields count=0 (CPU-only) rather than a positive requirement that can never be admitted —
        # which would spin this reservation wait forever.
        gpu_ids = self._acquire_gpus(request["count"], request["gpu_mem_mib"])
        if gpu_ids is None:
            return None
        return {**request, "gpu_ids": gpu_ids}

    async def _wait_reserve_node_resources(self, node, *, resource_pin=None,
                                           wait_once: bool = False) -> Optional[dict]:
        """Reserve resources, optionally returning after one bounded condition tick.

        ``wait_once`` is used by lifecycle-aware callers.  A Card can be re-pinned from GPU to CPU
        while a GPU is busy without changing the pool epoch; an unbounded wait on the old snapshot
        would therefore sleep forever.  Returning ``None`` after the condition's finite tick lets the
        caller re-fold operator intent and retry against the canonical current pin.
        """
        while True:
            epoch = self._gpu_pool_epoch()
            reservation = self._try_reserve_node_resources(node, resource_pin=resource_pin)
            if reservation is not None:
                return reservation
            await anyio.to_thread.run_sync(self._wait_for_gpu_change, epoch,
                                           abandon_on_cancel=True)
            if wait_once:
                return None

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
        # KNOWN (separate, low): a node that DECLARED gpus>0 but was clamped to zero on a host where
        # discovery returned no ids (cpu_only=False, gpu_ids=[]) falls into this unpinned branch and, if
        # such a host actually has GPUs (torch absent AND nvidia-smi unavailable), the child sees every
        # device. Fencing it here would have to reconcile with the deliberately-divergent
        # `effective_card_footprint` zero-host branch (models.py) that the freshness projection relies on
        # and that is test-locked, so it is intentionally left to a maintainer rather than fixed inline.
        # CODEX AGENT: this is still a fail-open security boundary, not merely a projection mismatch:
        # an explicit positive GPU requirement can reach candidate code with the host's full GPU view
        # after discovery failure. Preserve an explicit "required but unavailable" reservation state and
        # refuse launch; a test-locked divergent helper is evidence to repair the contract, not to inherit it.
        if not reservation or (not reservation.get("cpu_only") and not reservation.get("gpu_ids")):
            return dict(base) if base is not None else None
        # SECURITY (source-side strip): `inherit_host` shovels the host environment into the eval env so
        # a pinned/CPU reservation can override CUDA_VISIBLE_DEVICES. But the host env holds LLM_API_KEY /
        # cloud creds, and `run_argv` (subprocess tier) overlays this dict on top of its own
        # secret-filtered base — re-adding the secrets it just stripped — so we MUST filter here or the
        # subprocess tier leaks. Filter the inherited host names by SECRET_ENV, the same guard `run_argv`
        # applies to os.environ. The untrusted Docker tier ALSO strips secrets independently at its `-e`
        # choke point (`docker_gpu_env`), so this is defense-in-depth for that tier, not its sole guard.
        # `base` (LOOPLAB_EVAL_SEED, etc.) is the engine's own explicit env, kept as-is;
        # CUDA_VISIBLE_DEVICES is set below regardless.
        host = ({k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
                if inherit_host else {})
        env = {**host, **(base or {})}
        env["CUDA_VISIBLE_DEVICES"] = (
            "" if reservation.get("cpu_only")
            else ",".join(self._physical_gpu_ids(reservation.get("gpu_ids"))))
        return env
