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
(`code_origin.py::run_source`, the reading the base parent and the export share), else the source's
current HEAD. Overlapping edits come back as NAMED conflicts (`code_from_merge_conflict`, 409, the
paths), never a side picked. The merged tree then becomes the overlay through the SAME rules as a
worktree (`code_worktree.py::_overlay_between`: edit surface, protected names, size bounds, binary
blobs).

WHERE THE MERGE BASE IS FOUND (review 2026-10-16). In the FOREIGN repository itself when it holds
the start commit (a Claude Code worktree shares the operator's object database; a clone made after
the run started holds it), read-only — and then only TWO commits travel into the run repository, at
depth 1: the branch tip and the merge base, whose trees are all the merge reads. It used to fetch
the branch's whole history (no depth, a 600 s timeout) while holding the run repository's lock, and
to look for the merge base in the run repository, which version 2 made SHALLOW (the operator's
history is fetched to `HISTORY_DEPTH`): a branch that forked before that cut was refused
`code_from_git_no_merge_base` although the histories share an ancestor. Only when the foreign
repository lacks the start commit is the merge base sought in the run repository, by a BOUNDED
deepening (`FOREIGN_DEEPEN_ROUNDS` x `HISTORY_DEPTH` commits on each side), and the refusal names
that bound.

HOW, and why it writes nothing outside the run. Everything that runs in the foreign repository or
the operator's source is READ-ONLY (`code_repo.py::readonly_git`, `CodeRepo.fetch_readonly`):
`rev-parse` / `merge-base` there, and `git fetch` FROM it (its `upload-pack` writes nothing) into
the run's PRIVATE repository under `refs/looplab/foreign/<sha>` — the commit's own id, so a repeated
preview re-uses it. Never a write to the foreign repository's refs, objects, config, index or hooks
(`tests/test_code_identity_agent.py` checks all five). `git.repo` may name any directory INSIDE a
repository: the fetch reads its top (a subdirectory used to resolve, then fail the fetch as
`code_from_git_fetch_failed`). None of this holds the run repository's lock: git writes refs and the
shallow file under its own lockfiles, and the lock exists for LoopLab's multi-step base/attempt
writes (`code_identity.py::record_attempt`). The editable may be a SUBDIRECTORY of its repository,
so the three trees are taken at that prefix (`git rev-parse --show-prefix` in the source) and
re-rooted as synthetic commits on one common parent, which lets `git merge-tree --write-tree`
(git >= 2.38) find the merge base itself; older git is refused (`code_from_git_unsupported`) rather
than approximated.

The branch's start is usually NOT the run's base: the response names how many paths differ between
the two (`base_drift`) — "your branch lacks N uncommitted files of the source" — because those are
merged in for the evaluation but were absent wherever the agent tested its branch.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from looplab.engine.code_repo import (CodeRepo, CodeRepoError, is_dubious_ownership, is_object_id,
                                      readonly_git, run_git, safe_path)
from looplab.engine.code_worktree import OverlayRefusal, _overlay_between

MIN_GIT = (2, 38)
FOREIGN_REF = "refs/looplab/foreign/"
MAX_DRIFT_LISTED = 20
# The bounded search for a merge base IN the run repository (only when the foreign repository does
# not hold the start commit): this many `--deepen=HISTORY_DEPTH` rounds on each side.
FOREIGN_DEEPEN_ROUNDS = 4
FETCH_TIMEOUT_S = 120.0


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


def _repository_root(path: Path) -> Path:
    """The directory to fetch FROM for `git.repo`: its work tree's TOP (a subdirectory resolved
    and then failed the fetch, misreported as `code_from_git_fetch_failed`), or the git directory
    of a bare repository. Read-only."""
    top = readonly_git(path, "rev-parse", "--show-toplevel", check=False)
    if top.returncode == 0 and top.stdout.strip():
        return Path(top.stdout.decode("utf-8", "replace").strip())
    bare = readonly_git(path, "rev-parse", "--is-bare-repository", "--absolute-git-dir",
                        check=False)
    lines = bare.stdout.decode("utf-8", "replace").split("\n")
    if bare.returncode == 0 and lines[0].strip() == "true" and len(lines) > 1 and lines[1].strip():
        return Path(lines[1].strip())
    if is_dubious_ownership(top):
        raise OverlayRefusal("code_from_git_repo", f"git.repo {str(path)[:200]} is owned by another "
                                                   "user and git refuses to read it")
    raise OverlayRefusal("code_from_git_repo", f"git.repo {str(path)[:200]} is not inside a git "
                                               "repository")


