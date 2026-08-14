# Metric provenance: finding the core, and one decision (2026-08-13)

**Status: analysis and one recommendation. Nothing here is implemented.** Every number is measured on
this box on 2026-08-13; the command that produced each is in §8. Where I could not determine something
I say so, in §7.

**Relationship to `docs/31-path-isolation-options-2026-08-13.md`.** Doc 31 covers the same incident
from the read-fence side and reaches an adjacent conclusion ("the root is that a metric is an unbound
number"). This document does not repeat it. It adds four things doc 31 does not have, and **corrects
two of its rows**:

* the **corpus** number (doc 31 measured 10 nodes; this measures 116 node workdirs / 83 recorded
  metrics across 6 runs);
* doc 31's O5 (namespace isolation) is marked *"I executed nothing here … UNVERIFIED"* and ranked last
  at *"weeks; unknown tail"*. **I ran it.** Mount namespaces are dead on this box for a reason no
  amount of engineering fixes (§4a), and **Landlock is alive, enforces on native code and children,
  and costs +2.1 %** (§4b). That is a same-week change, not a multi-week one, and it makes doc 31's O4
  (`LD_PRELOAD`, +16 %) dead weight;
* doc 31 ranks O7 (score-stage `needs`) **#1, "Ship. Do it first."** I called `verify_stage_inputs`
  against the preserved node-4 workdir: **it returns `None`. Floor option 1 passes the incident** (§3a).
  It is still worth doing — as a *declaration channel*, not as a gate;
* the finding that the provenance mechanism is **already built, folded, and read by three subsystems**,
  and is populated on exactly 1 of 83 metrics because it is only written when something fails (§5).

---

## 0. The decision, in one sentence

**Make `metric_provenance` mandatory on every metric — binding the number to the content identity of a
declared *subject* artifact that the score stage read from a kernel-enforced allow-list — and let the
existing `metric_salvaged` path make an unbound metric counted, visible and never selectable.**

Second best: the kernel allow-list alone, without the binding. Why it lost is in §6.

---

## 1. The core, stated

The stated root cause — *`expect` checks what a stage WRITES and never what it READS* — is true and is
not the core. It is one of three symptoms of a single omission:

> **The engine records a metric as a bare `float`. A `float` has no referent. Every gate LoopLab owns
> reasons about the metric's VALUE and none about its SUBJECT.**

Follow it through the code and the three "separate" gaps collapse into one:

* `read_metric` (`looplab/runtime/command_eval.py:690`) returns `Optional[float]`. The reader functions
  build a `Path`, stat it, and **discard it**. `RunResult` has no source field.
* `node_evaluated.data` is `{node_id, generation, metric, stdout_tail, eval_seconds, extra_metrics,
  violations, trials}`. Nothing names an artifact.
* `_on_stage_finished` (`looplab/events/replay.py:1400`) copies exactly four keys —
  `{name, status, exit_code, seconds}` — so even the stage rows that *do* carry `expect_declared` and
  `expect_since` on the wire are invisible to the fold.
* Every fact the system derives about a stage-written file is consumed as a boolean and thrown away:
  `st_mtime >= since - 2.0` (freshness), `st_size > 0` (non-empty), `st_size < 256 MiB` (OOM guard).

So `expect` being write-only is not the defect; it is what a write-only contract looks like when the
thing being contracted has no identity. Given identity, `expect` and `needs` become two projections of
one relation ("this stage produced/consumed *this artifact*") and the asymmetry disappears.

**The sharpest single measurement in this document:** the node's own checkpoint and the human's are

```
92,174,712 bytes    node 4's own model.safetensors   sha256 e33abc1f972aa443…  inode 93213
92,174,712 bytes    the human's model.safetensors    sha256 273885e69075c9a5…  inode 181848
```

**Byte-identical in size.** Every predicate the artifact contract owns — exists, non-empty, fresh —
is satisfied by both files. The contract is not merely aimed at the wrong side; on this incident it is
*incapable of discriminating* even if you point it at the right side. Only content or inode identity
separates them, and the engine records neither.

---

## 2. The empirical answer

Scope: the six real repo runs that have an event log. `runs/rubertlite-dr-unified-v5` has **no**
`events.jsonl` (3 node workdirs), so it is excluded from the metric counts and included in the workdir
counts.

| # | Measurement | Value |
|---|---|---|
| **E1** | `node_evaluated` events carrying a metric, across the corpus | **83** |
| **E2** | …of which record **no `metric_provenance` at all** | **82 / 83 = 98.8 %** |
| **E3** | …of which are **provably about bytes the node did not produce** | **2 / 83 = 2.4 %** |
| **E4** | node workdirs carrying a checkpoint-shaped absolute path **outside** their own workdir | **7 / 116 = 6.0 %** |
| **E5** | node workdirs referencing **any** absolute path outside their own workdir | **116 / 116 = 100 %** |
| **E6** | nodes whose agent-authored `expect` declaration ever failed | **1 / 113** |

**E2 is the number that decides this.** The one exception is v6 node 3, and it is the *salvage* path —
provenance is written only when something already went wrong. On the happy path the engine records
which stage ran, how long it took, and what number came out, and nothing whatsoever about what the
number is about. Provenance is not rare in this corpus; it is **absent by default and present only on
failure**.

**E3 is not one accident.** The two are `rubertlite-dr-unified-v2` node 4 and
`rubertlite-dr-unified-v6` node 4, in runs three weeks apart, and **both recorded `0.224975`** — the
identical foreign checkpoint, scored twice, by two independently-authored nodes, from the same
`config.yaml:215` line. v2 node 4 trained to `0.737488` and reported `0.224975`; v6 node 4 trained to
`0.726350` and reported `0.224975`.

**E4 is the exposure, and it shows the reported rate understates the defect.** `rubertlite-dr-unified-v6`
node 0 carries the *same* authoring error — an absolute `checkpoint_path` into
`/home/jovyan/data/vectorizer-unified/…` — and reported a metric that matches its own `train.log`
(0.708762 vs 0.708747). It escaped only because the human's tree never contained a directory named
`unified-mnr-t05-b8192-e10_rubert-tiny-lite` (verified: it does not exist). The difference between "a
node wasted an hour" and "the run's champion is somebody else's model" is **whether the foreign file
happened to exist**. On this corpus that coin came up heads 2 times out of 7.

**E5 is the number that kills naive hermeticity.** Every single node workdir in the corpus reads
outside itself — data roots, base-model caches, HDFS keytabs. "A node cannot read outside its
workspace" would break 116 of 116 nodes. Any boundary here must be an **allow-list**, never a
confinement.

**E6 is the number that constrains anything built on agent declarations.** Agents author `expect`
correctly 112 times out of 113 — the declaration channel *works*. That is precisely why floor option 1
does not help: the incident does not need a *wrong* declaration to survive, it survives a *right* one
(§3a).

---

## 3. The three floor options, priced honestly

### 3a. Floor 1 — let the `score` stage declare `needs`

**It already ships**, as of today. `validate_stages` accepts `needs`
(`command_eval.py:952`), `verify_stage_inputs` implements it (`:1173`), `_run_stages` calls it
(`:1577`) and emits a `needs_failed` stage row. The only missing wire is that the engine-appended
protected stage (`engine/eval_stages.py:90-94`) is built as `{name, command, timeout}` with no `needs`.
So floor 1 is hours of work.

**And it passes the incident.** I ran it against the preserved workdir:

```
subject exists in node 4 workdir: True  92174712 bytes
verify_stage_inputs(['vectorsearch/experiments/unified-baseline_rubert-tiny-lite/final/model.safetensors'],
                    node_4_workdir, stage='score', producers={…:'train'}, since=now)
  -> None            # no problem reported
```

`verify_stage_inputs` is a **presence** check (exists, non-empty; it explicitly `del`s `since` — no
freshness rule). Node 4 *did* write that file. A perfectly correct `needs` declaration naming exactly
the right checkpoint is satisfied, the stage runs, and the stage reads the foreign copy anyway.

**Verdict: keep it, restate what it is.** It is not a provenance gate and must stop being described as
one. It is the natural place for the operator to *name the subject*, and under the recommendation that
is its job. Doc 31's ranking of O7 at #1 "Ship. Do it first." is right about the cost and wrong about
what it buys on this incident.

### 3b. Floor 2 — refuse absolute source-tree paths at materialization

Doc 31 measured this (its O1): 2 hits on 10 nodes, both the defective nodes, zero false positives,
0.08–0.62 s/node. My wider scan is consistent: **7/116 workdirs** carry a checkpoint-shaped absolute
outside path, and the two that bit are among them.

Its coverage gap is real and is exactly as the operator states — it catches a spelling. But it has one
property nothing else here has: **it fires at materialization, before the GPU is spent.** v6 node 4
cost 5,111 s of training and 189 s of scoring before anyone could have known. A build-time refusal with
an actionable message is worth keeping *for its latency*, not for its coverage.

**Verdict: keep, demote.** Not a boundary. A lint with good timing.

### 3c. Floor 3 — bind the metric to the digest of the artifact it came from

The operator asked whether artifact identity is genuinely absent or already half-built. **The answer is
both, in different halves,** and the distinction is what turns this from a patch into a mechanism.

*Already built and shipping:*

| Piece | Where | State |
|---|---|---|
| `Node.metric_provenance: Optional[dict]` | `core/models.py:721` | **folded** (`replay.py:930`), additive, free-form |
| the "counted but not trusted" enforcement | `metric_salvage.py:351` `violation_rows` → `metric_salvaged` | ships; `replay.py:931` `feasible = not violations` → out of `feasible_nodes()` |
| downstream readers | `engine/lessons_distill.py:56,61`; `search/speculation_quality.py:1879` | already consume it |
| generic digest minters | `core/jsonutil.py::canonical_json_digest`, `core/atomicio.py::file_identity` | ship |
| a digest on an event, as precedent | `reward_hack_suspected.code_digest` (`evaluate.py:2325`) | ships |
| content hashes of run **inputs** | `EV_DATA_PROVENANCE`, `orchestrator.py:2589` — *"pin a content hash of every task asset so a result is tied to the exact data"* | **ships** |

*Genuinely absent:* zero occurrences of `sha256` / `digest` / `file_identity` in `command_eval.py`
(1,882 lines), `eval_stages.py`, `metric_salvage.py`, or `repo_task.py`. `core/atomicio.file_identity`
— the project's own canonical "same file, unchanged?" tuple — is imported by the trace/fence side and
**by nothing in the eval path**.

So the repo already asserts, in production, that *a result must be tied to the exact input data*. It
hashes every dataset at run start. It then trains a model, scores it, and records the number with no
tie to the model at all. **The principle is already the house rule; it stops at the run boundary.**
Extending it to the artifact the run produced is not bolting a checksum beside a number — it is
finishing a sentence the codebase started.

**But floor 3 as worded is unimplementable, and that matters.** "the digest of the artifact it came
from" presumes the metric comes from an artifact. **83 of 83 corpus metrics use `stdout_regex`** — the
number is parsed out of the score stage's stdout. There is no source file to digest. The re-wording is
not cosmetic:

> bind the metric not to its **source** (where the number was parsed) but to its **subject** (what the
> number is about).

**Verdict: promote, after re-specification.** This is half the recommendation.

*And here is the honest hole, which is why it is only half.* Binding the subject's identity proves
"this is the checkpoint the train stage produced". It does **not** prove the score stage read it. Node
4's subject was present, fresh, correct and byte-recorded; the score stage read elsewhere. **Subject
binding alone does not catch node 4.** It needs a partner that makes "read elsewhere" impossible.

---

## 4. The core framings, measured

### 4a. Hermeticity by mount namespace — **dead on this box, and not for an engineering reason**

Doc 31 left this unverified and guessed "weeks; unknown tail". Measured:

```
unshare -Ur echo OK                        -> OK          (user namespaces work; max_user_namespaces=8255467)
unshare -m echo OK                         -> Operation not permitted
unshare -Urm echo OK                       -> cannot change root filesystem propagation: Permission denied
unshare -Urm --propagation unchanged sh -c 'mount --bind /tmp /tmp/x'
                                           -> mount: bind /tmp failed.  rc=32
cat /proc/self/attr/current                -> cri-containerd.apparmor.d (enforce)
```

A mount namespace can be *created* and is then **useless**: the containerd default AppArmor profile
denies `mount(2)` unconditionally, so no bind, no remount-ro, no overlay upper, no tmpfs. This is a
property of how the pod is admitted, not of the kernel and not of anything LoopLab can change. There is
also no `docker`, no `/var/run/docker.sock`, no `bwrap`, no `fuse-overlayfs`, no `proot`. The Docker
tier that doc 31 correctly notes is "already correct" is **not available on this host at all**.

**Verdict: eliminate. Do not spend a week discovering this.**

### 4b. Hermeticity by Landlock — **alive, enforcing, and cheap**

```
landlock_create_ruleset(NULL,0,LANDLOCK_CREATE_RULESET_VERSION) -> 2      (ABI 2, kernel 6.1.0-22)
NoNewPrivs: 1                                                            (prerequisite already met)
```

With a ruleset allowing `$WD` and denying `$SRC`, in one process, three readers:

| reader | baseline | under Landlock |
|---|---|---|
| Python `open()` | OK | **`PermissionError [Errno 13]`** |
| `subprocess.run(["cat", …])` — a **child process**, no `PYTHONPATH` trick | rc=0 | **rc=1, "Permission denied"** |
| `ctypes.CDLL("libc").fopen()` — pure native, invisible to `sys.addaudithook` | OK | **refused, errno 13** |

Landlock is inherited across `fork`/`exec` by the kernel, so the `sitecustomize`/`PYTHONPATH` mechanism
that the audit hook needs to reach children is not required at all.

Cost (open + read 4 KiB, best-of-5 over 20,000 iterations, two independent runs):

| ruleset | 4-component path | 10-component path | setup |
|---|---|---|---|
| none | 4,784 / 4,749 ns | 5,054 ns | — |
| allow-list, 12 rules | 4,886 / 4,844 ns (**+2.1 %**) | 5,302 ns (+4.9 %) | **0.12–0.15 ms** |
| deny-by-complement, 55 rules | 4,874 / 4,843 ns (**+1.9 %**) | 5,406 ns (+7.0 %) | 10–24 ms |

**This overturns a recorded measurement, in the system's favour.** The fence is documented as a PATH
fence and not an inode one *because `realpath` per open measured +88 % against the prefix compare's
+2.8 %*. That number is the cost of doing inode-grade resolution **in Python, per open, in an audit
hook**. Landlock performs the equivalent discrimination **inside the kernel's own path walk**, where it
is nearly free, and delivers +2.1 % — cheaper than the prefix compare the +88 % figure was rejected in
favour of. The measurement stands; its *conclusion* ("therefore we cannot have an inode fence") does
not survive contact with the kernel mechanism.

**The measured reason Landlock is not optional.** The shipped fence exists to stop exactly this
incident, and it cannot see the read that caused it:

```
safetensors.torch.load_file("<the 92 MB foreign model.safetensors>")
  -> 55 tensors loaded
  -> audit 'open' events naming the file: NONE
  -> total audit 'open' events during the load: 0
```

`safetensors` is Rust. The weights read raises **zero** audit events. Doc 31 reports that its
reproduction of the real `SentenceTransformer` load *was* refused — which is consistent, because
`transformers` reads `config.json` through Python `open` before the Rust loader touches the weights.
Both are true, and together they say something worse than either alone: **the fence's coverage of this
incident is a property of a third-party library's file ordering, not of the fence.** A loader that
reads a single self-describing artifact natively walks straight through. Under Landlock the weights
read itself is refused, and the coverage stops depending on `transformers`' implementation.

**Two costs I will not hide.**

1. **Landlock refuses with `EACCES` → `PermissionError` → an `OSError`.** That is precisely the shape
   `read_fence.py` deliberately refuses to be, because `except OSError: <fall back>` is *the* idiom
   around a file read and would turn the refusal into a silent skip. **This is why my recommendation
   does not delete the audit hook.** Keep it as the first rung: it fires before the syscall for Python
   opens and raises the non-`OSError` with the actionable `REFUSAL_MESSAGE`. Landlock catches only what
   the hook cannot see — native readers — where no Python `except OSError` is in play and the native
   library surfaces its own error. Residual, stated: a native library may still swallow its own EACCES.
2. **The allow-list must be right or legitimate work dies.** E5 says 116/116 nodes read outside their
   workdir. Worse, I found a measured false positive **for the mechanism that already ships**:
   `rubertlite-dense-retrieval` node 36 runs `--teacher_checkpoint
   /home/jovyan/data/vectorizer/dense-retrieval/models/rubertlite-20e-v7/last.ckpt` — legitimate
   distillation from a checkpoint that lives *inside* the editable source root, in a run that declares
   no `data:`/`references:` mount at all. Under today's `read_fence: "deny"` default that node fails.
   1/116 = 0.9 %, and it is exactly the shape the environment note warns about: a boundary that makes
   legitimate work fail is a boundary the Developer spends repair attempts fighting. The allow-list has
   to be **derived from the operator's declared mounts plus a documented default set** (workdir, run
   dir, `/tmp`, site-packages, model cache, `/dev/nvidia*`, `/sys`, `/proc`), and the refusal must name
   the fix.

