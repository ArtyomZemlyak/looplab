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
