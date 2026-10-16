"""External-agent code authoring against the run's code repository (doc 79 §4).

An external agent (Codex, Claude Code, a script) proposes code three ways, all ending in the same
`inject_node` `files`/`deleted` overlay the engine already materializes:

  * `files` directly (unchanged);
  * a WORKTREE LoopLab provides: `add_worktree` checks out the run's active base (or a node's
    attempt) from the PRIVATE run repository into `RUN_DIR/code/worktrees/<name>` on branch
    `agent/<name>`; the agent edits, runs, commits as it likes, and `overlay_from_worktree` turns
    "seed base -> worktree now" (commits, uncommitted edits and new files alike) into the overlay —
    from `node:<id>` that is the start node's own change PLUS the agent's, because the overlay is
    materialized over the base, not over the node (`_seed_base_of`);
  * a unified PATCH against the active base (or a node): `overlay_from_patch` applies it to a
    private index, and the overlay is again the whole change over the start's seed base.

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


def _seed_base_of(repo: CodeRepo, commit: str) -> str:
    """The nearest BASE commit `commit` descends from (itself when it is one), else `commit`.

    An overlay is materialized OVER A BASE (`engine/workspace.py::write_node_files` on the seed),
    so it must be the whole change from that base. Diffing against the START commit was right only
    for `start=base`: from `node:<id>` the start is that node's attempt commit, the overlay held the
    agent's edits alone, and the injected node silently reverted every change of the node it was
    built from (review 2026-10-15). Nearest by topological order: bases form one parent chain, and
    an attempt or agent commit sits on exactly one of them."""
    from looplab.engine.code_identity import BASE_REF
    bases = set(repo.refs(BASE_REF).values())
    if not bases or commit in bases:
        return commit
    out = repo.git("rev-list", "--topo-order", commit, check=False)
    for sha in out.stdout.decode("ascii", "replace").split():
        if sha in bases:
            return sha
    return commit


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
    # The WHOLE change over the seed base the start sits on (`_seed_base_of`), never over the start
    # itself: the overlay replaces the start node's overlay rather than stacking on it.
    base = _seed_base_of(repo, start)
    changed = repo.changed(base, tree_or_commit) if tree_or_commit != base else []
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
    inherited: list = []
    if refused and base != start:
        # A start node's attempt commit holds what its materialization WROTE, task assets included
        # (`code_identity.py::record_materialized` reads them off disk), and those are outside the
        # surface. A refused path the agent left exactly as the start had it is that inheritance,
        # not an edit: the materializer writes it anyway, so it is dropped, not refused.
        start_e, now_e = repo.entries(start), repo.entries(tree_or_commit)
        inherited = sorted(r["path"] for r in refused
                           if start_e.get(r["path"]) == now_e.get(r["path"]))
        if inherited:
            drop = set(inherited)
            refused = [r for r in refused if r["path"] not in drop]
            files = {p: t for p, t in files.items() if p not in drop}
            deleted = [p for p in deleted if p not in drop]
            binaries = [(p, d) for p, d in binaries if p not in drop]
            changed = [r for r in changed if r["path"] not in drop]
    if refused and not allow_outside_surface:
        raise OverlayRefusal("code_overlay_outside_surface",
                             "outside the task's edit surface or protected: "
                             + ", ".join(r["path"] for r in refused[:10]))
    # Stored only after the surface check: a refused overlay leaves nothing behind.
    for path, data in binaries:
        files[path] = store_blob(run_dir, data)
    out = {"files": files, "deleted": deleted, "changed": changed, "start_commit": start,
           "overlay_base": base, "base_digest": _digest_of(repo, base), "outside_surface": refused,
           "binary_files": sorted(p for p, _ in binaries)}
    if inherited:
        out["inherited_outside_surface"] = inherited
    return out


def _stale(repo: CodeRepo, start: str) -> bool:
    from looplab.engine.code_identity import BASE_BRANCH
    base = repo.rev(BASE_BRANCH)
    if base is None:
        return False
    return run_git("--git-dir", str(repo.path), "merge-base", "--is-ancestor", base, start,
                   check=False).returncode != 0


def overlay_from_worktree(run_dir, name: str, **kw) -> dict:
    """Committed AND uncommitted changes of worktree `name` against its start's seed base."""
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


