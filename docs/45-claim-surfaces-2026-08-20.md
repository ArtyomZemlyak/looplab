# 45 — Claim surfaces: why a recorded fact goes stale, and what to do about it (2026-08-20)

A root-cause pass over seven claim failures measured in the two days to 2026-08-20, an audit of the
claims aimed at AGENTS (those are the ones that kill runs), and the mechanism built in response.

Not a bug list. The bugs are fixed in the same change; this document is the shape they share and the
argument for the shape of the fix, including the options that were weighed and refused.

---

## 1. The shape

Every one of the seven is the same triple: **a CLAIM recorded in one place, a DECIDER that makes it
true or false somewhere else, and nothing connecting them.**

| # | The claim | Where it was recorded | Where its truth lives | Cost |
|---|---|---|---|---|
| 1 | "the manual e5-small recipe is 16k overall = 8k x 2 GPUs" (labelled VERIFIED) | `runs/e5small-dr-unified-v3`'s task goal | `vecsearch_benchmarks_readable.md` — that row is `rubert-tiny-lite`'s; the e5-small BASELINE block says batch 1750 / 4 GPUs / `n_negatives` 0 -> 0.89 | **all three nodes died of `torch.OutOfMemoryError`** chasing a per-device 8192 that needs ~530 GiB on a 139.8 GiB card |
| 2 | declaring more GPUs "does NOT get this experiment more hardware" | `agents/roles.py`, `engine/proposal_cues.py` | `engine/resources.py::_resource_request_for_node` / `_acquire_gpus` — a declared count IS honoured, all-or-nothing, fenced into `CUDA_VISIBLE_DEVICES` | no Card on this box has ever declared anything but `{"gpus": 1}` |
| 3 | `gpu_footprint_cue` is researcher-governable | the settings UI schema | `core/config.py::DEFAULT_AGENT_CONTROL` grants no such thing | an operator surface claiming an authority that does not exist |
| 4 | five backlog rows | `docs/BACKLOG.md` | the tree — the thing each said was missing had landed hours earlier | an agent told to work the backlog must re-derive the whole tree first |
| 5 | "eleven `FAILURE_REASONS`" | `docs/guide/concepts.md` | `core/models.py::FAILURE_REASONS` — twelve | — |
| 6 | a `torch.OutOfMemoryError` is a `crash` | `engine/triage.py::_failure_reason` | the traceback — the OOM branch recognised only the kernel-kill signature | run 3's own log records `crash` for three OOM deaths, so every later reader inherits a false cause |
| 7 | an `absent:` proof | `tests/test_open_item_index.py` | the tree — the proof was satisfiable by an unrelated code COMMENT | a guard reporting a live defect as shipped |

Items 2, 3, 5, 6 and 7 were already fixed on `master` when this pass began; 1 and 4 are the record.

### 1.1 The half of the shape that decides the mechanism

The obvious reading is DECAY: a claim outlives its cause. That reading is incomplete, and taking it
literally produces the wrong mechanism.

**Four of the seven were FALSE ON THE DAY THEY WERE WRITTEN.** Backlog rows 8 and 10 were closed on
2026-08-14, the day the list naming them was written. Doc 27's eval-corpus banner names two tests as
missing and both predate the document. Two of `docs/CODE_REVIEW.md`'s 21 rows are false as written,
one asserting the exact opposite of another row 24 lines below it. And item 1 — the expensive one —
was typed by a human reading a table, labelled "verified", and never checked against the machine.

So it is not age. **Writing a claim costs nothing; checking one costs a lookup — and the lookup is
skipped at authoring exactly as it is skipped at reading.** That is why the same defect appears in
both directions (CLAUDE.md's own measurement: a status marker nobody re-derives is wrong in BOTH
directions, 4 of 19 open rows closed and 8 of 36 shipped rows overstated).

This immediately rules out one candidate mechanism and selects another — see §3.

### 1.2 The claim surfaces, and what guards each today