def _resolve(repo_path: Path, ref: str) -> str:
    out = readonly_git(repo_path, "rev-parse", "--verify", "--quiet", "--end-of-options",
                       ref + "^{commit}", check=False)
    sha = out.stdout.decode().strip()
    if out.returncode != 0 or not is_object_id(sha):
        raise OverlayRefusal("code_from_git_ref", f"{ref[:120]!r} is not a commit of {repo_path}")
    return sha


def _has_commit(repo_path, sha: str) -> bool:
    return readonly_git(repo_path, "cat-file", "-e", sha + "^{commit}",
                        check=False).returncode == 0


def _ensure_commit(repo: CodeRepo, sources, sha: str, ref: Optional[str] = None) -> bool:
    """Make commit `sha` and its tree present in the run repository, fetched at depth 1 from the
    first source that serves it; True when it is here. A commit already here is NEVER fetched
    with a depth: that rewrites it as a shallow cut and truncates the history behind it
    (measured, git 2.43)."""
    if not repo.has_object(sha + "^{tree}"):
        for src in sources:
            if src is None:
                continue
            spec = f"+{sha}:{ref}" if ref else sha
            res = repo.fetch_readonly(src, [spec], depth=1, timeout=FETCH_TIMEOUT_S, check=False)
            if res.returncode == 0 and repo.has_object(sha + "^{tree}"):
                break
        else:
            return False
    if ref and repo.rev(ref) != sha:
        repo.update_ref(ref, sha)
    return True


def _source_info(run_dir: Path) -> tuple:
    """`(source dir or None, prefix in its repository, anchors)` — the root editable's operator
    source (`code_origin.py::run_source`), where it sits inside its git repository, and the commits
    a merge base is sought from (the recorded start commit first, then the source's HEAD now)."""
    from looplab.engine.code_identity import _read_events
    from looplab.engine.code_origin import run_source
    src_text, recorded = run_source(_read_events(run_dir), run_dir)
    anchors = [recorded] if recorded else []
    src = Path(src_text) if src_text else None
    prefix = ""
    if src is not None and src.is_dir():
        out = readonly_git(src, "rev-parse", "--show-prefix", check=False)
        if out.returncode == 0:
            prefix = out.stdout.decode("utf-8", "replace").strip().strip("/")
            head = readonly_git(src, "rev-parse", "--verify", "--quiet", "HEAD",
                                check=False).stdout.decode().strip()
            if is_object_id(head) and head not in anchors:
                anchors.append(head)
    return src, prefix, anchors


def _merge_base_in(where, sha: str, anchor: str) -> Optional[str]:
    out = readonly_git(where, "merge-base", sha, anchor, check=False)
    mb = out.stdout.decode().strip()
    return mb if out.returncode == 0 and is_object_id(mb) else None