# ---------------------------------------------------------------------------- agent worktree backup
# WHY (weekly-worktree-restore-loss, review 2026-10-15; closed 2026-10-16). The run mirror skips
# `code/repo.git` and `code/worktrees` because `sync_run` rebuilds the repository from the log — true
# for bases and recorded attempts, FALSE for an external agent's worktree that has not been injected
# yet: its `agent/<name>` branch, its `.meta` row and its uncommitted edits are in no event. A real
# mirror -> restore -> `sync_run` lost the branch, the unique committed proposal and every dirty byte.
#
# WHAT. `agent_backup` (the run mirror's `run_extras` producer, wired by `looplab run-mirror`) writes
# two files OUTSIDE the run directory, into the mirror's spool: `agents.bundle`, a self-contained
# `git bundle` of every `refs/heads/agent/*` plus one `refs/looplab/agent-dirty/<name>` commit per
# worktree whose files differ from its HEAD (tracked edits, deletions and untracked files `git add -A`
# would take — the repository's `info/exclude` keeps checkpoints out, exactly as for an overlay); and
# `agents.json`, the worktrees' metadata with each one's head and dirty commit. The mirror copies
# both to `code/agent_backup/` of the mirrored run. After a restore, `sync_run` calls
# `restore_agent_worktrees`, which imports the bundle, re-creates each missing branch and worktree
# and lays the dirty tree back over it (index at HEAD, so `git status` reads as it did).
#
# READ-ONLY ON THE RUN. The dirty capture runs `git add -A` against a COPY of the worktree's index
# with `GIT_OBJECT_DIRECTORY` pointed at a scratch object store whose alternate is the run
# repository's: the new blobs land in the scratch store, the agent's index and the run repository are
# never written, and no lock the engine (or the agent's own git) takes is taken.
#
# LARGE FILES ARE NOT GIT'S. A checkpoint, a dataset shard or any other file git is told to ignore
# (the repository's excludes) and every dirty file over `LARGE_FILE_BYTES` is kept OUT of the bundle
# and copied as a PLAIN file to `files/<worktree>/<path>.v<version>`, the version a digest of its
# (size, mtime) — a path with its version in its name, no content store, no DVC. A changed file is a
# new name (the old one is dropped from the spool, so the mirror drops it too), an unchanged one is
# never re-copied, and the restore lays each back at `<path>` after the dirty tree. If the small
# files alone would still exceed `MAX_BACKUP_DIRTY_BYTES` (what an overlay can carry), the largest
# move to the plain store until they fit. Bounded by `MAX_BACKUP_FILES` / `MAX_BACKUP_FILES_BYTES`;
# what is past a bound is named in the row (`files_skipped`), and a plain file over the mirror's
# `--max-file-mb` is listed as `skipped` like any other large file. Caches git and Jupyter make
# (`__pycache__/`, `.ipynb_checkpoints/`) are not backed up.
#
# CHANGE-DETECTED. The bundle is re-created only when its own KEY (every branch ref, every dirty
# tree, every HEAD) moved — a checkpoint rewritten every step re-copies that one plain file, never
# the history — so an idle worktree costs one stat-cached `git add -A` per pass and no upload.
AGENT_BACKUP_REL = "code/agent_backup"
AGENT_BUNDLE = "agents.bundle"
AGENT_BACKUP_INDEX = "agents.json"
AGENT_BRANCH_PREFIX = "refs/heads/agent/"
AGENT_DIRTY_REF = "refs/looplab/agent-dirty/"
AGENT_HEAD_REF = "refs/looplab/agent-head/"
MAX_BACKUP_DIRTY_BYTES = MAX_OVERLAY_BYTES + MAX_OVERLAY_BINARY_BYTES
AGENT_FILES_DIR = "files"
LARGE_FILE_BYTES = 8 * 1024 * 1024
MAX_BACKUP_FILES = 2000
MAX_BACKUP_FILES_BYTES = 64 * 1024 ** 3
_BACKUP_JUNK = ("__pycache__/", ".ipynb_checkpoints/")
_BACKUP_VERSION = 2
# The dirty commit is a pure function of (tree, parent): a fixed identity and date, so an unchanged
# worktree yields the same commit id pass after pass.
_FIXED_COMMIT_ENV = {"GIT_AUTHOR_NAME": "LoopLab", "GIT_AUTHOR_EMAIL": "looplab@invalid",
                     "GIT_COMMITTER_NAME": "LoopLab", "GIT_COMMITTER_EMAIL": "looplab@invalid",
                     "GIT_AUTHOR_DATE": "@0 +0000", "GIT_COMMITTER_DATE": "@0 +0000"}


