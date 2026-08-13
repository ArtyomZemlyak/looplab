# Path isolation: what the read fence establishes, and the options for a root fix (2026-08-13)

**Status: analysis, not a decision.** Nothing here is implemented. Every number below was measured on
this box on 2026-08-13 unless marked otherwise; every mechanism claim was either read out of the code
or driven in a bounded experiment in the scratchpad. Where I could not verify something I say so.

> Note on the file number: the two sibling notes written the same day landed as `32-` and `33-`, and
> this file keeps `31-`. All three are indexed in `00-INDEX.md` and listed in `mkdocs.yml`'s nav —
> `validation.nav.omitted_files: info` means an unlisted page would NOT fail `mkdocs build --strict`,
> so being reachable by URL is not evidence of being findable.

---

## 0. The one-paragraph answer

The fence is a patch, and the owner is right, but not for the reason it is usually stated. It is not
mainly that a path fence has holes (it does — measured below, and one of them is `safetensors`, the
loader for the exact file type both incidents corrupted). It is that **the fence is a read-side
mechanism aimed at a write-side authoring defect whose evidence the engine already holds, in a durable
event, hours before any GPU is spent.** The absolute path that corrupted v6 node 4 is sitting in that
node's `node_created` event, in `node.files["vectorsearch/configs/config.yaml"]`, verbatim. The fence
waits until a training process tries to follow it. And separately: nothing in the run binds the
recorded metric to the artifact the node produced, so even a perfect fence leaves "the number is about
this node's model" as an assumption rather than a fact.

The two cheapest interventions I found are also the two that score best on the corpus:
**(O1)** a materialize-time refusal of an absolute source-root path in the node's own working set —
**0.08–0.62 s per node, 2 hits across 10 real nodes, and both hits are exactly the two defective
nodes, zero false positives**; and **(O2)** binding the reported metric to a digest of the artifact
the scoring stage actually read — **~2 % on the open hot path to record, 0.3 s to digest a 92 MB
checkpoint.** Everything else is either an expensive hardening of a mechanism that is in the wrong
place, or a sandbox-tier rewrite.

---

## 1. What I verified, and how

Read: `looplab/runtime/read_fence.py`, `runtime/sandbox.py::run_argv`, `runtime/command_eval.py`
(`needs` / `expect` / `verify_stage_inputs` / `verify_stage_artifacts` / `stage_output_producers`),
`engine/resources.py::_read_fence_dir`/`_fenced_env`, `engine/eval_stages.py::_resolve_stages`,
`engine/workspace.py` (all of it), `engine/eval_dispatch.py::_data_binds`,
`adapters/repo_task.py::_entrypoint_protect`, `adapters/repo_write_tools.py::_source_root_note`,
`core/config.py::Settings.read_fence`, `docs/29-operator-backlog-2026-08-11.md` §F1c/§F1e.

Runs read (read-only): `runs/rubertlite-dr-unified-v5` nodes 0/2/4,
`runs/rubertlite-dr-unified-v6` nodes 0–6, both `events.jsonl`, both snapshots.

Experiments run: all in
`/tmp/claude-1000/-home-jovyan-data/…/scratchpad/fence/` — a synthetic "editable source root" with a
real 92 MB SentenceTransformer copied into it, the **real generated fence** (`read_fence.render`, not
a re-implementation), a 30-probe escape battery, an 11-variant hot-path benchmark, and a small
`LD_PRELOAD` shim compiled with the box's gcc.

**Not verified, deliberately:** anything about `/home/jovyan/data/vectorizer-unified` — I never read,
stat'd or listed it. And **no mount namespace was entered and no bind mount was performed**; §5.5 is
design-only, and its feasibility claims are marked UNVERIFIED. I checked only that
`/usr/bin/unshare` exists and `/proc/sys/user/max_user_namespaces` is non-zero (8255467), which says
the kernel would permit it — not that the design works.

---

## 2. What the two incidents actually were (both re-derived from the runs)

### v6 node 4 — the corrupt metric

`node_created` carries `files: ['looplab_stages.json', 'vectorsearch/configs/config.yaml',
'vectorsearch/train.py']`, and line 215 of that staged config is

```yaml
checkpoint_path: /home/jovyan/data/vectorizer-unified/vectorsearch/experiments/unified-baseline_rubert-tiny-lite/final
```

`looplab_stages.json` declares exactly one stage, `train`, with
`expect.files = [".../unified-baseline_rubert-tiny-lite/final/model.safetensors", ".../modules.json"]`
— **workdir-relative, correct, and satisfied**. `train.log` ends `RECALL@100: 0.726350`. The appended
protected `score` stage (`python -m vectorsearch.test`, from the task snapshot) then loaded the config's
absolute path and printed `RECALL@100: 0.224975`. The event log records:

```
node_evaluated {"node_id": 4, "metric": 0.224975, "stdout_tail": "RECALL@100: 0.224975\n",
                "eval_seconds": 5302.896, "extra_metrics": {}, "violations": [], "trials": []}
```

`violations: []`. There is no field in that event, or anywhere else in the run, that names an artifact.

### v6 node 0 — the same authoring error, loud

