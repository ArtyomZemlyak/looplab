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
before that commit with the conflicting paths named. The file MODE is carried beside the bytes and
resolved apart from them (`_merged_mode`): a chmod exports with or without a content change, and a
mode the target moved differently is a conflict (`mode_conflicts`), never a silent pick. A base
step never deletes: a target path the run's snapshot does not hold is counted (`not_in_snapshot`),
not removed — a seed copies tracked files only and may be bounded, so absence is not evidence of
deletion.

`--patches DIR` also writes the created branch as a `git format-patch` series for a merge request
on a host the operator chooses.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

from looplab.engine.code_repo import CodeRepo, CodeRepoError, readonly_git, run_git, safe_path

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


def _tracks_file_mode(repo_dir) -> Optional[bool]:
    """Whether the repository at `repo_dir` counts a WORKING TREE's executable bit, as `git status`
    there does: `core.fileMode=false` is what `git init` writes where `chmod` is not kept (vfat,
    NTFS, CIFS, 9p, WSL's `/mnt/c`, which report 0755 for every file), and git then never takes a
    mode off that working tree. Unset means true (git's default). None when `repo_dir` is not a
    readable git repository. Read-only (`readonly_git`).

    The question is asked of BOTH sides of the base step (`_base_step`): the seed archive read its
    modes off the SOURCE's working tree (`st_mode`), so whether they are evidence is the source's
    filesystem's answer, and the target's answer says whether the operator wants a mode carried at
    all. The delta steps compare the run's OWN commits (each read off the run's node workdirs), so
    neither question arises there."""
    try:
        res = readonly_git(repo_dir, "config", "--bool", "core.fileMode", check=False)
        if res.returncode not in (0, 1):
            return None
        if res.returncode == 1 and readonly_git(repo_dir, "rev-parse", "--git-dir",
                                                check=False).returncode != 0:
            return None                          # exit 1 = unset, or not a repository at all
    except CodeRepoError:
        return None
    return res.stdout.decode("utf-8", "replace").strip() != "false"


def _merged_mode(before, after, have) -> Optional[str]:
    """The target's mode once the run's `before -> after` is carried onto `have`, resolved APART
    from the text (review 2026-10-15, weekly-export-file-mode: the mode used to be the target's
    `have[0]` whatever the run did, so `chmod +x train.sh` exported as 100644). Each argument is a
    `(mode, sha)` pair or None; `after` is not None. None is a CONFLICT — the target moved the mode
    one way and the run another (or both added the path with different modes) — said, never a pick."""
    if have is None:
        return after[0]
    if have[0] == after[0] or (before is not None and before[0] == after[0]):
        return have[0]                   # the target already holds it, or the run left it alone
    if before is not None and have[0] == before[0]:
        return after[0]                  # the run moved it and the target did not
    return None


def _carry(target: Path, before, after, have, old_bytes, new_bytes) -> tuple:
    """Carry one path's `before -> after` onto the target's `have`. CONTENT is compared on the blob
    and the MODE is resolved apart from it (`_merged_mode`), never the two as one pair (review
    2026-10-16: pair equality made a deletion of a file the target only chmod'ed a conflict, and
    content the target already held a "change" when only the target's own mode differed).
    `old_bytes` / `new_bytes` are callables (read only when a merge or a write needs them). Returns
    `("already", None)`, `("set", (mode, sha) | None)` with None a deletion, `("conflict", None)`
    or `("mode_conflict", None)`."""
    if after is None:
        if have is None:
            return "already", None       # the target deleted it too
        # A deletion needs the target to hold the CONTENT the run deleted; a mode the target set
        # on it since is not content the deletion loses.
        return ("set", None) if before is not None and have[1] == before[1] else ("conflict", None)
    if have is None:
        if before is None:
            return "set", (after[0], _write_blob(target, new_bytes()))
        return "conflict", None          # the target deleted what the run changed
    mode = _merged_mode(before, after, have)
    if mode is None:
        return "mode_conflict", None
    if have[1] == after[1]:
        # The bytes are there already: "already present" unless the MODE still has to move.
        return ("already", None) if mode == have[0] else ("set", (mode, have[1]))
    if before is not None and have[1] == before[1]:
        return "set", (mode, _write_blob(target, new_bytes()))
    if before is None:
        return "conflict", None          # both added the path with different bytes
    ours = _tgit(target, "cat-file", "blob", have[1]).stdout
    merged = _merge3(target, ours, old_bytes(), new_bytes())
    if merged is None:
        return "conflict", None
    return "set", (mode, _write_blob(target, merged))


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


