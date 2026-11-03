"""Root-local run addresses, including safe nested campaign runs.

A nested ID is one URL segment containing URL-safe base64 of the whole relative UTF-8 path.
It is an address, not the run's immutable identity: ``run_started.run_id`` and ``run_uid`` stay
unchanged. A directory below another run is never a candidate, even if it contains events.jsonl.
"""
from __future__ import annotations

import base64
import binascii
import os
import re
from pathlib import Path, PurePath
from typing import NamedTuple

from looplab.core.pathsafe import run_child_name_defect, validate_run_child
from looplab.core.run_discovery import has_run_marker, is_run_dir

NESTED_RUN_PREFIX = "nested-"
MAX_NESTED_RUN_DEPTH = 4
MAX_NESTED_RUN_BYTES = 4096
RESERVED_RUN_IDS = {
    "reports", "assistant", ".reviews", ".reviews.lock", ".command-locks",
    "ui_settings.json", "ui_settings.json.lock", "secrets.json", "secrets.json.lock",
    ".settings-launch.lock", "projects.json", "projects.json.lock", ".agent-events",
}
_RESERVED_RUN_IDS = RESERVED_RUN_IDS
_NESTED_ID = re.compile(r"[A-Za-z0-9_-]+")


class RunAddress(NamedTuple):
    path: Path | None
    run_id: str
    nested: bool
    defect: str | None = None


def _nested_id(relative: str) -> str:
    encoded = base64.urlsafe_b64encode(relative.encode("utf-8")).decode("ascii").rstrip("=")
    return NESTED_RUN_PREFIX + encoded


def _relative_parts(root: Path, rd: Path) -> tuple[str, ...] | None:
    try:
        relative = rd.relative_to(root)
    except ValueError:
        return None
    if not relative.parts or len(relative.parts) > MAX_NESTED_RUN_DEPTH:
        return None
    for part in relative.parts:
        if run_child_name_defect(part) is not None or part.startswith("."):
            return None
    return relative.parts


def run_address(root: Path, rd: Path) -> str:
    """The canonical root-local address for an already validated run directory."""
    root = Path(root).resolve()
    rd = Path(rd).resolve()
    parts = _relative_parts(root, rd)
    if parts is None or (len(parts) > 1 and parts[0].lower() in _RESERVED_RUN_IDS):
        raise ValueError("not an addressable run directory")
    return parts[0] if len(parts) == 1 else _nested_id("/".join(parts))


def _candidate(root: Path, parts: tuple[str, ...], *, allow_missing: bool = False) -> RunAddress:
    if not parts or len(parts) > MAX_NESTED_RUN_DEPTH or parts[0].lower() in _RESERVED_RUN_IDS:
        return RunAddress(None, "", len(parts) > 1, "invalid_nested_id")
    current = root
    nested = len(parts) > 1
    for index, part in enumerate(parts):
        if run_child_name_defect(part) is not None or part.startswith("."):
            return RunAddress(None, "", nested, "invalid_nested_id")
        if is_run_dir(current) or has_run_marker(current):
            return RunAddress(None, "", nested, "inside_run")
        child = validate_run_child(current, part)
        if child.defect == "missing" and allow_missing:
            child = validate_run_child(current, part, must_exist=False)
        if child.defect is not None:
            return RunAddress(None, "", nested, child.defect)
        current = child.path
    return RunAddress(current, _nested_id("/".join(parts)) if nested else parts[0], nested)


def _decode_nested(run_id: str) -> tuple[str, ...] | None:
    encoded = run_id.removeprefix(NESTED_RUN_PREFIX)
    if not encoded or not _NESTED_ID.fullmatch(encoded) or len(encoded) % 4 == 1:
        return None
    try:
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        relative = raw.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    if not raw or len(raw) > MAX_NESTED_RUN_BYTES or "\\" in relative or "\x00" in relative:
        return None
    if _nested_id(relative) != run_id:
        return None
    parts = tuple(relative.split("/"))
    return parts if len(parts) > 1 and all(parts) else None


def resolve_run_address(root: Path, run_id, *, allow_missing: bool = False) -> RunAddress:
    """Resolve a root ID, nested ID or already-resolved nested path without following links.

    ``allow_missing`` is for post-delete receipts, never for opening an evaluation or engine.
    """
    try:
        root = Path(root).resolve()
    except OSError:
        return RunAddress(None, "", False, "unreadable")
    if isinstance(run_id, PurePath) and Path(run_id).is_absolute():
        requested = Path(run_id)
        try:
            resolved = requested.resolve(strict=True)
        except (FileNotFoundError, OSError):
            return RunAddress(None, "", True, "missing")
        parts = _relative_parts(root, resolved)
        if parts is None:
            return RunAddress(None, "", True, "outside_root")
        return _candidate(root, parts, allow_missing=allow_missing)
    run_id = str(run_id or "")
    direct = validate_run_child(root, run_id)
    if direct.defect == "missing" and allow_missing:
        direct = validate_run_child(root, run_id, must_exist=False)
    if not run_id.startswith(NESTED_RUN_PREFIX):
        return RunAddress(direct.path, run_id, False, direct.defect)
    parts = _decode_nested(run_id)
    if parts is None:
        # A pre-existing root run with this otherwise-reserved spelling keeps its legacy address.
        return (RunAddress(direct.path, run_id, False) if direct.defect is None
                else RunAddress(None, run_id, True, "invalid_nested_id"))
    candidate = _candidate(root, parts, allow_missing=allow_missing)
    legacy_exists = os.path.lexists(root / run_id)
    if candidate.defect is None and legacy_exists:
        return RunAddress(None, run_id, True, "collision")
    if candidate.defect in ("missing", "unreadable") and direct.defect is None:
        return direct
    if candidate.defect is not None:
        return RunAddress(None, run_id, True, candidate.defect)
    return candidate


__all__ = ["MAX_NESTED_RUN_DEPTH", "NESTED_RUN_PREFIX", "RunAddress",
           "run_address", "resolve_run_address"]
