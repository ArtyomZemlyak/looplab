"""External-agent code authoring against the run's code repository (doc 79 §4).

An external agent (Codex, Claude Code, a script) proposes code three ways, all ending in the same
`inject_node` `files`/`deleted` overlay the engine already materializes:

  * `files` directly (unchanged);
  * a WORKTREE LoopLab provides: `add_worktree` checks out the run's active base (or a node's
    attempt) from the PRIVATE run repository into `RUN_DIR/code/worktrees/<name>` on branch
    `agent/<name>`; the agent edits, runs, commits as it likes, and `overlay_from_worktree` turns
    "start commit -> worktree now" (commits, uncommitted edits and new files alike) into the overlay;
  * a unified PATCH against the active base: `overlay_from_patch` applies it to a private index.

Why a worktree is safe HERE when doc 37 declined it for node workspaces: the worktree is wired to the
run's own repository, never to the operator's object database (doc 37 §5c), and its start commit
is the archived seed — the operator's working tree with uncommitted edits — not the source HEAD
(doc 37 §5d). The node workspace itself stays a materialized copy, exactly as doc 37 decided.

Every overlay is held to the task's edit surface and protected names (`tools/patch.py::
SurfacePolicy`, the rule the upstream lane uses) and to the upstream lane's text bounds, and the
response says which base it was authored on so a stale worktree is visible before a paid eval.
"""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Optional

from looplab.engine.code_repo import CodeRepo, CodeRepoError, WORKTREES_REL, run_git

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MAX_OVERLAY_FILES = 128
MAX_OVERLAY_BYTES = 2 * 1024 * 1024

# BINARY FILES IN AN OVERLAY (doc 79 §9 leftover, 2026-10-14). `Node.files` is text; a worktree or
# patch that changed a binary file (a small model config in protobuf, an `.npy` fixture, an image)
# used to be refused (`code_overlay_binary`). Its bytes now go to a content-addressed store inside
# the run, `RUN_DIR/code/blobs/<sha256>`, and the overlay carries a MARKER string in their place,
# which `engine/workspace.py::write_node_files` resolves — verifying the hash — when it materializes
# the node. The marker starts with NUL, which no text file an agent writes on purpose begins with,
# so a text file can never be mistaken for one. Bounded per file and per overlay; the store is part
# of the run's record (the run mirror copies it, unlike the rebuildable `code/repo.git`).
BLOBS_REL = "code/blobs"
BLOB_MARKER = "\x00looplab-blob:v1:"
MAX_BLOB_BYTES = 32 * 1024 * 1024
MAX_OVERLAY_BINARY_BYTES = 64 * 1024 * 1024
_BLOB_SPEC = re.compile(r"^sha256=([0-9a-f]{64});size=([0-9]{1,12})$")


def is_blob_marker(content) -> bool:
    return isinstance(content, str) and content.startswith(BLOB_MARKER)


def store_blob(run_dir, data: bytes) -> str:
    """Put `data` in the run's blob store (idempotent) and return the overlay marker for it."""
    import hashlib
    import os
    digest = hashlib.sha256(data).hexdigest()
    path = Path(run_dir) / BLOBS_REL / digest
    if not path.is_file() or path.stat().st_size != len(data):
        from looplab.core.atomicio import atomic_write_bytes
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, data, mode=0o444)
        try:
            os.chmod(path, 0o444)
        except OSError:
            pass
    return f"{BLOB_MARKER}sha256={digest};size={len(data)}"


def resolve_blob(run_dir, marker: str) -> bytes:
    """The bytes a marker names, verified; raises `OverlayRefusal` when missing or altered."""
    import hashlib
    m = _BLOB_SPEC.match(marker[len(BLOB_MARKER):]) if is_blob_marker(marker) else None
    if m is None:
        raise OverlayRefusal("code_blob_marker_invalid", "not a LoopLab binary overlay marker")
    path = Path(run_dir) / BLOBS_REL / m.group(1)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise OverlayRefusal("code_blob_missing",
                             f"binary overlay blob {m.group(1)[:12]} is missing from "
                             f"{BLOBS_REL} of this run") from exc
    if len(data) != int(m.group(2)) or hashlib.sha256(data).hexdigest() != m.group(1):
        raise OverlayRefusal("code_blob_altered",
                             f"binary overlay blob {m.group(1)[:12]} does not match its hash")
    return data


class OverlayRefusal(CodeRepoError):
    """The worktree/patch cannot become an overlay; `code` says why."""