def _seed_mode_trust(target: Path, source: Optional[str]) -> dict:
    """Whether the seed's executable bits are carried (`trusted`), with each side's answer
    (`_tracks_file_mode`). Trusted only when NEITHER side says a working tree's mode is no evidence
    (review 2026-10-16: the target's answer alone let a seed copied off vfat / NTFS / CIFS / 9p /
    `/mnt/c`, where every file reads 0755, `chmod +x` every file of the export). A source that is
    not a readable repository any more (gone after a restore, or never git) gives no answer and
    the target's stands — the seed's `st_mode` is then the only evidence there is."""
    target_tracks = _tracks_file_mode(target)
    source_tracks = _tracks_file_mode(source) if source else None
    return {"trusted": target_tracks is not False and source_tracks is not False,
            "target": target_tracks, "source": source_tracks}


def _base_step(repo: CodeRepo, builder: _Builder, base_commit: str, *, run_id: str,
               digest: str, drift_from: Optional[str] = None,
               source: Optional[str] = None) -> dict:
    """Carry the run's starting base onto the target head (never deleting).

    With `drift_from` — the target's copy of the source commit the run started from — this is the
    DELTA that commit -> the run's seed (its uncommitted edits), applied path by path like any other
    delta, so a target that moved on since (another run's delivery) keeps its changes. Without it
    (a source that was not git, or a commit the target lacks) the target is made to hold the seed's
    bytes at every path the seed has.

    MODES. The seed's executable bit is a working tree's `st_mode`. Where either side says that is
    no evidence (`_seed_mode_trust`), the run's side of every path takes the mode GIT recorded for it
    in the source commit (`drift_from` in the target, else the first base's parent in the run's own
    repository — the same commit), then the target's, and a path new in the seed is 100644 — as git
    itself adds a file under `core.fileMode=false`. So an untrusted seed moves no mode at all; a
    trusted one moves exactly the paths whose mode differs from the source commit's: real chmods."""
    entries = repo.entries(base_commit)
    tpaths = {builder.tpath(p): p for p in entries}
    current = _target_entries(builder.target, builder.head, list(tpaths))
    origin = (_target_entries(builder.target, drift_from, list(tpaths))
              if drift_from is not None else None)
    trust = _seed_mode_trust(builder.target, source)
    recorded_modes: dict = {}
    if origin is not None:
        recorded_modes = {tp: row[0] for tp, row in origin.items()}
    elif not trust["trusted"]:
        parents = repo.commit_parents(base_commit)
        if parents and repo.has_object(parents[0] + "^{tree}"):
            recorded_modes = {builder.tpath(p): row[0]
                              for p, row in repo.entries(parents[0]).items()}

    def run_mode(tpath: str, mode: str, have) -> str:
        if trust["trusted"]:
            return mode
        return recorded_modes.get(tpath) or (have[0] if have else "100644")

    rows, unchanged, already, conflicts, mode_conflicts = {}, 0, 0, [], []
    for tpath, path in sorted(tpaths.items()):
        mode, sha = entries[path]
        have = current.get(tpath)
        if origin is not None:
            before = origin.get(tpath)
            run = (run_mode(tpath, mode, have), sha)
            if before == run:
                unchanged += 1                       # not part of the run's drift
                continue
            outcome, row = _carry(
                builder.target, before, run, have,
                lambda b=before: _tgit(builder.target, "cat-file", "blob", b[1]).stdout,
                lambda p=path: repo.blob(base_commit, p) or b"")
            if outcome == "already":
                already += 1
            elif outcome == "set":
                rows[tpath] = row
            else:
                conflicts.append(tpath)
                if outcome == "mode_conflict":
                    mode_conflicts.append({"path": tpath, "base": before[0] if before else None,
                                           "run": run[0], "target": have[0] if have else None})
            continue
        run = (run_mode(tpath, mode, have), sha)
        if have == run:
            unchanged += 1
            continue
        data = repo.blob(base_commit, path)
        if data is None:
            continue
        rows[tpath] = (run[0], _write_blob(builder.target, data))
    if conflicts:
        out = {"kind": "base", "commit": None, "conflicts": conflicts, "drift_from": drift_from,
               "file_mode": trust}
        if mode_conflicts:
            out["mode_conflicts"] = mode_conflicts
        return out
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
            "drift_from": drift_from, "file_mode": trust}


