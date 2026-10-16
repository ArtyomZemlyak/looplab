# Code of every experiment, and getting fixes into your repository

Every experiment LoopLab evaluates on a repository task is a **git commit** in a private repository
that belongs to the run, `RUN_DIR/code/repo.git`. Your own repository is never written during the
run. When you want the run's fixes, you export them into your repository as a **new branch**.
Design and measurements: [doc 79](../79-node-git-identity-and-code-upstream-2026-10-14.md).

## What is recorded

| Commit | Tree | Parent |
|---|---|---|
| base | exactly what LoopLab seeded: your working tree, uncommitted edits and untracked protected files included | the first base: the commit your editable was at when the run started; later: the base it replaced (an upstream advance) |
| attempt | the base plus the experiment's own file changes, read after materialization | the base it ran on (first parent); experiments an agent really `git merge`d (see below) |
| repair | the attempt after an in-place repair during its evaluation | the attempt |

Your history is the first base's history: `git log` and `git blame` in a worktree show your
commits, and `git diff <your HEAD> base` is exactly what you had not committed. The commit and up to
200 commits behind it are fetched from your repository, which is only read; the commit itself is
also kept in `RUN_DIR/code/origin/`, so a restored run rebuilds the same base without your
repository. The first base has no parent when the editable is a subdirectory of its repository,
there are several editables, the source is not git or `seed_base` is pinned (`base.origin.reason` on
the `code_snapshot` row says which). Runs recorded before 2026-10-16 keep their parentless bases.
Once a base id is recorded it never changes: if a rebuild can get neither your repository nor
`code/origin/`, the base still names the same parent and its history simply ends there
(`code-status` shows `history: unavailable` on that base). Your repository is only read, also
when another user owns it; the history fetch is bounded (30 s) and does not delay other work on
the run.