| Surface | What makes a claim there stale | Guarded today? |
|---|---|---|
| **Task JSON goal text** (`/home/jovyan/data/*-task.json`) | it asserts facts about a THIRD-PARTY repo the agent is licensed to edit, about the operator's own benchmark files, about prior runs and about the box — and it is spliced verbatim into every Researcher prompt | **Nothing.** The file lives outside the repo, so no test can reach it. This is the surface with the measured cost. |
| **Agent prompt / cue strings** (`agents/roles.py`, `engine/proposal_cues.py`, `adapters/repo_developer.py`, `core/hardware.py`, `engine/genesis.py`, the judges) | they state defaults, ceilings, cadences and what another module does; the module changes and the sentence does not | Partially. `test_gpu_footprint_choice.py` drives the real scheduler to falsify the GPU cue's prose — the best guard in the repo. `test_prompt_capability_sync.py` derives the tool names. Everything else is unguarded, including the five-role `core/hardware.py` brief. |
| **`Settings` field comments** (`core/config.py`) | they cite `<mod>.py::<symbol>` and constants (`_UNLIMITED_REPAIR_CEILING (50)`) | Nothing. (The separate UI-schema catalogue IS guarded — `_check_pinned_default` + a two-way reconciliation — and is the model this borrows from.) |
| **In-code comments about ANOTHER module** | a rename, a move, or an edit above a cited line | Nothing, until this change: 653 `<mod>.py::<symbol>` citations in `looplab/`, 471 distinct, 4 dead; 8 `<mod>.py:NNN` citations, 6 dead. |
| **`docs/guide/*.md`** | a default, a count, a threshold moves | Name-presence only (`test_config_docs_sync.py`). **No Default-column VALUE is checked anywhere.** |
| **`docs/BACKLOG.md` and the review docs** | the work lands | `OPEN[…]`/`DECLINED[…]` markers only, and coverage says it is partial. |

---

## 2. The audit: claims aimed at agents

A FALSE claim aimed at an agent is a defect with a measured cost. These were checked against the
site that decides each.

### 2.1 `/home/jovyan/data/e5small-v4-task.json` — every claim in the goal

The v4 goal is the CORRECTED successor to the one that killed run 3, and it holds up. Re-verified
2026-08-20:

| Claim | Verdict | Deciding site |
|---|---|---|
| repo is `/home/jovyan/data/vectorizer-unified`; scorer prints `RECALL@100: <value>` | TRUE | `vectorsearch/test.py` prints it |
| `config.yaml` still names rubert-tiny-lite as `train.base_model` | TRUE | that file's `base_model` line |
| the only prefix machinery is `MultiTaskConfig`'s ESCI class prefixes, off by default | TRUE | `vectorsearch/config.py::MultiTaskConfig.enabled = False`; no `query: `/`passage: ` anywhere |
| the manual e5-small BASELINE is batch 1750 / `n_gpus` 4 / `n_negatives` 0 / temp 0.01 -> 0.89 | TRUE | the `BASELINE` block of the benchmark file, verbatim |
| **the '8k x 2 GPUs' recipe in that file is rubert-tiny-lite's, NOT e5-small's** | **TRUE** | it is inside the `sergeyzh/rubert-tiny-lite looplab v1` checkpoint-details block — **this is run 3's fatal claim, correctly inverted** |
| the only e5-small row naming a per-device split says "16k overall (4k x 2gpu x 2acc)" | TRUE | one such row exists |
| `batch_size` is PER DEVICE (`build_trainer` passes `per_device_train_batch_size`) | TRUE | `vectorsearch/training/build_trainer.py` |
| `gradient_accumulation_steps` MULTIPLIES the effective batch | TRUE | HF semantics; reaches the trainer via `**config.__pydantic_extra__` |
| `gradient_checkpointing: true` in `train.training` is reachable | TRUE | same `__pydantic_extra__` passthrough |
| `NLLCosLoss` is per-device and ungathered | TRUE | its own docstring: "Operates on the per-device batch (no cross-process gather)" |
| this box has TWO GPUs; one H200, 139.80 GiB usable | TRUE | `nvidia-smi`: 2 x H200, 143,771 MiB each |
| e5-small-en-ru ~44.4M params (hidden 384, 12 layers, vocab 60302); ~2x rubert-tiny-lite's 22.8M; 4x layers, 1.5x width | TRUE (params drift +0.9 %) | the two `config.json`s; safetensors headers give **44.80M** and **23.04M** |
| `XLMRobertaModel` in the config is NOT the 278M -base variant | TRUE | the config's own dimensions |
| ~67 MiB/example at `n_negatives` 2, ~30 at 0; batch 2048 peaks 132.6 GiB, 3072 OOMs; 8192 needs ~530 GiB; with checkpointing 8192 fits in 80.6 GiB | **UNVERIFIABLE** | no site decides it. It is a measurement with no recorded artifact, on a box whose GPU is held by a live run. **This is the class §3.3 exists to abolish.** |
| "the local data root is already exported into every stage's environment" | UNVERIFIABLE here | `Settings.eval_env` in the same file — true at submit, and nothing re-derives it on resume |

