"""GPU inventory, footprint clamping, and lifecycle-scoped resource reservations.

The scheduler deals in *logical* GPU ids (the ordinals visible to the engine), while child
processes and Docker need the corresponding physical ids from ``CUDA_VISIBLE_DEVICES``.  Memory
inventory is deliberately optional: if the nvidia-smi inventory cannot be joined losslessly to the
visible logical set, admission falls back to count-only instead of guessing.
"""
from __future__ import annotations

import errno
import os
import stat
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO, Optional

import anyio

from looplab.core.hardware import detect_gpus
from looplab.core.models import effective_card_footprint, normalize_researcher_footprint
from looplab.runtime.sandbox import GpuPinUnenforceable, SECRET_ENV


_CUDA_DISABLED_SELECTORS = frozenset({"-1", "none", "nodevfiles", "void"})


def default_gpu_host_lease_path() -> Path:
    """Return the per-OS-user lease that serializes local Engine GPU pools.

    A single lease is deliberately more conservative than one file per selector.  Two processes can
    name the same device by ordinal, GPU UUID, or MIG UUID; one pool-wide file cannot accidentally
    treat those aliases as distinct hardware.  Container/OS-user boundaries still require their own
    external scheduler because they need not share this filesystem namespace.
    """
    suffix = str(os.getuid()) if hasattr(os, "getuid") else "user"
    return Path(tempfile.gettempdir()) / f"looplab-gpu-pool-{suffix}.lock"


def _try_acquire_gpu_host_lease(path: Path) -> Optional[BinaryIO]:
    """Try to lock ``path`` without blocking; return its live descriptor or ``None`` on contention.

    The descriptor is the ownership token: a normal release closes it explicitly and an abrupt process
    exit lets the OS release it.  Unsupported or untrustworthy locking fails closed because silently
    falling back to the process-local free list would reintroduce GPU over-allocation.
    """
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise GpuPinUnenforceable(
            f"host GPU allocation lease cannot be opened: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        entry = path.lstat()
        if (not stat.S_ISREG(opened.st_mode) or stat.S_ISLNK(entry.st_mode)
                or (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino)):
            raise GpuPinUnenforceable(
                "host GPU allocation lease is not a stable regular file")
        if opened.st_size == 0:
            os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        handle = os.fdopen(descriptor, "r+b", buffering=0)
        descriptor = -1
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            contention = (
                isinstance(exc, BlockingIOError)
                or exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
                or getattr(exc, "winerror", None) in {33, 36, 158}
            )
            if contention:
                handle.close()
                return None
            raise GpuPinUnenforceable(
                f"host GPU allocation lease is unsupported: {exc}") from exc
        except (ImportError, AttributeError, NotImplementedError, ValueError) as exc:
            raise GpuPinUnenforceable(
                f"host GPU allocation lease is unsupported: {exc}") from exc
        return handle
    except BaseException:
        try:
            if "handle" in locals():
                handle.close()
            elif descriptor >= 0:
                os.close(descriptor)
        except OSError:
            pass
        raise


def _release_gpu_host_lease(handle: BinaryIO) -> None:
    """Release a live host lease; closing the descriptor is the authoritative crash-safe backstop."""
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, ImportError, AttributeError, NotImplementedError, ValueError):
        pass
    finally:
        try:
            handle.close()
        except OSError:
            pass


