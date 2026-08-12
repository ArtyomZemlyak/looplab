"""The durable writer-fence PROTOCOL, shared by the reset and deletion fences (doc 25 CO-01).

`run_reset.py` and `run_deletion.py` are two fences with genuinely different schemas, error types and
lifecycles — but they implemented the same fail-closed marker protocol twice, ~180 of ~400 combined
lines of it. That is the wrong half to duplicate: this is security-adjacent validation whose
realistic failure mode is one copy gaining a fix the other misses, silently. The deletion side had
already drifted, re-deriving `atomicio.file_identity` as a local lambda while importing the
canonical one two lines above.

So the PROTOCOL lives here and the SCHEMAS stay with their owners. This module knows nothing about
what a marker means; each owner passes its own validator and its own storage-error class, so the
distinct key-sets and error types stay explicit at the call site (which is what makes each module
still readable on its own).

The read is deliberately paranoid, in this order:

  lstat -> reject a reparse point or anything that is not a regular file -> reject an oversized file
  -> bounded read -> lstat again -> reject if the file identity changed under the read.

The second lstat is the point: a marker replaced mid-read would otherwise be parsed as a mix of two
generations. Every failure raises the owner's storage error — a marker that cannot be read is an
UNKNOWN fence, never permission to mutate the run.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Callable, Optional

from looplab.core.atomicio import file_identity, strict_atomic_write_text
from looplab.core.pathsafe import is_reparse

# Both fences bind an operation to a UUID4-shaped id and a run to a 64-hex generation, and both cap
# the marker at 8 KiB. Declared once so the two cannot drift to different shapes.
FENCE_OPERATION_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
FENCE_GENERATION_RE = re.compile(r"^[0-9a-f]{64}$")
FENCE_MAX_BYTES = 8 * 1024

# The overwhelmingly common answer to "is there a fence here?" is NO — a marker exists only while a
# reset or a delete is actually in flight. So the hot cost of this protocol is not reading a marker,
# it is the NEGATIVE lookup that proves there is none, and on the network mounts a run root usually
# lives on that lookup is not free.
#
# MEASURED 2026-08-12 against `runs/rubertlite-dr-unified-v5` on the geesefs/S3 mount, while that
# run's engine was live (so the FUSE layer's cached directory listing kept being invalidated):
#   * `lstat` of an ABSENT marker: 105-950 ms, median ~250 ms — a round trip, every time.
#   * `lstat` of a PRESENT file in the same directory: 0.4 ms.
#   * one `scandir` of the containing directory: 1.4-1.8 ms, after which the same absent-marker
#     `lstat` costs 0.1-0.9 ms.
# The FUSE layer answers a negative lookup out of a directory listing it already holds and otherwise
# goes to the store; nothing forces it to hold one, so each absent-marker probe paid full price.
#
# `/api/runs/{run}/nodes/{n}/trace` performs FIVE of these per request (`AppState.run_dir`'s deletion
# fence, then the reset marker inside `_assert_trace_reset_clear` and `_state_payload`, twice each
# for the before/after lifecycle CAS) and the conversation twin performs more — measured 4-15 s and
# 20-22 s respectively for payloads as small as 1.4 KB, i.e. far past the browser's trace-read
# deadline, which is what the operator sees as "Trace unavailable".
#
# So warm the lookup before making it. This is a PREFETCH and nothing else: the authoritative `lstat`
# below is unchanged and still decides, the listing is never consulted, and every failure is
# swallowed. A stale or partial listing therefore cannot turn a present fence into an absent one —
# the only thing that can happen is that the probe is as slow as it was before.
_FENCE_LOOKUP_WARM_ENTRIES = 4096


def _warm_directory_lookup(path: Path) -> None:
    """Best-effort readdir of `path`'s directory, so the `lstat` below is served from a listing.

    Bounded and total: any OSError (a vanished/renamed directory, a permission change, a platform
    without readdir) leaves the probe exactly as it was. The entry cap keeps a pathologically large
    directory from turning an accelerator into the slow path — the readdir has already been issued
    by then, which is the whole effect being bought.
    """
    try:
        with os.scandir(path.parent) as entries:
            for seen, _entry in enumerate(entries, start=1):
                if seen >= _FENCE_LOOKUP_WARM_ENTRIES:
                    break
    except OSError:
        pass


def load_bounded_json_marker(
    path: Path,
    *,
    label: str,
    error_cls: type[Exception],
    validate: Callable[[dict[str, Any]], bool],
    max_bytes: int = FENCE_MAX_BYTES,
) -> Optional[dict[str, Any]]:
    """Read a fence marker under the paranoid protocol above, or ``None`` when there is none.

    `validate` receives the decoded object and returns whether it satisfies the owner's schema; a
    False answer raises ``error_cls(f"{label} is malformed")``. Every other failure mode raises
    ``error_cls`` too, with the phase named — an unreadable marker must never read as "no fence".
    """
    # Prefetch only — see `_warm_directory_lookup`. The `lstat` below remains the sole authority.
    _warm_directory_lookup(path)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise error_cls(f"{label} cannot be inspected: {exc}") from exc
    if is_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise error_cls(f"{label} is not a regular service-owned file")
    if before.st_size > max_bytes:
        raise error_cls(f"{label} exceeds its safety limit")
    try:
        with path.open("rb") as stream:
            raw = stream.read(max_bytes + 1)
        after = path.lstat()
    except OSError as exc:
        raise error_cls(f"{label} cannot be read: {exc}") from exc
    # `file_identity` is the canonical same-file-unchanged stat tuple. The deletion fence used to
    # spell its own six-field lambda here; a change to the canonical tuple would have left that copy
    # comparing a weaker identity, with nothing to notice.
    if file_identity(before) != file_identity(after):
        raise error_cls(f"{label} changed while it was being read")
    if len(raw) > max_bytes:
        raise error_cls(f"{label} exceeds its safety limit")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise error_cls(f"{label} cannot be decoded: {exc}") from exc
    if not isinstance(value, dict) or not validate(value):
        raise error_cls(f"{label} is malformed")
    return value


def publish_bounded_json_marker(
    path: Path,
    value: dict[str, Any],
    *,
    label: str,
    error_cls: type[Exception],
    confirm: Callable[[], Optional[dict[str, Any]]],
    max_bytes: int = FENCE_MAX_BYTES,
) -> dict[str, Any]:
    """Write a marker durably, then READ IT BACK through the owner's loader and confirm it.

    The read-back is not belt-and-braces: a strict write can fail after the replacement became
    visible, callers retry the same operation, and that later read is the authority on every
    subsequent decision. Confirming here means a half-published fence is a loud storage error rather
    than a fence nobody can satisfy.
    """
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > max_bytes:
            raise ValueError(f"{label} exceeds its safety limit")
        strict_atomic_write_text(path, encoded)
    except (OSError, TypeError, ValueError) as exc:
        raise error_cls(f"{label} could not be published durably: {exc}") from exc
    if confirm() != value:
        raise error_cls(f"{label} publication could not be confirmed")
    return value