One defect found: the task's `id` is `e5small-dr-unified-v2` while its `out` is
`runs/e5small-dr-unified-v4`.

### 2.2 Prompt and cue strings — the FALSE ones

Two live contradictions, both fixed in this change.

**(a) `core/hardware.py::operational_attention_points` — FALSE about the machine, and it reaches
five roles.** It said:

> "By DEFAULT use ALL available GPUs (e.g. `--gpus <N>` / DataParallel/DDP for N GPUs) unless the
> task says otherwise; don't leave GPUs idle or run a tiny single-GPU job on a multi-GPU box without
> reason."

Wrong in two ways at once. `engine/resources.py::_resource_request_for_node` gives an UNDECLARED
footprint exactly ONE device whenever the run evaluates in parallel, and `_resource_eval_env` fences
the child's `CUDA_VISIBLE_DEVICES` to precisely the reserved ids — so this instructs the role to
write `--gpus 2` into a command that will only ever SEE one. And the tool the same bullet recommends
cannot correct it: `nvidia-smi` reports the physical box regardless of `CUDA_VISIBLE_DEVICES`, which
`engine/resources.py` records in its own comment.

It also contradicted, in the SAME prompt, both paragraphs that actually govern the decision —
`roles.py::footprint_guidance` and `proposal_cues.py::_gpu_budget_hint_text`, which say `gpus=1` is
the ordinary declaration. Those two are gated by `Settings.gpu_footprint_cue` and were corrected on
2026-08-19. **This copy is gated by nothing, so it outlived the correction** — the exact mechanism of
item 2, one file over.

**(b) `engine/genesis.py` contradicts itself about data mounts.** One bullet says `data` entries are
"copied to ./&lt;name&gt;"; eighteen lines below, another says they are "mounted (symlinked) at
./&lt;name&gt;, never deep-copied". `adapters/repo_task.py::DataSpec.mount` defaults `True`, so the
symlink half is right and the copy half — the one a reader hits first — is false.

**(c) `engine/genesis.py` is where a device policy gets typed INTO a goal.** It carried "use ALL
available GPUs by default" and, worse, "Put operational guidance the agent needs (use all GPUs, …)
in the task `goal` in plain words". Genesis WRITES goal text. That instruction is the upstream half
of item 1: it tells the author to put a configuration into the one channel no guard reads.

**Still open, stated rather than patched:** `agents/roles.py::_FOOTPRINT_BUDGET_LEGACY` is the
pre-correction paragraph, byte for byte, and it is what an UNSTAMPED role gets — a bare
`LLMResearcher` in a library caller. The engine path always stamps (`gpu_footprint_cue` defaults
`True`), so no run gets it; a library caller does. `proposal_cues.py`'s legacy branch is the same
shape and the same argument. Both are deliberate — `false` must restore the old prompt byte for byte
— and both are false sentences that ship.

---

## 3. The mechanism

Four options were weighed. Two are refused on measurements.

### 3.1 REFUSED — an expiry on every claim

A claim carries a TTL and a guard goes red when it elapses. **It would have caught NONE of the four
born-false claims**: an expiry that has not elapsed is green, and a born-false claim is green for its
whole window. It also charges every true claim a recurring re-attestation, which is the "convention
people skip" CLAUDE.md measured is worse than none.

### 3.2 REFUSED as the primary mechanism — `auto_find_batch_size`

HuggingFace's `TrainingArguments.auto_find_batch_size` exists, defaults `False`, is recorded `False`
in every saved config under `/home/jovyan/data/vectorizer-unified`, and reaches the trainer through
`build_trainer`'s `**config.__pydantic_extra__` with no code change. It halves the batch on OOM and
retries. It is refused as the answer, on a measurement in the installed library:

> transformers 4.51.0's `Trainer.train` sets `self._train_batch_size` and `self.state.train_batch_size`
> to the reduced value, and writes back to `args.per_device_train_batch_size` only inside the
> DeepSpeed branch, restoring `original_bs` two lines later. So the DECLARED value is what
> `training_args.bin` and the checkpoint README carry, and the value that actually ran survives only
> in `trainer_state.json::train_batch_size` and a `logger.debug` line.

A run that reports "trained at 8192" while it trained at 1024 is a record diverging from reality —
the class of defect this whole document is about, reintroduced by the fix for it. Worse here than
elsewhere, because LoopLab RANKS nodes: two nodes whose recorded configs differ only in batch could
have trained at the same one. **Admissible only if the effective batch is lifted into a durable
LoopLab event**, which is an `extra_metrics`-shaped problem (`core/models.py::EXTRA_METRIC_CHANNELS`:
who authored the print statement) and is left open.

### 3.3 ADOPTED, the general rule — **a constraint of the machine is DISCOVERED by the thing that runs on it, never asserted in prose an agent reads**

A memory ceiling is a fact about (this model, this sequence length, this `n_negatives`, this card).
Change any of the four and a typed number is wrong; and nothing in the tree can re-derive it, which
is why row 14 of §2.1 is the only UNVERIFIABLE row in an otherwise checkable goal. Even an engine
that COMPUTED the ceiling and spliced it into the goal would be typing a claim — correct at splice
time and stale at the first `n_negatives` change.

**This repo already implements exactly this rule for TIME and not for memory.**
`proposal_cues.py::_cue_experiment_time_budget` has said, since before any of this:

> "If per-step time on THIS data/hardware is unknown, run a SHORT probe (a few hundred steps or a
> subsample) to measure it FIRST, then size epochs to fit — a smaller experiment that COMPLETES
> beats a bigger one that gets killed."

So the fix is symmetry, not invention: the corrected GPU cue now carries the memory twin — do not
size the batch from a recipe, a parameter count or a number someone wrote down; run one step at the
batch you intend as the first thing the pipeline does, read the allocator's own peak, size from
that. And `core/hardware.py` now says the same thing where it used to assert a device policy.

**Where the measurement runs, and the correction to the obvious answer.** The Developer's probe
(`tools/dev_probe.py`) cannot do it: **rule 4 is `CUDA_VISIBLE_DEVICES=""`**, deliberately, because
the host GPU-pool lease is one file per OS user and a probe that allocated on a device would corrupt
a SIBLING node's hours-long training. That rule should not be relaxed — the probe emits no domain
event precisely because rules 2-4 say it has no side effect. The lever that works is the node's own
pipeline: a short calibration step at the head of the declared stages runs **inside the devices this
node already reserved**, under the same fence and the same stage record, so there is no sibling to
corrupt and the finding is written by the node itself. That is what both cues now ask for.

**Other typed constraints this rule should replace.** TIME is already discovered (above) — nothing
to do. DISK is not typed anywhere today and should not become so. The remaining typed one is
`core/hardware.py`'s device count, fixed here.

### 3.4 ADOPTED, the guard — `CLAIM[<slug>] … decided:<predicate>`, plus a derived citation check

Because §3.3 cannot cover everything: a claim about the operator's benchmark file, or about which
row belongs to which model, is not a machine constraint and cannot be measured by a training step.
For those, the primitive is a predicate over the deciding site — chosen because **writing one forces
the lookup**, which is the authoring-time gap §1.1 identified. An author who has to name the line
that decides "8k x 2 is the e5-small recipe" opens the benchmark file, fails to find one, and never
types the sentence.

`looplab/core/claimpin.py` is the evaluator, and it has two halves with deliberately different costs.

**Half 1 — citations, ZERO adoption cost.** `<mod>.py::<symbol>` is already the house style; nothing
resolved one until now. `citation_defects()` re-derives all 653 in `looplab/`. The `<mod>.py:NNN`
form is REFUSED rather than resolved: a line number is falsified by any edit above it, so it is
unmaintainable by construction. Found and fixed here: 4 dead symbol citations (including
`events/replay.py::_card_debug_leaf_children`, written twice while the function lives in
`events/card_ledger.py` — which a THIRD site spelled correctly) and 6 live line citations.