def _clear_backup(out_dir: Path) -> None:
    import shutil
    for name in (AGENT_BUNDLE, AGENT_BACKUP_INDEX):
        try:
            (out_dir / name).unlink()
        except OSError:
            pass                           # never written, or already gone
    shutil.rmtree(out_dir / AGENT_FILES_DIR, ignore_errors=True)


def _dirty_entries(wt: Path, env: dict) -> list[tuple[str, bool, int]]:
    """`(path, ignored, size)` for every path `git status` (on the COPIED index,
    `--no-optional-locks`) lists as changed, untracked or IGNORED — size 0 for a deletion, a link
    or anything not a regular file (nothing a plain copy could carry)."""
    import os
    import stat as _stat
    out = run_git("-C", str(wt), "--no-optional-locks", "status", "--porcelain", "-z",
                  "--untracked-files=all", "--ignored=matching", "--no-renames",
                  env_extra=env).stdout
    rows = []
    for item in out.split(b"\x00"):
        if len(item) <= 3:
            continue
        rel = item[3:].decode("utf-8", "surrogateescape")
        ignored = item[:2] == b"!!"
        if ignored and _backup_junk(rel):
            continue                       # a cache git and Jupyter rebuild on their own
        if ignored and rel.endswith("/"):
            # An ignored DIRECTORY (`checkpoint-500/`) is reported whole: carry its files.
            for dirpath, dirnames, filenames in os.walk(wt / rel):
                dirnames[:] = sorted(d for d in dirnames if d + "/" not in _BACKUP_JUNK)
                for fname in sorted(filenames):
                    sub = Path(dirpath, fname).relative_to(wt).as_posix()
                    try:
                        st = Path(dirpath, fname).lstat()
                    except OSError:
                        continue
                    if _stat.S_ISREG(st.st_mode):
                        rows.append((sub, True, st.st_size))
            continue
        try:
            st = (wt / rel).lstat()
        except OSError:
            rows.append((rel, ignored, 0))  # a deletion (or a path gone since)
            continue
        rows.append((rel, ignored, st.st_size if _stat.S_ISREG(st.st_mode) else 0))
    return rows


def _backup_junk(rel: str) -> bool:
    return any(f"/{d}" in f"/{rel}" for d in _BACKUP_JUNK)


