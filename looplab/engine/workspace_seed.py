"""The candidate-workspace materialization rule: four primitives, and the ORDER they run in.

The engine's ``WorkspaceSeeder`` owns tracing and domain events.  This module owns the four
side-effecting primitives underneath it — and, since 2026-08-19, ``seed_candidate_workspace``, the
ORDER they are driven in — so a second candidate-workspace consumer cannot quietly acquire a
different definition of ``seed_mode``, protected files, input mounts or of the sequence itself.
They live beside the engine workspace policy: ``runtime`` is reserved for child-process execution
mechanics, while an agent-facing tool may depend on engine domain services when it needs them.

THE ORDER IS THE SAFETY ARGUMENT, not an implementation detail, which is why it is shared rather
than described: the shadow guard has to run AFTER the editables are materialized (only a real tree
can shadow a mount) and BEFORE the protected files (a `protect` entry like ``datasets/labels.csv``
would otherwise manufacture a top-level ``datasets/`` and raise a FALSE collision), and the
protected files have to land BEFORE the mounts (afterwards, the same entry writes THROUGH a
read-only mount symlink into the operator's original data).  ``tools/dev_commands.py`` re-derived
that whole sequence by hand until 2026-08-19, so the next fix to it would have landed in one body.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Callable


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


# The names a candidate tree never carries over from the operator's source: VCS metadata, python
# bytecode and the two dependency trees a repo checkout routinely holds. One spelling because the
# engine's eval workspace and the Developer's disposable candidate must contain the same repo — a
# tool that seeded `.venv` and an eval that did not would answer different questions about one edit.
IGNORE_NAMES = (".git", "__pycache__", "*.pyc", ".venv", "node_modules")


def candidate_ignore():
    """The `shutil.copytree` ignore callable for `IGNORE_NAMES`."""
    return shutil.ignore_patterns(*IGNORE_NAMES)


class MountCollision(RuntimeError):
    """A declared data/reference mount whose name a top-level entry of the seeded ROOT editable
    already occupies.

    A TYPE rather than two hand-written messages, because both raisers needed the same sentence and
    the reason it must be LOUD is subtle: `link_input`/`copy_input` are idempotent, so they SKIP a
    destination that already exists, and their `dst.exists()` guard cannot tell a repo file from a
    resumed mount. Left unraised, the eval reads the repo's placeholder instead of the declared
    source while the workspace receipt claims the mount succeeded.
    """

    def __init__(self, name: str, root_path: str):
        self.name = str(name)
        self.root_path = str(root_path)
        super().__init__(
            f"mount name {self.name!r} collides with a top-level entry of the root repo "
            f"({self.root_path}): the repo is seeded at the workspace root first, so the "
            f"mount would be silently shadowed and the eval would read the repo's copy "
            f"instead of the declared source. Rename the mount or the repo entry.")


@dataclass(frozen=True)
class SeedOps:
    """The four primitives `seed_candidate_workspace` drives, injectable as a bundle.

    An UNSET slot means "this module's function", and it stays unset rather than being defaulted to
    the function OBJECT — a dataclass default would bind at import and make
    `monkeypatch.setattr(workspace_seed, "seed_repo_tree", …)` resolve and reach nothing, which is
    the same "patch resolves but reaches nothing" shape `link_input`'s own docstring records and
    which `tests/test_dev_commands.py` caught the moment this bundle was introduced.

    The ENGINE fills all four, and that is not decoration either: `WorkspaceSeeder`'s delegators are
    documented patch seams (`Engine._seed_repo_tree`, `Engine._link_input`, and `copy_input`, whose
    override must reach BOTH the `mount:false` copy and `link_input`'s symlink-failure fallback).
    """

    seed_repo_tree: "Callable | None" = None
    seed_protected_files: "Callable | None" = None
    link_input: "Callable | None" = None
    copy_input: "Callable | None" = None


def seed_candidate_workspace(repo_spec, workdir, *, seed_mode: str = "auto", ignore=None,
                             ops: "SeedOps | None" = None) -> list[dict]:
    """Materialize `repo_spec` into `workdir` in the ONE order (module docstring), and RECEIPT it.

    Returns one row per thing materialized, in the order it happened — the ingredients each caller
    renders its own way rather than a rendered string, because the two callers report to different
    audiences (a `workspace_seeded` event for the operator, a `seed=` line in a tool result for the
    model) and a shared sentence would have made the receipt the reason they diverge again:

        {"kind": "editable",  "name", "mode", "count"}          name AS DECLARED; count -1 == copytree
        {"kind": "protected", "name", "files"}                  only when something was protected
        {"kind": "reference", "name", "source", "action", "symlink", "read_only"}
        {"kind": "data",      "name", "source", "action", "symlink", "read_only"}

    `symlink` is observed AFTER the input is materialized, because `link_input` falls back to a COPY
    (geesefs flattens symlinks) and a Docker bind list built from the DECLARATION rather than from
    the result would bind a path the candidate is not actually reading through.

    Raises `MountCollision`; see that class for what it prevents. Everything the two callers do NOT
    share — the tracing span, the domain event, the overlay of the Developer's staged edits — stays
    with them, and the overlay in particular MUST: an engine workspace gets the node's files from
    `_write_node_files`, so applying them here would give an eval a second, earlier writer.
    """
    ops = ops or SeedOps()
    # Every unset primitive is looked up HERE, by module-global name, so a patch of this module is
    # still what runs (see `SeedOps`).
    _seed_tree = ops.seed_repo_tree or seed_repo_tree
    _seed_protected = ops.seed_protected_files or seed_protected_files
    _link = ops.link_input or link_input
    _copy = ops.copy_input or copy_input
    ignore = candidate_ignore() if ignore is None else ignore
    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    editables = list(repo_spec.get("editables") or [])
    references = list(repo_spec.get("references") or [])
    data = dict(repo_spec.get("data") or {})
    rows: list[dict] = []

    def _target(editable):
        return work if editable.get("name") in (".", "") else work / editable["name"]

    for editable in editables:
        mode = editable.get("seed_mode") or seed_mode or "auto"
        count = _seed_tree(editable["path"], _target(editable), ignore, mode)
        # The name is the DECLARED one, verbatim: each caller renders it its own way (the engine
        # prints it raw into `workspace_seeded`, the tool normalizes a root "" to "."), and a
        # normalization here would silently rewrite one of two existing receipts.
        rows.append({"kind": "editable", "name": editable.get("name"), "mode": mode,
                     "count": count})

    # Fail loud on a data/reference mount name that collides with a top-level entry of the ROOT
    # editable (name "."/"" — seeded at the workspace root). The root repo is materialized FIRST,
    # so the mount's dst (`wd/<name>`) is already occupied and link_input/copy_input silently skip
    # it — their `dst.exists()` idempotency guard can't tell a repo file from a resumed mount — so
    # the eval reads the repo's placeholder instead of the declared source AND the WORKSPACE_SEEDED
    # record falsely claims the mount succeeded, silently invalidating the whole run's metrics.
    # (Non-root editables mount at `wd/<name>`, already guarded against name collisions at task
    # build.) The check runs AFTER the editables are seeded and reads the REAL workspace, not the
    # source listdir, because only the materialized tree can actually shadow a mount. Reading the
    # source instead produced two false RuntimeErrors that aborted every node of a valid task:
    #   * `seed_mode="auto"` copies only git-TRACKED files, so a gitignored top-level `data/` —
    #     the standard layout, datasets are never committed — was "shadowing" a `data:` mount that
    #     in fact seeded fine;
    #   * `_mounts` listed EVERY reference, but only `ref.get("mount")` ones are materialized
    #     below, so a context-only reference collided with nothing yet still raised.
    # Post-seed `dst.exists()` is exactly the condition link_input/copy_input silently skip on, so
    # it has no false positives by construction and stays correct across resume.
    mounts = [name for name in ([r["name"] for r in references if r.get("mount")] + list(data))
              if name]
    root_editable = next((e for e in editables if e.get("name") in (".", "")), None)
    if root_editable is not None:
        clash = next((name for name in mounts if (work / name).exists()), None)
        if clash is not None:
            raise MountCollision(clash, root_editable.get("path", ""))

    # The operator's PROTECTED files, materialized whatever the seed mode says (see
    # `seed_protected_files`). This position between the shadow guard and the mounts IS the
    # safety argument: BEFORE the guard, a protect entry like `datasets/labels.csv` would
    # manufacture a top-level `datasets/` and raise a FALSE collision that aborts every node of
    # a valid task; AFTER the mounts, the same entry would write THROUGH the read-only mount
    # symlink into the operator's original data. `reserved_top` covers the residue for the ROOT
    # editable (whose files land at the workspace root, where the mounts also live); a non-root
    # editable mounts under its own subdir, which `_names_distinct_and_safe` already keeps
    # disjoint from every mount name.
    for editable in editables:
        protected = _seed_protected(
            editable["path"], _target(editable), editable.get("protect"),
            reserved_top=(set(mounts) if editable.get("name") in (".", "") else set()))
        if protected:
            rows.append({"kind": "protected", "name": editable.get("name"),
                         "files": list(protected)})

    for ref in references:
        if not ref.get("mount"):                 # context-only reference: nothing is materialized
            continue
        # a runtime dependency -> symlink read-only input
        _link(ref["path"], work / ref["name"])
        rows.append({"kind": "reference", "name": ref["name"], "source": ref["path"],
                     "action": "link", "symlink": (work / ref["name"]).is_symlink(),
                     "read_only": True})
    for name, raw in data.items():
        # A DataSpec {path, mount, edit, …}; a bare string path is back-compat (all defaults).
        spec = raw if isinstance(raw, dict) else {"path": raw, "mount": True, "edit": False}
        dst = work / name
        if spec.get("mount", True):
            _link(spec["path"], dst)
            rows.append({"kind": "data", "name": name, "source": spec["path"], "action": "link",
                         "symlink": dst.is_symlink(), "read_only": not spec.get("edit", False)})
        else:                                    # copy INTO the candidate (editable if edit=true)
            _copy(spec["path"], dst, ignore)
            rows.append({"kind": "data", "name": name, "source": spec["path"], "action": "copy",
                         "symlink": False, "read_only": not spec.get("edit", False)})
    return rows