Same shape, different experiment name in the path (`unified-mnr-t05-b8192-e10_…`), which upstream
never contained, so the scorer's `.exists()` failed. Cost ~3.5 GPU-hours.

### v5 node 0 — and this one the fence would not have touched at all

Its config carries `checkpoint_path: vectorsearch/experiments/unified-baseline_rubert-tiny-lite/final`
— **relative, inside the workdir**. The train stage wrote to
`vectorsearch/experiments/unified-baseline_rubert-tiny-lite_rubert-tiny-lite/final` (the model name got
appended to the experiment name). Its `train.log` ends `RECALL@100: 0.743250` — the best number in that
run. The node died:

```
node_failed  reason=no_metric  eval_seconds=4569.099  failed_stage=train
  "stage 'train' exited 0 but did NOT produce its declared artifact
   'vectorsearch/experiments/unified-baseline_rubert-tiny-lite/final/model.safetensors'…"
```

`expect` caught it — after 76 minutes. **Nothing about this incident is on the source-tree side.** It
is a disagreement between two paths *inside the node's own workdir*. A source-tree read fence is
structurally incapable of seeing it, and it is the *more common* shape: 2 of the 3 path incidents on
this box (v5 node 0, and the F1e node-3 case in doc 29) are intra-workdir.

**Base rate on the corpus:** across 10 real repo nodes (v5 0/2/4, v6 0–6), 2 carry an absolute
source-root path in their working set. Both are the two that produced a wrong or absent metric. See
§5.1 for the measurement.

---

## 3. The escape surface, measured

Setup: the **real generated fence** (`read_fence.render(roots=[src], allow=(), policy="deny")`) written
to a directory placed first on the child's `PYTHONPATH`, exactly as `run_argv` does. Each row is one
probe that tries to get the bytes of a file under the fenced root back into the process.
`BLOCKED` = `LoopLabSourceReadRefused` raised. `READ` = the content came back.

| mechanism | verdict | why |
|---|---|---|
| `builtins.open` | BLOCKED | audit `open` |
| `io.open` | BLOCKED | audit `open` |
| `os.open` + `os.read` | BLOCKED | audit `open` |
| `io.FileIO` | BLOCKED | audit `open` |
| `pathlib.Path.read_text` | BLOCKED | audit `open` |
| `open()` with a **bytes** path | BLOCKED | `_resolve` fsdecodes it |
| `open()` for **WRITE** into the source | BLOCKED | audit `open` covers write modes too |
| `os.chdir(source)` then relative `open` | BLOCKED | `os.chdir` hook (`_fenced_dir`) |
| `os.chdir(source/sub)` then `open("../x")` | BLOCKED | `os.chdir` hook |
| `mmap` on an fd from `os.open` | BLOCKED | the `os.open` was audited |
| `shutil.copyfile` out of the source | BLOCKED | audit `open` |
| `shutil.rmtree` of a source dir | BLOCKED | rmtree opens the dir fd |
| `numpy.load` / `numpy.fromfile` | BLOCKED | both go through Python `open` |
| `torch.load` | BLOCKED | `_open_file` → Python `open` |
| `json.load(open(...))` | BLOCKED | audit `open` |
| **`SentenceTransformer(<source dir>)`** | **BLOCKED** | first refused read is `README.md`, then `modules.json` |
| a Python **subprocess** child | BLOCKED | `PYTHONPATH` is inherited; the child refused |
| — | | |
| **`safetensors.safe_open`** | **READ** | Rust `std::fs`; never reaches a CPython audit event |
| **`safetensors.torch.load_file`** | **READ** | same |
| `ctypes` → libc `fopen` | READ | no audit event |
| `ctypes` → libc `open`/`read` | READ | no audit event |
| **`subprocess` of a non-Python binary** (`cat`) | **READ** | the fence is an interpreter hook |
| `sqlite3.connect` on a db in the source | READ | raises `sqlite3.connect`, not `open` |
| **symlink (file) in the workdir → source** | **READ** | path fence, documented |
| **symlink (directory) in the workdir → source** | **READ** | path fence |
| **hardlink into the workdir** | **READ** | no path relation at all |
| **`os.chdir(<symlink to source>)` then relative `open`** | **READ** | `_fenced_dir` uses `abspath`/`normpath`, not `realpath`, so the chdir passes; then `_resolve`'s relative fast-bail applies |
| `os.open(name, …, dir_fd=…)` (`openat`) | READ | CPython audits the *relative* name |
| `os.listdir` / `os.scandir` / `glob` | READ | different audit events |
| `os.remove`, `os.rename`, `os.truncate`, `os.chmod`, `os.mkdir` in the source | READ (allowed) | different audit events |

Four of these are worth pulling out.

**(a) `safetensors` escapes.** It is the loader for `model.safetensors` — the file both incidents were
about. The fence still stopped the v6 read, but only *incidentally*: `sentence_transformers` opens
`README.md` and `modules.json` through Python first. A repo that calls `load_file(ckpt)` directly —
which is the ordinary way to write a checkpoint-merging stage, and v6 was running merge nodes — reads
straight through. So the fence's coverage of the *exact* defect it was built for is contingent on the
loader's incidental JSON reads. That is not a property anyone should rely on.