def _delta_step(repo: CodeRepo, builder: _Builder, old: str, new: str, message: str,
                kind: str) -> dict:
    """Carry the run's old->new change onto the target head; a conflict stops before committing.

    Each path is compared and carried as its (mode, blob) PAIR (`_carry`): a chmod with or without
    a content change exports as the run made it, and a mode the target moved differently is a
    conflict named in `mode_conflicts` (review 2026-10-15, closed 2026-10-16)."""
    changed = repo.changed(old, new)
    old_e, new_e = repo.entries(old), repo.entries(new)
    current = _target_entries(builder.target, builder.head, [builder.tpath(r["path"]) for r in changed])
    rows, conflicts, mode_conflicts, already = {}, [], [], 0
    for row in changed:
        path, tpath = row["path"], builder.tpath(row["path"])
        before, after, have = old_e.get(path), new_e.get(path), current.get(tpath)
        outcome, carried = _carry(builder.target, before, after, have,
                                  lambda p=path: repo.blob(old, p) or b"",
                                  lambda p=path: repo.blob(new, p) or b"")
        if outcome == "already":
            already += 1
        elif outcome == "set":
            rows[tpath] = carried
        else:
            conflicts.append(tpath)
            if outcome == "mode_conflict":
                mode_conflicts.append({"path": tpath, "base": before[0] if before else None,
                                       "run": after[0], "target": have[0] if have else None})
    if conflicts:
        out = {"kind": kind, "commit": None, "conflicts": conflicts, "changed": len(changed)}
        if mode_conflicts:
            out["mode_conflicts"] = mode_conflicts
        return out
    sha = builder.commit(rows, message)
    return {"kind": kind, "commit": sha, "changed": len(rows), "already_present": already}


def _editable_sources(run_dir: Path) -> Optional[list]:
    """`[(mount name, source path)]` of the run's editables, read through the task ADAPTER — so the
    composable `repo` alias, a multi-editable `editables` list and a `~`/`$VAR` spelling all mean
    what the run meant (review 2026-10-15: the raw `editable_path` key read none of them). A pinned
    `seed_base` points `path` at the archive; `origin_path` is the operator's source. None when the
    snapshot cannot be read as a task."""
    from looplab.core.errors import OperatorRefusal
    try:
        from looplab.adapters.tasks import load_task
        task = load_task(run_dir / "task.snapshot.json", existing_run=True)
        spec = task.repo_spec() if hasattr(task, "repo_spec") else None
    except (OperatorRefusal, OSError, ValueError, TypeError, KeyError, AttributeError):
        return None                          # unreadable or invalid snapshot: said in the report
    return [(str(ed.get("name") or "."), str(ed.get("origin_path") or ed.get("path") or ""))
            for ed in (spec or {}).get("editables") or [] if ed.get("origin_path") or ed.get("path")]


def _relative_inside(src: str, target_top: Path) -> Optional[str]:
    """`src` relative to the target's top ("" for the top itself), or None when outside it."""
    try:
        rel = os.path.relpath(os.path.realpath(os.path.expanduser(src)),
                              os.path.realpath(target_top))
    except ValueError:                       # Windows: another drive
        return None
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
        return None
    return "" if rel == os.curdir else Path(rel).as_posix()


def _default_prefix(target_top: Path, run_dir: Path) -> tuple:
    """`(prefix, note)`: where the run's tree sits inside the target repository, and — when the
    choice was not the obvious one — the sentence saying how it was made.

    The run's tree is the WORKDIR layout: the root editable (`.`) at the top, every named editable
    under its name. So the root editable decides the prefix when there is one; with only named
    editables, the prefix is the one directory P with every editable's source at `P/<name>` inside
    the target, and anything else is ambiguous and REFUSED rather than exported at the root."""
    eds = _editable_sources(run_dir)
    if not eds:
        return "", ("the task snapshot names no readable editable source; paths are exported at "
                    "the repository root (pass --prefix to place them)")
    root = [src for name, src in eds if name in (".", "")]
    others = sorted(name for name, _ in eds if name not in (".", ""))
    if root:
        rel = _relative_inside(root[0], target_top)
        note = None
        if rel is None:
            rel, note = "", ("the run's editable is not inside the target repository; its paths are "
                             "exported at the repository root (a clone of the same repository)")
        if others:
            note = ((note + "; ") if note else "") + (
                "the root editable decides the prefix; the other editables keep their mount names "
                "under it: " + ", ".join(others))
        return rel, note
    parents = set()
    for name, src in eds:
        rel, name = _relative_inside(src, target_top), name.strip("/")
        if rel is not None and rel == name:
            parents.add("")
        elif rel is not None and rel.endswith("/" + name):
            parents.add(rel[:-len(name) - 1])
        else:
            parents.add(None)
    if len(parents) == 1 and None not in parents:
        return parents.pop(), None
    raise ExportRefusal("export_prefix_ambiguous",
                        "the run's editables (" + ", ".join(others) + ") are mounted under their "
                        "names and no single target directory holds them that way; pass --prefix "
                        "(exported paths keep each editable's mount name under it)")