### 4c. The contract is write-only — right diagnosis, insufficient fix

Making the contract bidirectional is correct and **is already half-done**: `needs` shipped today,
`stage_output_producers` already maps a declared output path to the stage that declared it, and
`_validate_rel_paths` is one shared vocabulary for both directions. What it cannot do is enforce
*exclusivity*: a declared input set that is satisfied says nothing about the undeclared reads. §3a
measures that. Under the recommendation, the declaration stops being a lint and **becomes the mount
policy** — the `needs` set plus the declared mounts *is* the Landlock allow-list. That is the framing
the operator named, and it is right; it just cannot stand alone.

### 4d. Whose problem is this — **not the trust layer**

The cheapest core answer, and the wrong one. Measured:

* **No gate in `looplab/trust/` models inside-vs-outside a workdir.** `reward_hack._touches_protected`
  is a *containment* test (basename membership in the engine-placed asset set), consulted only for
  write/delete, never for reads. Its whole path vocabulary is `solutions?.csv|answer[_-]?key|test[_-]?labels`.
* **`detect_reward_hacks(..., stdout=res.stdout)` accepts stdout at `reward_hack.py:226`,
  `evaluate.py:777` dutifully passes it, and the body never references it.** The plumbing exists and
  terminates in a no-op.
* **The only filesystem-touching gate**, `AuditMixin._audit_workdir_writes`, loops `for name in
  protected` — it can only see files the engine itself placed.
