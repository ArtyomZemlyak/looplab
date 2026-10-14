"""Doc 79 §2: what per-attempt git identity costs on a realistic repository, beside doc 37's arms.

Usage:  python benchmarks/node_git_identity_cost.py <git-clone-of-task-repo> <scratch-dir> [reps]

Arms, each repeated `reps` times (median, min-max reported), all on the scratch filesystem:

  copy_seed        the shipped seed (`workspace_seed.seed_repo_tree`, mode auto: tracked files only)
  src_worktree     `git worktree add --detach` of the SOURCE repo + `worktree remove` (doc 37's arm)
  base_commit      one base commit in a fresh private run repo from the copied seed (fast-import)
  attempt_1file    one attempt commit on that base, a one-line env overlay (the external-agent shape)
  attempt_20files  one attempt commit, 20 changed source files
  agent_worktree   `git worktree add` of the PRIVATE run repo at the base + `worktree remove`

Disk: the private repo's bytes after the base and after 50 one-file attempts.
Writes nothing outside <scratch-dir>; reads the clone only.
"""
from __future__ import annotations

import json
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path


def _du(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())


def _timed(fn, reps):
    out = []
    for i in range(reps):
        t0 = time.perf_counter()
        fn(i)
        out.append(time.perf_counter() - t0)
    return {"median_s": round(statistics.median(out), 4), "min_s": round(min(out), 4),
            "max_s": round(max(out), 4), "reps": reps}


def main(src: Path, scratch: Path, reps: int = 5) -> dict:
    from looplab.engine.code_repo import CodeRepo
    from looplab.engine.workspace_seed import seed_repo_tree, seeded_base_revision
    scratch.mkdir(parents=True, exist_ok=True)
    results: dict = {"source": str(src)}
    tracked = subprocess.run(["git", "-C", str(src), "ls-files", "-z"], capture_output=True,
                             check=True).stdout.split(b"\0")
    results["tracked_files"] = len([t for t in tracked if t])

    def copy(i):
        dst = scratch / f"copy{i}"
        seed_repo_tree(src, dst, shutil.ignore_patterns(".git"), "auto")
    results["copy_seed"] = _timed(copy, reps)
    seeded = scratch / "copy0"
    rows: list = []
    receipt = seeded_base_revision(seeded, on_file=lambda n, d, x: rows.append((n, d, bool(x))))
    results["seed_bytes"] = receipt["bytes"]
    results["seed_files"] = receipt["file_count"]
    for i in range(reps):
        shutil.rmtree(scratch / f"copy{i}", ignore_errors=True)

    clone = scratch / "srcclone"
    subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(src), str(clone)], check=True)

    def worktree(i):
        wt = scratch / f"wt{i}"
        subprocess.run(["git", "-C", str(clone), "worktree", "add", "-q", "--detach", str(wt)],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(clone), "worktree", "remove", "--force", str(wt)],
                       check=True, capture_output=True)
    results["src_worktree"] = _timed(worktree, reps)

    def base(i):
        run = scratch / f"run{i}"
        repo = CodeRepo(run).ensure()
        repo.write_tree_commit("refs/looplab/bases/x", rows, message="base\n", when=1)
    results["base_commit"] = _timed(base, reps)
    repo = CodeRepo(scratch / "run0")
    base_sha = repo.rev("refs/looplab/bases/x")
    results["repo_bytes_after_base"] = _du(repo.path)
    py = [n for n, _, _ in rows if n.endswith(".py")][:20]

    def one(i):
        repo.write_overlay_commit(f"refs/looplab/attempts/a{i}", base_sha,
                                  {"looplab/experiment.env": (f"ARM=A{i}\n".encode(), False)},
                                  message=f"attempt {i}\n", when=2 + i)
    results["attempt_1file"] = _timed(one, reps)

    def twenty(i):
        changes = {n: ((dict((r[0], r[1]) for r in rows)[n] + f"\n# edit {i}\n".encode()), False)
                   for n in py}
        repo.write_overlay_commit(f"refs/looplab/attempts/b{i}", base_sha, changes,
                                  message=f"attempt20 {i}\n", when=100 + i)
    results["attempt_20files"] = _timed(twenty, reps)
    for i in range(50):
        repo.write_overlay_commit(f"refs/looplab/attempts/c{i}", base_sha,
                                  {"looplab/experiment.env": (f"ARM=C{i}\n".encode(), False)},
                                  message=f"attempt {i}\n", when=200 + i)
    results["repo_bytes_after_50_attempts"] = _du(repo.path)

    def agent(i):
        wt = scratch / f"agent{i}"
        repo.git("worktree", "add", "-q", "--detach", str(wt), base_sha)
        repo.git("worktree", "remove", "--force", str(wt))
    results["agent_worktree"] = _timed(agent, reps)
    return results


if __name__ == "__main__":
    out = main(Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else 5)
    print(json.dumps(out, indent=1))
