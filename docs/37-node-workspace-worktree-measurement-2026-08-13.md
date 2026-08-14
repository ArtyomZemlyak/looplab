# F3 · Node workspaces on `git worktree` — measured, and declined

**Status: a measurement and a decision, 2026-08-13. Nothing shipped.** Doc 29 §F3 said "measure the
seed cost before paying for that coupling." This is that measurement, taken on the real testbeds on
this box, and the answer is **no**: `git worktree` costs more disk than the copy it would replace,
saves a sub-second per node against a node that trains for 40 minutes, and *breaks the seed's
correctness today* on the v1 testbed. The disk problem the proposal was reaching for turns out to be
a different mechanism entirely, and §6 names it.

> **Status update (2026-08-14).** §7's requested doc-29 edit landed:
> `docs/29-operator-backlog-2026-08-11.md` §F3 now reads **MEASURED AND DECLINED (2026-08-13)**.
> None of §8's three rungs has shipped: the event registry still carries only
> `workspace_seeded`/`workspace_changed` (`events/types.py` — no workspace-size receipt anywhere),
> no disk-budget cue exists beside the time-budget/GPU-budget hints in `engine/proposal_cues.py`,
> and no retention policy or reaper exists in `looplab/`. R1 remains the recommended next step. Its
> cheapest shape, respecting §9's caveat that a per-node walk on geesefs is not free: a fold-ignored
> `DIAGNOSTIC_EVENTS` member appended at eval end from the eval path that already owns the workdir,
> carrying total bytes plus the largest subtrees from a walk bounded by an explicit entry cap — with
> the cap's exhaustion recorded in the receipt, so a truncated measurement can never read as a small
> workspace.

Everything below is measured, not reasoned. Where a number is a median over repetitions the spread is
given, because two of the conclusions turn on whether a difference is real.

---

## 1 · What the seed actually copies

`engine/workspace.py::seed_repo_tree` under the default `seed_mode="auto"` shells out to
`git -C <src> ls-files -z` and `shutil.copy2`s each result. It copies **git-tracked files only**;
untracked paths are never walked. The two real testbeds:

| testbed | used by | tracked entries | tracked bytes | whole tree on disk |
|---|---|---|---|---|
| `/home/jovyan/data/vectorizer/dense-retrieval` | v1 (`rubertlite-dense-retrieval`, `rubert-dr-*`) | 17 | **223,963 B** (0.22 MB) | **203,016,673,280 B (189 GiB)** |
| `/home/jovyan/data/vectorizer-unified` | v6 / v7 (`rubertlite-dr-unified-*`) | 76 (75 files + 1 submodule dir) | **910,829 B** (0.91 MB) | 937,381,888 B (894 MiB) |

Both are worktrees of the ONE repo `/home/jovyan/data/vectorizer` (branches
`dense-retrieval-tests` and `dense-retrieval-unified`); its `.git` is 1,217,024 B.

The 189 GiB in the v1 testbed is entirely **untracked** and entirely the operator's own: top-level
`models/` 29.7 GB, `pplx-30e-stf-norm-new-loss-` 21.5 GB, `bge/` 20.4 GB, three `pplx-30e-*` dirs at
14.3 GB each, four `nomic-moe-*` at 11.4 GB each, `e5-base-30e-stf-norm-v2` 6.7 GB, `core/` 5.0 GB.
There is no `.gitignore`. The 17 tracked paths are flat `.py` files plus `README.md` and
`requirements.txt`.

**So the seed copies 0.22 MB out of a 189 GiB tree — 0.00011 % of it.**

### Never once a full copy, on any real run

Across every run under `runs/`, the `workspace_seeded` records are:

```
97  .[auto]:16 tracked
17  .[auto]:75 tracked
 9  .[auto]:17 tracked
 9  .[auto]:copytree
```

All nine `copytree` records belong to `live-asha-0804`, `live-stall-0804`, `live-deps4-0804` and
`live-deps5-0804`, whose editables are `/home/jovyan/data/asha-testbed`,
`/home/jovyan/data/stall-testbed` and `/home/jovyan/data/deps-testbed` — tiny synthetic trees that
are not git repos, which is the documented `tracked is None` fallback. **Zero copytree seeds against
either real testbed.** The premise "four node workspaces were each a full 189 GB copy of the repo"
is not what the log records, and §6 shows what actually happened.