* **It already fired on node 4 and changed nothing.** `reward_hack_suspected` fired on v6 nodes 2, 3, 4
  and 6 with `critic:params_ignored`, and `node_evaluated.violations` was `[]` for all of them, because
  `Settings.trust_gate` defaults to `"audit"` and `critic:*` is advisory in `is_hard_signal`.

The trust layer is a family of **static detectors over agent-authored source text**. The evidence this
defect needs is **runtime artifact identity**, which no gate receives and none is shaped to receive.
Routing it here would mean building the identity mechanism anyway and then hanging it off a subsystem
whose default mode is to record and not act. **Verdict: not the home.** The one thing worth taking from
here is the *shape*: `metric_salvaged` → `feasible=False` is the enforcement path, and it lives in
`metric_salvage.py`, not in `trust/`.

---

## 5. Option table

Coverage column: **eliminates** = the class cannot occur; **detects** = it occurs and is caught;
**detects-sometimes** = caught conditionally on something outside our control.

| Option | Class: foreign tree | Class: wrong subject *inside* workdir | Class: native reader | Metric has a recorded referent | Measured cost | What it breaks | Demands of the agent |
|---|---|---|---|---|---|---|---|
| **F1** `needs` on `score` | **no** (measured: passes) | no | n/a | no | ~0 | nothing | a correct declaration — which it already writes 112/113 times, and which does not help |
| **F2** refuse abs source path at materialize | detects (2/2, 0 FP) | no | n/a | no | 0.08–0.62 s/node | nothing | nothing; message at build time |
| **F3** digest binding *as worded* | no | no | no | **unimplementable**: 83/83 metrics are `stdout_regex`, no source artifact | — | — | — |
| **F3′** *subject* binding (re-specified) | detects | **detects** | detects | **yes** | 336 ms full sha256 / 29 ms sampled / 1 stat inode, on a 5,303 s eval = 0.006 % | nothing; `metric_provenance` is additive and already folded | operator names the subject once in `EvalSpec`; agent unaffected |
| **(a)** mount namespace / overlay / container | — | — | — | — | **impossible here** (AppArmor `cri-containerd`, measured) | — | — |
| **(a′)** Landlock allow-list | **eliminates** | no | **eliminates** | no | **+2.1 %/open, 0.15 ms setup** | 1/116 legitimate node today if the allow-list is not mount-derived | must have a declared mount for every legitimate outside read |
| **(c)** bidirectional contract as *policy* | eliminates (as the allow-list source) | no | eliminates | no | = (a′) | same as (a′) | declare inputs; already the `needs` vocabulary |
| **(d)** trust layer | no (no gate sees a path) | no | no | no | ~0 | nothing | nothing — and it changes nothing: default `trust_gate="audit"` |
| **RECOMMENDED** = **F3′ + (a′) wired through (c)** | **eliminates** | **detects, non-selectable** | **eliminates** | **yes** | +2.1 %/open, +0.006 % eval, ~0.15 ms/eval | allow-list must be mount-derived; Landlock refuses as `OSError` (mitigated, §4b) | operator declares subject + mounts; **agent unchanged** |

