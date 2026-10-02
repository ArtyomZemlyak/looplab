"""Doc 72 §13: an operator-declared scorer boundary, never inferred import closure.

Literal workspace-relative files belong to editable mounts and feed their existing
protect/seeding rule. The receipt observes the SAME read as the copied-base digest;
unknown full seed identity cannot produce a partial boundary confirmation. No gate,
execution, environment identity, protected region or automatic advancement lives here.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat

from looplab.core.pathsafe import is_reparse, run_child_name_defect

MAX_BOUNDARY_FILES = 128
MAX_BOUNDARY_PATH_CHARS = 1024


def normalize_scorer_boundary(value):
    """Validate a portable, unambiguous declaration; None preserves legacy dumps."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"files"}:
        raise ValueError("scorer_boundary requires exactly {files: [workspace-relative paths]}")
    files = value["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_BOUNDARY_FILES:
        raise ValueError(f"scorer_boundary.files requires 1..{MAX_BOUNDARY_FILES} literal files")
    seen = set()
    for name in files:
        if (not isinstance(name, str) or not name or len(name) > MAX_BOUNDARY_PATH_CHARS
                or any(c in name for c in '\\:*?"<>|') or any(ord(c) < 32 for c in name)):
            raise ValueError("scorer_boundary.files requires canonical literal relative paths")
        parts = name.split("/")
        if any(run_child_name_defect(p, strict=True) is not None
               or p.startswith("~") or p.lower() == ".git" for p in parts):
            raise ValueError("scorer_boundary.files contains an unsafe or aliased path")
        if name.casefold() in seen:
            raise ValueError("scorer_boundary.files contains duplicate or case-aliased paths")
        seen.add(name.casefold())
    return {"files": sorted(files)}


def boundary_protection(mounts, declaration, *, reserved=()):
    """Map workspace paths to their declared owning editable; no live existence inference."""
    if declaration is None:
        return {}
    out = {}
    ordered = sorted(mounts, key=lambda ed: 0 if ed["name"] in ("", ".") else len(ed["name"]), reverse=True)
    for path in declaration["files"]:
        folded = path.casefold()
        if any(folded == name.rstrip("/").casefold()
               or folded.startswith(name.rstrip("/").casefold() + "/") for name in reserved):
            raise ValueError(f"scorer_boundary file {path!r} cannot name data/reference inputs")
        for ed in ordered:
            prefix = "" if ed["name"] in ("", ".") else ed["name"].rstrip("/") + "/"
            if path.startswith(prefix) and len(path) > len(prefix):
                out.setdefault(ed["name"], []).append(path[len(prefix):])
                break
        else:
            raise ValueError(f"scorer_boundary file {path!r} must belong to a declared editable")
    return out


def validate_boundary_sources(mounts, declaration, *, reserved=()):
    """Submit requires real operator files; resume may retain names with unavailable bytes."""
    protection = boundary_protection(mounts, declaration, reserved=reserved)
    for ed in mounts:
        for name in protection.get(ed["name"], []):
            root = Path(ed["path"])
            parts = name.split("/")
            try:
                for i in range(len(parts) + 1):
                    entry = root.joinpath(*parts[:i]).lstat()
                    expected = stat.S_ISREG if i == len(parts) else stat.S_ISDIR
                    if is_reparse(entry) or not expected(entry.st_mode):
                        raise ValueError(f"scorer_boundary file {name!r} must be a regular source file without links")
            except OSError as exc:
                raise ValueError(f"scorer_boundary file {name!r} must exist in its editable source") from exc


class BoundaryCapture:
    """Observe named members during the bounded full-base read, before overlay or mounts."""

    def __init__(self, declaration):
        self.names = frozenset(declaration["files"])
        self.rows = {}

    def add(self, name, data, executable):
        if name in self.names:
            self.rows[name] = {"path": name, "sha256": hashlib.sha256(data).hexdigest(),
                               "bytes": len(data), "executable": executable}

    def receipt(self, base):
        result = {"version": 1, "scope": "operator_declared_seed_files",
                  "declared_files": sorted(self.names), "complete": False,
                  "digest": None, "reason": "seed_identity_unavailable",
                  "members": [], "file_count": 0, "bytes": 0}
        if base.get("complete") is not True:
            return result
        if self.rows.keys() != self.names:
            return {**result, "reason": "declared_file_missing"}
        rows = [self.rows[name] for name in sorted(self.names)]
        preimage = [result["version"], result["scope"], result["declared_files"], rows]
        digest = hashlib.sha256(json.dumps(preimage, ensure_ascii=True,
                                          sort_keys=True).encode("ascii")).hexdigest()
        return {**result, "complete": True, "digest": digest, "reason": None,
                "members": rows, "file_count": len(rows), "bytes": sum(row["bytes"] for row in rows)}