**(b) The `os.chdir` argument in the docstring does not hold.** `read_fence.py` argues that the
relative-path fast bail in `_resolve` is safe *because* `os.chdir` into a root is refused. `_fenced_dir`
compares `abspath`/`normpath`, not `realpath`, so `os.chdir` into a **symlink** pointing at the source
is permitted; after it, every bare relative name reads the source and takes the fast bail. Measured
READ. The premise and the conclusion are both about symlinks, so this is the same documented residual —
but the docstring presents the chdir hook as *closing* the fast bail, and it does not.

**(c) It is not only a read fence.** `os.remove`, `os.rename`, `os.truncate`, `os.chmod` and `os.mkdir`
against the operator's editable source all succeed from inside a fenced node process. A runaway
cleanup step in generated training code can delete the operator's tree. The fence blocks
`open(..., "w")` and `shutil.rmtree`, which makes the surface look narrower than it is.

**(d) The refusal type only survives at the CPython layer.** `LoopLabSourceReadRefused` is deliberately
not an `OSError`. Any mechanism that works below CPython (§5.4, §5.5) can only return `EACCES`, which
Python surfaces as `PermissionError` — an `OSError`, i.e. exactly the shape
`except OSError: <fall back>` swallows. Measured: under the `LD_PRELOAD` shim, `safetensors` reported
`FileNotFoundError: No such file or directory`, having converted the `EACCES` itself. **Any
below-interpreter fence loses the "the message is the point" property that read_fence was designed
around.** That is a real cost, not a detail.

---

## 4. The four things the fence cannot establish (the owner's list, checked)

1. **It is a PATH fence.** Confirmed and quantified above. Symlink (file and dir), hardlink,
   `openat` with a dirfd, chdir-through-symlink, every C/Rust extension, every non-Python child.
2. **It says nothing about the wrong thing *inside* the workdir.** Confirmed by v5 node 0: the whole
   incident is workdir-relative. The workdir also persists across repair attempts by design, and
   `verify_stage_artifacts`'s freshness gate only covers stages the engine *re-runs*, never the ones
   `_safe_reuse_start` skips — and `verify_stage_inputs` deliberately has **no** freshness rule at all
   (its docstring says so: an input legitimately predates the stage). So a reused `train` output and a
   stale one are indistinguishable to the input contract.
3. **It does not stop the agent writing the absolute path.** Confirmed: `_source_root_note` is an
   advisory appended to a *successful* write, by design (the docstring explains the false-positive
   reasoning). The refusal rung is unused. And the path is in `node.files` — engine-visible, durable,
   pre-GPU.
4. **Nothing binds the metric to an artifact.** Confirmed: `node_evaluated` carries `metric`,
   `stdout_tail`, `eval_seconds`, `extra_metrics`, `violations`, `trials`. There is no artifact event
   type in `events/types.py`. The metric reader for this task is `stdout_regex` over the score stage's
   stdout, i.e. a number the scored process printed about a file nobody recorded.

### 4a. A fifth one I found: the `needs` contract cannot reach the stage that broke

`needs` shipped yesterday and is the right idea, but read `eval_stages.py::_resolve_stages`. In the
**single-command** mode this task uses (`task.snapshot.json` has `"stages": []`), the operator's `cmd`
is appended as the final protected stage built literally as:

```python
final = {"name": "score",
         "command": ...,
         "timeout": ...}
```

No `needs`. No `expect`. And `"score"` is in `materialized_stages`' `reserved` set, so the Developer
cannot declare it either. **The one stage whose read was corrupted in both incidents is the one stage
that structurally cannot declare what it reads.** Adding `needs` to the appended score stage is the
narrowest useful change in this whole document (§5.7).

---

## 5. Options

Costs are measured with the same workload `read_fence.py` used for its own numbers — `open` + read
4 KiB in a loop, N=20 000, best-of-5, **one fresh process per variant** (an audit hook can never be
removed, so measuring them in one process would report cumulative cost). Files on the overlay fs.

```
no hook                                        11,803 ns/open      baseline
noop audit hook                                11,929  (+1.0 %)    floor for ANY hook
SHIPPED prefix check                           12,117  (+2.7 %)    matches the shipped figure
prefix check, 8 roots instead of 1             12,152  (+3.0 %)    root count is free
record model-suffix opens only (no refusal)    12,063  (+2.2 %)    §5.2
9-event set membership + prefix                12,259  (+3.9 %)    §5.3
stat(dirname) inode, memoized                  12,320  (+4.4 %)    §5.3
realpath(dirname), memoized                    12,522  (+6.1 %)    §5.3  <-- closes symlinked DIRS
dircache + lstat(final component)              15,690  (+33 %)     §5.3  <-- closes symlinked FILES
realpath() on every open                       41,803  (+254 %)    rejected, and see below
LD_PRELOAD, prefix compare only                11,924  (+1.0 %)    §5.4
LD_PRELOAD, readlink(/proc/self/fd) post-open  13,695  (+16 %)     §5.4  <-- inode-grade
LD_PRELOAD, realpath() per open                24,796  (+110 %)    §5.4
```

### The `realpath` number in the shipped docstring is optimistic, and the reason matters