---

## 6. Recommendation

> **Make `metric_provenance` mandatory: every recorded metric must name a *subject* artifact and carry
> its content identity, captured inside a Landlock allow-list derived from the stage's declared inputs
> and the task's declared mounts — and a metric whose subject cannot be bound gets a violation row in
> the existing `metric_salvaged` family, so it is counted, shown, and never selectable.**

The three pieces, and why each is load-bearing:

1. **The subject.** `EvalSpec.metric` gains `subject`: workdir-relative paths, validated by the same
   `_validate_rel_paths` that `expect` and `needs` already use. This is the operator's field, on the
   operator's protected stage — it is not something the agent authors, so E6's declaration-quality
   question does not arise.
2. **The binding.** At score-stage start the engine records, per subject, `core/atomicio.file_identity`
   (one `stat`; it distinguishes the two 92 MB files by inode at zero read cost) plus a content digest
   under a size ceiling, plus the producing stage from `stage_output_producers`. It lands in
   `metric_provenance` on `node_evaluated` — an **already-folded, additive, free-form dict**. Absence
   → a violation row → `feasible = not violations` → out of `feasible_nodes()`. **Not one new
   enforcement path is required**; `metric_salvage.py` is the precedent and the machinery.
3. **The read set.** The score stage runs under a Landlock ruleset whose allowed subtrees are the node
   workdir, the run dir, `/tmp`, site-packages, the model cache, the device/proc surfaces, and the
   task's declared `data:`/`references:` mounts. Everything else is unreadable to every language.

