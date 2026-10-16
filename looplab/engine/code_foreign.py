"""Bring-your-own branch: a commit of ANOTHER local git repository as an `inject_node` overlay.

WHY (2026-10-16). An external agent rarely starts in the run's worktree: Claude Code makes its own
worktree of the OPERATOR's repository (`<repo>/.claude/worktrees/agent-<id>`, branch
`worktree-agent-<id>`), Codex edits a clone. Before this, the only way from such a branch into the
run was to copy files into `files=` or the run's own worktree — the copy is where the night of
2026-10-13 lost its fixes (doc 79 §1). `code_from: {"git": {"repo": <path>, "ref": <branch|commit>}}`
takes the branch itself.

WHAT IT COMPUTES. The candidate is a THREE-WAY merge of the agent's branch onto the run's ACTIVE
BASE, never "the branch's tree": the base holds the operator's uncommitted edits and untracked
protected files the seed copied (doc 37 §5d), which the agent's branch — cut from a commit — does not
have, so taking its tree would silently REVERT them. The merge base is where the branch left the
source: `merge-base(branch, the source commit the run recorded at start)`
(`run_started.workspace`, `code_export.py::_recorded_source_commit`), else the source's current
HEAD. Overlapping edits come back as NAMED conflicts (`code_from_merge_conflict`, 409, the paths),
never a side picked. The merged tree then becomes the overlay through the SAME rules as a worktree
(`code_worktree.py::_overlay_between`: edit surface, protected names, size bounds, binary blobs).

HOW, and why it writes nothing outside the run. Everything that runs in the foreign repository or
the operator's source is READ-ONLY: `rev-parse` to resolve the ref, and `git fetch` FROM it (its
`upload-pack` writes nothing) into the run's PRIVATE repository under `refs/looplab/foreign/<sha>`
— the commit's own id, so a repeated preview re-uses it. Never a write to the foreign repository's
refs, objects, config, index or hooks (`tests/test_code_identity_agent.py` checks all five). The
editable may be a SUBDIRECTORY of its repository, so the three trees are taken at that prefix
(`git rev-parse --show-prefix` in the source) and re-rooted as synthetic commits on one common
parent, which lets `git merge-tree --write-tree` (git >= 2.38) find the merge base itself; older
git is refused (`code_from_git_unsupported`) rather than approximated.

The branch's start is usually NOT the run's base: the response names how many paths differ between
the two (`base_drift`) — "your branch lacks N uncommitted files of the source" — because those are
merged in for the evaluation but were absent wherever the agent tested its branch.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from looplab.engine.code_repo import CodeRepo, CodeRepoError, run_git, safe_path
from looplab.engine.code_worktree import OverlayRefusal, _FIXED_COMMIT_ENV, _overlay_between

MIN_GIT = (2, 38)
FOREIGN_REF = "refs/looplab/foreign/"
_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
MAX_DRIFT_LISTED = 20


class OverlayConflict(OverlayRefusal):
    """The three-way merge has conflicts; `paths` names them."""

    def __init__(self, code: str, message: str, paths: list):
        super().__init__(code, message)
        self.paths = paths


def git_version() -> Optional[tuple]:
    """`(major, minor)` of the git on PATH, None when it cannot be read. A seam: tests drive the
    refusal below by patching this, never by installing an old git."""
    try:
        out = run_git("--version", check=False)
    except CodeRepoError:
        return None
    m = re.search(rb"(\d+)\.(\d+)", out.stdout or b"")
    return (int(m.group(1)), int(m.group(2))) if m else None


def _require_git() -> None:
    have = git_version()
    if have is None or have < MIN_GIT:
        raise OverlayRefusal(
            "code_from_git_unsupported",
            f"code_from git needs git >= {MIN_GIT[0]}.{MIN_GIT[1]} (merge-tree --write-tree); this "
            f"server has {'.'.join(map(str, have)) if have else 'no readable git'} — submit a "
            "patch (`git diff <start>..<branch>`) or a worktree instead")


def _spec(git) -> tuple:
    if not isinstance(git, dict) or not {"repo", "ref"} <= set(git) or set(git) - {"repo", "ref",
                                                                                   "prefix"}:
        raise OverlayRefusal("code_from_invalid",
                             'code_from git must be {"repo": "<absolute path>", "ref": '
                             '"<branch|commit>", "prefix"?: "<editable dir in that repo>"}')
    repo, ref, prefix = git.get("repo"), git.get("ref"), git.get("prefix")
    if not isinstance(repo, str) or not repo or len(repo) > 4096 or not Path(repo).is_absolute():
        raise OverlayRefusal("code_from_git_repo", "git.repo must be an absolute local path")
    path = Path(repo)
    if not path.is_dir():
        raise OverlayRefusal("code_from_git_repo", f"git.repo {repo[:200]} is not a directory")
    # A ref the `--` cannot guard (it is a revision, not a path): no option spelling, no range.
    if (not isinstance(ref, str) or not ref or len(ref) > 256 or ref.startswith("-")
            or ".." in ref or any(c in ref for c in " \t\n:~^?*[\\")):
        raise OverlayRefusal("code_from_git_ref", "git.ref must be one branch name or commit id")
    if prefix is not None and (not isinstance(prefix, str)
                               or (prefix.strip("/") and safe_path(prefix.strip("/")) is None)):
        raise OverlayRefusal("code_from_git_prefix", "git.prefix must be a relative directory")
    return path, ref, None if prefix is None else prefix.strip("/")


def _resolve(repo_path: Path, ref: str) -> str:
    out = run_git("-C", str(repo_path), "--no-optional-locks", "rev-parse", "--verify", "--quiet",
                  "--end-of-options", ref + "^{commit}", check=False)
    sha = out.stdout.decode().strip()
    if out.returncode != 0 or not _SHA.match(sha):
        raise OverlayRefusal("code_from_git_ref", f"{ref[:120]!r} is not a commit of {repo_path}")
    return sha


def _fetch(repo: CodeRepo, source: Path, sha: str) -> bool:
    """Fetch commit `sha` (and its history) FROM `source` into `refs/looplab/foreign/<sha>`.
    Read-only on `source`; True when the commit is now in the run repository."""
    if repo.rev(FOREIGN_REF + sha) == sha:
        return True
    res = repo.git("fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                   "--no-recurse-submodules", "--no-auto-maintenance", str(source),
                   f"+{sha}:{FOREIGN_REF}{sha}", check=False, timeout=600)
    return res.returncode == 0 and repo.rev(FOREIGN_REF + sha) == sha


def _source_info(run_dir: Path) -> tuple:
    """`(source dir or None, prefix in its repository, anchors)` — the root editable's operator
    source, where it sits inside its git repository, and the commits a merge base is sought from
    (the recorded start commit first, then the source's HEAD now)."""
    from looplab.engine.code_export import _editable_sources, _recorded_source_commit
    from looplab.engine.code_identity import _read_events, _run_meta
    _, started = _run_meta(_read_events(run_dir))
    workspace = started.get("workspace") if isinstance(started.get("workspace"), dict) else {}
    anchors = [c for c in (_recorded_source_commit(workspace),) if c]
    src = next((Path(s).expanduser() for name, s in _editable_sources(run_dir) or ()
                if name in (".", "") and s), None)
    prefix = ""
    if src is not None and src.is_dir():
        out = run_git("-C", str(src), "--no-optional-locks", "rev-parse", "--show-prefix",
                      check=False)
        if out.returncode == 0:
            prefix = out.stdout.decode("utf-8", "replace").strip().strip("/")
            head = run_git("-C", str(src), "--no-optional-locks", "rev-parse", "--verify",
                           "--quiet", "HEAD", check=False).stdout.decode().strip()
            if _SHA.match(head) and head not in anchors:
                anchors.append(head)
    return src, prefix, anchors


def _tree_at(repo: CodeRepo, commit: str, prefix: str) -> Optional[str]:
    spec = f"{commit}:{prefix}" if prefix else f"{commit}^{{tree}}"
    out = repo.git("rev-parse", "--verify", "--quiet", spec, check=False)
    sha = out.stdout.decode().strip()
    if out.returncode != 0 or not sha:
        return None
    kind = repo.git("cat-file", "-t", sha, check=False).stdout.decode().strip()
    return sha if kind == "tree" else None


def _commit(repo: CodeRepo, tree: str, parent: Optional[str], message: str) -> str:
    argv = ["commit-tree", tree, "-m", message] + (["-p", parent] if parent else [])
    return repo.git(*argv, env_extra=_FIXED_COMMIT_ENV).stdout.decode().strip()


def overlay_from_git(run_dir, git, **kw) -> dict:
    """The overlay a foreign repository's commit becomes, merged onto the run's active base."""
    from looplab.engine.code_identity import BASE_BRANCH, sync_run
    _require_git()
    foreign_path, ref, prefix_override = _spec(git)
    run_dir = Path(run_dir)
    sync_run(run_dir)
    repo = CodeRepo(run_dir)
    base = repo.rev(BASE_BRANCH) if repo.exists() else None
    if base is None:
        raise OverlayRefusal("code_repo_missing", "this run has no code repository yet (no seed)")
    sha = _resolve(foreign_path, ref)
    src, prefix, anchors = _source_info(run_dir)
    if prefix_override is not None:
        prefix = prefix_override
    with repo.lock():
        if not _fetch(repo, foreign_path, sha):
            raise OverlayRefusal("code_from_git_fetch_failed",
                                 f"could not fetch {sha[:12]} from {foreign_path} (read-only fetch)")
        merge_base = None
        for anchor in anchors:
            if not (_fetch(repo, foreign_path, anchor)
                    or (src is not None and _fetch(repo, src, anchor))):
                continue
            out = repo.git("merge-base", sha, anchor, check=False)
            merge_base = out.stdout.decode().strip() if out.returncode == 0 else None
            if merge_base:
                break
        if not merge_base:
            raise OverlayRefusal(
                "code_from_git_no_merge_base",
                "the branch shares no history with the commit the run's source started from "
                "(or the source is not in git); submit a patch or use the run's worktree")
        trees = {"start": _tree_at(repo, merge_base, prefix), "branch": _tree_at(repo, sha, prefix)}
        if None in trees.values():
            raise OverlayRefusal("code_from_git_prefix",
                                 f"the editable directory {prefix or '(root)'!r} is not in the "
                                 "branch's tree; pass git.prefix")
        # Three commits on ONE synthetic root, so merge-tree's own merge-base IS the branch's
        # start, at the editable's prefix (no `--merge-base` option, which needs git 2.40).
        root = _commit(repo, trees["start"], None, "looplab: foreign branch start")
        ours = _commit(repo, repo.tree(base), root, "looplab: run base")
        theirs = _commit(repo, trees["branch"], root, f"looplab: foreign {sha}")
        res = repo.git("merge-tree", "--write-tree", "-z", "--name-only", "--no-messages",
                       ours, theirs, check=False)
    parts = res.stdout.decode("utf-8", "replace").split("\x00")
    if res.returncode == 1:
        paths = sorted({p for p in parts[1:] if p})
        raise OverlayConflict("code_from_merge_conflict",
                              "the branch and the run's base both changed: " + ", ".join(paths[:10])
                              + " — merge the base into your branch (the run's base is branch "
                              "`base` of RUN_DIR/code/repo.git) or resolve these paths, then resubmit",
                              paths[:200])
    if res.returncode != 0 or not parts or not parts[0]:
        tail = res.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
        raise CodeRepoError("git_failed", f"git merge-tree failed: {tail[0][:300]}")
    out = _overlay_between(repo, run_dir, base, parts[0], **kw)
    drift = repo.changed(root, ours)
    out["foreign"] = {"repo": str(foreign_path), "ref": ref, "commit": sha,
                      "fetched_ref": FOREIGN_REF + sha, "merge_base": merge_base, "prefix": prefix,
                      "base_drift": {"count": len(drift),
                                     "paths": [r["path"] for r in drift[:MAX_DRIFT_LISTED]]}}
    out["stale_base"] = False                       # merged onto the CURRENT base by construction
    if drift:
        out.setdefault("warnings", []).append(
            f"your branch lacks {len(drift)} uncommitted file(s) of the source: the run's base "
            f"differs from your branch's start in {', '.join(r['path'] for r in drift[:5])}"
            f"{' …' if len(drift) > 5 else ''}. They are merged into this candidate, but your "
            "branch was tested without them")
    return out


__all__ = ["FOREIGN_REF", "MIN_GIT", "OverlayConflict", "git_version", "overlay_from_git"]