`read_fence.py` records realpath-per-open at **+88 %**. I measured **+144 %** on a 5-component path and
**+254 %** on a 9-component one, in the same process, same workload. `realpath` cost is linear in path
depth:

```
os.path.realpath, 5 components, overlay fs        15,023 ns
os.path.realpath, 9 components, overlay fs        29,652 ns
os.path.realpath, 13 components, GEESEFS         474,268 ns  and  513,626 ns
os.stat,          13 components, GEESEFS          65,379 ns  and   41,420 ns
bare open+close,  13 components, GEESEFS         214,969 ns
```

**Run workdirs are on geesefs.** `df -T /home/jovyan/data/looplab/runs` → `fuse.geesefs`, and a real
checkpoint path there is 13 components deep. A `realpath` on that mount costs **half a millisecond** —
more than twice the cost of the `open` it is guarding. Any design that calls `realpath` (or even
`stat`) per open is not "+88 % expensive", it is **~230 % expensive on the filesystem the workdirs
actually live on**, and it gets worse as the tree deepens. The rejection was right; the stated reason
understates it by 3×.

The same measurement points at the fix, though:

```
readlink("/proc/self/fd/N") on a geesefs file      1,944 ns  and  2,436 ns
```

**Resolving the fd *after* the open costs ~2 µs regardless of the backing filesystem**, because it is a
procfs read of an already-resolved dentry — versus ~474 µs to resolve the same path *before* the open.
That is a factor of ~240. The reason the inode fence looked unaffordable is that it was attempted on
the wrong side of the syscall, and the CPython audit hook only offers the wrong side: `open` fires
*before*, with a path and no fd. **This is the single most useful measurement in this document**, and
it is what makes §5.4 interesting.

---

### 5.1 · O1 — refuse the absolute source path where it is authored *and* where it lands

**What it establishes.** That a node's working set contains no absolute path into an editable source
root, checked before the eval starts. Two places, and both are worth having:

* **Authoring time** (`RepoWriteTools`): `_source_root_paths` already computes exactly this and returns
  it as a NOTE. Doc 29 §F1c already designed the false-positive-free version — refuse only when the
  absolute path *collides with the directory chain of a declared `expect.files` entry* — and states the
  two open questions (manifest availability during the STAGES phase; refuse vs bounce vs note).
* **Materialize time** (`engine/workspace.py::materialize`, after `_write_node_files`): scan the seeded
  working set for the source-root prefix. This is the one I measured, because it is the one that cannot
  be evaded by *how* the file arrived — a `write_file`, an `edit_file`, a CLI-agent diff, a repair, or a
  file that was simply already in the source and never edited all land in the same place.

**Cost, measured on the real corpus.** Walk the node workdir, skip `experiments/`, `__pycache__`,
`.git`, skip files > 2 MB, `grep` for the source root:

```
run                       node     files scanned   time    hit
rubertlite-dr-unified-v5  node_0        78         0.56 s   -
rubertlite-dr-unified-v5  node_2        78         0.50 s   -
rubertlite-dr-unified-v5  node_4        78         0.52 s   -
rubertlite-dr-unified-v6  node_0        78         0.58 s   vectorsearch/configs/config.yaml
rubertlite-dr-unified-v6  node_1        79         0.55 s   -
rubertlite-dr-unified-v6  node_2        79         0.53 s   -
rubertlite-dr-unified-v6  node_3        79         0.54 s   -
rubertlite-dr-unified-v6  node_4        79         0.09 s   vectorsearch/configs/config.yaml
rubertlite-dr-unified-v6  node_5        78         0.16 s   -
rubertlite-dr-unified-v6  node_6        82         0.08 s   -
```

**Two hits in ten nodes, and they are exactly the two defective nodes. No false positives.** 0.08–0.62 s
per node (the spread is geesefs cache warmth), against 76–86 minutes of training. 1.2 MB of text
scanned. This is the best precision/recall/cost ratio of anything in this document, and it is measured
against the real corpus rather than argued.

**What it does NOT cover.** Everything intra-workdir — v5 node 0 is untouched by it. A path assembled
at runtime (`os.environ["REPO"] + "/experiments/…"`, `Path(__file__).parents[3]`). A path in a file the
scan skipped (>2 MB, or under `experiments/`). A source root that is *legitimately* named — a large
untracked in-tree dataset, which is exactly the false positive doc 29 §F1c refuses to risk a repair
attempt on.

**What could go wrong.** (a) The false positive above becomes a hard node failure instead of a note.
Mitigation: make it a *violation on the node* plus a `needs`-style refusal message naming the three
legal answers (`data:` mount, `references:` mount, `seed_mode: "all"`) — the fence's own refusal text
already says this well. (b) The scan is a fixed cost on every materialization including confirm/ablation
paths; `materialize` is already the choke point but `_ablate` deliberately bypasses it, so scope has to
be decided rather than assumed. (c) A repo whose source root string legitimately appears in a README or
a lockfile — on this corpus, zero, but it is a repo-shaped assumption, so the check should report the
file and line, not just refuse.

**Cheap or a rewrite?** Cheap. One function in `engine/workspace.py` plus a refusal message. No new
subsystem, no event type strictly required (though one is worth it for the UI).