**Why it must be all three.** Measured: the allow-list alone does not catch a wrong subject *inside*
the workdir (v6 nodes ran up to 4 repair attempts, leaving stale checkpoints in the same tree — and
doc 31 records v5 node 0 as exactly this shape), and it leaves 82/83 metrics with no recorded referent,
so nothing is auditable at replay. The binding alone does not catch node 4 (§3c). Together, the
allow-list makes "the number is about something in this node's own read set" true by construction, and
the binding names which member and records its identity so replay can check the claim.

**Why this is a mechanism and not a patch.** It does not add a checker. It changes the **type of a
recorded result** from `float` to `(float, subject-identity)` and it **inverts the default**: today the
absence of provenance means "fine" (82/83 times); afterwards it means "unproven". That is a change to
what the system will accept as a result, and it is the same move the codebase already made for run
*inputs* with `EV_DATA_PROVENANCE`. Naming the referent is not defence-in-depth against one incident;
it is the missing half of what a metric *is*.

### Second best, and why it lost

**The Landlock allow-list alone** (framing (a′) wired through (c)). It is genuinely good: it eliminates
the entire foreign-read class for every language, costs +2.1 %, needs no operator field, and is the
cheapest thing here that *eliminates* rather than *detects*.

It lost on two measurements. First, **it leaves 82/83 metrics with no referent**: after it ships, the
run log still cannot answer "what is this number about?", and a wrong subject inside the workdir — a
class the corpus contains — passes untouched. Second, **it is the half that can break legitimate work**
(1/116 measured today), so shipping it without the binding means paying the false-positive cost and
still not getting the property the operator asked for.