def _meta_path(run_dir, name) -> Path:
    return Path(run_dir) / WORKTREES_REL / ".meta" / f"{name}.json"


def _resolve_start(repo: CodeRepo, start: str) -> str:
    from looplab.engine.code_identity import BASE_BRANCH, node_branch
    if start in ("", "base"):
        sha = repo.rev(BASE_BRANCH)
    elif start.startswith("node:") and start[5:].isdigit():
        sha = repo.rev(node_branch(int(start[5:])))
    elif re.fullmatch(r"[0-9a-f]{7,64}", start):
        sha = repo.rev(start)
    else:
        sha = None
    if sha is None:
        raise OverlayRefusal("code_start_unknown",
                             f"no commit for start {start!r}: use 'base', 'node:<id>' or a commit")
    return sha


def _digest_of(repo: CodeRepo, commit: str) -> Optional[str]:
    from looplab.engine.code_identity import BASE_REF
    for name, sha in repo.refs(BASE_REF).items():
        if sha == commit:
            return name[len(BASE_REF):]
    try:
        body = repo.git("log", "-1", "--format=%B", commit).stdout.decode("utf-8", "replace")
    except CodeRepoError:
        return None
    m = re.search(r"^Looplab-Base-Digest: ([0-9a-f]{64})$", body, re.M)
    return m.group(1) if m else None


def add_worktree(run_dir, name: str, *, start: str = "base") -> dict:
    """Create `RUN_DIR/code/worktrees/<name>` on branch `agent/<name>` at `start`."""
    from looplab.engine.code_identity import sync_run
    if not isinstance(name, str) or not NAME.match(name):
        raise OverlayRefusal("code_worktree_name", "name: 1-64 of letters, digits, '_', '.', '-'")
    run_dir = Path(run_dir)
    sync_run(run_dir)
    repo = CodeRepo(run_dir)
    if not repo.exists():
        raise OverlayRefusal("code_repo_missing", "this run has no code repository yet (no seed)")
    path = run_dir / WORKTREES_REL / name
    if path.exists() or _meta_path(run_dir, name).exists():
        raise OverlayRefusal("code_worktree_exists", f"worktree {name} already exists: {path}")
    with repo.lock():
        commit = _resolve_start(repo, start)
        path.parent.mkdir(parents=True, exist_ok=True)
        repo.git("worktree", "add", "--quiet", "-b", f"agent/{name}", str(path), commit)
        meta = {"name": name, "path": str(path), "branch": f"agent/{name}", "start": start,
                "start_commit": commit, "base_digest": _digest_of(repo, commit)}
        _meta_path(run_dir, name).parent.mkdir(parents=True, exist_ok=True)
        _meta_path(run_dir, name).write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return meta


def list_worktrees(run_dir) -> list:
    root = Path(run_dir) / WORKTREES_REL / ".meta"
    out = []
    for p in sorted(root.glob("*.json")) if root.is_dir() else ():
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def _surface_check(run_dir, paths) -> list:
    """Paths the task's edit surface or protection refuses, as `[{path, reason}]`."""
    try:
        from looplab.adapters.tasks import load_task
        from looplab.tools.patch import SurfacePolicy
        task = load_task(Path(run_dir) / "task.snapshot.json", existing_run=True)
        spec = task.repo_spec() if hasattr(task, "repo_spec") else None
    except Exception:  # noqa: BLE001 - no readable task: the engine's own gates still apply later
        return []
    if not spec:
        return []
    policy = SurfacePolicy(spec.get("edit_surface") or [], spec.get("protected_names") or [],
                           [e["name"] for e in spec.get("editables") or [] if e["name"] not in ("", ".")])
    out = []
    for p in paths:
        reason = policy.check(p)
        if reason:
            out.append({"path": p, "reason": str(reason)[:200]})
    return out