---

### 5.2 · O2 — bind the reported metric to the artifact that was actually read

This is the option that answers the owner's point 4, and it is the only one that is *sound under a
successful read*: it does not care whether the read was blocked, it cares whether the number is about
the node's own model.

**Mechanism, in three cheap pieces:**

1. **Record, don't refuse, on the hot path.** The same `sitecustomize` audit hook, in addition to the
   fence, records every `open` whose path is *not* under `sys.prefix`, keyed by **dirname**, into a
   bounded dict flushed at interpreter exit. Measured cost of a suffix-filtered recorder: **+2.2 %**
   (12,063 vs 11,803 ns/open) — inside the noise of the fence already installed. I drove it against the
   real `SentenceTransformer` load: 11 distinct non-site-packages paths, **7 distinct directories**, and
   the checkpoint directory is unambiguously among them. A per-stage ledger of that size is a few
   hundred bytes.
2. **Digest the declared outputs when the producing stage finishes.** The engine already knows
   `expect.files` for every stage. Hashing the real 92 MB `model.safetensors` on geesefs:
   `sha256` 0.29–0.43 s (217–313 MB/s), `blake2b` 0.31–0.36 s, and a **sampled** digest
   (size + head 1 MiB + tail 1 MiB, `blake2b`) **6–13 ms**. Even a 2 GB checkpoint is ~8 s of full
   hash against 76 minutes of train. Cost is not the constraint here; nothing is.
3. **Assert the binding after the score stage.** The metric is admitted only if the score stage
   demonstrably read *from* a directory that the pipeline declared it produced — or, stronger, read a
   file whose digest equals a recorded stage output. Otherwise: the node is evaluated, the number is
   recorded, and it carries a violation, exactly the way `metric_salvage` keeps a salvaged number out of
   `feasible_nodes()` without discarding it.

**What it establishes.** For the first time, a durable statement of the form *"metric 0.2249 was
produced by a process that read `<digest>`, which is the artifact stage `train` declared and produced."*
v6 node 4 fails that assertion (nothing under the workdir's declared `final/` was ever opened by the
score stage). v5 node 0 fails it too. And so does the case the fence cannot see at all — a scorer
reading a **stale artifact from a previous attempt in the same path**, because the digest recorded at
this attempt's train would not match.

**What it does NOT cover.** (a) `safetensors` again: the audit hook will not see the `.safetensors`
open, so the binding has to be at *directory* granularity for that loader, which is weaker (it proves
the scorer read the right directory, not the right bytes). Under §5.4's libc layer it would be
byte-exact. (b) A scorer that reads nothing at all and prints a constant — that is the reward-hack
surface `trust/` owns, not this one. (c) A stage that legitimately scores something it did not produce
(a baseline comparison) needs a declaration; that is a real design decision, not an edge case.

**What could go wrong.** The `PYTHONPATH` recorder is per-process and the score stage may fan out
(dataloader workers, a `torchrun` rank); the ledger has to be append-and-merge across pids, which the
fence's own `violations.log` already does the crude version of. And a bounded dict that fills silently
would make the assertion vacuous — it must record *that it truncated*, or the whole gate degrades to
"we saw nothing, so pass".

**Cheap or a rewrite?** Medium. New event type, a per-stage ledger file, a check in `eval_stages` /
`evaluate`. It is the largest of the cheap options and by far the most valuable, because it is the only
one that makes the *result* honest rather than the *filesystem access* honest.

---

### 5.3 · O3 — harden the fence in place

Four variants, all measured, all one-file changes to the generated template:

| variant | ns/open | Δ | closes | leaves open |
|---|---|---|---|---|
| **9-event set membership** (add `os.chdir`, `os.listdir`, `os.scandir`, `os.rename`, `os.remove`, `os.mkdir`, `os.truncate`, `os.chmod`) | 12,259 | +3.9 % | source enumeration and **mutation** (§3c) | everything below the interpreter |
| **`stat(dirname)`, memoized** — an inode fence on the *directory* | 12,320 | +4.4 % | symlinked directories, bind-mount aliases | symlinked files, hardlinks, C extensions |
| **`realpath(dirname)`, memoized** | 12,522 | +6.1 % | symlinked directories, **chdir-through-symlink** | symlinked files, hardlinks, C extensions |
| **`realpath(dirname)` + `lstat(final)`** | 15,690 | +33 % | + symlinked **files** | hardlinks, C extensions, non-Python children |
| `realpath()` per open | 41,803 | +254 % | same as the row above | same, and unaffordable — see the geesefs numbers |

The memoized rows are the interesting ones and they were not considered when the fence was designed.
`realpath` is only catastrophic *per open*; memoized **per directory** it is one 474 µs geesefs call per
distinct directory a process ever touches, amortized to nothing. A training process touches tens to
low-thousands of directories.

**What could go wrong.** The memo is a TOCTOU window by construction: a symlink created or repointed
after the directory was first cached is not seen again. That is acceptable against an *accident* and
worthless against an *adversary* — and the fence's own docstring is honest that it is the former. The
cache also needs a bound (an unbounded dict in a process that opens millions of distinct paths is a
leak) and the eviction policy is then a correctness question, not a memory one.