### Invariants

* **#1 (sole writer)** — untouched. `metric_provenance` rides on `node_evaluated`, already main-task.
* **#3 (side effect gated on an event)** — the subject capture is a read, not a side effect; the
  binding lands in the existing single terminal.
* **#5 (fold deterministic, order-tolerant)** — `metric_provenance` is already folded set-only under
  `first_terminal`; new keys are additive with reader-side defaults. Satisfied.
* **The +88 % `realpath` measurement** — see §4b. Not broken; its conclusion is superseded by a kernel
  mechanism that was not on the table when it was taken. Say so in `read_fence.py`'s docstring when
  this lands. **I have not edited `runtime/read_fence.py`**; a sibling agent owns it.
* **`read_fence.py` is not superseded** — it is demoted from *the* mechanism to the *message* rung, and
  it must stay, because Landlock's refusal is an `OSError` and the fence's is deliberately not.

### Amendment 2026-08-14 — the freshness half is SCOPED to attempts that produce their own artifact

The binding refuses a subject that predates the attempt (`unbound_reason: "stale"`), and §1's argument
for it is right for a normal attempt: `verify_stage_inputs` deliberately has no freshness rule because
an INPUT may legitimately predate its stage, while this attempt's NUMBER cannot be about an earlier
attempt's checkpoint — v6 nodes ran up to 4 repair attempts in one workdir.