def _recorded_source_commit(workspace: dict) -> Optional[str]:
    """The source commit the run's EXPORTED tree started from, off `run_started.workspace`.

    That dict holds the editables first and the `data:*` / `ref:*` mounts after them
    (`engine/workspace.py::workspace_fingerprint`), so the LAST `git:` value was a data or reference
    repository's HEAD whenever one was git-backed (review 2026-10-15). Only an `editable:` entry
    counts: the root editable's when the run has one (git or not — another editable's commit is
    another repository), else the one commit every named editable agrees on."""
    editables = {k[len("editable:"):]: v for k, v in (workspace or {}).items()
                 if isinstance(k, str) and k.startswith("editable:")}
    for root in (".", ""):
        if root in editables:
            value = editables[root]
            return value[4:] if isinstance(value, str) and value.startswith("git:") else None
    commits = {v[4:] if isinstance(v, str) and v.startswith("git:") else None
               for v in editables.values()}
    return commits.pop() if len(commits) == 1 else None


def export_code(run_dir, target, *, branch: Optional[str] = None, onto: Optional[str] = None,
                prefix: Optional[str] = None, include_base: bool = True,
                include_upstream: bool = True, node: Optional[str] = "champion",
                patches_dir=None, dry_run: bool = False) -> dict:
    """Create `branch` in `target` carrying the run's code (module docstring). Never pushes.

    `node` is `"champion"`, a node id, or None for bases only. `onto` defaults to the source commit
    the run recorded at start when the target has it, else the target's HEAD."""
    from looplab.engine.code_identity import (BASE_REF, _read_events, base_lineage,
                                              active_base_digest, recorded_snapshots,
                                              _latest_by_lifecycle, sync_run, _run_meta)
    from looplab.engine.shared import engine_fold as fold
    run_dir, target = Path(run_dir), Path(target)
    if node is not None and str(node).lower() in ("none", ""):
        node = None
    if node is not None and node != "champion" and not str(node).isdigit():
        raise ExportRefusal("export_node_invalid", f"node must be 'champion', an id or 'none': {node}")
    events = _read_events(run_dir)
    # What the sync BUILT counts as much as what the log RECORDED (review 2026-10-15): a run with no
    # `code_snapshot` rows (every run before doc 79, a kill between seed and record) has its attempt
    # commits only here — `sync_run` appends no event — and a named node's export was skipped.
    synced = [b for b in (sync_run(run_dir, events).get("built") or [])
              if b.get("status") == "recorded" and b.get("commit")]
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
    # The one reading of where the run's code came from, shared with the base parent and a foreign
    # branch's merge base (`code_origin.py::run_source`).
    from looplab.engine.code_origin import run_source
    source, recorded = run_source(events, run_dir)
    if not (recorded and _tgit(target, "cat-file", "-e", recorded + "^{commit}",
                               check=False).returncode == 0):
        recorded = None
    if onto is None:
        onto = recorded or "HEAD"
    onto_sha = _tgit(target, "rev-parse", "--verify", onto + "^{commit}").stdout.decode().strip()
    prefix_note = None
    if prefix is None:
        prefix, prefix_note = _default_prefix(target, run_dir)
    prefix = prefix.strip("/")
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
    if prefix_note:
        report["prefix_note"] = prefix_note
    if not chain:
        raise ExportRefusal("export_nothing", "the run recorded no base to export")
    first = repo.rev(BASE_REF + chain[0])
    if first is None:
        raise ExportRefusal("export_nothing", "the run's base archive is unavailable")
    if include_base:
        steps.append(_base_step(repo, builder, first, run_id=run_id, digest=chain[0],
                                drift_from=recorded, source=source))
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
        snaps = _latest_by_lifecycle({
            **recorded_snapshots(events),
            **{(b["node_id"], b["generation"], b["seed_event_seq"]): b for b in synced}})
        if node == "champion":
            best = state.best()
            target_node = best.id if best is not None else None
        else:
            target_node = int(node)
        n = state.nodes.get(target_node) if target_node is not None else None
        # ONLY the target lifecycle's own commit. `refs/heads/champion` is NOT a fallback: it moves
        # only when the current champion HAS a commit, so with none it still names an EARLIER
        # champion, and exporting it put node 3's diff under node 7's id, title and metric (review
        # 2026-10-15). No commit for this lifecycle is a skipped step that says so.
        row = snaps.get((n.id, int(n.attempt))) if n is not None else None
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