def cuda_visible_device_tokens(value: object) -> Optional[list[str]]:
    """Validate a ``CUDA_VISIBLE_DEVICES`` value without guessing device identity.

    ``None`` means the variable is absent and callers may use another inventory probe. An empty,
    disabled, malformed, or duplicate selector list means no schedulable devices. Numeric aliases are
    canonicalized for duplicate detection (``03`` and ``3`` name the same ordinal); UUID/MIG tokens are
    compared case-insensitively. Unknown non-empty token syntax remains CUDA's responsibility.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return []
    raw = value.split(",")
    tokens = [token.strip() for token in raw]
    if not tokens or any(not token for token in tokens):
        return []
    lowered = [token.casefold() for token in tokens]
    if any(token in _CUDA_DISABLED_SELECTORS for token in lowered):
        # A singleton disabled token is a normal CPU fence; mixing one with devices is malformed. Both
        # expose zero schedulable devices and must not be filtered into a positive-capacity list.
        return []
    seen: set[tuple[str, object]] = set()
    for token, folded in zip(tokens, lowered):
        identity: tuple[str, object] = (
            ("ordinal", int(token, 10)) if token.isdecimal() else ("token", folded))
        if identity in seen:
            return []
        seen.add(identity)
    return tokens


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
        tokens = cuda_visible_device_tokens(cvd) or []
        # _detect_gpu_ids derives the same count.  A mismatch means one of the probes changed or the
        # environment was malformed; an empty mapping makes any independently forged reservation fail
        # closed instead of escaping the operator's visibility fence through logical-id fallback.
        if len(tokens) != len(ids):
            return ({}, {})
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
        if not hasattr(self, "_gpu_host_lease_path"):
            # Focused mixin users stay process-local unless they opt in. Engine always installs the
            # default path when it detects a GPU pool.
            self._gpu_host_lease_path = None
        if not hasattr(self, "_gpu_host_lease_handle"):
            self._gpu_host_lease_handle = None
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
        if "gpus" in out and (total > 0 or out["gpus"] == 0):
            # Clamp over-declaration to a NON-EMPTY pool, but preserve a positive requirement when
            # discovery found no devices. The request layer below turns that state into an immediate
            # fail-closed reservation, so it neither spins forever nor silently runs as CPU/whole-box work.
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
        GPU-less host -> ``required_unavailable``), so admission can fail closed without waiting forever
        for capacity that cannot exist or silently changing an explicit positive requirement into CPU work.
        """
        raw = effective_card_footprint(
            getattr(getattr(node, "idea", None), "footprint", None),
            resource_pin,
        )
        effective = self._clamp_resource_footprint(raw)
        declared = raw is not None and "gpus" in raw
        cpu_only = bool(declared and raw.get("gpus") == 0)
        pool_size = len(getattr(self, "_gpu_ids", []) or [])
        required_unavailable = bool(declared and raw.get("gpus", 0) > 0 and pool_size == 0)
        parallel = max(1, int(self._eval_parallel or 1))
        if cpu_only:
            count = 0
        elif required_unavailable:
            count = int(raw["gpus"])
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
            "required_unavailable": required_unavailable,
            "unspecified": not declared,
            "pin": bool(count > 0 and not required_unavailable),
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
        # ``admission_unpinnable`` is a fail-closed host-lease marker (also an admission outcome, not a
        # source field), so it must not make a still-valid pin look stale and trigger a release/retry loop.
        admitted_source = {
            key: value for key, value in reservation.items()
            if key not in {"gpu_ids", "pin", "whole_pool_unpinned", "admission_unpinnable"}
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
            lease_path = self._gpu_host_lease_path
            if lease_path is not None and self._gpu_host_lease_handle is None:
                # `_try_acquire_gpu_host_lease` returns None when the lease is HELD by another live
                # holder (retryable contention → wait/re-scan) but RAISES GpuPinUnenforceable when the
                # lease cannot even be OPENED (EACCES on a squatted/stale /tmp/looplab-gpu-pool-<uid>.lock,
                # ELOOP, read-only fs). That raise is a non-retryable host-infra failure; the admission
                # boundary `_try_reserve_node_resources` catches it and converts it into a durable
                # `admission_unpinnable` reservation marker, so `_resource_eval_env` re-raises it into
                # each caller's existing node-terminal / retry contract instead of aborting the run mid
                # task-group with no terminal and re-crashing on every resume.
                handle = _try_acquire_gpu_host_lease(Path(lease_path))
                if handle is None:
                    return None
                self._gpu_host_lease_handle = handle
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
            if (len(self._free_gpus) == len(self._gpu_ids)
                    and self._gpu_host_lease_handle is not None):
                handle = self._gpu_host_lease_handle
                self._gpu_host_lease_handle = None
                _release_gpu_host_lease(handle)
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
        # A known positive requirement on an empty detected pool is admitted only as an immediate
        # fail-closed marker. Evaluate/confirm terminalize it without launching candidate code; returning
        # a marker (rather than None) avoids polling an epoch that can never move.
        if request["required_unavailable"]:
            return {**request, "gpu_ids": []}
        reserve_count = request["count"]
        whole_pool_unpinned = bool(
            request["unspecified"] and reserve_count == 0 and self._gpu_ids)
        if whole_pool_unpinned:
            # Legacy serial execution intentionally leaves CUDA visibility untouched so one candidate
            # may use the whole box. Reserve every logical device internally for the same lifecycle;
            # otherwise an unpinned serial Run could bypass both the local pool and the host lease.
            reserve_count = len(self._gpu_ids)
        try:
            gpu_ids = self._acquire_gpus(reserve_count, request["gpu_mem_mib"])
        except GpuPinUnenforceable as exc:
            # The host GPU-pool lease could not be OPENED (EACCES on a squatted/stale lock, ELOOP,
            # read-only fs). This is NOT retryable via the pool epoch, and no admission caller handles
            # the raw exception — it would abort the run mid task-group with no terminal and re-crash on
            # every resume. Fail closed at the admission boundary with a durable marker (reservation, not
            # None, so no forever-wait) that `_resource_eval_env` re-raises at the launch boundary into
            # each caller's existing GpuPinUnenforceable → node-terminal / retry contract. Keep the
            # request's own `required_unavailable` (the pool may hold devices) so
            # `_node_resource_reservation_is_current` still matches the source; the extra marker key is
            # excluded from that comparison.
            return {**request, "gpu_ids": [], "admission_unpinnable": str(exc)[:400]}
        if gpu_ids is None:
            return None
        return {
            **request,
            "gpu_ids": gpu_ids,
            **({"whole_pool_unpinned": True} if whole_pool_unpinned else {}),
        }

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

    def _eval_reservation_under_other_generation(self, node_id: int, generation: int) -> bool:
        """True when this node holds a live eval reservation under a DIFFERENT generation.

        That is the exact signature of a reset that landed between the dispatcher's admission (which
        registered the reservation under the OLD generation) and an eval worker's fresher fold (which
        reads the NEW ``node.attempt``): the current-generation lookup misses, but the dispatcher still
        owns this node's devices under the superseded key. It distinguishes that stale-key miss from a
        never-admitted node (recovery/test seam), which holds no reservation under any generation.
        """
        self._ensure_resource_state()
        with self._gpu_condition:
            return any(
                nid == int(node_id) and gen != int(generation)
                for (nid, gen) in self._eval_gpu_reservations
            )

    def _physical_gpu_ids(self, logical_ids) -> list[str]:
        mapping = getattr(self, "_gpu_physical_ids", {})
        logical = list(logical_ids or [])
        try:
            physical = [str(mapping[gpu]).strip() for gpu in logical]
        except (KeyError, TypeError):
            raise GpuPinUnenforceable(
                "reserved GPU has no trustworthy physical selector") from None
        # Defense in depth for manually constructed/resumed engines: a missing, disabled, malformed or
        # duplicate physical map must not make two scheduler leases target one device (or escape a fence).
        if logical and cuda_visible_device_tokens(",".join(physical)) != physical:
            raise GpuPinUnenforceable(
                "reserved GPU physical selectors are malformed or not unique")
        return physical

    def _resource_eval_env(self, reservation: Optional[dict], *, base: Optional[dict] = None,
                           inherit_host: bool = False) -> Optional[dict]:
        """Build the child env for a reservation without changing the unpinned legacy branch."""
        if reservation and reservation.get("admission_unpinnable"):
            # The host GPU-pool lease could not be opened at admission (see _try_reserve_node_resources).
            # Re-raise the exact cause HERE, at the launch boundary and BEFORE the unpinned-fallback
            # branch below, so a fail-closed host-lease marker (which keeps required_unavailable=False on
            # a populated pool) can never leak an unpinned full-host launch. Each caller's existing
            # GpuPinUnenforceable handler converts it into a durable node terminal / retry.
            raise GpuPinUnenforceable(reservation["admission_unpinnable"])
        if reservation and reservation.get("required_unavailable"):
            # CODEX AGENT: discovery failure must not turn an explicit positive declaration into an
            # unpinned full-host launch. Evaluate/confirm convert this defensive refusal into their
            # durable terminal/retry contracts before any candidate process is started.
            raise GpuPinUnenforceable(
                "explicit GPU requirement cannot be satisfied: no GPUs were detected")
        if (not reservation or reservation.get("whole_pool_unpinned")
                or (not reservation.get("cpu_only") and not reservation.get("gpu_ids"))):
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