def _plain_version(st) -> str:
    import hashlib
    return hashlib.sha256(f"{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest()[:12]


def _capture_dirty(repo: CodeRepo, wt: Path, scratch: Path, head: str, row: dict) -> None:
    """Set `row["dirty_commit"]` to a commit (in `scratch`'s object store) of `wt`'s SMALL files as
    they are, parented on `head` — or leave it None when they equal `head`'s tree — and
    `row["plain"]` to the large and ignored files a plain copy carries instead (see above)."""
    objects = scratch / "objects"
    index = scratch.parent / f"index-{row['name']}"
    real = run_git("-C", str(wt), "rev-parse", "--git-path", "index").stdout.decode().strip()
    real_path = Path(real) if Path(real).is_absolute() else wt / real
    env = {"GIT_INDEX_FILE": str(index), "GIT_OBJECT_DIRECTORY": str(objects)}
    try:
        # The agent's own index carries the stat cache, so `add -A` re-hashes only what changed.
        index.write_bytes(real_path.read_bytes())
    except OSError:
        run_git("-C", str(wt), "read-tree", head, env_extra=env)
    entries = _dirty_entries(wt, env)
    plain = {rel for rel, ignored, size in entries
             if size and (ignored or size > LARGE_FILE_BYTES)}
    small = sorted(((size, rel) for rel, ignored, size in entries
                    if not ignored and rel not in plain), reverse=True)
    total = sum(size for size, _ in small)
    for size, rel in small:                # the largest leave git first, until the rest fits
        if total <= MAX_BACKUP_DIRTY_BYTES:
            break
        plain.add(rel)
        total -= size
    row["plain"] = sorted(plain)
    # Only a NON-ignored path needs excluding (`add -A` never takes an ignored one, and git refuses
    # a pathspec that names one).
    ignored = {rel for rel, was_ignored, _ in entries if was_ignored}
    pathspec = scratch.parent / f"pathspec-{row['name']}"
    pathspec.write_bytes(b"\x00".join([b"."] + [
        (":(exclude,literal)" + rel).encode("utf-8", "surrogateescape")
        for rel in sorted(plain - ignored)]))
    run_git("-C", str(wt), "add", "-A", "--pathspec-from-file", str(pathspec),
            "--pathspec-file-nul", env_extra=env)
    tree = run_git("-C", str(wt), "write-tree", env_extra=env).stdout.decode().strip()
    if tree == repo.tree(head):
        return
    row["dirty_tree"] = tree
    row["dirty_commit"] = run_git(
        "-C", str(wt), "commit-tree", tree, "-p", head, "-m",
        f"looplab: uncommitted work of agent worktree {row['name']} (mirror backup)",
        env_extra={**env, **_FIXED_COMMIT_ENV}).stdout.decode().strip()


def _copy_plain(wt: Path, out_dir: Path, rows: list) -> dict:
    """Copy each row's `plain` paths to `files/<name>/<path>.v<version>` (only what is not there
    yet), drop every stored file no row wants any more, and return `{rel: path}` for the mirror.
    Rewrites each row's `plain` into `files` (`{path, stored, size, mtime_ns}`) and
    `files_skipped`."""
    import os
    import shutil
    root = out_dir / AGENT_FILES_DIR
    wanted: dict = {}
    count, total = 0, 0
    for row in rows:
        files, skipped = [], []
        base = wt / row["name"]
        for rel in row.pop("plain", None) or ():
            try:
                st = (base / rel).lstat()
            except OSError:
                continue                   # gone since the status: nothing to carry
            if count >= MAX_BACKUP_FILES or total + st.st_size > MAX_BACKUP_FILES_BYTES:
                skipped.append({"path": rel, "reason": "backup_bound"})
                continue
            stored = f"{AGENT_FILES_DIR}/{row['name']}/{rel}.v{_plain_version(st)}"
            dest = out_dir / stored
            if not dest.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_name(dest.name + ".part")
                try:
                    shutil.copyfile(base / rel, tmp)
                    os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
                    os.replace(tmp, dest)
                except OSError:
                    try:
                        tmp.unlink()
                    except OSError:
                        pass               # never created
                    skipped.append({"path": rel, "reason": "unreadable"})
                    continue
            count, total = count + 1, total + st.st_size
            wanted[stored] = dest
            files.append({"path": rel, "stored": stored, "size": st.st_size,
                          "mtime_ns": st.st_mtime_ns})
        row["files"] = files
        if skipped:
            row["files_skipped"] = skipped
    # An older version (or a file the agent deleted) leaves the spool, so it leaves the mirror.
    if root.is_dir():
        for path in sorted(root.rglob("*"), reverse=True):
            rel = path.relative_to(out_dir).as_posix()
            if path.is_file() and rel not in wanted:
                path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
    return {f"{AGENT_BACKUP_REL}/{rel}": path for rel, path in wanted.items()}


def agent_backup(run_dir, out_dir) -> dict:
    """The run mirror's `run_extras` producer: `{rel: path}` of the backup files in `out_dir`.

    Returns `{}` (and removes stale outputs) when there is nothing to back up — no repository, no
    agent branch — and ALSO when the run directory still holds an unconsumed backup a restore put
    there (`code/agent_backup/agents.json`): until `restore_agent_worktrees` re-created the
    worktrees from it, that restored copy IS the backup, and the mirror carries it as a run file."""
    import hashlib
    import os
    from looplab.core.atomicio import atomic_write_bytes
    from looplab.core.jsonutil import canonical_json
    run_dir, out_dir = Path(run_dir), Path(out_dir)
    repo = CodeRepo(run_dir)
    if (run_dir / AGENT_BACKUP_REL / AGENT_BACKUP_INDEX).is_file() or not repo.exists():
        _clear_backup(out_dir)
        return {}
    refs = repo.refs(AGENT_BRANCH_PREFIX)
    if not refs:
        _clear_backup(out_dir)
        return {}
    out_dir.mkdir(parents=True, exist_ok=True)
    bundle, index_path = out_dir / AGENT_BUNDLE, out_dir / AGENT_BACKUP_INDEX
    rows = []
    with tempfile.TemporaryDirectory(prefix=".agent-backup-", dir=str(out_dir)) as tmp:
        scratch = Path(tmp) / "scratch.git"
        run_git("init", "--bare", "--quiet", "--template=", str(scratch))
        (scratch / "objects" / "info").mkdir(parents=True, exist_ok=True)
        (scratch / "objects" / "info" / "alternates").write_text(
            str((repo.path / "objects").resolve()) + "\n", encoding="utf-8")
        for meta in list_worktrees(run_dir):
            name = meta.get("name") if isinstance(meta, dict) else None
            if not isinstance(name, str) or not NAME.match(name):
                continue
            row = {k: meta.get(k) for k in ("name", "branch", "start", "start_commit",
                                            "base_digest")}
            row.update(branch_head=refs.get(f"{AGENT_BRANCH_PREFIX}{name}"), dirty_commit=None)
            wt = run_dir / WORKTREES_REL / name
            if wt.is_dir():
                head = run_git("-C", str(wt), "rev-parse", "--verify", "HEAD",
                               check=False).stdout.decode().strip()
                if head:
                    row["head"] = head
                    # WHICH branch the worktree stands on (None = detached): an agent that
                    # switched to a branch of its own gets it back, not a detached HEAD.
                    row["head_ref"] = run_git("-C", str(wt), "symbolic-ref", "-q", "HEAD",
                                              check=False).stdout.decode().strip() or None
                    _capture_dirty(repo, wt, scratch, head, row)
            rows.append(row)
        extra = {r["head_ref"]: r["head"] for r in rows
                 if (r.get("head_ref") or "").startswith("refs/heads/")
                 and r["head_ref"] not in refs}
        bundle_key = hashlib.sha256(canonical_json(
            {"version": _BACKUP_VERSION, "refs": refs, "extra": extra,
             "worktrees": [{k: v for k, v in r.items() if k != "plain"} for r in rows]}
        )).hexdigest()
        extras = _copy_plain(run_dir / WORKTREES_REL, out_dir, rows)
        index = {"version": _BACKUP_VERSION, "refs": refs, "worktrees": rows,
                 "bundle_key": bundle_key}
        key = hashlib.sha256(canonical_json(index)).hexdigest()
        try:
            prior_index = json.loads(index_path.read_text(encoding="utf-8"))
            prior, prior_bundle = prior_index.get("key"), prior_index.get("bundle_key")
        except (OSError, ValueError, AttributeError):
            prior = prior_bundle = None
        if prior_bundle != bundle_key or not bundle.is_file():
            for ref, sha in sorted({**refs, **extra}.items()):
                run_git("--git-dir", str(scratch), "update-ref", ref, sha)
            for row in rows:
                if row.get("dirty_commit"):
                    run_git("--git-dir", str(scratch), "update-ref",
                            AGENT_DIRTY_REF + row["name"], row["dirty_commit"])
                elif row.get("head") and row["head"] != row.get("branch_head"):
                    # The agent moved the worktree off its branch: carry that commit too.
                    run_git("--git-dir", str(scratch), "update-ref",
                            AGENT_HEAD_REF + row["name"], row["head"])
            # A FULL bundle (the objects behind the alternate are packed in): the history it needs
            # is the run repository's, which a restore rebuilds only for bases and attempts.
            staged = Path(tmp) / AGENT_BUNDLE
            run_git("--git-dir", str(scratch), "bundle", "create", "--quiet", str(staged),
                    "--all", timeout=300)
            os.replace(staged, bundle)
        if prior != key:
            atomic_write_bytes(index_path, json.dumps({**index, "key": key}, indent=1,
                                                      sort_keys=True).encode("utf-8"))
    return {f"{AGENT_BACKUP_REL}/{AGENT_BUNDLE}": bundle,
            f"{AGENT_BACKUP_REL}/{AGENT_BACKUP_INDEX}": index_path, **extras}


# Reported, but nothing a later sync could still do: the worktree IS restored (its commit, its
# files); keeping the backup would only re-block the next one.
_NOT_PENDING = frozenset({"branch_missing", "head_branch_detached", "plain_files_missing"})


def _restore_plain(root: Path, worktree: Path, row: dict) -> list:
    """Lay each backed-up plain file (`files` of the row) back at its path in `worktree` with its
    mtime; returns the paths whose stored copy is not in the backup (the mirror skipped it as over
    `--max-file-mb`, or it was never copied). Never writes outside the worktree."""
    import os
    import shutil
    from looplab.core.pathsafe import contained_member
    missing = []
    for item in row.get("files") or ():
        rel, stored = item.get("path"), item.get("stored")
        src = contained_member(root, stored) if isinstance(stored, str) else None
        dest = contained_member(worktree, rel) if isinstance(rel, str) else None
        if src is None or dest is None:
            continue
        if not src.is_file():
            missing.append(rel)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".looplab-restore")
        shutil.copyfile(src, tmp)
        if isinstance(item.get("mtime_ns"), int):
            os.utime(tmp, ns=(item["mtime_ns"], item["mtime_ns"]))
        os.replace(tmp, dest)
    return missing


