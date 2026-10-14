"""Export a run's code into the TASK repository as a reviewable branch (doc 79 §5).

WHAT IS EXPORTED, in this order, each as one commit on a NEW branch of the operator's repository:

  1. `base`     — the code the run STARTED from, relative to `onto`: the uncommitted or unpublished
                  edits the seed copied (doc 37 §5d: the base is the working tree, not HEAD). On the
                  external-agent MiniOneRec runs this commit IS the night's scattered fixes.
  2. `upstream` — each base the run's upstream lane advanced to (`base_advanced`), as the diff from
                  the base it replaced, with the proposal, source node, flag and gate evidence in
                  the message.
  3. a node     — optionally the champion's (or a named node's) own change against the base it ran
                  on, with its metric.

HOW. Tree-level cherry-picks with git plumbing in the target repository and a TEMPORARY index:
`hash-object -w`, `update-index --index-info`, `write-tree`, `commit-tree`, then `update-ref` of the
new branch with the all-zero old value (create-only). The operator's HEAD, index, working tree,
existing branches, remotes and config are never touched, nothing is pushed, no hook runs. Each
changed path applies when the target still holds the run's OLD bytes there (or already holds the new
ones); otherwise a text three-way merge (`git merge-file`) is tried, and a conflict stops the export
before that commit with the conflicting paths named. A base step never deletes: a target path the
run's snapshot does not hold is counted (`not_in_snapshot`), not removed — a seed copies tracked
files only and may be bounded, so absence is not evidence of deletion.

`--patches DIR` also writes the created branch as a `git format-patch` series for a merge request
on a host the operator chooses.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

from looplab.engine.code_repo import CodeRepo, CodeRepoError, run_git, safe_path

ZERO = "0" * 40
_ID_ENV = {"GIT_AUTHOR_NAME": "LoopLab", "GIT_AUTHOR_EMAIL": "looplab@invalid",
           "GIT_COMMITTER_NAME": "LoopLab", "GIT_COMMITTER_EMAIL": "looplab@invalid"}


class ExportRefusal(CodeRepoError):
    """The export cannot start (bad target, existing branch, nothing recorded)."""


def _tgit(target: Path, *argv, input=None, env_extra=None, check=True):
    return run_git("-C", str(target), *argv, input=input, check=check, env_extra=env_extra)


def _target_entries(target: Path, commit: str, paths) -> dict:
    """{path: (mode, sha)} for `paths` in `commit` of the target repository."""
    paths = list(paths)
    out: dict = {}
    for i in range(0, len(paths), 500):
        chunk = paths[i:i + 500]
        res = _tgit(target, "ls-tree", "-z", "--full-tree", commit, "--", *chunk)
        for item in res.stdout.split(b"\x00"):
            if not item:
                continue
            meta, _, path = item.partition(b"\t")
            mode, kind, sha = meta.decode().split(" ")
            if kind == "blob":
                out[path.decode("utf-8", "replace")] = (mode, sha)
    return out


def _write_blob(target: Path, data: bytes) -> str:
    return _tgit(target, "hash-object", "-w", "--no-filters", "--stdin",
                 input=data).stdout.decode().strip()


def _merge3(target: Path, ours: bytes, base: bytes, theirs: bytes) -> Optional[bytes]:
    if b"\x00" in ours or b"\x00" in base or b"\x00" in theirs:
        return None
    with tempfile.TemporaryDirectory(prefix="looplab-merge-") as tmp:
        paths = []
        for name, data in (("ours", ours), ("base", base), ("theirs", theirs)):
            p = Path(tmp) / name
            p.write_bytes(data)
            paths.append(str(p))
        res = run_git("merge-file", "-p", *paths, check=False)
    return res.stdout if res.returncode == 0 else None


class _Builder:
    """Accumulates commits on top of `onto` in the target repo through a private index."""

    def __init__(self, target: Path, onto: str, prefix: str):
        self.target, self.head, self.prefix = target, onto, prefix
        self.commits: list = []

    def tpath(self, path: str) -> str:
        return f"{self.prefix}/{path}" if self.prefix else path

    def commit(self, rows: dict, message: str) -> Optional[str]:
        """`rows[target_path]` = (mode, sha) or None (delete). Returns the new commit or None."""
        if not rows:
            return None
        with tempfile.TemporaryDirectory(prefix="looplab-export-") as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
            _tgit(self.target, "read-tree", self.head, env_extra=env)
            lines = []
            for path, row in sorted(rows.items()):
                if row is None:
                    lines.append(f"0 {ZERO}\t{path}")
                else:
                    lines.append(f"{row[0]} {row[1]}\t{path}")
            _tgit(self.target, "update-index", "-z", "--index-info",
                  input=("\x00".join(lines) + "\x00").encode("utf-8"), env_extra=env)
            tree = _tgit(self.target, "write-tree", env_extra=env).stdout.decode().strip()
        parent_tree = _tgit(self.target, "rev-parse", self.head + "^{tree}").stdout.decode().strip()
        if tree == parent_tree:
            return None
        sha = _tgit(self.target, "commit-tree", tree, "-p", self.head, "-F", "-",
                    input=message.encode("utf-8"), env_extra=_ID_ENV).stdout.decode().strip()
        self.commits.append({"commit": sha, "subject": message.splitlines()[0][:200],
                             "files": len(rows)})
        self.head = sha
        return sha


def _base_step(repo: CodeRepo, builder: _Builder, base_commit: str, *, run_id: str,
               digest: str, drift_from: Optional[str] = None) -> dict:
    """Carry the run's starting base onto the target head (never deleting).

    With `drift_from` — the target's copy of the source commit the run started from — this is the
    DELTA that commit -> the run's seed (its uncommitted edits), applied path by path like any other
    delta, so a target that moved on since (another run's delivery) keeps its changes. Without it
    (a source that was not git, or a commit the target lacks) the target is made to hold the seed's
    bytes at every path the seed has."""
    entries = repo.entries(base_commit)
    tpaths = {builder.tpath(p): p for p in entries}
    current = _target_entries(builder.target, builder.head, list(tpaths))
    origin = (_target_entries(builder.target, drift_from, list(tpaths))
              if drift_from is not None else None)
    rows, unchanged, already, conflicts = {}, 0, 0, []
    for tpath, path in sorted(tpaths.items()):
        mode, sha = entries[path]
        have = current.get(tpath)
        if origin is not None:
            before = origin.get(tpath)
            if before is not None and before[1] == sha:
                unchanged += 1                       # not part of the run's drift
                continue
            if have is not None and have[1] == sha:
                already += 1
                continue
            if not ((have is None and before is None) or
                    (have is not None and before is not None and have[1] == before[1])):
                ours = _tgit(builder.target, "cat-file", "blob", have[1]).stdout if have else None
                base_bytes = (_tgit(builder.target, "cat-file", "blob", before[1]).stdout
                              if before else None)
                merged = (_merge3(builder.target, ours, base_bytes, repo.blob(base_commit, path) or b"")
                          if ours is not None and base_bytes is not None else None)
                if merged is None:
                    conflicts.append(tpath)
                else:
                    rows[tpath] = (have[0], _write_blob(builder.target, merged))
                continue
        elif have is not None and have[1] == sha:
            unchanged += 1
            continue
        data = repo.blob(base_commit, path)
        if data is None:
            continue
        rows[tpath] = (have[0] if have is not None else mode, _write_blob(builder.target, data))
    if conflicts:
        return {"kind": "base", "commit": None, "conflicts": conflicts, "drift_from": drift_from}
    under = _tgit(builder.target, "ls-tree", "-r", "-z", "--name-only", "--full-tree", builder.head,
                  *(["--", builder.prefix] if builder.prefix else [])).stdout.split(b"\x00")
    not_in_snapshot = sum(1 for p in under if p and p.decode("utf-8", "replace") not in tpaths)
    files = "\n".join(f"  {p}" for p in sorted(rows)[:60])
    more = f"\n  … {len(rows) - 60} more" if len(rows) > 60 else ""
    origin_line = (f"relative to {drift_from}, the source commit it was seeded from"
                   if drift_from else "relative to this branch's start")
    message = (f"LoopLab run {run_id}: code the run started from\n\n"
               "The working tree LoopLab seeded every experiment from, " + origin_line + ":\n"
               "edits that were never committed, or were made in a copy.\n\n"
               f"Changed files:\n{files}{more}\n\n"
               f"Looplab-Run: {run_id}\nLooplab-Base-Digest: {digest}\n"
               f"Looplab-Run-Commit: {base_commit}\n")
    sha = builder.commit(rows, message)
    return {"kind": "base", "commit": sha, "changed": len(rows), "unchanged": unchanged,
            "already_present": already, "not_in_snapshot": not_in_snapshot,
            "drift_from": drift_from}


def _delta_step(repo: CodeRepo, builder: _Builder, old: str, new: str, message: str,
                kind: str) -> dict:
    """Carry the run's old->new change onto the target head; a conflict stops before committing."""
    changed = repo.changed(old, new)
    old_e, new_e = repo.entries(old), repo.entries(new)
    current = _target_entries(builder.target, builder.head, [builder.tpath(r["path"]) for r in changed])
    rows, conflicts, already = {}, [], 0
    for row in changed:
        path, tpath = row["path"], builder.tpath(row["path"])
        before, after, have = old_e.get(path), new_e.get(path), current.get(tpath)
        if (after is None and have is None) or (after is not None and have is not None
                                                and have[1] == after[1]):
            already += 1
            continue
        if (have is None and before is None) or (have is not None and before is not None
                                                 and have[1] == before[1]):
            if after is None:
                rows[tpath] = None
            else:
                data = repo.blob(new, path) or b""
                rows[tpath] = (have[0] if have is not None else after[0],
                               _write_blob(builder.target, data))
            continue
        if have is not None and before is not None and after is not None:
            ours = _tgit(builder.target, "cat-file", "blob", have[1]).stdout
            merged = _merge3(builder.target, ours, repo.blob(old, path) or b"",
                             repo.blob(new, path) or b"")
            if merged is not None:
                rows[tpath] = (have[0], _write_blob(builder.target, merged))
                continue
        conflicts.append(tpath)
    if conflicts:
        return {"kind": kind, "commit": None, "conflicts": conflicts, "changed": len(changed)}
    sha = builder.commit(rows, message)
    return {"kind": kind, "commit": sha, "changed": len(rows), "already_present": already}


def _default_prefix(target_top: Path, run_dir: Path) -> str:
    """The editable's path inside the target repo when the run's single editable lives there."""
    try:
        import json
        task = json.loads((run_dir / "task.snapshot.json").read_text(encoding="utf-8"))
        src = task.get("editable_path")
        if isinstance(src, str) and src:
            rel = os.path.relpath(os.path.realpath(src), os.path.realpath(target_top))
            if not rel.startswith(".."):
                return "" if rel == "." else Path(rel).as_posix()
    except (OSError, ValueError):
        pass
    return ""


def export_code(run_dir, target, *, branch: Optional[str] = None, onto: Optional[str] = None,
                prefix: Optional[str] = None, include_base: bool = True,
                include_upstream: bool = True, node: Optional[str] = "champion",
                patches_dir=None, dry_run: bool = False) -> dict:
    """Create `branch` in `target` carrying the run's code (module docstring). Never pushes.

    `node` is `"champion"`, a node id, or None for bases only. `onto` defaults to the source commit
    the run recorded at start when the target has it, else the target's HEAD."""
    from looplab.engine.code_identity import (BASE_REF, CHAMPION_BRANCH, _read_events, base_lineage,
                                              active_base_digest, recorded_snapshots,
                                              _latest_by_lifecycle, sync_run, _run_meta)
    from looplab.engine.shared import engine_fold as fold
    run_dir, target = Path(run_dir), Path(target)
    if node is not None and str(node).lower() in ("none", ""):
        node = None
    if node is not None and node != "champion" and not str(node).isdigit():
        raise ExportRefusal("export_node_invalid", f"node must be 'champion', an id or 'none': {node}")
    events = _read_events(run_dir)
    sync_run(run_dir, events)
    repo = CodeRepo(run_dir)
    if not repo.exists():
        raise ExportRefusal("export_nothing", "this run has no code repository (no repo-task seed)")
    top = run_git("-C", str(target), "rev-parse", "--show-toplevel", check=False)
    if top.returncode != 0:
        raise ExportRefusal("export_target_invalid", f"{target} is not inside a git repository")
    target = Path(top.stdout.decode().strip())
    run_id, started = _run_meta(events)
    branch = branch or f"looplab/{run_id}"
    if run_git("check-ref-format", "--branch", branch, check=False).returncode != 0:
        raise ExportRefusal("export_branch_invalid", f"not a valid branch name: {branch}")
    if _tgit(target, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
             check=False).returncode == 0:
        raise ExportRefusal("export_branch_exists",
                            f"branch {branch} already exists in {target}; choose another --branch")
    recorded = None
    workspace = started.get("workspace") if isinstance(started.get("workspace"), dict) else {}
    for value in workspace.values():
        if isinstance(value, str) and value.startswith("git:"):
            recorded = value[4:]
    if not (recorded and _tgit(target, "cat-file", "-e", recorded + "^{commit}",
                               check=False).returncode == 0):
        recorded = None
    if onto is None:
        onto = recorded or "HEAD"
    onto_sha = _tgit(target, "rev-parse", "--verify", onto + "^{commit}").stdout.decode().strip()
    prefix = (prefix if prefix is not None else _default_prefix(target, run_dir)).strip("/")
    if prefix and safe_path(prefix) is None:
        raise ExportRefusal("export_prefix_invalid", f"not a repository-relative path: {prefix}")
    lineage = base_lineage(events)
    active = active_base_digest(events, lineage)
    chain = []
    digest = active
    while digest is not None and len(chain) < 256:
        chain.append(digest)
        digest = (lineage.get(digest) or {}).get("parent")
    chain.reverse()
    builder = _Builder(target, onto_sha, prefix)
    steps: list = []
    report = {"target": str(target), "branch": branch, "onto": onto_sha, "prefix": prefix,
              "run": run_id, "steps": steps, "status": "created"}
    if not chain:
        raise ExportRefusal("export_nothing", "the run recorded no base to export")
    first = repo.rev(BASE_REF + chain[0])
    if first is None:
        raise ExportRefusal("export_nothing", "the run's base archive is unavailable")
    if include_base:
        steps.append(_base_step(repo, builder, first, run_id=run_id, digest=chain[0],
                                drift_from=recorded))
        if steps[-1].get("conflicts"):
            report["status"] = "conflict"
    prev = first
    for digest in chain[1:] if report["status"] == "created" else ():
        sha = repo.rev(BASE_REF + digest)
        if sha is None:
            continue
        info = lineage.get(digest) or {}
        adv = info.get("advance") or {}
        if not include_upstream and info.get("reason") == "upstream":
            prev = sha
            continue
        subject = (f"LoopLab upstream from node {adv.get('source_node_id')}: "
                   f"{' '.join(str(adv.get('summary') or 'pipeline change').split())[:150]}"
                   if info.get("reason") == "upstream" else
                   "LoopLab: the run's source changed during the run")
        flag = adv.get("flag")
        message = (subject + "\n\n" + "\n".join(
            f"{k}: {v}" for k, v in (("Looplab-Run", run_id), ("Looplab-Base-Digest", digest),
                                     ("Looplab-Upstream-Proposal", adv.get("proposal_id")),
                                     ("Looplab-Upstream-Source-Node", adv.get("source_node_id")),
                                     ("Looplab-Upstream-Gate-Seq", adv.get("gate_seq")),
                                     ("Looplab-Upstream-Evidence", adv.get("evidence_token")),
                                     ("Looplab-Upstream-Flag", flag if isinstance(flag, dict) else None),
                                     ("Looplab-Run-Commit", sha)) if v is not None) + "\n")
        step = _delta_step(repo, builder, prev, sha, message, info.get("reason") or "upstream")
        step["digest"] = digest
        steps.append(step)
        if step.get("conflicts"):
            report["status"] = "conflict"
            break
        prev = sha
    if report["status"] == "created" and node is not None:
        state = fold(events)
        snaps = _latest_by_lifecycle(recorded_snapshots(events))
        if node == "champion":
            best = state.best()
            target_node = best.id if best is not None else None
        else:
            target_node = int(node)
        n = state.nodes.get(target_node) if target_node is not None else None
        row = snaps.get((n.id, int(n.attempt))) if n is not None else None
        if row is None and node == "champion":
            champion = repo.rev(CHAMPION_BRANCH)
            row = {"commit": champion, "base": {}} if champion else None
        if row is not None and row.get("commit"):
            node_commit = row["commit"]
            node_base = (row.get("base") or {}).get("commit") or repo.git(
                "rev-parse", node_commit + "^").stdout.decode().strip()
            metric = None if n is None else (n.task_metric if n.task_metric is not None else n.metric)
            idea = getattr(n, "idea", None)
            title = " ".join(str(getattr(idea, "title", "") or getattr(idea, "hypothesis", "") or
                                 getattr(idea, "rationale", "") or "").split())[:140]
            message = (f"LoopLab node {target_node}: {title or 'experiment change'}\n\n"
                       f"Looplab-Run: {run_id}\nLooplab-Node: {target_node}\n"
                       f"Looplab-Generation: {getattr(n, 'attempt', '')}\n"
                       f"Looplab-Metric: {metric}\nLooplab-Run-Commit: {node_commit}\n")
            step = _delta_step(repo, builder, node_base, node_commit, message, "node")
            step["node_id"] = target_node
            steps.append(step)
            if step.get("conflicts"):
                report["status"] = "conflict"
        else:
            steps.append({"kind": "node", "commit": None, "node_id": target_node,
                          "skipped": "no recorded attempt commit"})
    report["commits"] = builder.commits
    if not builder.commits:
        report["status"] = "nothing_to_export" if report["status"] == "created" else report["status"]
        return report
    if dry_run:
        report["status"] = "dry_run" if report["status"] == "created" else report["status"]
        return report
    _tgit(target, "update-ref", "-m", f"looplab export of {run_id}", f"refs/heads/{branch}",
          builder.head, ZERO)
    report["head"] = builder.head
    if patches_dir is not None:
        out = Path(patches_dir)
        out.mkdir(parents=True, exist_ok=True)
        res = _tgit(target, "format-patch", "--no-signature", "-o", str(out),
                    f"{onto_sha}..{builder.head}")
        report["patches"] = [line for line in res.stdout.decode().splitlines() if line]
        if not report["patches"]:
            report["patches"] = sorted(str(p) for p in out.glob("*.patch"))
    return report