**What it does NOT cover.** All of it, still: `safetensors`, `faiss`, `pyarrow`, any non-Python child,
`openat` with a dirfd, hardlinks. Hardening the audit hook cannot reach below CPython. **This is the
option that most looks like progress and least changes the shape of the problem.**

**Cheap or a rewrite?** Cheap — but it buys the least per unit of hot-path cost, and it does nothing at
all for the intra-workdir majority of incidents.

---

### 5.4 · O4 — a libc-level fence (`LD_PRELOAD`)

I compiled a ~90-line shim (`gcc 13.3` is on the box) interposing `open`/`open64`/`openat`/`fopen`, and
drove the same probes through it.

| probe | audit hook | `LD_PRELOAD` shim |
|---|---|---|
| `builtins.open` | BLOCKED | BLOCKED |
| **`safetensors.load_file` / `safe_open`** | **READ** | **BLOCKED** |
| **symlink (file) into the source** | **READ** | **BLOCKED** |
| **symlink (directory) into the source** | **READ** | **BLOCKED** |
| **`openat` with a dirfd** | **READ** | **BLOCKED** |
| **`subprocess` `cat`** (non-Python child) | **READ** | **BLOCKED** |
| hardlink into the workdir | READ | READ |
| `ctypes.CDLL("libc.so.6").fopen` | READ | READ (dlopens libc directly, past the interposition) |

Cost, same workload:

```
LD_PRELOAD, prefix compare only                   11,924 ns  (+1.0 %)   symlinks NOT resolved
LD_PRELOAD, readlink("/proc/self/fd/N") post-open 13,695 ns  (+16 %)    inode-grade
LD_PRELOAD, realpath() before open                24,796 ns  (+110 %)   don't
```

The middle row is the one that matters and is why §5's `readlink` measurement was worth taking: it
opens the file, asks the kernel what it actually opened, and refuses if that is under a root. It is the
**only mechanism measured here that resolves symlinks, covers C/Rust extensions, covers non-Python
children, and costs 16 % rather than 254 %** — and on geesefs, where a pre-open `realpath` costs 474 µs,
the `readlink` costs 2 µs, so the *relative* cost on the real filesystem is far better than 16 %, not
worse.

**What it does NOT cover.** Static binaries, anything that `dlopen`s libc itself, `syscall(2)` directly,
Go binaries (which do not use libc). All adversarial, none accidental.

**What could go wrong, and this is the serious one.** Three things:

1. **The refusal becomes an `OSError`.** §3(d): `EACCES` → `PermissionError` → swallowed by
   `except OSError: <fall back>`, the exact pattern `read_fence.py` restructured itself to avoid. And
   `safetensors` did not even surface `EACCES` — it reported `FileNotFoundError: No such file or
   directory`, which is *actively misleading* to the repair loop. A libc fence buys coverage and pays
   with the diagnostic. It is only worth it **alongside** the audit hook, not instead of it: keep the
   audit hook for the good message on the Python path, add the shim as the backstop for what the audit
   hook cannot see, and have the shim write to the same `violations.log` so the engine can convert a
   confusing child-side error into the right sentence.
2. **`LD_PRELOAD` is fragile in ways `PYTHONPATH` is not.** It is inherited the same way (good), but a
   `sudo`, a setuid binary, a static `torchrun` launcher, or a conda activation that rewrites the env
   drops it. It also has to be built per-architecture and per-libc, which means shipping a `.so` or
   compiling at run setup — a real packaging cost for a project that currently ships pure Python.
3. **A bug in the shim breaks every process in the run**, including ones the fence was never meant to
   touch. The audit hook's `except Exception: return` fail-open has an equivalent here (fail open on any
   internal error), but a segfault has no equivalent.

**Cheap or a rewrite?** Medium-to-expensive: new build artifact, new packaging surface, per-platform
testing. Not a rewrite of the sandbox tier, but the first non-pure-Python thing in `runtime/`.

---

### 5.5 · O5 — real filesystem isolation (namespaces / a container tier) — **design only**

**I executed nothing here.** No namespace was entered, no bind mount was performed. What follows is
design, and its feasibility on this box is UNVERIFIED except where noted.

**The observation that makes this option smaller than it looks:** the Docker tier already *is* this
option, and it is already correct. `eval_dispatch.py::_data_binds` binds only the workdir plus the
declared `data:`/`references:` sources; the editable source tree is never bound in. `run_argv`
explicitly skips the `PYTHONPATH` prepend for a `docker run` argv precisely because the container is
fenced by construction. So "real filesystem isolation" is not an unbuilt feature — it is a *tier that
exists and that this task does not select*. The live run's snapshot has `trust_mode: "trusted_local"`,
which is the host subprocess tier.

That reframes the option into three honest sub-options:

* **O5a — run the repo task in the existing untrusted tier.** Zero new mechanism. Costs: GPU passthrough
  and image management for a repo whose environment is currently the host conda env; the whole `deps.py`
  install path would have to work inside the image; wall-clock for image builds. This is a real project,
  and on a box with limited GPU time and a live run it is not a same-week change. But it is the only
  option in this document that is *sound* rather than *good enough*, and it is already designed and
  partly built.
