"""Explicit retry/resume input declarations and pre-cleanup validation (doc 84 §6.2).

Validates declared `resume_inputs` BEFORE `node_reset` admission and BEFORE
`WorkspaceSeeder.materialize` removes an existing `nodes/node_<id>` workdir so an attempt
never wipes its own declared checkpoint or proceeds with missing/corrupt/drifted inputs.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat as _stat
from pathlib import Path
from typing import Any, Optional

from looplab.core.pathsafe import resolve_settled

RESUME_INPUT_KINDS = ("file", "directory")
MAX_RESUME_INPUTS = 32
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_KEYS = frozenset({"path", "kind", "sha256", "source_attempt", "receipt_path"})
_MAX_SYMLINK_SCAN = 512
_MAX_RECEIPT_BYTES = 1024 * 1024


class RetryInputPreflightError(ValueError):
    """Raised before workspace cleanup when a declared `resume_inputs` entry fails preflight."""

    def __init__(self, detail: dict[str, Any]) -> None:
        self.detail = dict(detail)
        self.code = str(detail.get("code") or "invalid_resume_input")
        super().__init__(str(detail.get("message") or self.code))


def _validate_abs_path(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value or len(value) > 4096:
        raise ValueError(f"{field_name} must be a non-empty path string of at most 4096 characters")
    raw = value.strip()
    if not os.path.isabs(raw):
        raise ValueError(f"{field_name} must be an explicit absolute path, never a workdir-relative path")
    return raw


def normalize_resume_inputs(raw: Any) -> list[dict[str, Any]]:
    """Validate and normalize optional `resume_inputs` list."""
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_RESUME_INPUTS:
        raise ValueError(f"resume_inputs must be a list of at most {MAX_RESUME_INPUTS} declarations")
    out: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"resume_inputs[{idx}] must be an object")
        unknown = set(item) - _ALLOWED_KEYS
        if unknown:
            raise ValueError(
                f"resume_inputs[{idx}] has unknown field(s): {', '.join(sorted(unknown))}; "
                f"allowed: {', '.join(sorted(_ALLOWED_KEYS))}"
            )
        path = _validate_abs_path(item.get("path"), f"resume_inputs[{idx}].path")
        if path in seen_paths:
            raise ValueError(f"duplicate resume_inputs path: {path!r}")
        seen_paths.add(path)
        entry: dict[str, Any] = {"path": path}
        kind = item.get("kind")
        if kind is not None:
            if kind not in RESUME_INPUT_KINDS:
                raise ValueError(f"resume_inputs[{idx}].kind must be one of {', '.join(RESUME_INPUT_KINDS)}")
            entry["kind"] = kind
        sha = item.get("sha256")
        if sha is not None:
            if not isinstance(sha, str) or _SHA256_RE.fullmatch(sha.strip().lower()) is None:
                raise ValueError(f"resume_inputs[{idx}].sha256 must be a 64-character hex SHA-256")
            if kind == "directory":
                raise ValueError(f"resume_inputs[{idx}].sha256 requires kind='file', not 'directory'")
            entry["sha256"] = sha.strip().lower()
        attempt = item.get("source_attempt")
        if attempt is not None:
            if type(attempt) is not int or attempt < 0:
                raise ValueError(f"resume_inputs[{idx}].source_attempt must be a non-negative integer")
            entry["source_attempt"] = attempt
        receipt_path = item.get("receipt_path")
        if receipt_path is not None:
            entry["receipt_path"] = _validate_abs_path(receipt_path, f"resume_inputs[{idx}].receipt_path")
        out.append(entry)
    return out


def _is_within(candidate: Path, root: Path) -> bool:
    return candidate == root or root in candidate.parents


def _points_into_disposable(raw_path: str, wd_raw: Path, wd_resolved: Path, canary_resolved: Optional[Path]) -> Optional[str]:
    p = Path(raw_path)
    if _is_within(p, wd_raw):
        return str(p)
    try:
        resolved = resolve_settled(p)
    except OSError:
        resolved = p
    if _is_within(resolved, wd_resolved):
        return str(resolved)
    if canary_resolved is not None and _is_within(resolved, canary_resolved):
        return str(resolved)
    # Walk prefix components in case a broken intermediate symlink targets wd_resolved.
    cur = Path(p.anchor)
    for part in p.parts[1:]:
        cur = cur / part
        try:
            if os.path.islink(cur):
                link_target = resolve_settled(cur)
                if _is_within(link_target, wd_resolved) or (
                    canary_resolved is not None and _is_within(link_target, canary_resolved)
                ):
                    return str(link_target)
        except OSError:
            continue
    # A directory declaration includes its descendants and reachable directory links.
    if resolved.is_dir():
        if _is_within(wd_resolved, resolved) or (canary_resolved is not None and _is_within(canary_resolved, resolved)):
            return str(resolved)
        scanned = 0
        pending = [resolved]
        visited = set()
        while pending:
            directory = pending.pop()
            try:
                info = directory.stat()
                identity = (info.st_dev, info.st_ino)
                if identity in visited:
                    continue
                visited.add(identity)
                with os.scandir(directory) as entries:
                    for entry in entries:
                        scanned += 1
                        if scanned > _MAX_SYMLINK_SCAN:
                            return "scan_limit_exceeded:" + str(resolved)
                        member = Path(entry.path)
                        target = resolve_settled(member)
                        if _is_within(target, wd_resolved) or (canary_resolved is not None and _is_within(target, canary_resolved)):
                            return str(target)
                        if member.is_dir():
                            if _is_within(wd_resolved, target) or (canary_resolved is not None and _is_within(canary_resolved, target)):
                                return str(target)
                            pending.append(target)
            except (OSError, RuntimeError):
                return "scan_unavailable:" + str(directory)

    return None


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def preflight_resume_inputs(
    resume_inputs: Any,
    *,
    workdir: Path,
    run_dir: Optional[Path] = None,
    node_id: Optional[int] = None,
    current_attempt: Optional[int] = None,
    phase: str = "rematerialize",
) -> Optional[dict[str, Any]]:
    """Return a structured refusal dict before cleanup when any declared input is unsafe or invalid."""
    normalized = normalize_resume_inputs(resume_inputs)
    if not normalized:
        return None
    wd_raw = Path(workdir)
    wd_resolved = resolve_settled(wd_raw)
    canary_resolved = resolve_settled(Path(run_dir) / "canary") if run_dir is not None else None

    for item in normalized:
        raw_path = item["path"]
        ephemeral_hit = _points_into_disposable(raw_path, wd_raw, wd_resolved, canary_resolved)
        if ephemeral_hit is not None:
            if ephemeral_hit.startswith(("scan_limit_exceeded:", "scan_unavailable:")):
                return {"code": "resume_input_scan_incomplete", "operation": phase, "node_id": node_id,
                        "path": raw_path, "message": "resume input directory safety scan was incomplete; declare narrower durable inputs",
                        "retryable": False}
            return {
                "code": "ephemeral_resume_input",
                "operation": phase,
                "node_id": node_id,
                "path": raw_path,
                "resolved_path": ephemeral_hit,
                "workdir": str(wd_resolved),
                "message": (
                    f"{phase} refused for node {node_id}: declared resume input {raw_path!r} "
                    f"resolves inside disposable workspace {str(wd_resolved)!r}; preserve it in an "
                    "external durable directory before retrying"
                ),
                "retryable": False,
            }

        target = Path(raw_path)
        try:
            st = os.stat(target)  # follows symlinks: broken symlink fails as missing
        except OSError:
            return {
                "code": "missing_resume_input",
                "operation": phase,
                "node_id": node_id,
                "path": raw_path,
                "message": f"{phase} refused for node {node_id}: declared resume input {raw_path!r} does not exist or is unreadable",
                "retryable": False,
            }

        kind = item.get("kind")
        is_reg = _stat.S_ISREG(st.st_mode)
        is_dir = _stat.S_ISDIR(st.st_mode)
        if kind == "file" and not is_reg:
            return {
                "code": "invalid_resume_input_kind",
                "operation": phase,
                "node_id": node_id,
                "path": raw_path,
                "expected_kind": "file",
                "message": f"{phase} refused for node {node_id}: declared file input {raw_path!r} is not a regular file",
                "retryable": False,
            }
        if kind == "directory" and not is_dir:
            return {
                "code": "invalid_resume_input_kind",
                "operation": phase,
                "node_id": node_id,
                "path": raw_path,
                "expected_kind": "directory",
                "message": f"{phase} refused for node {node_id}: declared directory input {raw_path!r} is not a directory",
                "retryable": False,
            }
        if "sha256" in item and not is_reg:
            return {
                "code": "invalid_resume_input_kind",
                "operation": phase,
                "node_id": node_id,
                "path": raw_path,
                "expected_kind": "file",
                "message": f"{phase} refused for node {node_id}: sha256 verification on {raw_path!r} requires a regular file",
                "retryable": False,
            }

        source_attempt = item.get("source_attempt")
        if source_attempt is not None and type(current_attempt) is int:
            invalid_bound = (
                source_attempt > current_attempt
                if phase == "node_reset"
                else (source_attempt >= current_attempt if current_attempt > 0 else source_attempt > 0)
            )
            if invalid_bound:
                return {
                    "code": "resume_input_attempt_drift",
                    "operation": phase,
                    "node_id": node_id,
                    "path": raw_path,
                    "source_attempt": source_attempt,
                    "current_attempt": current_attempt,
                    "message": (
                        f"{phase} refused for node {node_id}: declared source_attempt={source_attempt} "
                        f"for {raw_path!r} is incompatible with current attempt {current_attempt}"
                    ),
                    "retryable": False,
                }

        receipt_raw = item.get("receipt_path")
        if receipt_raw is not None:
            ephemeral_receipt = _points_into_disposable(receipt_raw, wd_raw, wd_resolved, canary_resolved)
            if ephemeral_receipt is not None:
                if ephemeral_receipt.startswith(("scan_limit_exceeded:", "scan_unavailable:")):
                    return {"code": "resume_input_scan_incomplete", "operation": phase, "node_id": node_id,
                            "path": receipt_raw, "message": "resume receipt safety scan was incomplete; declare a regular durable receipt",
                            "retryable": False}
                return {
                    "code": "ephemeral_resume_input",
                    "operation": phase,
                    "node_id": node_id,
                    "path": receipt_raw,
                    "resolved_path": ephemeral_receipt,
                    "workdir": str(wd_resolved),
                    "message": (
                        f"{phase} refused for node {node_id}: declared receipt_path {receipt_raw!r} "
                        f"resolves inside disposable workspace {str(wd_resolved)!r}"
                    ),
                    "retryable": False,
                }
            rpath = Path(receipt_raw)
            try:
                rst = os.stat(rpath)
                if not _stat.S_ISREG(rst.st_mode) or rst.st_size > _MAX_RECEIPT_BYTES:
                    raise OSError("invalid receipt")
                receipt = json.loads(rpath.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {
                    "code": "missing_resume_input",
                    "operation": phase,
                    "node_id": node_id,
                    "path": receipt_raw,
                    "message": f"{phase} refused for node {node_id}: receipt_path {receipt_raw!r} is missing or invalid JSON",
                    "retryable": False,
                }
            if not isinstance(receipt, dict):
                return {
                    "code": "resume_input_attempt_drift",
                    "operation": phase,
                    "node_id": node_id,
                    "path": receipt_raw,
                    "message": f"{phase} refused for node {node_id}: receipt_path {receipt_raw!r} must contain a JSON object",
                    "retryable": False,
                }
            binding = receipt.get("native_binding") if isinstance(receipt.get("native_binding"), dict) else {}
            r_node = binding.get("node_id", receipt.get("node_id"))
            r_attempt = binding.get("attempt", receipt.get("attempt"))
            if node_id is not None and r_node is not None and r_node != node_id:
                return {
                    "code": "resume_input_attempt_drift",
                    "operation": phase,
                    "node_id": node_id,
                    "path": receipt_raw,
                    "receipt_node_id": r_node,
                    "message": (
                        f"{phase} refused for node {node_id}: receipt {receipt_raw!r} binds node {r_node}, "
                        f"not node {node_id}"
                    ),
                    "retryable": False,
                }
            if source_attempt is not None and r_attempt != source_attempt:
                return {
                    "code": "resume_input_attempt_drift",
                    "operation": phase,
                    "node_id": node_id,
                    "path": receipt_raw,
                    "source_attempt": source_attempt,
                    "receipt_attempt": r_attempt,
                    "message": (
                        f"{phase} refused for node {node_id}: receipt {receipt_raw!r} binds attempt "
                        f"{r_attempt!r}, expected source_attempt={source_attempt}"
                    ),
                    "retryable": False,
                }

        expected_sha = item.get("sha256")
        if expected_sha is not None:
            try:
                actual_sha = _file_sha256(target)
            except OSError:
                return {
                    "code": "missing_resume_input",
                    "operation": phase,
                    "node_id": node_id,
                    "path": raw_path,
                    "message": f"{phase} refused for node {node_id}: could not read {raw_path!r} for sha256 check",
                    "retryable": False,
                }
            if actual_sha != expected_sha:
                return {
                    "code": "resume_input_integrity_mismatch",
                    "operation": phase,
                    "node_id": node_id,
                    "path": raw_path,
                    "expected_sha256": expected_sha,
                    "actual_sha256": actual_sha,
                    "message": (
                        f"{phase} refused for node {node_id}: sha256 mismatch on {raw_path!r} "
                        f"(expected {expected_sha[:12]}..., got {actual_sha[:12]}...)"
                    ),
                    "retryable": False,
                }
    return None
