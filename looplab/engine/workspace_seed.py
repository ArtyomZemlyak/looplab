"""Pure filesystem primitives shared by evaluation and disposable Developer commands.

The engine's ``WorkspaceSeeder`` owns orchestration, tracing and domain events.  This module owns
the four side-effecting primitives underneath it so a second candidate-workspace consumer cannot
quietly acquire a different definition of ``seed_mode``, protected files or input mounts.
They live beside the engine workspace policy: ``runtime`` is reserved for child-process execution
mechanics, while an agent-facing tool may depend on engine domain services when it needs them.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys


def seed_repo_tree(src, dst, ignore, mode: str = "auto") -> int:
    """Materialize an editable repo's *source* into a candidate under a seeding ``mode``:

    - ``auto`` (default) / ``tracked``: copy git-tracked files so a tree bloated with untracked
      artifacts is not deep-copied. Both fall back to a full copy outside a git worktree.
    - ``all``: force a full recursive copy.

    Returns the number of tracked files copied, or ``-1`` for a full copytree.
    """
    from looplab.runtime.sandbox import git_subprocess_env

    src = Path(src)
    dst = Path(dst)
    tracked = None
    if mode != "all":
        # Ask git directly (no `.git`-at-root check): the editable repo is often a SUBDIR of a
        # larger git repo whose `.git` lives in a parent, so `(src/'.git').exists()` is False even
        # though `git -C src ls-files` correctly lists the files tracked under src. Use it whenever
        # git returns a non-empty tracked set; otherwise (non-git / nothing tracked) fall back.
        try:
            out = subprocess.run(["git", "-C", str(src), "ls-files", "-z"],
                                 capture_output=True, text=True, timeout=120,
                                 env=git_subprocess_env())
            if out.returncode == 0:
                files = [p for p in out.stdout.split("\0") if p]
                if files:
                    tracked = files
        except Exception:  # noqa: BLE001 - git missing / not a repo -> copytree fallback
            tracked = None
    if tracked is None:
        shutil.copytree(src, dst, dirs_exist_ok=True, ignore=ignore)
        return -1
    dst.mkdir(parents=True, exist_ok=True)
    copied = 0
    for rel in tracked:
        source = src / rel
        if source.is_dir() or not source.exists():     # submodule dir / deleted-but-tracked path
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        # Follow a tracked symlink into the candidate instead of preserving an escape from scratch.
        shutil.copy2(source, target, follow_symlinks=True)
        copied += 1
    return copied


def seed_protected_files(src, dst, protect, *, reserved_top=()) -> list[str]:
    """Materialize operator-protected files regardless of the source seeding mode.

    Deliberately takes the per-editable protect list rather than the derived protected-name set:
    the latter also contains data mounts and output files, neither of which should be copied from
    source. Tree entries (``dir/**``) are skipped before glob expansion. A match resolving outside
    the source or a destination resolving outside the candidate is dropped. ``reserved_top`` keeps
    a protected source path from shadowing a declared data/reference mount.
    """
    src = Path(src)
    dst = Path(dst)
    try:
        root = src.resolve()
        base = dst.resolve()
    except OSError:                              # unreadable source/destination -> nothing to seed
        return []
    out: list[str] = []
    seen: set[str] = set()
    for entry in (protect or []):
        rel = str(entry).replace("\\", "/").strip()
        while rel.startswith("./"):
            rel = rel[2:]
        if not rel or rel in (".", "/") or rel.endswith("/**"):
            continue
        # `Path.glob` on a pattern with no magic still works, but it silently yields NOTHING for a
        # pattern carrying a `..` segment, so a literal name is resolved directly and then checked
        # against the root — an escape is dropped, not quietly ignored as "no match".
        matches = sorted(root.glob(rel)) if any(c in rel for c in "*?[") else [root / rel]
        for match in matches:
            try:
                real = match.resolve()
                relpath = real.relative_to(root)    # never copy from OUTSIDE the editable source
            except (OSError, ValueError):
                continue
            if not real.is_file():                  # missing entry / directory has nothing to copy
                continue
            key = relpath.as_posix()
            if key in seen or relpath.parts[0] in tuple(reserved_top):
                continue
            target = dst / relpath
            try:                                    # …and never WRITE outside the candidate
                target.resolve().relative_to(base)
            except (OSError, ValueError):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(real, target)
            seen.add(key)
            out.append(key)
    return out


def copy_input(src, dst, ignore=None) -> None:
    """The one copy-in path for data/reference sources.

    Idempotent. For a directory, try a CoW clone first; without filesystem support this falls back
    to a normal recursive copy. Edits to either representation stay candidate-local.
    """
    src = Path(src)
    dst = Path(dst)
    if dst.is_symlink() or dst.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        if sys.platform != "win32" and shutil.which("cp"):
            result = subprocess.run(
                ["cp", "-R", "--reflink=always", "--", str(src), str(dst)],
                capture_output=True)
            if result.returncode == 0:
                return
            shutil.rmtree(dst, ignore_errors=True)   # discard a partial clone before fallback
        shutil.copytree(src, dst, dirs_exist_ok=True, ignore=ignore)
    elif src.is_file():
        shutil.copy2(src, dst)


def link_input(src, dst, copy_fallback=None) -> None:
    """Expose a large task input by symlink, falling back to a COPY.

    `copy_fallback` is the seam, and it is not decoration. Before this rule was extracted out of
    `workspace.py` the fallback read `self.copy_input(src, dst)` — instance dispatch — so a
    subclass override or a monkeypatch of `WorkspaceSeeder.copy_input` covered it. As a direct
    module-level call such a patch still intercepts the `mount:false` copy branch in
    `seed_workspace` and silently stops reaching THIS one: the "patch resolves but reaches nothing"
    shape the repo's seam rules exist to prevent, and invisible until a symlink actually fails at
    runtime — on geesefs, where symlinks flatten, that is not a hypothetical path.

    `WorkspaceSeeder.link_input` passes `self.copy_input`, so the two branches go through one
    dispatch again; a caller that passes nothing gets the module function, which is what every
    non-seeder caller wants.
    """
    src = Path(src)
    dst = Path(dst)
    if dst.is_symlink() or dst.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(src, dst, target_is_directory=src.is_dir())
    except OSError:
        (copy_fallback or copy_input)(src, dst)
