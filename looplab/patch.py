"""Patch application with an out-of-surface gate (I4, ADR-14).

When a Developer backend edits files via a unified diff (rather than whole-file
rewrites), the diff is double-gated before it touches disk:
  1. parse the target paths and **reject** (not strip) the whole patch if any path is
     outside the edit-surface allow-list or escapes it (`..`, absolute, drive letter);
  2. apply with `git apply --check` (dry-run) then for real — kernel-grade enforcement
     so a parser bug can't leak.
A rejected forbidden-file touch is a *signal* (returned to the caller), not noise.
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from pathlib import Path

_DRIVE = re.compile(r"^[A-Za-z]:")


def changed_paths(diff_text: str) -> list[str]:
    """Target paths referenced by a unified diff (a/ b/ prefixes stripped, /dev/null
    ignored)."""
    paths: set[str] = set()
    for line in diff_text.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            p = line[4:].strip().split("\t")[0]
            if p == "/dev/null":
                continue
            if p[:2] in ("a/", "b/"):
                p = p[2:]
            paths.add(p)
        elif line.startswith("diff --git "):
            for tok in line.split()[2:]:
                paths.add(tok[2:] if tok[:2] in ("a/", "b/") else tok)
    return sorted(paths)


def _escapes(path: str) -> bool:
    pp = path.replace("\\", "/")
    # leading slash = POSIX-absolute (os.path.isabs misses this on Windows); drive =
    # Windows-absolute; ".." = traversal.
    if pp.startswith("/") or _DRIVE.match(path) or os.path.isabs(path):
        return True
    return ".." in pp.split("/")


def _ci(s: str) -> str:
    return s.replace("\\", "/").lower()   # case-insensitive, forward-slash (cross-platform)


def _match(path: str, glob: str) -> bool:
    """fnmatch, but treat a leading `**/` as 'zero OR more directories' so a surface like
    `**/*.py` also matches a ROOT-level file (`train.py`) — plain fnmatch requires the literal
    slash and would silently reject root files."""
    if fnmatch.fnmatch(path, glob):
        return True
    return glob.startswith("**/") and fnmatch.fnmatch(path, glob[3:])


def _in_surface(p: str, allow: list[str], prefixes: list[str]) -> bool:
    """Is `p` within the edit surface? With multi-editable `prefixes` (named repo subdirs), a
    path UNDER a named repo is governed ONLY by that repo's prefixed globs, and a root path only
    by the non-prefixed globs — so a broad root glob (`**/*.py`) can't widen a named repo's
    narrower surface (fnmatch's `*` crosses `/`)."""
    pl = p.replace("\\", "/")
    owner = next((pre for pre in prefixes if pl.startswith(pre + "/")), None)
    if owner:
        globs = [g for g in allow if g.startswith(owner + "/")]
    elif prefixes:
        globs = [g for g in allow if not any(g.startswith(pre + "/") for pre in prefixes)]
    else:
        globs = allow
    return any(_match(pl, g) for g in globs)


def gate(diff_text: str, allow: list[str], protect: list[str] | None = None,
         prefixes: list[str] | None = None) -> dict:
    """Check every target path against the allow-list (globs), the protect-list, and escape
    rules. `ok` iff the patch is non-empty and every path is within the surface AND none is
    protected. A protected path is rejected (reject-not-strip) even when it matches the surface
    — the agent must never edit the eval/grader/metric/adapter files, case-insensitively (the
    surface gate + NTFS are case-insensitive, so a case-variant must not slip through).
    `prefixes` (named multi-editable repo dirs) scopes each repo's surface to its own subdir."""
    prot = [_ci(g) for g in (protect or [])]
    pre = [x.rstrip("/") for x in (prefixes or [])]
    paths = changed_paths(diff_text)
    rejected = [p for p in paths
                if _escapes(p)
                or not _in_surface(p, allow, pre)
                or any(_ci(p) == g or fnmatch.fnmatchcase(_ci(p), g) for g in prot)]
    return {"ok": bool(paths) and not rejected, "paths": paths, "rejected": rejected}


def apply_patch(diff_text: str, repo_dir: str, allow: list[str]) -> dict:
    """Gate, then `git apply --check` then apply, inside `repo_dir`. Never applies a
    patch that fails the surface gate or the dry-run."""
    g = gate(diff_text, allow)
    if not g["ok"]:
        return {"applied": False, "paths": g["paths"], "rejected": g["rejected"],
                "error": "out-of-surface" if g["rejected"] else "empty patch"}
    repo = Path(repo_dir)
    patch_file = repo / ".LoopLab.patch"
    patch_file.write_text(diff_text, encoding="utf-8")
    try:
        chk = subprocess.run(["git", "apply", "--check", patch_file.name],
                             cwd=str(repo), capture_output=True, text=True)
        if chk.returncode != 0:
            return {"applied": False, "paths": g["paths"], "rejected": [],
                    "error": chk.stderr.strip()}
        ap = subprocess.run(["git", "apply", patch_file.name],
                            cwd=str(repo), capture_output=True, text=True)
        return {"applied": ap.returncode == 0, "paths": g["paths"], "rejected": [],
                "error": "" if ap.returncode == 0 else ap.stderr.strip()}
    finally:
        patch_file.unlink(missing_ok=True)