Branches: `base` (the active base), `nodes/n<id>` (latest attempt of each node), `champion`,
`agent/<name>` (external-agent worktrees). Each attempt also gets an `effective_config` record: the
learning rate, batch, steps, warmup, seed and similar values the run actually showed (launch env,
the experiment's env files, configuration dumps in stage logs), checked against the parameters the
experiment declared. A declared value the run did not use is listed under `mismatches`.

```bash
looplab code-status runs/my-run            # bases, attempts, what is pending upstream
looplab code-status runs/my-run --sync     # old runs: build the commits from the event log first
git --git-dir runs/my-run/code/repo.git log --graph --all --oneline
git --git-dir runs/my-run/code/repo.git show nodes/n3     # the review of node 3's change
```

The same data is `GET /api/runs/{run_id}/code`; one commit's diff is
`GET /api/runs/{run_id}/code/diff?commit=<sha>`.

## What is pending upstream

With a task `upstream` block, the upstream lane (`upstream_mode` auto/propose) still decides what
reaches the base, and only through its measured gate. `code-status` lists each proposal with its
source experiment, that experiment's metric and the last gate verdict, plus nominations not yet
proposed. A run without an upstream block lists **code changes without a lane**: measured
experiments that changed code rather than only their recipe — export them by node id.

## Export into your repository

```bash
looplab code-export runs/my-run --repo ~/src/my-task --dry-run          # see the plan
looplab code-export runs/my-run --repo ~/src/my-task --patches /tmp/mr  # create looplab/<run_id>
git -C ~/src/my-task log --stat looplab/my-run
```

The branch holds, in order: the code the run started from (edits that were never committed in
your repository, or were made in a copy), each upstream base advance with its gate evidence, and the
champion's change (`--node <id>` or `--node none` to choose). The node step uses only that
experiment's own commit (recorded, or built from the log for an older run); when it has none the
step is skipped and says so — an earlier champion's commit is never exported under its name. It
starts from the commit your editable source was at when the run started (never a data or reference
mount's), if your repository has it, else `HEAD` (`--onto` to choose). Files are placed at the
editable's path inside your repository, read from the task the way the run read it (`repo`,
`editable_path` or `editables`, `~` expanded); a run whose named editables do not sit together under
one directory of the repository is refused until you pass `--prefix`. Your
`HEAD`, index, working tree, other branches and remotes are not touched and nothing is pushed —
open the merge request yourself, or apply the `--patches` series. A conflict stops the export and
names the paths; nothing is created. A file's mode travels with its bytes: a `chmod +x` exports with
or without a content change, and a mode your branch changed differently is reported as a conflict
(`mode_conflicts`) rather than silently picked. The executable bit of the working tree the run
started from is exported only when neither your target repository nor the source checkout says
`core.fileMode=false` (what git writes on vfat, NTFS, CIFS or WSL's `/mnt/c`, where every file
reads as executable); otherwise every file keeps the mode its commit has (`file_mode` in the
report).

## External agents

The default agent workflow is a git worktree of the run's code; `inject_node` with `files` still
works.

```bash
looplab code-worktree runs/my-run fix-lr        # RUN_DIR/code/worktrees/fix-lr, branch agent/fix-lr
# edit, run, commit there; `git merge nodes/n3` combines experiment 3's change; then submit:
#   submit_experiment(..., code_from={"worktree": "fix-lr"}, parent_ids=[3])   (MCP)
#   inject_node {"idea": {...}, "code_from": {"worktree": "fix-lr"}, "parent_ids": [3]}
# a unified diff against the active base:  "code_from": {"patch": "--- a/x.py\n+++ b/x.py\n..."}
# a branch of your own repository:         "code_from": {"git": {"repo": "/abs/path", "ref": "my-branch"}}
```

The worktree is checked out from the run's private repository at the active base (or
`node:<id>`, or a commit of the run — a commit of your own history is refused as
`code_start_not_run_commit`), never from your repository; MCP `code_worktree` / `code_worktrees`
make and list them. On a tmpfs run root, `--path` (route/MCP `path`) puts it on persistent disk — refused inside
your source tree or another part of the run directory; the mirror then backs up its branch but not
its files, and a restore re-links the same directory. The submitted overlay is always the whole
change over the seed base, so a worktree started from `node:<id>` carries that experiment's change
plus yours. Combining experiments is an ordinary `git merge nodes/n<id>` in the worktree: the
injected experiment's commit then has those experiments as git parents (each checked against the
worktree's history), so merging it elsewhere brings all of them — when the worktree is submitted
clean; with uncommitted changes no git parent is claimed. An ensemble LoopLab builds itself
only names its other parents in a `Looplab-Co-Parents` trailer — it keeps one git parent, because
its code is a new change, not a merge git could vouch for. A `git` source (e.g. Claude Code's
`worktree-agent-<id>` branch of your repository) is fetched read-only into
`refs/looplab/foreign/<commit>` and three-way merged onto the active base from where it left the
source (git >= 2.38; `repo` may be any directory of that repository, and a shallow clone works):
your uncommitted edits stay, an overlap is `409 code_from_merge_conflict`
naming the paths, and the reply warns how many base files the branch lacked. Your repository's refs,
objects, config, index and hooks are never written. `POST /api/runs/{run_id}/code/overlay` (MCP
`code_overlay`) previews what any of these becomes, refused exactly as the inject would be
(`serve/code_from.py`): paths outside the task's edit surface or protected files, an empty change
(`code_from_empty`), a worktree whose directory is gone (`409 code_worktree_missing`), and a change
made on a base an upstream advance has since moved under a path it also changed
(`409 code_from_stale_base` naming the paths: `git merge base` in the worktree, then resubmit).
`start` belongs to a patch only. With the scoped agent token `git.repo` must be the run's own
source repository or a git worktree of it (`403 code_from_git_not_permitted`); the owner may name
any repository. A rejected inject keeps the refusal's `paths` on its command record; a git
timeout is a 503 with no record, so the same key retries.
A worktree you have not submitted yet is in no event, so the log cannot rebuild it; on a tmpfs run
root `looplab run-mirror` backs up its branch and uncommitted files as `code/agent_backup/`, and
`looplab code-status --sync` (and nothing else) re-creates the worktree after a restore
([deployment](deployment.md#run-directories-in-tmpfs-the-durable-mirror)).