**It is wrong for the one attempt the engine itself declares a reuse.** On a stage-scoped re-run
(`start_stage`, chosen by `_safe_reuse_start` after proving the repair did not touch the earlier
stages, and paid for by `inline_repair_retrain_cap`) the whole point of skipping `train` is that the
earlier attempt's checkpoint IS this attempt's subject. Binding it against this attempt's clock called
the engine's own reuse `stale`: under the shipped `audit` rung a false referent plus an
operator-blaming message, and under `require` a `metric_salvaged` row that takes the node out of
`feasible_nodes()` — a metric excluded for a reuse nobody was wrong to make.

The corpus shape it fires on: `runs/rubertlite-dense-retrieval` has **21 nodes** whose `train`
`stage_finished` row is `reused` (0.0 s) with `score` re-run beside it — 33 reused rows in all — and
`runs/rubert-dr-0807` three more. (`rubertlite-dr-unified-v6`/`v7` are *not* among them: every repair
there took the full-retrain branch, and v6 has the one `full_retrain_charged` row to show for it.)

The fix is a scope, not a deletion, and it is one derivation shared by the two decisions that turn on
it — `runtime/command_eval.py::attempt_freshness_floor`, over
`reused_stage_count(stages, start_stage)`:

* the metric SUBJECT binding at the score stage's start, and
* the tail's already-shipped relaxation of the secondary readers (constraints, extra metrics, the
  drift cross-check), which had the same rule spelled 300 lines away as `None if start_stage else …`.

Sharing it also closed a hole in the second: a `start_stage` naming no stage reuses nothing —
`_run_stages` falls back to a full re-run, the fail-safe direction — and the truthiness test dropped
the freshness gate anyway. What keeps its floor unconditionally is the PRIMARY metric read (it comes
from the final, re-RUN stage, so a no-op stage must not promote an old value), the stall-salvage read
beside it, and `verify_stage_artifacts`, which is held to its own stage's start.

Driven in both directions by `tests/test_metric_subject.py::
test_a_checkpoint_the_engine_itself_chose_to_reuse_is_the_subject_not_a_stale_leftover` — the reuse
binds and mints no row, the same on-disk state under a normal attempt is still `stale` and still
excluded under `require`, and an unknown `start_stage` is still strict.

### What survives, what is dead weight

| | |
|---|---|
| **F1 `needs` on `score` — KEEP, restated** | It is hours of work and it is the **declaration channel** the allow-list and the subject are derived from. It is **not** a provenance gate and must stop being called one (measured: it passes the incident). |
| **F2 abs-path refusal at materialize — KEEP, demoted** | Dead as a boundary once the allow-list exists. Alive for its **latency**: it fires before 5,111 s of training, with a message naming the fix. 2/2 precision, 0 FP. |
| **F3 as worded — DEAD. F3′ — PROMOTED** | "digest of the artifact it came from" is unimplementable (83/83 metrics are `stdout_regex`). "identity of the artifact it is about" is the recommendation. |
| **doc 31 O4 (`LD_PRELOAD`, +16 %) — DEAD** | It exists to cover native readers. Landlock does that at +2.1 %, in the kernel, inherited by children, with no packaging. |
| **doc 31 O5 (namespaces/container) — DEAD on this host** | AppArmor `cri-containerd` denies `mount(2)`; no docker, no bwrap, no overlay. Measured. |
| **framing (d) trust layer — DEAD as a home** | No gate receives a path; `stdout` is passed and discarded; it already fired on node 4 under `trust_gate="audit"` and changed nothing. |

---

## 7. What I could not determine