def restore_agent_worktrees(run_dir) -> Optional[dict]:
    """Re-create the agent branches and worktrees a restored `code/agent_backup/` carries.

    None when the run holds no backup. Never overwrites: a branch that already exists at ANOTHER
    commit, or a worktree path or `.meta` row already present, is reported under `skipped` and the
    backup is KEPT (`pending: true`), so nothing in it is lost; when every entry is restored (or was
    already in place) the consumed backup is removed and the next mirror pass backs up the live
    worktrees again. Takes the repository lock: it writes refs, like the engine does."""
    import shutil
    run_dir = Path(run_dir)
    root = run_dir / AGENT_BACKUP_REL
    try:
        index = json.loads((root / AGENT_BACKUP_INDEX).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    report: dict = {"refs": [], "restored": [], "skipped": [], "pending": False}
    bundle = root / AGENT_BUNDLE
    if not isinstance(index, dict) or not bundle.is_file():
        report.update(pending=True, skipped=[{"name": "*", "reason": "backup_unreadable"}])
        return report
    repo = CodeRepo(run_dir).ensure()
    with repo.lock():
        listed = {}
        for line in repo.git("bundle", "unbundle", str(bundle), timeout=300).stdout.decode(
                "utf-8", "replace").splitlines():
            sha, _, ref = line.strip().partition(" ")
            if sha and ref:
                listed[ref] = sha
        current = repo.refs(AGENT_BRANCH_PREFIX)
        diverged = set()
        for ref, sha in sorted(listed.items()):
            if not ref.startswith(AGENT_BRANCH_PREFIX):
                continue
            have = current.get(ref)
            if have is None:
                repo.git("update-ref", ref, sha, "")     # "" = create only, never move one
                report["refs"].append(ref)
            elif have != sha:
                diverged.add(ref)
                report["skipped"].append({"name": ref[len(AGENT_BRANCH_PREFIX):],
                                          "reason": "branch_diverged", "kept": have,
                                          "backup": sha})
        repo.git("worktree", "prune")
        for row in index.get("worktrees") or ():
            name = row.get("name") if isinstance(row, dict) else None
            if not isinstance(name, str) or not NAME.match(name):
                continue
            ref = AGENT_BRANCH_PREFIX + name
            path, meta_file = run_dir / WORKTREES_REL / name, _meta_path(run_dir, name)
            if path.exists() and meta_file.exists():
                continue                                    # already in place
            if ref in diverged:
                continue                                    # reported above
            if path.exists() or meta_file.exists() or repo.rev(ref) is None:
                report["skipped"].append({"name": name, "reason": "worktree_path_exists"
                                          if path.exists() or meta_file.exists()
                                          else "branch_missing"})
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            repo.git("worktree", "add", "--quiet", str(path), f"agent/{name}")
            head, dirty = row.get("head"), row.get("dirty_commit")
            head_ref = row.get("head_ref")
            if (head and head_ref and head_ref != ref and head_ref.startswith("refs/heads/")
                    and listed.get(head_ref) == head):
                # The agent had switched to a branch of its own: put THAT branch back (created
                # if missing, never moved), checked out here. A branch that exists at another
                # commit, or is checked out elsewhere, leaves HEAD detached at the backed-up
                # commit and says so — the commit itself is never lost.
                if repo.rev(head_ref) is None:
                    repo.git("update-ref", head_ref, head, "")
                    report["refs"].append(head_ref)
                short = head_ref[len("refs/heads/"):]
                moved = repo.rev(head_ref) != head
                if moved or run_git("-C", str(path), "checkout", "--quiet", short,
                                    check=False).returncode:
                    run_git("-C", str(path), "checkout", "--quiet", "--detach", head)
                    report["skipped"].append({"name": name, "reason": "head_branch_detached",
                                              "branch": short})
            elif head and head != row.get("branch_head") and (
                    listed.get(AGENT_HEAD_REF + name) == head
                    or listed.get(AGENT_DIRTY_REF + name) == dirty):
                # The worktree stood DETACHED (or a pre-v2 backup that did not record where):
                # put HEAD back exactly where it was — detached, as it was.
                run_git("-C", str(path), "checkout", "--quiet", "--detach", head)
            if dirty and listed.get(AGENT_DIRTY_REF + name) == dirty:
                # Switch the files (not HEAD) to the dirty tree — tracked edits, deletions and new
                # files alike — then put the index back at HEAD: the worktree reads as it did.
                run_git("-C", str(path), "read-tree", "-m", "-u", "HEAD", dirty)
                run_git("-C", str(path), "reset", "--quiet")
            missing = _restore_plain(root, path, row)
            if missing:
                report["skipped"].append({"name": name, "reason": "plain_files_missing",
                                          "paths": missing[:20], "count": len(missing)})
            meta = {k: row.get(k) for k in ("name", "branch", "start", "start_commit",
                                            "base_digest")}
            meta.update(path=str(path), restored_from_backup={
                "branch_head": row.get("branch_head"), "dirty_commit": dirty,
                **({"dirty_skipped": row["dirty_skipped"]} if row.get("dirty_skipped") else {}),
                **({"files_skipped": row["files_skipped"]} if row.get("files_skipped") else {})})
            meta_file.parent.mkdir(parents=True, exist_ok=True)
            meta_file.write_text(json.dumps(meta, indent=1), encoding="utf-8")
            report["restored"].append(name)
    # Pending only on a CONFLICT (something here the backup would overwrite): a worktree row whose
    # branch was already gone when it was backed up has nothing to restore and holds nothing back.
    report["pending"] = any(s["reason"] not in _NOT_PENDING for s in report["skipped"])
    if not report["pending"]:
        shutil.rmtree(root, ignore_errors=True)
    return report