* **O5b — a mount namespace per eval, with the source root made invisible.** `unshare -Urm` then either
  bind the workdir over the source path or simply not have the source path in the new root. Kernel
  support looks present (`/proc/sys/user/max_user_namespaces` = 8255467). **Unverified**, and I did not
  test it because probing it here is exactly the class of action a previous agent on this box performed
  without authorisation. Design risks I can state without running anything: GPU device nodes and the
  NVIDIA driver's `/dev` + `/proc` surfaces have to survive the namespace; geesefs is a FUSE mount and
  FUSE mounts are not trivially re-exposed inside a new user namespace; and every child the eval spawns
  must stay inside, which interacts with `run_argv`'s `_RLIMIT_LAUNCHER` exec chain and with
  `_kill_tree`/pgid handling. Each of those is a plausible multi-day discovery.
* **O5c — the narrow version: make the source root unreadable, not invisible.** Nothing in LoopLab
  requires the eval process to run as the same uid as the engine. This is worth naming because it is the
  only namespace-adjacent idea that needs no namespace: it needs a second uid and a `chmod`, both of
  which are the operator's call and neither of which I can test here.

**What none of them covers.** The intra-workdir failures — v5 node 0 and the F1e case are untouched by
perfect source-tree isolation, because both paths are inside the node's own copy. Isolation is the right
answer to "a node read the operator's tree" and no answer at all to "a node read the wrong thing".

**Cheap or a rewrite?** O5a is a tier migration (weeks, plus image and dependency work). O5b is a
sandbox-tier rewrite with an unknown tail. **Say this plainly to the owner: neither is a fix for the
live run, and neither should be started before O1/O2/O7 have landed**, because O1/O2 close failure modes
that isolation does not touch.

---

### 5.6 · O6 — deterministic relocation: rewrite absolute source paths at seed time

**What it establishes.** That every absolute path into an editable source root in the node's seeded
working set is rewritten to the corresponding path inside the node's workdir, so it resolves to the
node's own copy. `engine/workspace.py::sandbox_cwd` already does exactly this for the eval `cwd` — the
mapping (`src → wd`, or `src → wd/<name>` for a named editable) is written and tested. Extending it from
"the cwd" to "text files in the seeded set" is a small step in code and a large one in meaning.

**Cost.** The same scan as O1: 0.08–0.62 s per node.

**What could go wrong, and why I would not do this.** A rewrite makes a *wrong* declaration *silently
work*. v6 node 4's config would have been rewritten to the node's own `final/`, the scorer would have
loaded the node's own model, and the run would have recorded 0.726 — the right number, from a pipeline
whose author still believes an absolute source path is a legal way to name their output. The next thing
that path names will not be rewritable (assembled at runtime, or in a `.pkl`, or in a `.env`), and the
failure comes back without the loud signal. It also silently edits the agent's own authored content,
which breaks the "the agent's edits are what ran" property that the whole repair loop reasons over.
And a rewrite in a `.py` file is a code edit performed by the engine, which is a category the write
gates exist to prevent.

Worth having as a **`warn`-style diagnostic** ("here is what this path would have to be"), attached to
O1's refusal message. Not as a mutation.

---

### 5.7 · O7 — give the score stage a `needs` (the narrowest real fix)

Per §4a, `_resolve_stages` builds the appended protected `score` stage with `{name, command, timeout}`
and nothing else, and `"score"` is reserved so the Developer cannot declare it.