def _overlay_between(repo: CodeRepo, run_dir, start: str, tree_or_commit: str, *,
                     allow_outside_surface: bool = False) -> dict:
    changed = repo.changed(start, tree_or_commit) if tree_or_commit != start else []
    files, deleted, total, binary_total, binaries = {}, [], 0, 0, []
    if len(changed) > MAX_OVERLAY_FILES:
        raise OverlayRefusal("code_overlay_too_large",
                             f"{len(changed)} changed paths; an overlay holds at most {MAX_OVERLAY_FILES}")
    for row in changed:
        path = row["path"]
        if row["status"] == "D":
            deleted.append(path)
            continue
        data = repo.git("cat-file", "blob", f"{tree_or_commit}:{path}").stdout
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if text is not None and not text.startswith("\x00"):
            total += len(data)
            if total > MAX_OVERLAY_BYTES:
                raise OverlayRefusal("code_overlay_too_large",
                                     f"changed text exceeds {MAX_OVERLAY_BYTES} bytes")
            files[path] = text
            continue
        # A BINARY file: stored by content, carried as a marker (see `BLOB_MARKER`).
        if len(data) > MAX_BLOB_BYTES:
            raise OverlayRefusal("code_overlay_binary_too_large",
                                 f"{path} is {len(data)} bytes; a binary overlay file holds at most "
                                 f"{MAX_BLOB_BYTES} (larger inputs belong in a data mount)")
        binary_total += len(data)
        if binary_total > MAX_OVERLAY_BINARY_BYTES:
            raise OverlayRefusal("code_overlay_binary_too_large",
                                 f"binary files exceed {MAX_OVERLAY_BINARY_BYTES} bytes in one overlay")
        binaries.append((path, data))
    refused = _surface_check(run_dir, [*files, *(p for p, _ in binaries), *deleted])
    if refused and not allow_outside_surface:
        raise OverlayRefusal("code_overlay_outside_surface",
                             "outside the task's edit surface or protected: "
                             + ", ".join(r["path"] for r in refused[:10]))
    # Stored only after the surface check: a refused overlay leaves nothing behind.
    for path, data in binaries:
        files[path] = store_blob(run_dir, data)
    return {"files": files, "deleted": deleted, "changed": changed, "start_commit": start,
            "base_digest": _digest_of(repo, start), "outside_surface": refused,
            "binary_files": sorted(p for p, _ in binaries)}


def _stale(repo: CodeRepo, start: str) -> bool:
    from looplab.engine.code_identity import BASE_BRANCH
    base = repo.rev(BASE_BRANCH)
    if base is None:
        return False
    return run_git("--git-dir", str(repo.path), "merge-base", "--is-ancestor", base, start,
                   check=False).returncode != 0


def overlay_from_worktree(run_dir, name: str, **kw) -> dict:
    """Committed AND uncommitted changes of worktree `name` against its start commit."""
    if not isinstance(name, str) or not NAME.match(name):
        raise OverlayRefusal("code_worktree_name", "unknown worktree name")
    meta_file = _meta_path(run_dir, name)
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OverlayRefusal("code_worktree_unknown", f"no worktree {name} in this run") from exc
    repo = CodeRepo(run_dir)
    path = Path(meta["path"])
    with tempfile.TemporaryDirectory(prefix="looplab-overlay-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        run_git("-C", str(path), "read-tree", "HEAD", env_extra=env)
        run_git("-C", str(path), "add", "-A", ".", env_extra=env)
        tree = run_git("-C", str(path), "write-tree", env_extra=env).stdout.decode().strip()
    out = _overlay_between(repo, run_dir, meta["start_commit"], tree, **kw)
    out.update(worktree=name, branch=meta.get("branch"), stale_base=_stale(repo, meta["start_commit"]))
    return out


def overlay_from_patch(run_dir, patch: str, *, start: str = "base", **kw) -> dict:
    """A unified diff (paths relative to the repository root, `git diff` style) applied to `start`."""
    if not isinstance(patch, str) or not patch.strip():
        raise OverlayRefusal("code_patch_empty", "patch is empty")
    if len(patch.encode("utf-8")) > MAX_OVERLAY_BYTES:
        raise OverlayRefusal("code_overlay_too_large", f"patch exceeds {MAX_OVERLAY_BYTES} bytes")
    from looplab.engine.code_identity import sync_run
    sync_run(run_dir)
    repo = CodeRepo(run_dir)
    if not repo.exists():
        raise OverlayRefusal("code_repo_missing", "this run has no code repository yet (no seed)")
    commit = _resolve_start(repo, start)
    with tempfile.TemporaryDirectory(prefix="looplab-patch-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        repo.git("read-tree", commit, env_extra=env)
        res = repo.git("apply", "--cached", "--whitespace=nowarn", "-", input=patch.encode("utf-8"),
                       env_extra=env, check=False)
        if res.returncode != 0:
            tail = res.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
            raise OverlayRefusal("code_patch_does_not_apply", f"patch does not apply: {tail[0][:300]}")
        tree = repo.git("write-tree", env_extra=env).stdout.decode().strip()
    out = _overlay_between(repo, run_dir, commit, tree, **kw)
    out["stale_base"] = _stale(repo, commit)
    return out
