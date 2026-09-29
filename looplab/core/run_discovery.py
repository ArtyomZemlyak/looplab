"""Which directories under a runs ROOT are runs — at any depth, not only the root's children.

A run is a directory holding an `events.jsonl`. `looplab run --out` takes any path, so a campaign
written as `runs/<campaign>/<seed>` is as ordinary as `runs/<run>` — and every reader that listed
only `root.iterdir()` saw the campaign directory, not its runs. For the corpus instruments that was
a silent under-count; for `serve/memory_cascade.py::surviving_run_identities` it was worse: a live
nested run's uid was unknown, so its cross-run rows read as ORPHANS and `looplab memory-orphans
--apply` would offer them for removal.

The walk stops AT a run: a run's own per-node workdirs sit under it, and a nested `events.jsonl`
there is not a second run — that is the reason the one-level readers gave for not recursing, and it
is kept by never descending into a directory once it is a run. Hidden directories are skipped (the
readers already skipped them at the top level). A symlinked directory is followed, as `iterdir` +
`is_dir` always followed one; a directory reached twice (a link cycle) is walked once.

What the walk could NOT see is RETURNED, never dropped: a subtree it could not list and a subtree
deeper than `max_depth` land in `unwalked`, so a caller that must fail closed on an unknown (the
orphan survey) can, and a caller that only counts can say what it did not count.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

EVENTS_FILE = "events.jsonl"
# Deep enough for `<root>/<group>/<campaign>/<seed>`; a bound, not a guess about layouts: a subtree
# past it is REPORTED in `unwalked`, never silently skipped.
DEFAULT_MAX_DEPTH = 4


@dataclass
class RunDiscovery:
    runs: list[Path] = field(default_factory=list)
    unwalked: list[Path] = field(default_factory=list)


def is_run_dir(path: Path) -> bool:
    try:
        return (path / EVENTS_FILE).is_file()
    except OSError:
        return False


def discover_run_dirs(root: str | Path, *, max_depth: int = DEFAULT_MAX_DEPTH) -> RunDiscovery:
    """Every run directory under `root` (the root itself when IT is a run), sorted by path.

    `max_depth` counts levels below the root: 1 is the one-level listing the readers used to do.
    """
    base = Path(root)
    found = RunDiscovery()
    if is_run_dir(base):
        found.runs.append(base)
        return found
    seen: set[str] = set()
    stack: list[tuple[Path, int]] = [(base, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            key = os.path.realpath(directory)
        except (OSError, ValueError):
            found.unwalked.append(directory)
            continue
        if key in seen:
            continue
        seen.add(key)
        try:
            children = sorted(directory.iterdir())
        except OSError:
            found.unwalked.append(directory)
            continue
        for child in children:
            try:
                if child.name.startswith(".") or not child.is_dir():
                    continue
            except OSError:
                found.unwalked.append(child)
                continue
            if is_run_dir(child):
                found.runs.append(child)
            elif depth + 1 < max_depth:
                stack.append((child, depth + 1))
            elif _has_subdirectory(child):
                found.unwalked.append(child)
    found.runs.sort()
    found.unwalked.sort()
    return found


def _has_subdirectory(path: Path) -> bool:
    """Whether a directory at the depth bound could still hide a run (it holds a directory)."""
    try:
        return any(not entry.name.startswith(".") and entry.is_dir() for entry in path.iterdir())
    except OSError:
        return True