**The change:** derive `needs` for the appended score stage from the *last* preceding stage's
`expect.files` (or let the operator's `EvalSpec` declare it explicitly). Then `verify_stage_inputs` runs
before the scorer starts, `stage_output_producers` names the producing stage, and v5 node 0's failure
message becomes *"stage 'score' did not start: it reads `…/final/model.safetensors`, which does not
exist; stage 'train' DECLARES this path as one of its own outputs, so the two declarations disagree"* —
before the scorer runs, instead of a `no_metric` after 76 minutes.

**What it establishes.** That the artifact the pipeline declared exists at the declared path when
scoring begins. **What it does NOT establish** — and this is the crucial limit — **that the scorer reads
it.** v6 node 4 would still pass: the declared file existed, was fresh, and was non-empty; the scorer
simply read a different one. `needs` is a *presence* contract. Only O2 turns it into an *identity* one.

**What could go wrong.** Inferring `needs` from the last stage's `expect.files` is a guess about
pipeline shape; a pipeline whose last preceding stage is `eval_prep` rather than `train` gets a wrong
declaration and a false refusal. Safer: derive it from the *union* of preceding `expect.files` that no
later stage overwrites, or require the operator to say it. That decision should be made explicitly, not
inferred, because a false refusal here costs a whole node.

**Cheap or a rewrite?** The cheapest thing in this document — a few lines in `_resolve_stages`, reusing
machinery that shipped yesterday. It should probably ship regardless of what else is chosen.

---

### 5.8 · O8 — record-and-adjudicate-after (the cheap tier under O2)

If O2 is too much for now, its first half is nearly free and is a strict improvement over `warn`:
record every non-`sys.prefix` `open` **dirname** during each stage into a per-run ledger (+2.2 %
measured), and have the engine adjudicate *after* the stage — resolving the recorded paths once,
out-of-band, where `realpath` costs nothing because it runs a handful of times instead of a million.

That single change closes the **symlink hole at zero hot-path cost**, because the resolution happens off
the hot path. It converts the fence from prevent-only to prevent-plus-detect, and the detect half is
inode-grade while the prevent half stays a cheap prefix compare.

**What it does NOT cover.** It is detect-after: the node has already burned the GPU hours. For the
*corruption* problem that is sufficient (a violation keeps the number out of `feasible_nodes()`); for
the *waste* problem it is not.

---

## 6. Ranking and recommendation

Ranked by (defect actually closed) ÷ (cost + risk), against this box's real corpus:

| # | Option | Closes | Hot-path cost | Build size | Verdict |
|---|---|---|---|---|---|
| **1** | **O7** score-stage `needs` | v5 node 0 *before* the GPU spend | 0 | hours | **Ship. Do it first.** |
| **2** | **O1** absolute-source-path refusal at materialize | v6 nodes 0 and 4, pre-GPU | 0.08–0.62 s/node | ~1 day | **Ship.** 2/2 precision, 0 false positives on 10 nodes |
| **3** | **O2** metric↔artifact identity binding | *all four* incidents, including intra-workdir and stale-reuse | +2.2 % | ~3–5 days | **Ship. This is the root fix.** |
| 4 | O8 record-and-adjudicate | the symlink hole, off the hot path | +2.2 % | ~1 day | Ship as O2's first increment |
| 5 | O3 `realpath(dirname)` memoized + more audit events | symlinked dirs, chdir-through-symlink, source **mutation** | +6.1 % | hours | Worth it, but do not mistake it for the fix |
| 6 | O4 `LD_PRELOAD` shim | `safetensors`, C extensions, non-Python children, symlinks, `openat` | +16 % | ~1 week + packaging | Only *alongside* the audit hook; it degrades the refusal message |
| 7 | O6 seed-time path rewriting | nothing honestly | 0.6 s/node | ~1 day | **Do not do this** — as a diagnostic only |
| 8 | O5 namespace / container isolation | source-tree reads, soundly | 0 | weeks; unknown tail on this box | Right long-term answer, wrong thing to start now |

**Recommendation: O7 + O1 now, O2 next, and stop calling the fence the mechanism.**

The framing I would put to the owner is this. The fence answers *"can a node read the operator's
tree?"*. That is not the question either incident asked. v6 node 4 asked *"is the number this run
recorded about the model this node trained?"* — and the answer was no, and would still be no with a
perfect fence if the wrong checkpoint had been inside the workdir. v5 node 0 asked the same question and
the wrong path was **already** inside the workdir. **The root is not the source tree. The root is that a
metric is an unbound number.** O2 binds it. O1 and O7 are the cheap upstream checks that stop the run
paying for the discovery.

Keep the fence. It is 2.7 %, it correctly refused the real `SentenceTransformer` load in my
reproduction, and its refusal message is the best-written thing in this whole area. Just stop treating
it as the thing that makes a result trustworthy — it makes a *filesystem access* trustworthy, and only
from CPython.

---

## 7. Two operational notes that are not options

**7a. The live run is not fenced, and cannot become fenced without a restart.** `runs/rubertlite-dr-unified-v6`
is running now (pid 1823049, started Aug 12). Its `config.snapshot.json` has `read_fence: null` (the
field did not exist when it started), there is no `runs/rubertlite-dr-unified-v6/.looplab-fence/`
directory, and the engine process predates commit `c862045c` (2026-08-13 05:19), so it is executing code
in which `read_fence.py` does not exist. Engine invariant 6 (snapshot settings win on resume) means a
resume would also not turn it on without an explicit setting. **Whatever is chosen here, the run
currently producing results has none of it.** That is worth knowing before reading v6's numbers.

**7b. The fence permits destructive mutation of the tree it protects.** §3(c): `os.remove`,
`os.rename`, `os.truncate`, `os.chmod`, `os.mkdir` against the editable source all succeed from a fenced
node process, because they raise audit events the hook does not watch. Adding those event names to the
hook is the +3.9 % row in §5.3 and is close to free. Given that the source root is
`/home/jovyan/data/vectorizer-unified` — an operator's working tree with a human's July checkpoint in
it — this is arguably more urgent than any of the read-side options, and it is a five-line change.

---

## 8. Reproducing the measurements

Everything is in the session scratchpad, nothing in the repo or under `runs/`:

```
<scratchpad>/fence/
  src/           synthetic editable source root (+ a real 92 MB SentenceTransformer copy)
  wd/            synthetic node workdir (symlinks/hardlinks planted for the escape probes)
  fencedir/      the REAL generated sitecustomize.py (read_fence.render), + violations.log
  probe.py       the 30-probe escape battery         -> §3
  bench.py       11 hot-path variants, one per proc  -> §5
  shim.c / shim_proc.c / shim_cheap.c                -> §5.4
```

`bench.py` takes the variant name as `argv[1]` and must be run once per variant — an audit hook cannot
be removed, so measuring them in one process reports cumulative cost. That is also the single easiest
way to get this benchmark wrong.
