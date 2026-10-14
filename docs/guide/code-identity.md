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
champion's change (`--node <id>` or `--node none` to choose). It starts from the commit your source
was at when the run started, if your repository has it, else `HEAD` (`--onto` to choose). Your
`HEAD`, index, working tree, other branches and remotes are not touched and nothing is pushed —
open the merge request yourself, or apply the `--patches` series. A conflict stops the export and
names the paths; nothing is created.

## External agents

An external agent can keep using `inject_node` with `files`, or work in a git worktree:

```bash
looplab code-worktree runs/my-run fix-lr        # RUN_DIR/code/worktrees/fix-lr, branch agent/fix-lr
# edit, run, commit in that directory as usual, then submit:
#   inject_node {"idea": {...}, "code_from": {"worktree": "fix-lr"}}
# or a unified diff against the active base:
#   inject_node {"idea": {...}, "code_from": {"patch": "--- a/x.py\n+++ b/x.py\n..."}}
```

The worktree is checked out from the run's private repository at the active base, never from your
repository. `POST /api/runs/{run_id}/code/overlay` previews the files a worktree or patch becomes;
paths outside the task's edit surface or protected files are refused, as in any inject.
