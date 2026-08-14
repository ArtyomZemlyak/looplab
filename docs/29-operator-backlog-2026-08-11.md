# Operator backlog — 2026-08-11

Feature-sized asks from the operator that are **not** bugs and were deliberately not half-implemented
in the same pass. Each entry states what was asked, what the code does today (verified, with the
site), and what building it would actually take. Bugs from the same session were fixed and are in the
git history, not here.

Unlike [`BACKLOG.md`](BACKLOG.md) — which warns at length that it contradicts itself and is six weeks
stale — every status below was read off the tree on 2026-08-11.

---

## F1 · Derive the run width from the proposals, not from the box — **BUILT, 2026-08-13**

**Asked:** "if I want to run one experiment per card — who decides that and how? Ideally
automatically, from the propose."

**Today.** `eval_parallel` / `llm_parallel` / `speculation_depth` all settle ONCE at run start and are
pinned into `run_started` (`engine/orchestrator.py`, the `_eval_parallel_value == 0` branch). AUTO
means *one experiment per detected GPU*, i.e. the width is a fact about the BOX. A per-Card footprint
(`{"gpus": N}`, `core/models.py::effective_card_footprint`) does influence that node's device
reservation and admission, but nothing lets a proposal change the run's width. The full behaviour is
now written up in [configuration.md](guide/configuration.md#one-experiment-per-gpu-who-decides-and-what-happens-when-two-runs-want-the-same-cards).

**What it would take.** The width is load-bearing for replay: it is pinned so a resume on a different
box continues the run's own treatment (engine invariant #6), and `speculation_depth` AUTO resolves off
it. A proposal-derived width therefore cannot just be recomputed per turn — it needs a durable
event that RE-pins the width mid-run (`budget_extend` is the existing shape for exactly this), plus a
rule for what happens when the proposals ask for more cards than exist. Start there, not at the
resolver.

**BUILT, 2026-08-13 — `Settings.proposal_width` (ON), `EV_RUN_WIDTH_SETTLED`.**

**The durable event is a NEW one, not `budget_extend`**, and the four reasons are why this entry's
suggestion did not survive contact:

1. `budget_extend` is a CONTROL event (`serve/protocol.py::CONTROL_EVENTS`) — a HUMAN intent the
   UI/CLI author. `_repin_settled_widths` stands aside on any axis a control event has taken over, so
   the engine's own first re-pin would permanently disable the re-entry refusal invariant #6 rests
   on, for the operator too.
2. It folds into `state.budget_overrides`, which `_apply_control_overrides` re-applies every turn.
   Engine and operator writing the same cell leaves no way to say the human's number outranks the
   research's.
3. `_apply_control_overrides` **raises** on any non-empty `budget_overrides` during a speculation
   calibration run — an engine-authored one would hard-crash that lane.
4. `search/speculation_quality.py::_FORBIDDEN_CALIBRATION_LIFECYCLE_EVENTS` already contains
   `budget_extend`, so writing one would silently disqualify every run as calibration evidence.

`run_width_settled` is engine-appended and folds LAST-WRITE-WINS into fields of its own
(`RunState.eval_parallel_settled`/`llm_parallel_settled`), never onto `run_started`'s pins — the same
separation, for the same order-tolerance reason, as `speculation_depth_pinned`/`_settled`. Unlike the
depth ratchet it is TWO-WAY (proposals narrow the run and widen it back), so a minimum would be wrong.
`_repin_settled_widths` resolves the three layers: **pin < proposal re-pin < operator `budget_extend`**.

**The rule** is `engine/widths.py::proposal_derived_width`, stated beside `per_experiment_gpu_budget`
because the two are halves of one fact:

```
width = min( gpu_pool // widest declared footprint.gpus, the width run_started pinned )
```

The **count** of open cards is deliberately not a term, even though "one experiment per card" sounds
like it should be. The board holds one ready card most of the time, so counting it narrows the run to
serial on board churn and widens it back a turn later, appending a durable row each way — caught by a
real toy run doing exactly that. It also buys nothing: the dispatcher fills up to the width from
whatever work exists.

**When the proposals exceed the box, the surplus QUEUES — the width never oversubscribes the pool.**
Five one-GPU cards on a two-GPU box stay at 2. Three reasons: it is what keeps F1b's per-experiment
announcement true (`pool // width >= need` by construction, so the next Card is never quoted less than
the open Cards already declared); an eval slot above the device count buys a node blocked in
`_acquire_gpus`, which is F1f's barrier; and `_dispatch_evals`'s aged-head escape hatch cannot fire at
all when the width exceeds the batch's own semaphore total.

**`speculation_depth` is NOT re-derived from the new width**, and this is the loud part. It is a
`run_started` pin, it is `CalibrationRuntime` field #6, the calibrated replay lane re-checks
`admitted_depth == engine.speculation_depth`, and `_require_pinned_speculation_receipt` raises
`SpeculationAuthorizationError` on a resolved-vs-recorded mismatch — so moving it here would make a
re-pinned run unresumable through the guarded path at the NEXT re-entry rather than at the decision.
The residual is a depth that may exceed the width after a narrowing, which is the same benign desync a
Strategist width change has always produced.

**Calibration receipts.** A calibration run never re-pins (refused outright, beside the AUTO gate the
profile's spelled `1`s already close), and `run_width_settled` is now a named member of
`_FORBIDDEN_CALIBRATION_LIFECYCLE_EVENTS` so a re-pinned run is refused as evidence BY NAME rather
than as an anonymous unlisted type. Separately and unavoidably: `speculation_implementation_digest`
hashes every shipped `.py`, so **this change revokes every previously issued speculation-quality
receipt** — as does any code change, but it is worth stating rather than discovering.

Also fixed here, because the feature makes mid-run width changes routine: `_dispatch_evals` compared
free semaphore tokens against the LIVE `self._eval_parallel` while the semaphore's total was captured
at construction. Lowered mid-batch it declared a wide head unsatisfiable while its GPUs were merely
busy (and `head_unsatisfiable` is sticky); raised, the escape hatch became unreachable and a genuinely
unsatisfiable head wedged the batch. It now compares against the batch's own captured width.

`tests/test_proposal_derived_width.py` drives it: the rule's truth table, the F1b announcement
property over every pool/footprint pair, fold order-tolerance against `run_started`, a real Card board
through the real planner, every stand-aside, and a real resume onto a four-GPU box that must reach the
re-pinned width from the log alone.

### F1b · Tell the Researcher what the per-experiment GPU budget IS

**Asked:** "why is the current rubert run using 2 GPUs for one experiment instead of running them in
parallel?"

**Measured, 2026-08-12, on `rubertlite-dr-unified-v5`.** Nothing in the engine was wrong.
`run_started` pins `eval_parallel: 2` — AUTO settled correctly off the two H200s — and the engine was
in fact running card-1's build concurrently with node 0's eval. But `card_added` for BOTH cards
carries `footprint: {"gpus": 2}`, so `_resource_request_for_node` took the declared value (declared
beats AUTO, `engine/resources.py`), `_clamp_resource_footprint` clamped it to the POOL size rather
than to the per-experiment share, and the reservation took `[0, 1]`. Confirmed at the process level:
the eval launcher's `/proc/<pid>/environ` reads `CUDA_VISIBLE_DEVICES=0,1`, two ranks,
`WORLD_SIZE=2`. The Developer then sized the stage to that envelope —
`accelerate launch --multi_gpu --num_processes 2` — which is exactly what
`roles.py::_developer_footprint_guidance` instructs it to do. So `eval_parallel=2` is real and
currently unusable: node 0 holds both devices for its whole lifecycle, and card-1 will block on
`_acquire_gpus(2)` until it terminates.

**The gap.** `roles.py::_FOOTPRINT_GUIDANCE` asks the Researcher to declare `gpus` and never tells it
a per-experiment ceiling. The operator's goal prose is the only channel that names a number the
Researcher can act on — v5's said "two H200 GPUs are available", and the Researcher reasonably read
it as "you may have both". A role asked to size a request against a budget it cannot see will get it
wrong at some rate, and there is no reason for that rate to be non-zero.

**CORRECTION (2026-08-13).** The first version of this entry said the goal prose was the ONLY channel
carrying GPU information at all. That was wrong: `engine/proposal_cues.py::_cue_gpu_contract` already
spliced "GPU RESOURCE CONTRACT — this pool exposes at most {pool} GPU(s)". So the engine did speak —
and what it announced was the POOL SIZE, which if anything invited the `{"gpus": 2}` declaration this
entry is about. A pool announcement and a per-experiment ceiling are different facts, and only the
second one answers "how much may THIS card ask for". The fix therefore splices the ceiling LAST, so
it is the final device number the model reads; the pool line is left untouched because prompt strings
are contracts.

**What it would take.** Either (a) a hint carrying `max(1, pool // eval_parallel)` into both
Researcher prompts — which means the `RESEARCHER_HINT_ATTRS` registry, both readers, every
delegating wrapper and the forwarding tests, per the registry rule in CLAUDE.md; or (b) clamping
the declared footprint to the per-experiment share instead of to the pool, which decides that the
operator's `eval_parallel` outranks the Researcher's declaration. (b) is one line and a real policy
change — it would fence a legitimately multi-GPU experiment onto one device — so it is a decision,
not a fix. (a) is the honest one and is the larger job.

**BUILT, 2026-08-13 — option (a).** A new registry hint `_gpu_budget_hint`
(`agents/roles.py::RESEARCHER_HINT_ATTRS` + `RESEARCHER_PROMPT_CUES`, so it reaches BOTH readers
through the shared `collect_hint_cues` and all four wrappers through the shared `forward_hints`),
stamped per proposal by `engine/proposal_cues.py::_stamp_gpu_budget_hint` off the rule
`engine/widths.py::per_experiment_gpu_budget`. The proposed `max(1, pool // eval_parallel)` was
adopted with three edges decided against it: pool 0 yields **0** (a positive `gpus` is
`required_unavailable` and fails admission closed, so "you may have one" would produce exactly the
declaration that cannot be served); a CPU-locked task (`gpu_capable() -> False`) is told **nothing**;
a width above the pool still yields **1**, never the floor's 0. The stamp is per-proposal rather than
at construction so it reads the width AFTER `_repin_settled_widths` and after any `budget_extend` —
computing it in `Engine.__init__` beside the width settling would quote a resumed box's own AUTO
resolution instead of the pin. The POOL half stays live, because the reservation clamps against the
live pool. `_FOOTPRINT_GUIDANCE` was also reworded: it said "`gpus=1` only when the experiment
specifically needs one GPU", which read as discouraging the common case; it now says a stated ceiling
is a ceiling and that exceeding it serialises the run instead of buying hardware.
`tests/test_researcher_gpu_budget_hint.py` drives it end to end (both real prompts, a real wrapper
chain, and a real resume onto a bigger box). Option (b) — clamping the declaration to the share — was
NOT taken; a declared footprint is still authoritative at admission.

**Today's workaround.** Pin the Card's resource request (`card_resource_pinned`, `{"gpus": 1}`) —
`effective_card_footprint` merges the pin over the declaration at admission. **Caveat that matters:**
the pin reaches the SCHEDULER, not the Developer prompt (`_developer_footprint_guidance` reads
`idea.footprint`), so pinning after the code is written fences a `--num_processes 2` program onto one
device. Pin before the build lands, or expect to repair the stage command. For the next run, say it
as the contract the engine actually reads: *"each experiment gets exactly ONE GPU — declare
`footprint: {"gpus": 1}` on every card and write single-GPU training code."*

## F1c · Catch a path that escapes the node's own workspace

**BUILT, 2026-08-14** — the COLLISION check ships in `adapters/repo_write_tools.py`
(`manifest_path_collisions` + the three tool sites) and the operator-side warning in
`adapters/repo_task.py::eval_source_tree_command_paths`. What was built is smaller than what this
entry describes, and the reasons are below under *What survived*. Both open questions are answered
from the code rather than guessed, and **two factual claims in the entry's own opening are wrong** —
they are corrected in place, with the evidence, because an operator triaging this would otherwise
re-derive them.

**CORRECTION 1 — node 0 DID record a metric.** Its event log:
`node_evaluated {"metric": 0.708762, "eval_seconds": 13995.5, "violations": []}`, and 0.708762 is
its own `train.log`'s number. **CORRECTION 2 — the waste is a double training, not a lost run.**
Attempt 2 ran `train` for 4,618 s (ok) and then `score` for **4,593 s** before failing: the scorer
found nothing at the source path and ran `subprocess.run([sys.executable, "-m",
"vectorsearch.train"])` itself, and `overwrite: true` destroyed the artifact the train stage had just
produced. Attempt 3 then re-trained for another 4,588 s. So ~9,180 s of the node's 13,995 s — about
**2.55 GPU-hours** — bought nothing. That is the same defect `repo_task.py::eval_entrypoint_unprotected`
already records as "2x GPU per node"; this entry and that docstring were describing one incident.

**Found by watching, 2026-08-12.** `rubertlite-dr-unified-v6` node 0 burned roughly 3.5 GPU-hours and
produced no recorded metric. The train stage was correct throughout: it wrote its checkpoint to
exactly the workdir-relative path its manifest declared. `vectorsearch/configs/config.yaml` carried

```yaml
checkpoint_path: /home/jovyan/data/vectorizer-unified/vectorsearch/experiments/<this node's name>/final
```

— an ABSOLUTE path into the editable SOURCE tree. The experiment name in it is this node's, so the
Developer authored the line. A node runs in its own materialized copy, so a path into the source tree
can never name a node's own output: `.exists()` fails on every node, on every attempt, forever.

The commit `b0327182` closed the half that let this become expensive (the scorer is now protected, so
a Developer can no longer "fix" it by teaching the scorer to retrain). It also ships a cheap advisory
— a note on a successful write whose content hard-codes a path under an editable source root. What
remains is the MECHANICAL detection, and it is deliberately not a blanket ban.

**Why not a blanket ban.** An absolute source path is legitimately needed for a large untracked
in-tree input that `seed_mode: auto` does not copy. The first-class answer there is a `data:` or
`references:` mount, or `seed_mode: "all"` — but refusing a write the model cannot satisfy costs a
repair attempt, and the STAGES phase's retries are the scarcest budget in the loop.

**The false-positive-free version is a COLLISION check against the manifest.** For each declared
`expect.files` entry `F` and each editable source root `R`, a staged file containing
`R + "/" + <path overlapping F's directory chain>` is an unambiguous "this node writes it here and
reads it there" contradiction, decidable from two artifacts LoopLab already owns. On v6,
`F = vectorsearch/experiments/<name>/final/model.safetensors` and the config carried
`<R>/vectorsearch/experiments/<name>/final` — a prefix of `F` under `R`.

**Two things to decide before building it.** (a) ORDERING: `RepoWriteTools` has the manifest in
`self.files["looplab_stages.json"]` after the STAGES phase but not DURING it, and not for a repair
whose manifest arrives only in the seeded working set — so the check must degrade gracefully exactly
where it matters most, and refuse-vs-bounce-vs-note is a real call. (b) A RUNTIME half would be
stronger and is probably the better first build: "the score stage spawned a process running a stage
command the pipeline already ran" is a fact the engine can assert, it cannot be evaded by the
indirection any static check can, and there is already per-stage process supervision to hang it on
(`engine/train_monitor.py`, `engine/asha_monitor.py`). It belongs beside `engine/eval_stages.py` with
its own event type.

**A third, orthogonal and trivial piece:** refuse an `eval.command` / `eval.cwd` that itself names the
editable source root absolutely, at submit time. Cheap — but it catches an operator mistake, not this
one.

### What survived, and what the four mechanisms that landed after this entry already cover

Checked against `read_fence` (deny), its mutation half, `metric_subject` (audit), the `needs` the
protected `score` stage now derives from the declared subject, and `read_allowlist`/`landlock` (off).

**Nothing that shipped can see node 0, and that is the point.** The operative call is
`(checkpoint_path / "model.safetensors").exists()` on a path that is not there. `os.stat` raises **no
CPython audit event at all** (doc 38 §1d), so the fence never engages — there is no read to refuse.
Landlock ABI 2 does not mediate metadata either (doc 38 §3.3), so the kernel rung would not see it
if it were on. `metric_subject` binds at the score stage's start and node 0's subject was present and
correct, so it binds cleanly; the `needs` check is a PRESENCE check and node 0's train stage really
did write the declared file, so it passes — doc 35 §3a measured exactly that against the preserved
workdir. **Every one of the four is downstream of a read that never happens.** The one thing that
sees it is a check on the two DECLARATIONS, which is what this entry proposed.

Node 4 is different and is *conditionally* covered: the fence refuses the SentenceTransformer load,
but only because `transformers` opens `config.json` through Python first (doc 35 §4b measured the
weights read at **zero** audit events), and `metric_subject`'s binding does not prove the scorer read
the subject. So the corruption class is covered-if-you-are-lucky and the waste class is not covered
at all.

**BUILT: the collision check, at the two moments the two declarations meet.** For each absolute path
into an editable source root spelled in an authored file, translate it into the workdir frame and
compare it **component-wise** against every `expect.files` entry in the node's staged manifest;
either being a prefix of the other (equality included) is the contradiction. Refused on
`write_file` / `edit_file` (the new content), and on `declare_stages` (an already-staged file against
the incoming declaration — which is the only route that reaches a repair's *seeded* config, never
handed to a write tool). The refusal names both declarations and both ways out, and rides the same
channel as the existing compile-error refusal: one tool call, nothing staged.

**Measured on the real corpus, through the shipped function.** Over all **2,577** `node_created` /
`node_repaired` working sets in `runs/` — i.e. exactly what the check would have been handed:

| rule | refuses | of which defective |
|---|---|---|
| blanket ban on an absolute source path | 8 nodes (18 sets) | 3 |
| **this collision check** | **3 nodes (6 sets)** | **3** |

The three are v2 node 4, v6 node 0 and v6 node 4 — every known incident, nothing else. The five a ban
would have killed are all legitimate: `rubert-dr-0807` nodes 0/1/3 read a committed base model at
`models/converted/e5-small-v1.1.1`; `rubertlite-dense-retrieval` node 36 is the measured 1-in-116
teacher checkpoint inside the editable root; v2 node 0's config names the repo's own committed
default for a *different* experiment than the one it declares. **The property that makes this
false-positive-free is not a threshold — a legitimate INPUT is never a path the node's own manifest
declares it WRITES.** Component granularity is load-bearing: `models/rubertlite-20e-v7` shares a
string prefix with `models/rubertlite_run` and would be a false positive under `startswith`.

**(a) ORDERING — answered from the code, and the entry's worry does not bite.** `repo_developer.py::_run`
builds `RepoWriteTools`, then for a fresh repo node runs the STAGES phase FIRST; that phase's
`_finalize` persists `write.files["looplab_stages.json"]`, and the write/edit tools are composed only
afterwards for PLAN and IMPLEMENT. So the manifest is in `self.files` at every write. *During* the
stages phase there is nothing to check — its tool set is read-only (`EnvInspectTools` + scouts) and
the manifest arrives through the phase EMIT, not a tool — but the EMIT path is bounced by the same
rule, so a merge node declaring fresh stages over a carried-over config is caught there. A REPAIR
skips the stages phase and `repair_from` pre-loads the failing node's own files, manifest included;
an improve pre-loads the parent's. Confirmed on the incident: v6 node 0's `node_created.files` is
`{looplab_stages.json, vectorsearch/configs/config.yaml, vectorsearch/test.py}` — manifest first, in
the same session. The one case with no manifest is a node whose stages phase produced nothing and
that has no parent, where there is no declaration to contradict and silence is correct. **Refuse, not
bounce or note:** a refused tool call costs one tool call (the compile-error refusal already sets that
price), and the note rung is spent — `_source_root_note` fired verbatim on node 4's own edit and the
node still scored somebody else's model.

**(b) The runtime half is NOT the better first build, and the entry's premise for it is false.**
The FACT is real: node 0's score.log records the scorer shelling out to the train stage's own argv.
But (i) `engine/train_monitor.py` and `engine/asha_monitor.py` are **log** supervision, not process
supervision — `resolve_stage_log`, `read_training_tail`, `snapshot_training_logs`; neither enumerates
a descendant pid or reads an argv, so there is nothing to hang it on and the mechanism would be new;
(ii) any such check is a poll, so it misses a short spawn, and it is evaded by the *more* natural
spelling `import vectorsearch.train; train.main()` in-process; (iii) it does not touch the corruption
class at all — v6 node 4 and v2 node 4 spawned nothing, they read a foreign file. It also cannot
reach the RECORD under doc 36's rule without authenticated evidence, and the only authenticated
version of "the score stage re-ran the pipeline" is not a process at all: it is the score stage
CHANGING the identity of the subject `metric_subject` bound at its start. That is a real follow-up
and it belongs to the subject binding, not to process supervision.

**The third piece, split by what the code already does.** `eval.cwd` needs no rule —
`engine/workspace.py::sandbox_cwd` already remaps an absolute cwd under an editable source onto the
node workdir, and refusing it would retire tested behaviour. The executed FILE is already named:
`entrypoint_candidates` returns `[]` for an absolute path, so `eval_entrypoint_unprotected` already
warns. What was left is an argv ARGUMENT, now reported by `eval_source_tree_command_paths` at both
submit surfaces — a **warning**, not a refusal, for this entry's own reason: there is no manifest to
collide it against at submit time and a fixed in-tree input is legitimate (node 36's shape, authored
by an operator).

**Still open, stated rather than closed.** A colliding line that arrives in the seeded working set
and is never re-written *and* never re-declared is unreachable from the authoring side; doc 31's O1
(the same rule at `engine/workspace.py::materialize`, measured at 0.08–0.62 s/node) is the backstop,
and it was deliberately not built here because its only available action is failing the whole node
rather than costing one tool call. A path assembled at runtime
(`os.environ["REPO"] + "/experiments/…"`) is out of reach of any static rule.

## F1d · A repo task cannot declare ENVIRONMENT for its stages

**Found by watching, 2026-08-12.** `rubertlite-dr-unified-v6` node 0 crashed on its first attempt with
`botocore ClientError: InvalidAccessKeyId … ListObjects` — the data loader reached for S3 because
`VS_LOCAL_DATA_ROOT` was unset. The repair was correct and took three minutes: it added

```python
os.environ.setdefault("VS_LOCAL_DATA_ROOT", "/home/jovyan/data/dr-local")
```

to `vectorsearch/config.py`, at import, so both the train and the score stage see it.

Then **node 1 hit the identical error**, because a node is seeded from the SOURCE repo, not from a
sibling node's workdir. Every node in the run rediscovers the same fact and spends one repair attempt
on it. With `inline_repair_attempts: 12` that is affordable but wasteful, and it is not the agent's
mistake: `EvalSpec` has no `env` field and a stage accepts only
`{name, command, timeout, check, expect}`, so **code is the only surface the Developer has** for an
environment variable. It did the best available thing.

**The workaround, for now:** export the variable in the ENGINE's environment at launch —
`VS_LOCAL_DATA_ROOT=/home/jovyan/data/dr-local python -m looplab.cli run …` — and every node inherits
it with no repair spent. The goal text asking the agent to "set VS_LOCAL_DATA_ROOT" is asking for
something the operator can supply once instead.

**BUILT, 2026-08-13.** Three levels, one rule (`core/envsafe.py::validate_env_map`), most specific
wins: `Settings.eval_env` (every stage of every node) < `cmd.env` (this task's eval) <
`cmd.stages[].env` (this stage). The launch line the workaround above becomes:

```bash
looplab run task.yaml -s eval_env=VS_LOCAL_DATA_ROOT=/home/jovyan/data/dr-local
```

Both open questions were answered the way this entry leaned.

*Is it part of the pinned treatment?* **Yes, and it had to be.** The run level is recorded in
`run_started` (and `config.snapshot.json`), folded onto `RunState.eval_env`, and restored at re-entry
by `Engine._repin_declared_env` beside `_repin_settled_widths` — a declared variable is *why* a node
read one corpus and not another, so a resume taking a different value from live config would keep
appending nodes to a log whose earlier nodes are no longer comparable, with nothing saying so. It
ADOPTS the record and warns rather than refusing (unlike the widths): the value is normally spelled
once in a file the resume re-reads anyway, so refusing would turn an unchanged file into a hard stop.
The key is written only when non-empty, which keeps the default `run_started` payload byte-identical —
load-bearing, because `search/speculation_quality.py::_CALIBRATION_RUN_STARTED_FIELDS` compares that
payload's key SET for equality and an unconditional key would revoke every issued calibration receipt.
The two task levels ride in `task.snapshot.json`, which `resume` re-validates verbatim.

*May the Developer declare it?* **No** — `validate_stages(allow_env=…)` defaults to refusing, so the
fail-closed direction is the default and the four operator call sites opt in. `declare_stages` refuses
the key by name and points at the operator surface; a hand-written `looplab_stages.json` carrying one
is dropped by `materialized_stages`. The stages-phase prompt now also tells the agent not to bake an
`os.environ.setdefault` into the repo instead, and to name the missing variable in its notes.

*A third decision this entry did not raise: secrets.* A value whose name or value looks like a
credential is **refused at declaration time**, at all three levels, by the same screen
(`is_secret_env`) that strips secrets out of every child process — which moved into `core/envsafe.py`
so the declared-env rule and the child-process strip cannot become two answers to one question. The
reasoning is durability, not danger: a declared environment is written verbatim into
`task.snapshot.json` / `config.snapshot.json` / `events.jsonl`, all of which get exported, rendered
and pasted into bug reports, and no redaction keeps a value both safe and reproducible. **No secret
store was invented.** The refusal names where credentials already go (`LOOPLAB_LLM_API_KEY`, a
profile's `api_key_env` — runtime-only, never snapshotted, refused by `--set`) and says plainly that a
credential the *eval's own code* needs has no such boundary, because the eval sandbox is where
agent-written code runs: export it in the launching shell if you accept that.

The two sandbox tiers agree by construction — `_run_eval` composes run+task level ONCE into the single
`env` dict handed to both `make_docker_wrap` and `run_command_eval` — and the per-stage layer, the one
that necessarily differs per child, is applied in `_run_stages`, where the subprocess tier merges it
into the child's env dict and the Docker tier is rebound to forward it as `-e` pairs. A container wrap
that *cannot* carry it fails the stage (`env_unsupported`) rather than running it in an environment the
task did not declare. `tests/test_stage_environment.py` drives every one of these against a real child
process, a real `docker run` argv, and a real event log re-entered by a second Engine.

## F1e · Re-check a repaired artifact contract instead of leaving the metric SALVAGED

**BUILT, 2026-08-13** — `2b980d0` shipped the re-check (`metric_salvage.py` RE-CHECK section,
bound in `evaluate.py::_recheck_repaired_contract`) and `7e9a9a2` hardened it with
`declaration_actually_corrected` the same morning; the reachability analysis and the two open
operator questions are [doc 32](32-f1e-operator-declaration-options-2026-08-13.md).
<!-- FIXED 2026-08-13 (mega-review, doc 40): this entry ended at "what to decide before building it"
     while its sibling F1b carried a BUILT note from the same window — an operator triaging the
     backlog would re-schedule finished work. -->

> **Status update (2026-08-14).** Two facts landed after the note above. (1) Doc 32's option C is
> adopted: `metric_salvage.py::recheckable_salvage` requires `producer == OPERATOR_PRODUCED`
> (`d9d12cc`), so an agent-declared pipeline can never be promoted, and the feature is named by
> `Settings.metric_salvage_repair` (default `true`). Net effect, stated in the module's own RE-CHECK
> section: in both of today's pipeline shapes the promotion is unreachable and v6 node 3 stays
> salvaged. (2) Doc 32's recommended option D — finish the pipeline instead of promoting — is NOT
> built: the stage-scoped machinery exists in the repair loop
> (`command_eval._run_stages(start_stage=…)`, `evaluate._safe_reuse_start`), but the salvage path
> still re-checks-or-leaves-salvaged and never resumes the stages after the corrected one.

**Found by watching, 2026-08-13, and it is a systematic bias — not a one-off.** On
`rubertlite-dr-unified-v6`:

| node | operator | metric | provenance |
|---|---|---|---|
| 0 | draft | 0.708762 | measured |
| 1 | draft | 0.715142 | measured |
| 2 | draft | 0.727991 | measured |
| 3 | **merge** | **0.728113** | **salvaged** |
| 4 | merge | 0.224975 | measured |

**CORRECTION (same night).** The first version of this entry read "every DRAFT is measured, every
MERGE is salvaged" and predicted node 4 would fail the same way. It did not: node 4 set
`run_name: unified-baseline`, the repo composed `unified-baseline_rubert-tiny-lite`, and the
declaration named exactly that — a correct path, a fresh checkpoint, a measured metric. (Its 0.2249
is a real negative result: mean-merging the two models' weights destroyed them.) So the sample is
ONE salvaged merge, not a systematic bias against the merge operator, and the claim was n=1 plus a
prediction. What follows stands on the single honest case and should not special-case merges. A merge node's Developer authors a fresh config
with a new `run_name` and gets the testbed's composed `<run_name>_<model>` directory wrong, so the
`train` stage exits 0, fails its declared artifact contract, and metric salvage recovers the number
from the stage's stdout. Node 4 declares `unified-baseline_rubert-tiny-lite/final/…` — the OLD HUMAN
experiment's name, not its own.

**The consequence is the part that matters.** Under the default `metric_salvage: audit` a salvaged
metric carries a `metric_salvaged` violation and is excluded from `feasible_nodes()`, so it can never
become champion or be bred from. Node 3 produced the best number in the run (0.728113 vs the
champion's 0.727991) and cannot win — on the strength of a path typo, not of anything about the
number. Neither `audit` nor `select` is the right answer to that — `select` would
admit agent-produced bytes wholesale, which is the boundary salvage exists to keep.

**The fix is specific and cheap: the metric was never actually unmeasured.** The pipeline DID produce
the artifact — the near-miss diagnostic proves it, naming the exact file — and `metric_salvage_repair`
already fixes the declaration in the same attempt (node 3: `changed: ["looplab_stages.json"]`,
`cause_repaired: true`). What is missing is one step: after the cause repair corrects the manifest,
**re-run the artifact CHECK against the corrected declaration — not the stage.** The file is on disk;
`verify_stage_artifacts` is a handful of `stat` calls. If it now passes, the contract is satisfied by
the artifact the pipeline really produced, and the node should be recorded as MEASURED with no
violation, because nothing about the number was ever in doubt — only the sentence describing where it
lived.

**What to decide before building it.** (a) The freshness gate: the re-check must keep `since` at the
stage's own start, or a leftover from an earlier attempt could satisfy the corrected path. (b) Which
repairs qualify: only a repair whose `changed` set is exactly the manifest — a repair that touched
CODE has changed what the stage would produce, and its artifact must be re-run, not re-checked.
(c) Ordering against `metric_salvage`: the re-check belongs BEFORE the salvage decision, so a node
that passes never enters the salvage path at all and needs no provenance.

This subsumes most of the value of [F1c](#f1c-catch-a-path-that-escapes-the-nodes-own-workspace)'s
static half without its false-positive problem, because it acts on a contract that has already failed
and an artifact that already exists.

## F1f · The eval batch is a BARRIER, so one slow node idles a GPU for hours

**Found while watching `rubertlite-dr-unified-v6`, 2026-08-13.** Not the same defect as the
`freshness_stale` one fixed in `6f0a8be3` — that one stopped two cards from ever being *selectable*
at the same time. This one stops a new turn from *starting*, and it survives that fix.

**Measured.** `run_started` pins `eval_parallel: 2`. Nodes 5 and 6 were dispatched from the same turn
and really did train concurrently, so the fan-out works. Node 6 finished at 06:19 (86 minutes); node
5 was still training. At 08:11 — **~2 hours later** — `nvidia-smi` showed GPU 1 at `4 MiB` and 0%,
`card-3` folded to `selection_ready=True` with an EMPTY blocker list, and the log since 06:19
contained nothing but `llm_usage`, `train_monitor_alert`, and four `research_completed`/`hint` pairs.
No `card_build_requested`, no `node_building`, no `card_added`. Node 5 was at step 4415/7060 with
2:09 remaining, so the idle window is ~4 hours on one device.

**Why — CORRECTED 2026-08-13, the first answer named the wrong code.** The measurements above stand;
the mechanism below replaces what this entry originally said.

*What it said, and why it was wrong.* It blamed `Engine._dispatch_evals`, which does join its whole
task group before returning. But v6's `run_started` pins `card_driven_selection: true` and
`speculation_depth: 2`, so `_speculation_enabled()` is true and every eval goes through
`Engine._run_card_session` (`engine/speculation.py`) — which delegates to `_dispatch_evals` ONLY when
speculation is off. There are two dispatchers with two different barriers, and the one this entry
described is in the path no run on this box uses. A second claim was wrong the same way: nodes 5 and
6 were NOT "dispatched from the same turn" — node 5 was created 04:01:08, card-7's build was
requested 04:01:11, and node 6 was admitted 04:46:13, i.e. while node 5 was already training.

*What actually happens.* The continuous cross-turn dispatcher this entry asked to be built ALREADY
EXISTS. The Card session admits from `state.pending_nodes()` — the whole folded board, not a per-turn
batch — refills a freed slot on the next poll, runs its own producer, and already commits
`node_created` from the main task inside the dispatch loop. It is switched off by two booleans:
`CardSession.consumer_completed`, set in the `finally` of EVERY eval child, and `yield_outer`. Either
makes `open_for_new_work()` false for ALL slots, and `_card_phase_decide_exit` then will not let the
session return until the LAST eval drains. So the run stops starting work at the FIRST terminal and
still cannot reach the outer boundary until the slowest eval lands. That asymmetry is the defect, and
the code carries its own unresolved `CODEX AGENT` TODO at that line.

Verified on a bounded toy-backend run, not inferred: at the idle moment the probe reads width 2, one
slot free, `terminal`/`budget`/`outer_rebuild` all False, `consumer_completed=True`,
`yield_outer=True`, `open_for_new_work=False`, and `admissible_pending: [2]` — a prefetched,
committed node the engine itself judged admissible, sitting unstarted.
`tests/test_card_budget_refund.py:488` independently documents the same latch.

**The cost, measured across the six width-2 runs on this box.** 115.6 GPU-h of barrier idle against
164.4 GPU-h of work actually done — **82.6% of all second-slot time available while the box was busy
went unused**. Worst single window: `rubert-dr-0807`, 41.8 h at occupancy 1 after having been at 2.
v6 reached width 2 for 1.55 h out of 17.31; v5 never ran two evals at once at all. A SECOND and
larger cost sits beside it — 167.7 GPU-h with no eval running at all, same root (`yield_outer` latches
the producer off during a long eval because the board is only refilled by outer-loop cadences), and
it deserves its own entry.

**FIXED 2026-08-13 — Option 1 ("adopting sessions"), plus a correction to what it costs.** The full
option table is `docs/33-cross-turn-dispatch-options-2026-08-13.md` §6; the shipped shape is its
Option 1 with one deliberate departure, recorded in that doc's §9. What landed:

* the eval task group is owned by `Engine.run`, so a session RETURNS while its evaluations burn and
  the next session ADOPTS them from the run-scoped `Engine._eval_inflight`;
* `CardSession`'s one gate `open_for_new_work` became two — `open_for_admission` (the fold-derived
  stop conditions only) and `open_for_production` (those plus the two live flags). The two flags
  never said anything about the consumer: `consumer_completed` (now `boundary_owed`) and
  `yield_outer` both mean *the outer boundary is owed a turn*, and the answer to that is to return,
  not to go sterile. So a freed slot is refilled by the very turn that observes the terminal;
* quiescence gained its missing half. `_refuse_finish_over_adopted_evals` makes every finish CAS
  refuse over a running evaluation and ask the loop for a drain, `_drain_adopted_evals` pays it
  where it is free (the run is stopping), and the outer loop's own `_drop_stale_speculation` and
  the AUTO-depth ratchet now both see `_eval_inflight`;
* NO new exception to engine invariant #1. This is a LIFETIME change, not a writer change:
  `_record_eval_start_boundary` still runs on the main task at the dispatch decision, node creation
  still commits from the main task in `_card_phase_serve_head`, and every node terminal is still
  appended under `_write_lock`.

**The residual price the option table predicted does NOT exist, and the recommended answer to it was
wrong.** Doc 33 said `_proposal_authority_seq`'s quiet window is lost for the outer `creates` branch
and recommended gating that branch on eval quiescence. Gating it would have been a much larger cost
than the doc admits — `_stage_card_creates`, the ONLY writer of Card inventory, lives in that branch,
so gating it re-creates F1g one lane over: no inventory can be minted while an evaluation runs, which
is precisely the 167.7 GPU-h defect. It is also unnecessary. Every `_reserve_node_build` call site is
reached from SYNCHRONOUS main-task code, and every eval terminal is appended from an anyio task on
the same event loop, so a terminal cannot interleave with the fence's window at all. The window is
quiet by construction rather than by waiting. The one writer that *can* interleave is an eval WORKER
THREAD, and the only folded rows it writes are the `SETUP_THREAD_APPENDABLE` pair, which the fence
now excludes — the exclusion the doc warns against is for `node_evaluated`, which genuinely carries
selection authority; `run_setup_open`/`run_setup_done` are the one folded pair whose splice-position
neutrality this repo has actually PROVEN (`tests/test_setup_thread_appendable.py`), and a fold that
keys them purely by command cannot change which action the policy would choose.

**The regression the code's own TODO asked for now exists:**
`tests/test_card_refill_unequal_durations.py` — a real `Engine`, real Cards, real admission, two
evaluations of unequal duration at width 2, and a minimal outer loop. All five tests fail on the
pre-fix tree with the defect named in the assertion message, and pass after.

**Cheap mitigation available today, no code:** an `eval_timeout` closer to the real training cost
bounds the worst-case idle. `eval_parallel: 1` is the honest floor but costs the 1.55 h v6 did use
and forecloses the prefetch design's justification. Neither is a fix.

**Related, and worth stating because it made this window worse:** node 5's batch size was 8192 as
proposed and is 256 as it runs — three repair rounds shrank it 32x chasing an OOM that never
happened (the watchdog-vs-OOM misclassification fixed in `c862045c`). At 2.93 s/it that turned a
~1.5 h training into a ~6 h one, and it is the 6 h that the barrier then idles a GPU against.

## F1g · `yield_outer` sterilizes the run mid-eval, so NO eval runs for 167.7 GPU-h

**The larger half of [F1f](#f1f-the-eval-batch-is-a-barrier-so-one-slow-node-idles-a-gpu-for-hours),
and a separate defect.** F1f measures a second slot idling while a first one works. This one measures
the box idling *entirely*: **167.7 GPU-h of "serial gap" across the 52-run corpus — time with no
evaluation running at all**, against 164.4 GPU-h of work actually done. It is the single largest
number in this backlog and it is bigger than the barrier it sits next to. It was written up inside
F1f's entry and inside `docs/33-cross-turn-dispatch-options-2026-08-13.md` §5; it deserved its own
row and now has one.

**Measured, `runs/rubertlite-dr-unified-v6`.** Nodes 0→4 are strictly serial, with a 15–37 minute
hole between every consecutive pair (`docs/33` §2c has the occupancy table). During node 0's FOUR
HOUR evaluation the session requested **no** build at all — not one `card_build_requested`, not one
`node_building`. The Cards that would have been buildable arrive two minutes *after* node 0's
terminal (`card_merged`/`card_enriched` at 20:09:30 against a terminal at 20:07:29), so the run pays
the full ~28 minute build latency SERIALLY after each terminal instead of hiding it behind the
evaluation that was already running. Median v6 build ~28 min against 1.4–4.6 h evals, so
`_ADAPTIVE_DEPTH_MIN_EVAL_FRACTION` (0.1) is comfortably satisfied: the prefetch pays here. It just
never fired.

**Why — the same root as F1f, one lane over.** New Cards are produced by OUTER-LOOP work
(`_run_cadences`' hypothesis-board consolidation and Card merges; `_stage_card_creates`, the only
writer of Card INVENTORY, which lives in `_handle_create_actions`). None of it can run while a
session is open. So mid-evaluation the board frequently has nothing selectable,
`_request_card_build` declines, `speculative_raw_actions` returns `[]`
(`card_selection.py` — nothing when `selected` is non-empty *or* `fallback` is empty), and
`_card_phase_request_build` sets `yield_outer = True`. From that instant the session is sterile for
the rest of the evaluation — and, before the fix, still could not RETURN, because
`_card_phase_decide_exit` held it open on `eval_inflight`. `yield_outer` means *the producer needs a
fresh outer authority snapshot*; the session's answer to that was to stop asking for one and wait.

**FIXED 2026-08-13, in the same change as F1f.** `yield_outer` now closes the PRODUCER lane only, and
`_card_phase_decide_exit` returns instead of waiting for the evaluations — so a producer yield during
a long evaluation reaches the outer loop *while the GPU is still busy*, the cadences and the creates
branch refill the board, and the next session elects and prefetches behind the running eval.
Deliberately NOT fixed by giving the outer `creates` branch a quiescence gate, which
`docs/33` §6 recommends for a different reason: that gate would make Card inventory unmintable
whenever an evaluation is running, which is this defect restated.

Driven by `tests/test_card_refill_unequal_durations.py::test_a_producer_yield_reaches_the_outer_boundary_during_a_long_eval`
— one long evaluation, an empty board, and the session must hand back while the GPU is busy. It fails
on the pre-fix tree.

**THE REMAINDER, FIXED 2026-08-14 — production is paced on OCCUPANCY (`docs/33` §10).** Making the
boundary reachable was necessary and not sufficient, exactly as the paragraph this replaces said.
What it got wrong is WHERE the node-count pace was binding. It is not `_run_cadences`: those members
run on every outer turn already, and the two that produce Card material (`_maybe_merge_hypotheses`,
`_sync_card_enrichments`) are board-size-paced and ungated respectively. The binding pace is one lane
lower and stricter than a node count.

*The mechanism, measured on this tree rather than inferred.* A Node under evaluation is still
`pending` in the fold, so `card_next_actions` answers with THAT Node's evaluate action for the whole
of the evaluation — an action the turn cannot start, because the consumer is already running it. The
outer loop's `creates` branch is skipped whenever any evaluate action exists, so `_stage_card_creates`
— the ONLY writer of Card inventory — is reachable only in the instants when NOTHING is running.
Production was gated on occupancy ZERO, which is exactly backwards, and it is the whole of the
remaining serial gap. On a toy-backend run at the GPU-run shape (3 s evaluations against 0.6 s
builds, `eval_parallel=2`, no isolated producer pair) it fired ONCE in a 12-node run, at node 0.

*What landed.*

* `engine/cadence.py::occupancy_due(inflight, queued, width)` — the SECOND pacing rule, beside
  `cadence_due` and deliberately not a variant of it: an evaluation is running and the supply behind
  it does not cover the width. It reads no node count and no mark, and **records none**, which is
  what keeps it and `cadence_due`/`already_covered_at` from satisfying each other's gate. A pace that
  wrote an `at_node` would close the node-count window for a full `every` nodes AND make the at_node
  twin refuse the next starvation at the same count — i.e. it would be a node-count rule renamed.
  Its own idempotence is the CONDITION, which is self-clearing: supply closes it.
* `Engine._occupancy_paced_creates` — when a turn plans no create and every evaluate action names a
  Node already in flight, the same two producer queries the Card session uses
  (`speculative_card_actions`, then `speculative_raw_actions`) are asked with the running Nodes
  masked, bounded to the free slots. No second selection authority: both are existing production
  entry points, and doc 33 option 4 is the write-up of why a new one is the expensive mistake here.
* `_claim_existing_card_builds` takes the same mask. Without it the claim re-derives the lane
  UNMASKED, `forced_card_actions` answers with the running Node's evaluate lane, the comparison reads
  `['card-1'] -> []`, and three turns later the Card is durably `card_auto_dropped` as "unclaimable".
  Measured: with only the inventory half of this change, every Card minted during an evaluation was
  retired that way and the run built nothing until the terminal — no improvement at all.

**NO NEW EVENT, and that was the first question asked.** "An evaluation is running and the board has
nothing selectable" is already derivable: the live half from `Engine._eval_inflight` (the durable
half, `node_eval_started`, is what a RESUMED process reads — and after a crash the honest answer is
that nothing is running), the board half from the fold. A FOLDED row would move
`_proposal_authority_seq` and discard paid proposals; a DIAGNOSTIC row is excluded from that fence
today but would still be an append per poll turn for the whole of a multi-hour evaluation, recording
that nothing happened. There is also nothing for a receipt to fence, because the pace has no mark.

**Measured, toy backend, same 12 evaluations and the identical event-type inventory** (12
`card_added`, 12 `node_building`, 12 `node_created`, 12 `node_evaluated`, **0** `card_auto_dropped`
on both arms), A/B on one tree with the pace disabled in the A arm:

| | max occupancy | time at occupancy 2 | serial gap | slot utilisation | wall |
|---|---|---|---|---|---|
| pace off | **1** | 0.00 s | 8.14 s (18.1 %) | 40.9 % | 45.0 s |
| pace on | **2** | 15.09 s (63 %) | 0.82 s (3.4 %) | **79.8 %** | **24.0 s** |

On the arm WITH an isolated producer pair — where the prefetch already fills the board — the two
arms are identical (79.4 % vs 79.2 % utilisation): the pace stands down when it is not needed, which
is the `queued`/`buildings`/head-request half of the gate doing its job.

**`boundary_owed` is still a BOOL and this change does not make that worse.** At width > 1 several
terminals landing in one poll window collapse into a single owed outer turn, so the
one-turn-per-terminal property `_card_eval_one` claimed is weaker than its comment — now corrected
there, with the three reasons it is left alone. The one this change owns: the pace asks for every
FREE slot rather than for one, and the outer loop is not rationed by the flag (it keeps turning until
nothing is starved), so ONE hand-back refills a collapse of any size. The other two are that two
terminals in one window are at the same node count, where every at_node-idempotent cadence would
decide identically, and that a counter would need a consumer that cannot crash between its read and
its decrement.

**Driven by** four new tests in `tests/test_card_refill_unequal_durations.py`, all failing on the
pre-fix tree: a real `Engine.run` where a Node must be BUILT while another Node's evaluation holds a
slot; the same run asserting no Card is retired as unclaimable; the two-pace separation
(`occupancy_due` cannot read a mark, is due twice at one frozen node count, and `cadence_due` is
unmoved either way); and that the pace's query appends nothing.

**Still open.** The width-1 lookahead — hiding a build behind an evaluation when there is no free
GPU at all — stays the prefetch's job, bounded by `speculation_depth`, because the pace's supply
target is the width. And the eleven outer-loop cadences are still not individually audited for
safety against a moving log (`docs/33` §8).

### F1h · Tell BOTH roles what the per-eval TIME budget IS — F1b, one axis over

**Observed live on `rubertlite-dr-unified-v7`, 2026-08-14.** Node 0's second attempt paced at

```
50/35300 [00:42<7:50:00, 1.25it/s]
```

i.e. the schedule needed **7 h 50 m** against a per-eval budget of **21600 s (6 h)**. The operator had
to hand the run that arithmetic themselves, as a control-plane `hint` (`events.jsonl` seq 791): *"each
evaluation gets 21600 s (6 h) of wall clock, hard … A shorter run that reports a number beats a longer
one that reports nothing."*

**The correction this entry has to make first, because it is F1b's own correction repeated.** The
first framing of this ask was *"the TIME ceiling does not reach the roles at all"*. That is FALSE for
the Researcher: `engine/proposal_cues.py::_cue_experiment_time_budget` has announced it on repo tasks
since before F1b, and v7's own `spans.jsonl` carries the line **54 times verbatim** — *"each experiment
(train+eval) must finish within ~21600s (~6.0h)"*, with the estimate-then-size advice attached. So the
engine did speak; what it did not do is speak to everyone who decides, or say what the number allows.
Three gaps, each a different fact:

1. **The DEVELOPER was told nothing, anywhere** — and it is the role that picks the batch size, writes
   the loop, and declares the `train` stage's `timeout`. v7 node 0's `looplab_stages.json` declares
   `"timeout": 172800.0` — **48 hours against a 6-hour budget** — which is exactly what
   `adapters/repo_developer.py`'s *"Give `train` a GENEROUS `timeout`"* asks for when no ceiling is
   named. The Researcher picks epochs and the Developer picks the batch size; **the pair** determines
   wall clock, and only one of them could see the budget.
2. **The SCRIPT-solution path was silent for the Researcher too.** `_cue_experiment_time_budget` is
   gated on `self._repo_spec`, so on sandbox tasks — where `Settings.timeout` *is* the whole budget and
   `_EVAL_TIMEOUT_GUIDANCE` invites an `eval_timeout` request — no number reached the role at all.
3. **Nothing scoped the Researcher's own `eval_timeout`.** It is ignored outright unless
   `agent_control.timeout` grants `researcher`, and hard-clamped at `max_eval_timeout` when it is
   granted (`engine/shared.py::effective_researcher_eval_timeout`). The prompt asks for the number in
   both worlds and states neither — F1b's sentence exactly: *a role asked to size a request against a
   budget it cannot see will get it wrong at some rate, and there is no reason for that rate to be
   non-zero.*

**WHICH NUMBER IS ANNOUNCED, and why that one.** The one the stage is actually killed at *by the
engine's own resolution*, not the one an operator typed into a field this task never reads:
`engine/shared.py::effective_eval_time_budget` resolves it the way `eval_dispatch::_run_eval` does. An
ACTIVE eval spec owns it through the new `runtime/command_eval.py::eval_spec_time_budget` — the
LARGEST of the base and every profile `timeout`, because a node runs under whichever profile it
selects — and `Settings.timeout` is not consulted at all on that branch. Otherwise the run default
`Settings.timeout` stands. Deliberately **not** `timeout * sweep_timeout_mult`: the multiplier applies
only to an Idea that already carries a `space`, which does not exist at proposal time, and quoting the
stretched budget would overstate it 8x on the shipped default. `None` — a partially built Engine, a
non-finite or non-numeric timeout — prints **nothing**, on F1b's rule that a plausible wrong ceiling is
worse than no ceiling.

**ONE derivation, three readers.** It lives in `runtime/command_eval.py` (spec half) because the third
reader is `adapters/repo_developer.py`, which may not import `engine`. The existing repo cue
`_experiment_time_budget` now delegates instead of re-deriving: two prose statements of one budget,
drifting inside a single prompt, is not a failure anyone would see.

**A HONEST LIMIT THIS CHANGE DOES NOT CLOSE, and the reason it is a statement and not a clamp.**
`command_eval._run_stages` takes a declared stage `timeout` as a REPLACEMENT for the budget, never a
clamp (`finite_timeout(_stg.get("timeout", timeout), timeout)`), so v7's 48-hour leash is REAL — the
train stage would not have been killed at 6 h; it would have overrun the budget the run was planned
around, and the protected `score` stage is the only one the operator's number always binds. Clamping a
declared stage timeout to the eval budget is the option-(b)-shaped policy change F1b declined for
footprints, and it is declined here for the same reason: it would fence a legitimately long training,
it changes execution rather than what a role plans, and it needs its own evidence. So both prompts say
the true thing — *a longer stage `timeout` is not more budget; it only removes the guard.*

**THE EVIDENCE ARRIVED THE SAME DAY, AND THE LIMIT IS NOW CLOSED AT THE OTHER END (F1h-b,
2026-08-14).** v7 node 0 was not one Developer being careless. Measured over every
`looplab_stages.json` in `runs/` against its own run's `task.snapshot.json`: of the **39** declared
stages that have both numbers, **17 exceed the operator's budget** — v7 node 0 at 8.0x, `rubert-dr-0807`
nodes 0-4/7 at 6.0x (86400 s vs 14400 s), v7 node 1 at 4.2x, 0807 nodes 8/9 at 3.8x, v6 node 6 at 2.0x,
`rubert-dr-0805` node 0 at 1.8x and five more at 1.5x. Five runs, **44 %** of the comparable
declarations. Note what the measurement does *not* say, because the tempting reading is wrong: every
one of those 17 predates `_time_budget_note` (`7aa4cbdc`, 08:19 today), so the advisory rung was never
*tried* on them, let alone spent. That is an argument for keeping the note, not for stopping there —
an advisory alone can never make the two numbers agree, because nothing checks the number it asked for.

The clamp this section declined is still declined, and now for a measured reason rather than a
cautious one: on v7 node 0 the node needed ~27960 s against 21600 s, so clamping at the wall discards
**21600 s of GPU time and returns no metric** to avoid **6360 s** of unbudgeted spend — 3.4x worse
than the overrun, at the one moment nothing can act on it. What changed is *where* the bound is held:

* **AUTHORING time.** `declare_stages` — both the stages phase's emit-spec bounce and the
  implement/repair tool — refuses a stage `timeout` above `eval_spec_time_budget`, naming both numbers
  and asking for the schedule to be cut. The same correction costs zero GPU seconds there. The bound is
  deterministic because it is the operator's (docs/36); the *response* — fewer epochs, a subsample, a
  larger batch — stays the agent's, which is a wider action space and not a wider trusted set.
* **CONSUME time, recorded and never killed.** A manifest that reaches the workdir another way
  (hand-written, carried over from a parent on an improve/merge — v7 node 1's 90000 s is exactly this —
  or resumed from before the gate) makes `_resolve_stages` emit a `stage_timeout_over_budget` span
  carrying the stage, the declared seconds and the budget.
* **PER STAGE, not the sum.** A stage `timeout` is a ceiling, not an estimate, and the protected
  `score` stage runs at the operator's number on top of whatever precedes it. Measured: over the 31
  manifests declaring at least one stage timeout, the per-stage rule refuses 17 and the sum rule 18.
  Three percent more coverage is not worth a rule that fires on generous prep ceilings nobody spends.
* **The operator may be the one who is wrong**, and this has to stay diagnosable. Both the refusal and
  the span carry the number the Developer *asked for*; that is the only signal an operator whose budget
  is genuinely too small ever gets, and the refusal tells the Developer to declare an honest estimate
  and say in its notes how long the experiment really needs. What is still open: nothing aggregates
  those spans into a run-level "your budget is short by X" statement, and nothing bounds the SUM of a
  pipeline's stage timeouts, so `prep(budget) + train(budget) + score(budget)` still fits every rung
  here.

**BUILT, 2026-08-14.** A new registry hint `_time_budget_hint`
(`agents/roles.py::RESEARCHER_HINT_ATTRS` + `RESEARCHER_PROMPT_CUES`, so it reaches BOTH readers
through the shared `collect_hint_cues` and all four wrappers through the shared `forward_hints`),
stamped per proposal by `engine/proposal_cues.py::_stamp_time_budget_hint`. Per-proposal for a sharper
version of F1b's reason: `self.timeout` is retuned mid-run by an operator's `budget_extend{timeout}`
(`_apply_control_overrides`, every turn) and by a granted Strategist decision, and role instances are
pooled across builds — a construction-time stamp would keep quoting a budget nobody is running under.
Spliced LAST, after `_gpu_budget_hint`; the two do not fight for the final slot because neither names
the other's quantity. On the sandbox path it also states the governed headroom (`up to {max_eval_timeout}s`,
or *"an `eval_timeout` you set is NOT honoured here"* when ungranted). **The Developer got it too**:
`adapters/repo_developer.py::_time_budget_note`, spliced into the STAGES phase *after* the untouched
"generous timeout" ask (prompt strings are contracts) and into the implement/repair message, which is
the only one a repair session reaches.

**`eval_deadline_grace_s` (shipped 2026-08-13, default `0.0`).** Named only when it is ON, and named as
a RESCUE: *"a judge may grant a stage that is demonstrably about to finish up to Ns more. That is a
rescue, not budget — plan as if it does not exist."* The announced ceiling is unchanged by it. A
ceiling that reads as "you have more than this" would re-open the defect from the other side.

**The wording, and why it is not a bare number.** F1b had to reword `_FOOTPRINT_GUIDANCE` because a
ceiling with no meaning read as discouragement; the same trap here is a schedule so short it measures
nothing. The operator's own framing is spliced — *a shorter experiment that REPORTS A NUMBER beats a
longer one that reports nothing* — with the guard attached: *that is not licence to propose something
too small to answer the question … cut the SCHEDULE, never the comparison.*

`tests/test_researcher_time_budget_hint.py` drives it end to end (the truth table, both real prompts,
a real wrapper chain, a live `budget_extend` retune, both governance answers, the grace clause on and
off, and the real Developer prompt).

**NOT built, and it is the honest remainder.** The **script-path Developer** (`agents/roles.py::LLMDeveloper`,
whose analogous surface is `_developer_footprint_guidance`) is still told nothing. Its only channel to
engine facts is the `Idea`, and `idea.eval_timeout` is an unclamped REQUEST rather than the effective
ceiling — announcing it would be exactly the "number an operator typed" this entry rejects. Giving it
the real one needs a Developer-side inbound hint registry (the mirror of `RESEARCHER_HINT_ATTRS`,
forwarded through `ValidatingDeveloper` / best-of-N / the facade), which does not exist; passing it at
construction is refused on F1b's own recorded ground, since `Settings.timeout` moves mid-run. That is a
separate change. Also unclosed: nothing yet tells either role the MEASURED per-step cost of this repo
on this box before the first node pays for it — the repo cue's calibration line only exists once a node
has finished.

## F2 · Give the Developer simple shell commands

**Asked:** "let the developer run simple bash commands (to check compilation, validate data, etc.)."

**Today.** The Developer writes files and declares evaluation *stages*; it has no interactive shell.
`tools/env_inspect.py` is the read-only escape hatch (package version / API / source), and dependency
installation goes through the declarative path (`runtime/deps.py`, `engine/crash_repair.py`). When a
Developer hits something it cannot inspect it improvises around it — the shape the operator saw was
*"Since I have no shell/install ability, the cleanest repair is a small loguru shim module"*.

**What it would take.** The sandbox tiers already exist (`runtime/sandbox.py`), so the execution is
not the hard part; the contract is. A shell tool needs: an allow-list or a tier that cannot mutate the
shared environment (the 2026-08-11 incident where a mid-run `pip install` corrupted a running node is
the cautionary case), a bounded output projection, and a decision about whether its output becomes a
durable event — because if the Developer can act on something not in the log, replay stops
reconstructing the run.

### DECIDED AND SHIPPED, 2026-08-13 — `looplab/tools/dev_probe.py`, `Settings.developer_probe` (ON)

**It is not a shell, and the reason is the read fence.** `runtime/read_fence.py` is a CPython audit
hook: it covers `open` inside an interpreter and it covers *nothing else*. A tool that could run
`cat`, `cp`, `find` or `bash` would be an execution surface the fence cannot see — one
`cp <source>/experiments/final/model.safetensors ./ckpt` away from laundering a human's artifact into
a node's workspace, i.e. the v6 node 4 defect performed on purpose rather than by accident. So:

> **The probe surface is the INTERPRETER, because the interpreter is what the fence can cover.**

That is the boundary doc 36 asks for rather than the allow-list it rejects: there is no table of
permitted programs to maintain, and the one rule is the same rule that decides whether the read fence
applies at all. `run_probe(code)` runs a short **Python program** in the node's staged files, and the
tool description carries the one-line spellings of the bash the Developer would have typed
(`ls` → `os.listdir`, `head -5` → `readlines()[:5]`, …).

**The three questions the entry above asked, answered.**

| question | decision | why |
|---|---|---|
| what may it run | any Python, under four rules that are path/event predicates and not lists: **no read** of the editable source tree (a fence from the SAME `fence_inputs`/`render` the engine installs, always `deny`), **no write** anywhere (audit hook for existence, `RLIMIT_FSIZE 0` for content), **no new program** (a fork inherits the hook, an exec replaces it), **no GPU** (`CUDA_VISIBLE_DEVICES=""`) | the `pip install` case is closed by "cannot write a file" and "cannot start a program", never by a check for the word *pip* — which is what stops it rotting |
| bounded output projection | exit status + each stream as a TAIL, through the SHARED `tools/_base.py::stream_tails` that `shell_tools.run_command` now also uses | the end of a command is where the error is; a shared split is what stops the two surfaces disagreeing about which half of a failure survives `RESULT_CAP` |
| durable event? | **No — a `tool` span in `spans.jsonl`, like every other Developer tool call** | the four rules *are* the statement that a probe has no side effect, so engine invariant #3 has nothing to gate. Its whole world is a temp directory deleted when the call returns |

**And the read/write question: READ-ONLY, including its own workspace.** This is what buys the answer
above, and the two are one decision. Authoring already has a recorded channel (`write_file`/`edit_file`,
which carry the absolute-path advisory the fence's own docstring quotes); a probe that could write
would be a *second, unrecorded* authoring channel, `node_created.files` would stop being the whole
record of what the Developer built, and the probe would then need to be a folded event. It is doc 36's
second corollary applied literally — a wider action space, and not one inch of extra trusted set.

**The prompt is half the fix.** The Developer's system body asserted *"There is NO shell / bash /
run-command tool — you CANNOT execute anything yourself"*, which is the sentence the observed failure
quoted back at itself. It is now two alternatives spliced at the same position, and the replacement's
load-bearing line is not the tool announcement but **"PROBE BEFORE YOU WORK AROUND SOMETHING"**. With
`developer_probe=false` the prompt is byte-identical to what it was.

**Left open, deliberately.** The probe replicates only the node's STAGED files, not the seeded repo
tree — at build time the node workdir does not exist yet (`RepoWriteTools` collects writes;
`engine/workspace.py::materialize` puts them on disk at eval time), so a compile check of a file that
imports repo modules still gets an ImportError. Network is not cut (an eval stage on the trusted tier
has it, so cutting it here would be a rule the surface it mirrors does not honour). The run directory
stays readable, so a Developer *can* read `events.jsonl`; that is a context concern rather than a
record concern, because nothing the probe reads can reach the record except through `edit_file`.
And `ctypes.dlopen` reaches libc, which reaches `execve` — no audit hook can close that, and the
read fence's own symlink residual is stated the same way.

## F3 · Node workspaces on `git worktree` — **MEASURED AND DECLINED (2026-08-13)**

> **Status update (2026-08-14).** Re-verified against the tree, and doc 37's ask — carry this row as
> DECLINED WITH MEASUREMENT, never as deferred — is what the heading now says (a stray duplicate
> heading from the rename was removed here). Doc 37 §8's R1 follow-up (record the workspace's real
> size as a fold-ignored diagnostic) is NOT yet in code: `engine/workspace.py` still appends
> `workspace_seeded` with only the `materialized` name list and no byte total. The root close is one
> additive field — a byte sum taken during the seed walk, added to that payload with a reader-side
> default (invariant #5-safe) — followed by the written retention policy doc 37 §8 requires before
> any checkpoint reclaim.

**Asked:** "move to git worktree?"

**Today.** A node workspace is a materialized copy (`workspace_seeded` records `.[auto]:N tracked`).
On this box that is ~75 tracked files per node onto a geesefs mount.

**Answered, not deferred.** This entry said "measure the seed cost before paying for that coupling."
That measurement is **[doc 37](37-node-workspace-worktree-measurement-2026-08-13.md)**, taken on both
real testbeds, and it says keep the copy. Do not re-open this without reading it — deferred invites a
re-litigation that will re-derive the same numbers. The four headline findings:

- **The disk win is backwards.** `git worktree add` is a CHECKOUT, not a link: it writes every tracked
  file as a real file exactly as the copy does, and shares only the object DB — which the copy never
  copies. Measured per node: copy 910,829 B, worktree 929,851 B (**+2.1 %**, a `.git` pointer plus an
  18,944 B admin dir). The proposal's stated win is a measured loss.
- **The seed-time win is real only under concurrency and is four orders of magnitude too small.** Full
  cycle at W=1: copy 0.346 s vs worktree 0.376 s (the worktree is *slower* — its teardown is 1.9×). At
  W=12: 0.880 s vs 0.463 s. Against `rubertlite-dense-retrieval` — 91.3 s of seeding, 452,121 s of
  evaluating — the best case is ~45 s, i.e. **0.010 percentage points**.
- **Two of the four costs above are retired by measurement, and two are disqualifying.** The exec bit
  and hardlinks bite neither arm (`core.filemode` is already `false` on that repo; note the sharper
  fact that `os.link` is `ENOTSUP` on geesefs *even same-mount*, not merely `EXDEV`), and the shared
  index lock does not exist — a worktree keeps its own index, 12 concurrent adds had 0 failures, and a
  stale 0-byte `index.lock`/`worktrees.lock` still left `worktree add` at rc=0. What *is* confirmed:
  `git add -A` from inside a node worktree measured rc=0 and wrote blobs into the SOURCE repo; and a
  worktree checks out a COMMIT while the copy seeds the WORKING TREE — 10 of the v1 testbed's 17
  tracked files are dirty vs HEAD and `looplab_eval.py`, the protected scorer, is staged and never
  committed, so a worktree seed would silently train against the operator's last commit.
- **The disk incident was misattributed to this mechanism.** Seeding has never copied an untracked
  file on either real testbed (all 9 `copytree` records in the corpus belong to three non-git
  synthetic testbeds); the v1 testbed is 189 GiB of which **0.22 MB** is tracked. The 727 GB was the
  runs' own retained training checkpoints — v6 `node_4` is 944,779,776 B, 937,847,296 of it three
  intermediate checkpoints plus a final, because that node's own `build_trainer.py` says
  `save_total_limit=3`. The seed is 0.096 % of that workspace.

**The root fix instead**, in cost order (doc 37 §8): record a node workspace's real size as a
fold-ignored diagnostic — today `workspace_seeded`'s 0.9 MB is the *only* workspace fact in the log,
which is exactly why the copy got blamed — then bound retention agent-side, and only then reclaim
non-champion checkpoints, behind a written retention policy, because `metric_salvage` reads those
files.

## F4 · Assistant: an always-on mode

**Asked:** "infinite assistant mode; waiting on statuses; monitoring every N."

**SHIPPED 2026-08-13.** Both halves. The bug was reproduced before it was fixed, and the
reproduction found a second, larger defect than the one suspected.

**The bug, as suspected.** `run_turn` read the engine-wide `agent_time_budget_s` and then applied
`or 300.0`, so the settings table's documented "0 = no cap" silently meant five minutes for the
chat, with no way to raise it without also raising every engine role's. `Settings.assistant_time_
budget_s` is now the chat's OWN wall clock — unset = inherit, else 300 s; `0` = genuinely no cap —
and `serve/assistant.py::assistant_time_budget` states the rule with a truth table.

**The bug, as found.** `drive_tool_loop` has FIVE exits that end a turn without the model choosing
to emit, and only two of them reported anything. Driven against a scripted client that starts
repeating itself, the whole reply was `"Let me look at the next file."` — a bare interstitial
narration, no notice, no `budget_exhausted` key. That is the operator's report verbatim, and it was
the half nobody had looked at: the wall-clock exit had already been given a notice on 2026-08-12,
and the three that fall through to `fallback(messages)` had not. `tool_loop.LOOP_CUTOFF_KINDS` is
now the closed vocabulary of all five and `assistant.cutoff_notice` gives each one a sentence. The
machine-readable fact is also persisted with the turn and rides the SSE `done` payload, whose fixed
key list had been dropping it — i.e. the envelope half of the earlier fix reached nobody, because
the UI uses the streaming endpoint.

**The feature.** `serve/assistant_watch.py` — a durable **watch** record plus a lazily-started
scheduler, deliberately NOT a `serve/jobs.py` job: that registry is process-local, capacity-capped
and evicts a completed receipt after eleven minutes, all correct for "a slow request the browser is
waiting on" and all wrong for "monitoring that must outlive the process". A watch waits on a run
state or on a schedule; the SERVER evaluates the condition over the folded run projection with a
bounded backoff to a 60 s ceiling (an unmet condition costs no model call), and the wake-up carries
the operator's own instruction, so it is re-enterable by a process that did not arm it. Each
wake-up appends a normal assistant turn to the chat. Per doc 36: the agent is woken *because* a
state was observed, never because it said one was; a wake-up runs at the mode pinned when the watch
was armed, with exactly the toolset that mode already grants; the module appends no event and names
no control intent; a wake-up may not arm further watches; and every watch carries a recorded
wake-up budget and lifetime. After a restart a read-only watch is re-armed and a mutating one is
left `interrupted` with its reason, because its turn may have applied half a change.

## F5 · Debug nodes: keep, scope, or remove

**Asked:** "we have an inline-repair limit; when we exceed it a Debug node is created and the fixing
starts again. This looks useless. Either name the cases where it is right, or drop it."

**Today.** Half of this shipped on 2026-08-11: a run in which **nothing has ever worked** now stops
instead of grinding (`systemic_failure_stop`, and see `orchestrator.systemic_failure_stop_reason` for
the 26-hour / 1,705-call measurement that motivated it). What was left was the narrower question the
operator actually asked — once the environment is proven, is a fresh Debug node a better use of a
budget slot than the inline repair that just gave up?

**DECIDED, 2026-08-13 — the Debug node goes.** The operator answered the question rather than asking
for the measurement: *"дебаг ноду нафиг убираем. У нас репейринг есть. Им вот и должно всё
решаться."* And the half that matters more, because it is what the removal would otherwise be evaded
by: **no `draft`/`improve` node may be created that is a Debug node under another name** — i.e. a new
node whose purpose is another attempt at an experiment that just failed. A failure is fixed **inside
the one node, for as long as it takes.**

That "as long as it takes" is not a licence to spin: the bound moves from a counter to a judgment,
which is [F8](#f8-repair-without-a-fixed-bound-stopped-by-judgment-instead-of-by-a-counter) and is
the same change. `systemic_failure_stop` stays as the floor under both. The two must land together —
removing the Debug node while the repair bound is still a fixed count would convert "give up and open
a new node" into "give up", which is strictly worse than today.

**LANDED 2026-08-13, with F8, in one change.** Every producer is cut: `search/policy.py::debug_action`
and `_debug_lineage` (and with them the four policies' forced-debug branch and `legal_actions`' debug
option), the Card lane's forced debug prefix and its `_matching_ready_debug_card` id-reuse helper,
the orchestrator's `debug` create filter entry / `_prepare_node_idea` branch / build branch, and the
operator INJECT surface — which was the last one and the one that most looked like an exception worth
making. The evasion route is closed too and is DRIVEN rather than reasoned about: `improve` anchors on
`breedable_nodes()`, which never contains a failed node, and `tests/test_debug_node_removed.py` pins
that no policy, no legal-action envelope, no Card lane and no real crashing run breeds from a failure.

Three things are deliberately KEPT and documented as inert: `KIND_DEBUG` (an event-log spelling —
preserved runs contain `debug` nodes and must still fold, render and replay), `Settings.debug_depth`
(`LOOPLAB_DEBUG_DEPTH`, every `config.snapshot.json`, and the calibrated speculation envelope all pin
the name), and the fold's debug-leaf readers in `events/card_ledger.py`. Selection moved the other
way: a historical `debug` Card is never live again. Replay reports what happened; selection refuses to
repeat it — and removing the fold readers while leaving the folded shape they interpret is precisely
the silent break, since the L3 budget and `selection_ready` both key on that map.

Two things the removal exposed, both fixed here: `_rule_triage` abandoned a non-mechanical crash,
which was only *conservative* while an abandoned node then got a Debug node — with it gone the same
verdict throws the lineage away, so a `RuntimeError` one repair would have fixed ended a node; and
`_triage_crash` handed the rule path `10**9` instead of the effective cap, harmless while `0` was
rare and wrong the moment F8 made it the default.

Design principle behind both: [`36-agent-driven-decisions-2026-08-13.md`](36-agent-driven-decisions-2026-08-13.md).

## F7 · The Research Atlas: what it uniquely holds, and what it duplicates — **SHIPPED**

**Asked:** "What is the Atlas even for?"

**Shipped.** The recommendation below was accepted and built. The screen is now **Claims & Curation**
at `#/claims` (`ui/src/ClaimsCuration.jsx` / `claimsCurationModel.js`); `#/atlas` and
`#/research-atlas` are canonicalized aliases so every existing bookmark and doc link still lands.
The concepts section is gone — the header now links to `#/concepts`, a new deep link that opens the
run list ON its Concepts view, because a link that landed on the List tab would have been a worse
answer than the section it replaced. The name is the deliverable: the three remaining sections are a
claim ledger (every claim record, and the mixed-evidence subset) and the steward curation log, and
"Claims & Curation" is those two things said out loud, where "Research Atlas" named the one thing the
screen no longer has.

The HTTP contract deliberately did **not** move. `/api/cross-run/atlas` still serves the
mixed-evidence claim records the screen reads, `/api/cross-run/claims` still serves the ledger, and
the `looplab atlas` CLI is untouched — renaming a route whose only production reader is this one
client would have cost a contract change and bought nothing. What did change on the client is what it
keeps: the atlas envelope's `explored`/`thin_coverage` sections and the concept-capsule read receipt
are no longer projected into React state, and the "Evidence source incomplete" notice no longer fires
on a partial capsule store the screen does not touch.

**Today, measured 2026-08-12.** It has four sections, and they are not equal:

| section | also available elsewhere? |
|---|---|
| Concepts seen across runs (+ "observed in one run") | YES — the run list's **Concepts** view is strictly richer: a full `is_a` forest, co-occurrence, a per-concept detail pane and, since this week, the lessons/cases/notes that carry each concept |
| Mixed-evidence claim records | **NO** |
| Claim records | **NO** — 30 rows live on this box; `/api/cross-run/claims` has no other reader |
| Recent proposals + outcomes (the steward curation log) | **NO** |

So three of its four sections are the ONLY home for research claims and for what the paid stewards
proposed and what came of it. The one section that reads as redundant is its concepts list, which a
different view outgrew.

**Recommendation, for the owner to accept or reject.** Keep it and narrow it: drop the concepts
section in favour of a link to the Concepts view, and rename the destination to what it uniquely
holds — claims and curation. Deleting the Atlas outright would take the claim ledger and the steward
outcomes offline with it, which is not what the question was about. Renaming is not cosmetic here:
"Research Atlas" is what makes an operator expect the concept map and then find a worse one.

**ACCEPTED, 2026-08-13.** The operator agreed the rename is the point. So: drop the concepts section
in favour of a link to the Concepts view, and rename the destination after the three things it is the
only home for — the claim records, the mixed-evidence records, and the steward curation log. The name
is the deliverable here, not a layout tweak.

## F6 · Conversation trace: usability, and the steps that are invisible — SHIPPED 2026-08-13

**Asked:** "make the conversation trace more convenient. You cannot see traces from earlier versions
of a node (bugs happened and repair kicked in)."

**What this actually was.** The first reading of it — "earlier GENERATIONS are invisible" — was half
right and shipped on 2026-08-12 as the Inspector's attempt picker (`6640349e`). Measuring the node
the complaint came from showed the other half, and it is the bigger one:

* `runs/rubert-dr-0804` node 1 has **14,507 spans across 2,345 inline repairs over 3 h 50 m, and ALL
  of them are lifecycle generation 0.** `Node.attempt` is bumped by `node_reset`, never by inline
  repair (`core/models.py::Node.attempt` says so; `traceProjection.js` and `docs/guide/ui.md` both
  claimed the opposite until today). So on the very node the operator was reading, the attempt picker
  has one option and does not render at all.
* The window those routes read is a **TAIL**, so widening is the same tail extended. That node's
  512-span default window covers its **last 7.6 minutes** and the 4,096-span ceiling its **last
  59.3** — 74 % of the experiment, including every early repair where the bug first showed,
  unreachable at any `limit`. Raising the ceiling is not the fix and was not done: the conversation
  costs 3.4 ms/span in `full_spans_for_node` plus 0.9 ms/span in `build_conversation`, all on the
  request thread (17.3 s at the ceiling on that node, ~64 s with no ceiling).

**What shipped.**

1. **The window MOVES.** `?before=<span_id>` on `/nodes/{n}/trace` and `/nodes/{n}/conversation`:
   the same `limit` spans, ending at a chosen step. Same rows read, same cost, same ceiling. An
   anchor the run's index cannot place is refused (409 `trace_anchor_unknown`), never degraded to the
   tail — answering with the newest spans under an older episode's label is worse than an empty
   panel. It is material in both the index's window revision and the route's ETag, so no conditional
   read can answer one anchor with another's body.
2. **The node publishes where to point it.** `GET /nodes/{n}/episodes` — every band the conversation
   reads, with none of their contents, each carrying its `anchor`, its recorded repair ordinal, its
   duration and its triage reason. It IS `_conversation_bands`, so the map cannot drift from the
   surface it maps, and it is derived from the in-memory light index with no `spans.jsonl` bytes at
   all: 7,048 episodes in 82 ms on that node.
3. **The render caps stopped throwing away work already paid for.** `_CONVERSATION_TURN_CAP = 256`
   and `_CONVERSATION_STAGE_CAP = 64` were flat numbers scaled by the window; on that node a
   512-span window derived 256 bands and 425 turns and rendered 64 and 105. They are now the
   window's own arithmetic bound (≤1 band and ≤2 turns per span), so what the window READ is what
   the operator READS. Re-measured 2026-08-13, capped vs uncapped at the same window:
   `build_conversation` 0.17 s vs 0.18 s at x1 and 1.42 s vs 1.39 s at the x8 ceiling — **0 ms**, as
   `CLAUDE.md` recorded. It costs bytes: 193 KB -> 778 KB at the default window.
4. **A VISIBLE control** (`ui/src/traceEpisodeModel.js` + `Inspector.jsx::TraceEpisodes`), beside the
   attempt picker and explicitly not its substitute — attempt selects a lifecycle, this selects a
   position inside one. It steps by ordinal rather than listing 2,345 rows, and the map loads when
   the control is opened.

Seeking node 1 to its first repair now returns `propose → implement → train → triage →
inline_repair #1` — the beginning of the experiment, which no surface could open before.

Related and already fixed: run-level agents (the Researcher above all) had no surface at all — see the
new Operations panel.

## F8 · Repair without a fixed bound, stopped by judgment instead of by a counter

**Asked, 2026-08-13:** *"я бы хотел чтобы репейринг у нас был по сути бесконечный, но стопался бы
каким-нибудь LLM критиком и самим девелопером, что типа я фиг знает как чинить."* Paired with the
[F5](#f5-debug-nodes-keep-scope-or-remove) decision to delete the Debug node: everything is fixed
inside the ONE node, for as long as it takes, and nothing may open a fresh node to have another go.

**Today.** The transition from "keep repairing" to "stop" is a COUNTER. A counter cannot distinguish
a repair loop converging on a fix from one that has been rewriting the same line for an hour, and the
two recorded disasters are both cases where the counter was the only thing looking: the 2,345-repair
runaway on `rubert-dr-0804` (whose 369 distinct error signatures defeated the anti-stuck counter
because the underlying `transformers`/`torch` break renamed its symbol every attempt), and the three
rounds of batch-halving on v6 node 5 chasing an OOM that never happened.

**Two signals that bear directly on the question already exist and are not used for it.** The
Developer knows when it is out of ideas — nothing asks it. A critic can see whether successive
attempts address different causes or circle one — nothing runs one. Both are cheap next to the GPU
hours a bad stop decision costs in either direction.

**What it must not become.** An unbounded spend with no floor. The bound moves from a count to a
budget plus a judgment; `systemic_failure_stop` (a run where nothing has ever worked stops) remains
underneath, and the money ceiling stays a hard number. The judgment decides *whether to keep going*,
never *what the result was* — the line is drawn in
[`36-agent-driven-decisions-2026-08-13.md`](36-agent-driven-decisions-2026-08-13.md).

**Ordering constraint.** F5 and F8 land together. Removing the Debug node while the repair bound is
still a fixed count turns "give up and open a new node" into plain "give up".

**LANDED 2026-08-13.** `inline_repair_attempts` defaults to `0` — no count cap — and the transition is
a judgment made of the two signals that already existed:

  * **the Developer's own verdict.** `core/models.py::DEVELOPER_STUCK_PREFIX` — it may answer a repair
    ask with `(developer stuck: <why>)` instead of code, and the engine's repair ask now TELLS it so
    (`engine/repair_judgment.py::developer_stuck_contract`; a sentinel nobody is told about is a
    sentinel nobody emits). No attempt is spent and no `node_repaired` is written, because nothing was
    repaired. It is checked ABOVE `_repair_provider_failure` and that ordering is load-bearing: the
    declaration is not Python, so the provider classifier would call it `unparseable`, and three of
    them would terminalize the node as `developer_crash` **and pause the whole run** naming a provider
    that is answering perfectly. "No fix left" and "the session is dead" are opposite facts.
  * **a critic** (`engine/repair_judgment.py`, `UnifiedAgent.repair_critic`, consulted from
    `repair_critic_after = 3` repairs on) asked one question and given one power: are successive
    attempts addressing different causes, or circling one — and it may only STOP.

**How the judgment is kept away from the record.** Every stop — triage's `abandon`, the Developer's
declaration, the critic's `stop` — terminalizes the node carrying the eval's OWN authenticated
`_failure_reason`. No LLM verdict sets `reason`, so none of them can reach salvage, selection, the
champion, or whether a violation stands. That is doc 36's table: this decides what to do NEXT, never
what the result WAS. And the critic's evidence is authenticated — the per-attempt CAUSE it compares is
the engine's `reason` column (from the sandbox's out-of-band signal channel, now carried durably on
`node_repaired`), while the stderr tail beside it is LABELLED candidate-controlled in the prompt. A
critic that read the KIND of a failure off a banner the failing script printed would hand the
candidate the stop decision, which is exactly what `c862045c` took away from the failure classifier.

**The floors are unchanged and are what keeps this from being an unbounded spend:**
`_UNLIMITED_REPAIR_CEILING` (50), the run's eval-time budget, `systemic_failure_stop`, and the LLM
money ceiling, which raises `BudgetExceeded` at the client where the money is actually spent and
therefore cannot be talked past by any judge. The critic FAILS OPEN — unwired, unreachable or
unreadable contributes nothing — which is the opposite of triage's fail-closed default and is
deliberate: it is an extra veto, not the only stop, so one flapped socket must not kill a node every
other participant considers healthy.

---

## Verified-but-unfixed bugs from the same session

Kept here so they are not lost; each is a fix, not a feature.

* **A timed-out control command makes a run PERMANENTLY uncontrollable.** FIXED 2026-08-13 (fault 2
  had already been fixed on 2026-08-12); the record below is kept as written because the mechanism is
  the reason the fix looks the way it does. What was still live on 2026-08-13, measured by driving
  the real service: fault 1 (the pause postcondition) and fault 3 (retry re-driving a consumed
  event). The loop was also TIGHTER than recorded here — a fresh `pause` with a new idempotency key
  was refused `409 retry_existing_command` naming the spent command, whose `/retry` could only time
  out, so both doors were closed and each named the other. `pause` now completes on the FOLD (with
  the engine-process half reported as `engine_stopped`, never gating), `/retry` mints a fresh intent
  under a fresh marker when the run has superseded the old one, a spent record no longer blocks
  admission, and the legacy `paused_and_stopped` records already on disk are read under the new rule
  so the runs wedged today are freed rather than only future ones. `tests/test_control_plane_liveness.py`
  is the general guard: it searches the reachable state space with real commands and requires every
  refusal to name a remedy that leads somewhere. It found the legacy-record wedge itself.
  Reproduced end to end on
  `rubertlite-dr-unified-v2`, 2026-08-11, and it is worse than a dead chip in the timeline. Three
  compounding faults:

  1. The pause postcondition is `paused_and_stopped` — it requires the engine PROCESS to exit, which
     a pause cannot deliver while a multi-hour evaluation is in flight. On exactly the runs where
     pausing matters it can only ever time out. Here it did, on 2026-08-10, ~20 minutes after a pause
     that had actually landed within one second.
  2. From then on `reject_if_active` refused **every** later control with `command_retry_required`.
     The run could not be paused, stopped or steered at all.
  3. The documented remedy — `POST /commands/{id}/retry` — returned `accepted`, then `executing`,
     then `timed_out` again, and **no `pause` event was ever appended**. The retry re-drives the
     ORIGINAL command, whose `event_seq` points at an intent that a later `resume` already consumed,
     so it re-observes a superseded event instead of issuing a new pause. The run stayed in phase
     `search` throughout.

  Net effect: after 30 hours, 8 failed nodes, 0 evaluated and 2,323 provider calls, the only way to
  stop the run was `kill` on the engine PID. A control plane that fails closed must still leave one
  path forward; this one leaves none. Fixing it means at minimum: a pause postcondition that observes
  the PAUSE (the effect the operator asked for) separately from process exit, a retry that mints a
  FRESH intent when the old one has been superseded, and a UI that surfaces the remedy instead of a
  frozen "Stop requested…" chip.
* **Node build and resume are silent.** FIXED 2026-08-13 (`b4416be`): the engine now emits a
  `phase_progress` beacon at each step boundary of a build and of a resume
  (`events/types.py::EV_PHASE_PROGRESS` — diagnostic, fold-ignored), and
  `ui/src/buildingModel.js::openPhases` folds the beacons into the Dock strip, node card and age
  clock. The record below is kept as written. Long waits with nothing on screen; the operator asked
  for "maximum transparency". The pieces exist (`ui/src/buildingModel.js`, the `node_building`
  event) but nothing streams what phase the build is in.
* **Cross-run memory is mostly toy residue.** MECHANISMS LANDED, STORE NOT RE-AUDITED (2026-08-14):
  the write path now merges exact and paraphrase duplicates
  (`engine/lesson_hygiene.py::consolidate_lessons`), salvaged and trust-flagged metrics no longer
  feed the cross-run writers (`engine/memory.py::unreliable_metric_ids`, 2026-08-13), and deleting a
  run can purge its rows from the five shared stores (`serve/memory_cascade.py`). None of that
  removes what is already in the store, so the 2026-08-11 measurement below stands until someone
  purges the toy runs through the cascade and re-measures `/api/memory` — which is the close.
  Measured through `/api/memory` on 2026-08-11: 161
  lessons of which 7 come from a real task, 163 notes of which **71 are distinct** (56% duplicates,
  one repeated 23 times), 10 cases of which 9 are test fixtures. Retrieval over that store is what
  produces the "changing x and y parameters regressed the metric" card the operator screenshotted.
