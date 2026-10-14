"""KEEP THE LOGS OF EARLIER ATTEMPTS when a node's workdir is rebuilt (doc 78 §3; audit L-07).

WHY. Every lifecycle of a node is materialized afresh (`engine/workspace.py::WorkspaceSeeder.
materialize` removes the node directory first), so a `node_reset` — the agent's ordinary way to
retry a stage after fixing its environment — deleted the very logs that said why the previous
attempt failed. Incident C1: the first attempt's `FileNotFoundError` survived only in a copy the
agent's own script had made; LoopLab's record kept the wrapper's `AssertionError`.

WHAT. Before the directory is removed, its top-level `*.log` files (the stage logs, `eval.log`,
`setup.log`) are MOVED — a rename, no copy — to `<run>/attempt_logs/node_<id>/<archive>/`, where
`<archive>` names the generation about to start and the UTC time (`before-gen3-20261014T084318Z`).
At most `MAX_ARCHIVES_PER_NODE` archives are kept per node (oldest removed first). Best-effort:
nothing here can fail or delay the evaluation; a log that cannot be moved is simply not kept.
Readers: `GET /api/runs/{run_id}/attempt-logs` lists them; their bytes are read through the run
artifact route (`root=run`, `path=attempt_logs/...`).
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from typing import Optional

_LOG = logging.getLogger(__name__)

ARCHIVE_DIR = "attempt_logs"
MAX_ARCHIVES_PER_NODE = 20
_SUFFIXES = (".log",)


def node_archive_root(run_dir, node_id: int) -> Path:
    return Path(run_dir) / ARCHIVE_DIR / f"node_{int(node_id)}"


def archive_attempt_logs(run_dir, workdir, node_id: int, next_generation: Optional[int],
                         *, now: Optional[float] = None) -> Optional[Path]:
    """Move the workdir's top-level logs aside before the directory is rebuilt; returns the
    archive directory, or None when there was nothing to keep."""
    wd = Path(workdir)
    try:
        candidates = [entry for entry in os.scandir(wd)
                      if entry.name.endswith(_SUFFIXES)
                      and entry.is_file(follow_symlinks=False)
                      and entry.stat(follow_symlinks=False).st_size > 0]
    except OSError:
        return None
    if not candidates:
        return None
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now if now is not None else time.time()))
    label = f"before-gen{int(next_generation)}" if next_generation is not None else "before-rebuild"
    root = node_archive_root(run_dir, node_id)
    target = root / f"{label}-{stamp}"
    suffix = 1
    while target.exists():
        suffix += 1
        target = root / f"{label}-{stamp}-{suffix}"
    try:
        target.mkdir(parents=True)
    except OSError as exc:
        _LOG.warning("could not archive the logs of node %s before rebuilding it: %s", node_id, exc)
        return None
    moved = 0
    for entry in candidates:
        try:
            os.rename(entry.path, target / entry.name)
            moved += 1
        except OSError:
            continue
    if not moved:
        try:
            target.rmdir()
        except OSError:
            pass
        return None
    _prune(root)
    return target


def _prune(root: Path) -> None:
    try:
        archives = sorted((p for p in root.iterdir() if p.is_dir()),
                          key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for old in archives[:-MAX_ARCHIVES_PER_NODE]:
        shutil.rmtree(old, ignore_errors=True)


def list_attempt_logs(run_dir, node_id: Optional[int] = None) -> list[dict]:
    """`[{node_id, archive, files: [{name, bytes, path}]}]`, newest archive first."""
    base = Path(run_dir) / ARCHIVE_DIR
    out = []
    try:
        nodes = sorted(p for p in base.iterdir() if p.is_dir() and p.name.startswith("node_"))
    except OSError:
        return out
    for node_dir in nodes:
        try:
            nid = int(node_dir.name.removeprefix("node_"))
        except ValueError:
            continue
        if node_id is not None and nid != node_id:
            continue
        try:
            archives = sorted((p for p in node_dir.iterdir() if p.is_dir()),
                              key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            continue
        for archive in archives:
            files = []
            try:
                for f in sorted(archive.iterdir()):
                    if f.is_file() and not f.is_symlink():
                        files.append({"name": f.name, "bytes": f.stat().st_size,
                                      "path": f"{ARCHIVE_DIR}/{node_dir.name}/{archive.name}/{f.name}"})
            except OSError:
                continue
            out.append({"node_id": nid, "archive": archive.name, "files": files})
    return out
