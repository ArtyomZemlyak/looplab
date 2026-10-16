# Code of every experiment, and getting fixes into your repository

Every experiment LoopLab evaluates on a repository task is a **git commit** in a private repository
that belongs to the run, `RUN_DIR/code/repo.git`. Your own repository is never written during the
run. When you want the run's fixes, you export them into your repository as a **new branch**.
Design and measurements: [doc 79](../79-node-git-identity-and-code-upstream-2026-10-14.md).

## What is recorded

| Commit | Tree | Parent |
|---|---|---|
| base | exactly what LoopLab seeded: your working tree, uncommitted edits and untracked protected files included | the base it replaced (an upstream advance), or none |
| attempt | the base plus the experiment's own file changes, read after materialization | the base it ran on |
| repair | the attempt after an in-place repair during its evaluation | the attempt |

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
(`mode_conflicts`) rather than silently picked. With `core.fileMode=false` in your repository, the
executable bit of the working tree the run started from is not exported.

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
`node:<id>`), never from your repository; MCP `code_worktree` / `code_worktrees` make and list
them. On a tmpfs run root, `--path` (route/MCP `path`) puts it on persistent disk — refused inside
your source tree or another part of the run directory; the mirror then backs up its branch but not
its files, and a restore re-links the same directory. The submitted overlay is always the whole
change over the seed base, so a worktree started from `node:<id>` carries that experiment's change
plus yours, and one that merged `nodes/n<id>` carries both. A `git` source (e.g. Claude Code's
`worktree-agent-<id>` branch of your repository) is fetched read-only into
`refs/looplab/foreign/<commit>` and three-way merged onto the active base from where it left the
source (git >= 2.38): your uncommitted edits stay, an overlap is `409 code_from_merge_conflict`
naming the paths, and the reply warns how many base files the branch lacked. Your repository's refs,
objects, config, index and hooks are never written. `POST /api/runs/{run_id}/code/overlay` (MCP
`code_overlay`) previews what any of these becomes; paths outside the task's edit surface or
protected files are refused, as in any inject.
A worktree you have not submitted yet is in no event, so the log cannot rebuild it; on a tmpfs run
root `looplab run-mirror` backs up its branch and uncommitted files as `code/agent_backup/`, and
`looplab code-status --sync` re-creates the worktree after a restore
([deployment](deployment.md#run-directories-in-tmpfs-the-durable-mirror)).