1. **Whether the shipped fence would have stopped v6 node 4 in practice.** I measured the negative half
   (`safetensors` weights read → 0 audit events). Doc 31 reports the positive half (its real
   `SentenceTransformer` reproduction *was* refused, presumably on `config.json`). I could not
   reproduce that here: neither `transformers` nor `sentence_transformers` is installed in the engine's
   interpreter, and the node's own venv is not reconstructible under a read-only `runs/`. The honest
   conclusion is the conditional one in §4b, not "the fence fails".
2. **Whether a Landlock allow-list survives a real GPU eval.** I benchmarked `open`/`read`, not a torch
   run. CUDA, NCCL, `/dev/nvidia*`, `/sys/class`, `/dev/shm` and geesefs all have read surfaces I have
   not enumerated. This is the single largest unknown in the recommendation and it needs one real GPU
   eval under a candidate ruleset before anyone commits. My allow-list omitted `/sys` and `/dev`
   entirely and would have failed such a run.
3. **Landlock's `EACCES` swallowing rate in real training code.** I argued the mitigation (audit hook
   first) but did not measure how often a native library catches its own `EACCES` and degrades
   silently.
4. **The deny-by-complement variant is fragile in a way I measured but did not resolve**: 211 candidate
   top-level rules produced only 55 accepted, because a path that fails to `open(O_PATH)` is silently
   *omitted* — i.e. silently **denied**. An allow-list built by enumeration must verify every rule was
   accepted or it fails closed in an unpredictable place. This is an argument for the mount-derived
   allow-list over the complement construction.
5. **`rubertlite-dr-unified-v5` has no `events.jsonl`** — its 3 node workdirs are in E4/E5 and out of
   E1–E3. Whether that run's nodes carried the same defect is not answerable from the log.
6. **Engineering time.** I measured runtime costs, not build costs. Every "hours"/"days" estimate in
   doc 31 is doc 31's, not mine.

---

## 8. Reproducing the measurements

All scan scripts are in the session scratchpad, not committed. `runs/` was treated as read-only
throughout; nothing under it was modified or deleted.

| # | Command |
|---|---|
| E1–E3 | `python3 scan_prov.py` — folds `node_evaluated` from each run's `events.jsonl`, compares `metric` against the last `RECALL@\d+:` in the node's own `train.log`, and records `'metric_provenance' in data` |
| E4–E5 | same script's workdir walk: absolute `/home/`, `/data/`, `/mnt/` literals in `.py/.yaml/.yml/.json/.toml/.sh/.env/.cfg` under each `nodes/node_*`, excluding paths under the node's own root, `#`-comment lines, and `experiments/`/`outputs/` |
| E6 | fold `stage_finished.status` across the corpus → `{ok:175, fail:43, reused:37, check_failed:21, timeout:4, expect_failed:1}` over 113 nodes that ran a declared stage |
| §1 sizes/digests | `sha256sum`; `os.stat` for `(st_dev, st_ino, st_size, st_mtime_ns)`; full sha256 of the 92 MB checkpoint = **336.2 ms**, sampled (size + 1 MiB head + 1 MiB tail) = **29.7 ms** |
| §3a | `verify_stage_inputs([subject], node_4_workdir, stage='score', producers={subject:'train'}, since=time.time())` → `None`; `validate_stages([...{"needs":[subject]}])` → accepted |
| §4a | `unshare -Ur/-m/-Urm …`; `mount --bind` inside the ns; `cat /proc/self/attr/current`; `command -v docker bwrap fuse-overlayfs proot` |
| §4b enforcement | `ll_probe.py` — `landlock_create_ruleset`/`add_rule`/`restrict_self` via `ctypes`, then Python `open`, `subprocess.run(["cat", …])` and `libc.fopen` against one allowed and one denied path (the denied path deliberately **not** under `/tmp`, which the first run of this probe allow-listed by mistake — the corrected run is the one reported) |
| §4b cost | `ll_bench.py` — best-of-5 × 20,000 `os.open`+`readv(4 KiB)`+`close`, two independent runs, at 4- and 10-component path depths |
| §4b safetensors | `sys.addaudithook` recording `open` events around `safetensors.torch.load_file` on the exact 92 MB foreign checkpoint → 55 tensors, **0** events |
| §4b false positive | `runs/rubertlite-dense-retrieval/nodes/node_36/looplab_stages.json:55` `--teacher_checkpoint`; that run's `task.snapshot.json` has `repo` and `cmd` only — no `data`/`references` mount |