## 2 · What the seed actually costs, on real runs

From each run's own `spans.jsonl` (`seed_workspace` is a real span):

| run | seeds | median seed | total seed | total `evaluate` | seed share |
|---|---|---|---|---|---|
| `rubertlite-dr-unified-v6` | 8 | 1.651 s | 12.1 s | 40,742.3 s | **0.030 %** |
| `rubertlite-dr-unified-v2` | 6 | 2.387 s | 15.2 s | 12,087.2 s | 0.126 % |
| `rubert-dr-0807` | 12 | 0.868 s | 11.8 s | 272,134.7 s | **0.0043 %** |
| `rubertlite-dr-unified-v4` | 11 | 0.676 s | 8.9 s | 166,232.7 s | 0.0054 % |
| `rubertlite-dense-retrieval` | 81 | 1.065 s | 91.3 s | 452,121.1 s | **0.020 %** |

The 81-seed run spent **91 seconds seeding and 125 hours evaluating**. Any change to the seed
mechanism is bounded above by that 91 seconds. A perfect seed — zero cost, instantaneous — buys back
0.02 % of that run.

## 3 · The head-to-head, on the same mount

A clone of the real repo (`git clone --no-hardlinks`) placed on the same `fuse.geesefs` mount,
checked out at `dense-retrieval-unified` (75 tracked files, 910,829 B). Arm A is the shipped
`seed_repo_tree("auto")` code path verbatim. Arm B is `git worktree add --detach`. Teardown is
measured too, because `WorkspaceSeeder.materialize` `rmtree`s the workdir on **every**
re-materialization. Five repetitions per cell; `W` is the number of workspaces built concurrently.

| W | arm | seed median | seed min–max | teardown median | **full cycle** |
|---|---|---|---|---|---|
| 1 | copy | 0.2978 s | 0.2415 – 0.3109 | 0.0482 s | **0.346 s** |
| 1 | worktree | 0.2836 s | 0.2646 – 0.3185 | 0.0928 s | **0.376 s** |
| 4 | copy | 0.4588 s | 0.3060 – 0.4676 | 0.0588 s | **0.518 s** |
| 4 | worktree | 0.2998 s | 0.2890 – 0.3128 | 0.1049 s | **0.405 s** |
| 8 | copy | 0.6240 s | — | 0.0610 s | **0.685 s** |
| 8 | worktree | 0.2000 s | — | 0.0800 s | **0.280 s** |
| 12 | copy | 0.7880 s | — | 0.0920 s | **0.880 s** |
| 12 | worktree | 0.3090 s | — | 0.1540 s | **0.463 s** |

Read this honestly in both directions.

**Serially the worktree is slower.** 0.376 s against 0.346 s per full cycle: its seed advantage
(14 ms) is inside the noise band, and its teardown is genuinely 1.9× more expensive (`git worktree
remove` has to unregister an admin directory; `rmtree` of 75 files does not).

**Under concurrency the worktree is genuinely faster, and it does not matter.** At W=12 it saves
0.417 s per batch. The 81-seed `rubertlite-dense-retrieval` run is the largest seeding load in the
corpus; at its measured seed total of 91.3 s the very best case for the worktree arm is ~45 s saved
across 125 hours of evaluation — **0.010 percentage points**. That is the entire upside, and it is
the upside *before* paying any of §4 or §5.

## 4 · The disk claim is backwards: a worktree costs MORE

This is the part the proposal has exactly inverted, and it is worth stating plainly because the word
"worktree" reads like "link":

> **`git worktree add` is a checkout, not a link.** It writes every tracked file into the destination
> as a real file, byte for byte, exactly as the copy does. Nothing is shared except the object
> database — and the copy never copies `.git` in the first place (`shutil.ignore_patterns(".git", …)`).

Measured, per node workspace:

| | bytes in the workspace | admin bytes in `$GIT_DIR/worktrees/<name>` | **total** |
|---|---|---|---|
| copy | 910,829 | 0 | **910,829** |
| worktree | 910,907 (+78 B for the `.git` pointer file) | 18,944 (`index`, `HEAD`, `ORIG_HEAD`, `commondir`, `gitdir`, `logs/HEAD`) | **929,851** |

**+19,022 B per node, +2.1 %.** F3's stated win is disk. The measurement is a disk loss, on every
node, forever. There is no configuration of this that recovers the win, because the win was never
there: the bytes the proposal hoped to avoid are the 189 GiB of *untracked* files, and §1 shows the
copy has never touched those.

## 5 · The costs — three retired by measurement, two confirmed and disqualifying

Doc 29 §F3 lists four specific costs. Measuring them separated the folklore from the real ones.

### 5a · geesefs facts (both confirmed, neither one bites `worktree add`)

| probe | result |
|---|---|
| `chmod +x` then `ls -l` | mode stays `0o100600` — the bit is silently dropped |
| `os.access(path, os.X_OK)` | `False` |
| executing the file | `Permission denied` |
| `os.link` **same mount** | `OSError [Errno 95] Operation not supported` |
| `os.link` cross mount | `OSError [Errno 18] Invalid cross-device link` |
| `os.symlink` | works |
| `findmnt -no FSTYPE` | `fuse.geesefs` |

Note the sharper form of the hardlink fact: on geesefs `os.link` fails **ENOTSUP even within one
mount**, not merely `EXDEV` across mounts. Hardlinks are not available at all here.

Neither bites `git worktree add`, which hardlinks nothing and executes nothing. And the exec bit is
already accommodated: `git -C /home/jovyan/data/vectorizer config core.filemode` returns **`false`**.
Both arms lose the exec bit identically — `shutil.copy2` cannot set it either. The repo does carry
four `100755` tracked files (`ranking/train_*.py`), none of them inside a seeded editable.
**Retired as a differentiator.**

### 5b · The shared-index-lock fear is measurably unfounded

`git worktree` gives each worktree its **own** index under `$GIT_DIR/worktrees/<name>/index`; it does
not take the common `index.lock`. Measured:

- 8 and 12 concurrent `git worktree add` against one source: **0 failures**, per-op max 0.195 s and
  0.304 s respectively — i.e. no lock queueing at all.
- A stale 0-byte `$GIT_DIR/index.lock` left in place: `git worktree add` → **rc=0**.
- A stale 0-byte `$GIT_DIR/worktrees.lock` left in place: `git worktree add` → **rc=0**.

The stale-lock wedge that has twice jammed commits in *this* repo does not reproduce here. It is also
worth saying why the framing over-weighted it: the contended `.git` in the incident is **looplab's**,
which currently has 46 live agent worktrees. The `.git` a node workspace would share is the
**testbed's**, which no agent touches. **Retired.**

### 5c · The source-repo blast radius is real, and reproduces

A node workspace that is a git worktree *is a live git working tree wired to the operator's object
database*. Measured in a worktree carrying synthesized run artifacts:

- `git status` on a clean node worktree: 0.241 s. On one carrying 48 untracked run artifacts:
  0.317 s (and one entry — the run's own output, now reported as repo noise).
- `git add -A` from inside the node worktree: **rc=0**, 0.158 s, and the blobs landed in the
  **source repo's** `.git/objects`.

Scale that to the v1 testbed: a node that runs `git add -A` — which a Developer with shell access can
do, and which is the single most common way to "save my work" — ingests the operator's 189 GiB of
untracked checkpoints into their object store, irreversibly (loose objects and packs are not
casually removable). The copy arm cannot do this at all: a copied workspace is not a git repository,
so `git status` there is an instant "not a git repository" and the source is unreachable by
construction. **Confirmed. This is the same class of harm as the fence incident being paid for
today** — a node reaching outside its own workspace — and it is being *added*, not removed.

### 5d · The disqualifier nobody listed: a worktree cannot seed uncommitted work

`git worktree add` checks out a **commit**. The copy seeds the **working tree** — what the operator
has on disk right now. On the v1 testbed those are not the same thing:

```
$ git --no-optional-locks -C /home/jovyan/data/vectorizer/dense-retrieval status --porcelain -uno
 M README.md
 M dense-retrieval/README.md
 M dense-retrieval/dataset.py
A  dense-retrieval/looplab_eval.py
 M dense-retrieval/loss.py
 M dense-retrieval/model.py
 M dense-retrieval/requirements.txt
 M dense-retrieval/test.py
 M dense-retrieval/to_stf.py
 M dense-retrieval/tokenizing.py
 M dense-retrieval/train.py
 D ranking/vectorizator
```

**Ten of the seventeen tracked files under the seeded editable differ from HEAD**, and
`looplab_eval.py` — the operator's protected scorer — is staged and has *never been committed*. That
file is the subject of `seed_protected_files`'s entire docstring: "an operator scorer that was never
committed — the normal state of a file added to drive LoopLab".

A worktree seed would silently train against the operator's **last commit** instead of their current
code. No error, no `expect_failed`, no violation — a run that reports a metric about a source the
operator is not working on. That is precisely the v6-node-4 failure mode: a number recorded about
somebody else's work, with the artifact contract passed. Doc 36's table puts this in the RECORD row,
where a wrong answer is not recoverable.

(`vectorizer-unified` is currently clean, so this would have bitten the v1 testbed and not v6/v7 —
which is exactly how a defect of this shape survives a demo.)

## 6 · Where the 727 GB actually came from

The seed contributed **0.22 MB per node** to it. Here is what the bytes are, from a surviving
workspace of the v6 run:

`runs/rubertlite-dr-unified-v6/nodes/node_4` = 944,779,776 B. Of that,
`vectorsearch/experiments/unified-baseline_rubert-tiny-lite/` is 937,847,296 B:

| entry | bytes |
|---|---|
| `checkpoint-7060` | 279,736,320 |
| `checkpoint-5648` | 279,703,552 |
| `checkpoint-4236` | 279,671,808 |
| `final` | 95,741,952 |
| `eval`, `tests`, `runs` | 2,249,216 |

and the reason there are three intermediate checkpoints is one line the node itself wrote:

```
vectorsearch/training/build_trainer.py:85    save_strategy="steps",
vectorsearch/training/build_trainer.py:86    save_steps=config.val_frequency,
vectorsearch/training/build_trainer.py:87    save_total_limit=3,
```

**The seed is 0.096 % of that workspace. The node's own retained intermediate checkpoints are
99.2 %.** All seven v6 node workspaces measure 848 MB – 1.04 GB, and the run directory totals are
`rubertlite-dense-retrieval` 46.4 GB, `rubert-dr-0807` 10.8 GB, `rubertlite-dr-unified-v4` 7.4 GB,
`rubertlite-dr-unified-v2` 2.2 GB — in every case dominated by what the run wrote, not what it copied.

Now scale the *same* retention rule to the v1 testbed, whose per-experiment output directories
measure 11.4 – 29.7 GB (§1): three intermediate checkpoints plus a final at that scale is ~189 GB in
one node workspace, and four such nodes is ~756 GB. **That is the reported figure, and it is entirely
the run's own training output.** `git worktree` would have changed it by −0.22 MB per node and then
added 19 KB back.

### Why it was misattributed, which is the fixable part

`workspace_seeded` is the **only** workspace fact in the event log, and it says `.[auto]:75 tracked`.
It is an accurate statement about 0.9 MB, and it is the sole thing anyone reading the log can see
about a directory that will grow to 944 MB. Nothing in LoopLab measures a node workspace after the
seed. So the one visible number named the copy, and the copy got blamed.

## 7 · Decision

**Do not adopt `git worktree` for node workspaces. Keep the materialized tracked copy.** The
scoreboard, all measured on this box:

| | copy (today) | `git worktree` |
|---|---|---|
| disk per node | 910,829 B | **929,851 B (+2.1 %)** |
| full cycle, W=1 | **0.346 s** | 0.376 s |
| full cycle, W=12 | 0.880 s | **0.463 s** |
| best-case saving on the corpus's heaviest run | — | ~45 s of 452,121 s (**0.010 pp**) |
| seeds the operator's uncommitted work | **yes** | **no — 10 of 17 files wrong on the v1 testbed** |
| a node can write into the source repo | no (not a repo) | **yes — `git add -A` measured rc=0** |
| geesefs exec bit / hardlinks | unaffected | unaffected |
| shared index-lock contention | n/a | none measured (own index per worktree) |

Two of the four costs doc 29 listed are retired by measurement, and I would rather record that than
leave folklore standing. But the proposal fails on its own stated terms before any of them: the win
is disk, and the measurement is a disk **loss**. The seed-time win is real only under concurrency and
is four orders of magnitude below the wall clock it would improve. And the correctness regression in
§5d is disqualifying on its own — it is a RECORD-row failure in doc 36's terms, of exactly the shape
the read fence shipped this week to close.

**Update `docs/29-operator-backlog-2026-08-11.md` §F3 to declined-with-measurement, not deferred.**
Deferred invites a re-litigation that will re-derive these same numbers.

## 8 · The cheaper root fix, in the order it should be paid for

The root is not the copy. It is that **a node workspace accumulates the node's own retained training
checkpoints, and nothing measures or reclaims them.** Three rungs, cheapest first:

**R1 — Make the workspace's real size a recorded fact.** Today the log's only workspace statement is
about 0.9 MB; the 944 MB is invisible until someone runs `du`. A per-node byte receipt at eval end,
naming the largest subtrees, would have named `checkpoint-*` immediately and this entire proposal
would not have been written. It can be a fold-ignored `DIAGNOSTIC_EVENTS` member, so it touches no
engine invariant and no replay behaviour — the cheapest possible change that permanently retires the
misattribution. **This is the recommended next step.**

**R2 — Bound retention where it is generated.** `save_total_limit=3` is in the *node's own trainer*,
written by the agent. Doc 36's table places "how much intermediate state to keep" in the NEXT row,
not the RECORD row, so a workspace disk budget stated to the Developer is a legitimate agent-side
rung. It must not be the only one — the agent writes the text, so it cannot also be the accountant.

**R3 — Reclaim intermediate checkpoints from non-champion nodes at run end.** Deterministic and by
far the largest saving (it is ~99 % of every run directory measured above), but it deletes evidence:
`engine/metric_salvage.py` reads a failed node's workspace, and the salvage decision is precisely
about a node whose artifact is not where its manifest said. This needs an explicit written retention
policy — *which* files survive a node, and for how long — before any code. Shipping it without one
is how a reaper eats the champion's checkpoint.

## 9 · Risks I could not retire

- **Concurrency was measured on an uncontended clone**, with no engine running and no other writer on
  that repo. That is the *best* case for the worktree arm, and it still loses on disk and on
  correctness — so the conclusion is robust to the measurement being generous. It does mean the
  W=8/12 numbers are a ceiling on the worktree's advantage, not a field measurement.
- **The 727 GB run itself is gone.** `runs/rubertlite-dr-unified-v5` retains only three node
  directories (92.2 MB, 48.6 KB, 1.5 KB) and no `events.jsonl`, so §6 reconstructs the arithmetic
  from the surviving v6 workspaces and the v1 testbed's own experiment-directory sizes rather than
  measuring the dead run directly. The mechanism is confirmed (`save_total_limit=3`, three retained
  checkpoints, measured byte-for-byte in v6 node_4); the exact multiplier that produced 727 rather
  than 756 GB is not.
- **`git status` blast radius was measured with 48 synthesized artifacts**, not with a real 189 GiB
  untracked tree — running that against the operator's repo would have meant writing to a read-only
  testbed. The `git add -A` → shared-object-DB path is confirmed; its *cost* at 189 GiB is inferred.
- **R1's event shape is not designed here.** Naming "largest subtrees" cheaply on geesefs is its own
  measurement problem — an `os.walk` over a finished workspace is 168 files for v6 node_4 but is
  unbounded in general, and a per-node walk on this mount is not free.