**Half 2 — `CLAIM[…]`, opt-in.** One greppable key, the open-item index's slug discipline, and the
same three predicates plus one:

```
CLAIM[<slug>] <the sentence> decided:<predicate>[+<predicate>]
grep -rn 'CLAIM\[' .
```

* `present:<literal>@<path>` · `absent:<literal>@<path>` · `missing:<path>` — inherited verbatim.
* `line:<a>&&<b>@<path>` — **the one addition, and the e5 defect is why.** A bare
  `present:8k x 2gpu@bench.md` HOLDS: that string is in the file, on rubert-tiny-lite's row. A
  predicate that binds two literals to ONE line is what separates "this string occurs" from "this
  string is said about that subject", and it is the difference between a pin that passes and a pin
  that stops the run. Driven in `tests/test_claim_pins.py`.

Design choices, each against something measured:

* **`decided:` and not `claim:`** — `claim:` already occurs 64 times in this tree (research claims
  are domain vocabulary here), the `STILL OPEN` collision the open-item index was designed around.
  `decided:` occurs zero times.
* **A separate token from `OPEN[…]`, sharing ONE evaluator.** The reds mean opposite things: red on
  `OPEN[…]` means the item SHIPPED (delete the marker); red on `CLAIM[…]` means the SENTENCE IS FALSE
  (fix the sentence). Merging the tokens would merge two opposite repair actions. But
  `tests/test_open_item_index.py` now IMPORTS the predicate evaluator, the tree walk and the
  marker-stripping rule from `claimpin.py` — §0.8 found four implementations of one claim/verdict
  join and every drift was between the copies. One consequence is already a fix: the stripping rule
  now removes BOTH families' marker lines, so neither index can satisfy the other's predicate.
* **Two carriers, one evaluator.** `tests/test_claim_pins.py` runs in the suite over the repo.
  `python -m looplab.core.claimpin <task.json>` checks a task goal, which no pytest can reach — the
  file is outside the repo and cites the operator's own machine. `allow_absolute` is the parameter
  that keeps the suite passing against a bare `git archive HEAD` tree while letting a goal cite
  `/home/jovyan/data/vecsearch_benchmarks_readable.md`.
* **NOT a CLI subcommand.** `looplab/cli/`'s five groups each have a stated domain and a repo-hygiene
  checker fits none of them; adding a sixth on no evidence is the drift CLAUDE.md's file map warns
  about. `python -m` is the entry until someone has a reason.
* **It must not make it easier to type an answer into a goal.** A pin does the opposite: a goal can
  state a MEASURED limit with its decider attached, and a sentence that cannot be pinned is visibly
  a recipe rather than a constraint. The Genesis prompt now says so in as many words.

**The known way to defeat it, stated rather than patched:** delete the pin and keep the sentence.
Nothing detects that. What is bought instead is that every pin is greppable in one command, the
failure message says exactly this, and the half with zero adoption cost (citations) cannot be opted
out of at all.

---

## 4. What landed

* `looplab/core/claimpin.py` — the evaluator, both carriers, `python -m` entry.
* `tests/test_claim_pins.py` — the in-repo carrier plus six negative controls, including the e5
  defect driven end to end through the task-goal carrier.
* `tests/test_open_item_index.py` — refactored onto the shared evaluator.
* `core/hardware.py` — the "use ALL available GPUs" bullet replaced by the §3.3 rule, pinned.
* `engine/genesis.py` — the mount contradiction fixed and pinned; the two clauses that put a device
  policy into a goal replaced by "objective, constraints and MEASURED limits, never the
  configuration".
* `engine/proposal_cues.py` — the memory twin of the existing time-budget probe invitation.
* 10 dead citations re-pointed at symbols across `looplab/`.
* `docs/guide/concepts.md` — the inline-repair list enumerated eleven reasons under a sentence
  saying twelve (`not_learning` omitted); the enumeration is now DERIVED from `FAILURE_REASONS` by
  `tests/test_inline_repair_reason_coverage.py`.

Not fixed, and listed so nobody re-discovers them: the two legacy prompt branches (§2.2), the four
dead line citations in `ui/src/` (`cardBoardModel.js`, `CardBoard.jsx`) which belong to whoever owns
that surface this week, and the durable-effective-batch event §3.2 would need.
