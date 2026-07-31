"""Agentic retrieval toolset (I17, ADR-16): lexical navigation tools the agent
chooses between (grep/glob/read) alongside the vector store. Pure-Python here
(production grep shells out to ripgrep); the point is the *toolset*, agent-chosen.
"""
from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path

# Shared with the repo scout on purpose: both walk caller-supplied roots that in practice are whole
# task repos, and both must skip the same VCS/venv/checkpoint noise.
from looplab.tools.reposcout import _SKIP_DIRS


def _walk(base: Path):
    """Yield `(directory, filenames)` for every directory under `base`, noise directories PRUNED.

    The plain `rglob` these helpers used descended into .git, node_modules, .venv and checkpoint
    dirs. That is merely wasteful for a small knowledge dir, but `RepoTools.repo_grep` points them
    at entire task repos, where the walk is slow AND surfaces VCS internals the caller would have
    to filter back out (it doesn't — see the SECURITY note in knowledge_tools.RepoTools).
    """
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        yield Path(dirpath), filenames


@dataclass
class GrepHit:
    path: str
    lineno: int
    line: str


def grep(pattern: str, root: str, glob: str = "*", max_hits: int = 200,
         max_file_bytes: int = 2_000_000) -> list[GrepHit]:
    """Regex search across files under `root` matching `glob` (ripgrep-style). The pattern may
    be model/agent-supplied, so: reject an over-long pattern (cheap ReDoS mitigation — Python
    `re` has no match timeout) and cap per-file read size so a huge file can't blow up memory."""
    if not pattern or len(pattern) > 1000:
        return []
    try:
        rx = re.compile(pattern)
    except re.error:
        return []
    hits: list[GrepHit] = []
    base = Path(root)
    # Still globally sorted (so which hits survive `max_hits` is unchanged), but over the PRUNED
    # tree — the sort now costs what the repo actually contains, not what .git contains.
    for path in sorted(d / name for d, names in _walk(base) for name in names):
        if not path.is_file() or not fnmatch.fnmatch(path.name, glob):
            continue
        try:
            if path.stat().st_size > max_file_bytes:
                continue                       # skip oversized files (don't read into memory)
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            try:
                matched = rx.search(line)
            except re.error:
                return hits
            if matched:
                hits.append(GrepHit(str(path), i, line.rstrip()))
                if len(hits) >= max_hits:
                    return hits
    return hits


def glob_files(pattern: str, root: str) -> list[str]:
    # `rglob(pat)` is the union of `d.glob(pat)` over every descendant directory, so pruning the walk
    # and globbing each surviving directory yields the same set minus the noise dirs — without
    # descending into them. The set dedups the overlap a `**` pattern produces across directories.
    return [str(p) for p in sorted({q for d, _ in _walk(Path(root)) for q in d.glob(pattern)})
            if p.is_file()]


def read_file(path: str, max_bytes: int = 200_000) -> str:
    # Bound the read BEFORE decoding so max_bytes is a real memory guard (mirrors grep's size
    # cap); otherwise a multi-GB file is fully decoded into memory before the slice.
    with open(path, "rb") as f:
        data = f.read(max_bytes)
    return data.decode("utf-8", errors="replace")