def _merge_base(repo: CodeRepo, foreign: Path, src: Optional[Path], sha: str,
                anchors: list) -> tuple:
    """`(merge base, the repository it was found in)` or `(None, None)`.

    First in a repository that holds BOTH commits, read-only — the foreign one, then the operator's
    source (a worktree branch shares its object database) — so no history has to travel. Else in
    the run repository, deepened `HISTORY_DEPTH` at a time on both sides for at most
    `FOREIGN_DEEPEN_ROUNDS` rounds."""
    for anchor in anchors:
        for where in (foreign, src):
            if where is not None and _has_commit(where, sha) and _has_commit(where, anchor):
                mb = _merge_base_in(where, sha, anchor)
                if mb:
                    return mb, where
    from looplab.engine.code_origin import HISTORY_DEPTH
    for anchor in anchors:
        sources = [foreign, src]
        if not repo.has_object(sha + "^{commit}"):
            repo.fetch_readonly(foreign, [f"+{sha}:{FOREIGN_REF}{sha}"], depth=HISTORY_DEPTH,
                                timeout=FETCH_TIMEOUT_S, check=False)
        if not repo.has_object(anchor + "^{commit}"):
            for where in sources:
                if where is not None and repo.fetch_readonly(
                        where, [anchor], depth=HISTORY_DEPTH, timeout=FETCH_TIMEOUT_S,
                        check=False).returncode == 0 and repo.has_object(anchor + "^{commit}"):
                    break
        if not (repo.has_object(sha + "^{commit}") and repo.has_object(anchor + "^{commit}")):
            continue
        for round_ in range(FOREIGN_DEEPEN_ROUNDS + 1):
            out = repo.git("merge-base", sha, anchor, check=False)
            mb = out.stdout.decode().strip()
            if out.returncode == 0 and is_object_id(mb):
                return mb, foreign
            if round_ == FOREIGN_DEEPEN_ROUNDS or not repo.shallow():
                break                     # nothing is cut: there is no history left to bring
            repo.fetch_readonly(foreign, [sha], deepen=HISTORY_DEPTH, timeout=FETCH_TIMEOUT_S,
                                check=False)
            for where in sources:
                if where is not None and _has_commit(where, anchor):
                    repo.fetch_readonly(where, [anchor], deepen=HISTORY_DEPTH,
                                        timeout=FETCH_TIMEOUT_S, check=False)
                    break
    return None, None


def _tree_at(repo: CodeRepo, commit: str, prefix: str) -> Optional[str]:
    spec = f"{commit}:{prefix}" if prefix else f"{commit}^{{tree}}"
    out = repo.git("rev-parse", "--verify", "--quiet", spec, check=False)
    sha = out.stdout.decode().strip()
    if out.returncode != 0 or not sha:
        return None
    kind = repo.git("cat-file", "-t", sha, check=False).stdout.decode().strip()
    return sha if kind == "tree" else None


def overlay_from_git(run_dir, git, **kw) -> dict:
    """The overlay a foreign repository's commit becomes, merged onto the run's active base."""
    from looplab.engine.code_identity import BASE_BRANCH, sync_run
    from looplab.engine.code_origin import HISTORY_DEPTH
    _require_git()
    asked, ref, prefix_override = _spec(git)
    foreign_path = _repository_root(asked)
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
    if not _ensure_commit(repo, [foreign_path], sha, FOREIGN_REF + sha):
        raise OverlayRefusal("code_from_git_fetch_failed",
                             f"could not fetch {sha[:12]} from {foreign_path} (read-only fetch)")
    merge_base, found_in = _merge_base(repo, foreign_path, src, sha, anchors)
    if not merge_base or not _ensure_commit(repo, [found_in, foreign_path, src], merge_base):
        raise OverlayRefusal(
            "code_from_git_no_merge_base",
            "the branch shares no history with the commit the run's source started from (neither "
            "repository holds both, and none was found within "
            f"{HISTORY_DEPTH * (FOREIGN_DEEPEN_ROUNDS + 1)} commits of either; or the source is "
            "not in git); submit a patch or use the run's worktree")
    trees = {"start": _tree_at(repo, merge_base, prefix), "branch": _tree_at(repo, sha, prefix)}
    if None in trees.values():
        raise OverlayRefusal("code_from_git_prefix",
                             f"the editable directory {prefix or '(root)'!r} is not in the "
                             "branch's tree; pass git.prefix")
    # Three commits on ONE synthetic root, so merge-tree's own merge-base IS the branch's start,
    # at the editable's prefix (no `--merge-base` option, which needs git 2.40). Objects only —
    # no ref, so no lock: `CodeRepo.commit_tree` under the fixed identity, the same ids as before.
    root = repo.commit_tree(trees["start"], (), "looplab: foreign branch start")
    ours = repo.commit_tree(repo.tree(base), [root], "looplab: run base")
    theirs = repo.commit_tree(trees["branch"], [root], f"looplab: foreign {sha}")
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
