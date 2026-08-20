# Agent-driven decisions — the direction, and the line it must not cross

**Status: a standing design principle, stated by the operator on 2026-08-13.** It is not a feature
entry. It is the rule new work is expected to be argued against, the way the engine invariants in
`CLAUDE.md` are.

---

## The principle

> Wherever the same evidence can mean different things depending on context, the decision belongs to
> an agent that can read the context — not to a fixed rule that reads a signature.

And its second half, which the operator stated in the same breath and which is what makes this hard:

> Give the agent MANY possible actions — **without weakening safety or the trustworthiness of the
> result.**

## Why: two incidents where a fixed rule decided something only context could resolve

**A watchdog kill read as an out-of-memory.** Both live-log watchdogs tree-kill, so both exit `-9`
with no traceback — *byte-identical* to the kernel OOM signature `_failure_reason` recognised. On
`rubertlite-dr-unified-v6` node 5 the diverge watchdog correctly stopped a training whose loss went
`1.2e+25` and then NaN. The classifier called it `oom`. The Developer then spent three repair rounds
halving the batch size (8192 → 2048 → 512 → 256, ~10 GPU-minutes) while the actual R-Drop/KL
instability went untouched, and its own final rationale said *"(kind=oom) … a memory ceiling"* —
quoting the health-check banner back at itself. Fixed in `c862045c` by splitting `diverged`/`stalled`
out of `oom`, and the deeper reading is this: **no signature could have been written that separated
those two causes, because the two causes produce the same bytes.** Only something that looks at the
loss curve knows which one happened.

**A path that only a reader could see was wrong.** `expect` describes what a stage WRITES and, until
`c862045c`, nothing described what it READS. v6 node 4 trained a model to recall@100 **0.726** and
then scored a human's July checkpoint that an absolute path in an editable config named — **0.225**,
which is the number the run recorded, with the artifact contract PASSED, no `expect_failed`, no
salvage, no violation. v5 node 0 trained for 76 minutes and its scorer read a directory the trainer
never wrote to. In both cases the mistake was obvious *to anyone reading the two declarations side by
side* and invisible to every check that existed. This is the shape the operator names directly: an
agent should recognise "the file is in the wrong place, I can fix that right here" and fix it,
instead of the run dying on a contract or, worse, recording a number about somebody else's work.

## The line: where agent judgment is right, and where it must not go

This is the part that keeps "give the agent more options" from becoming "the agent grades its own
homework". The rule the codebase already follows in `engine/metric_salvage.py` states it in one
sentence, and it generalises:

> The agent writes the training script, and therefore writes the very text an extractor — or an LLM
> — would read.

So the boundary is not *"LLM vs deterministic"*. It is **what the decision feeds**:

| the decision determines… | who decides | why |
|---|---|---|
| what to do NEXT — how to repair, whether to keep trying, which of several fixes to attempt, what the error actually was | **an agent**, with the full context | recoverable, re-checkable, and the evidence is ambiguous by nature |
| what goes into the RECORD — the metric, the champion, whether a node is selectable, whether a violation stands | **deterministic rungs over authenticated evidence** | the candidate's own output is forgeable, and a wrong answer here is not recoverable: it becomes the result |

`c862045c` is worth reading as a worked example of the second row and is *not* a counter-example to
the first. Its classifier now reads the **authenticated out-of-band verdict** and never the stderr
sentinel — precisely because the sentinel is mixed into the candidate's own output. That is the right
call for "what failed", because the failure reason gates salvage and selection. What the same commit
leaves open, and what this principle asks for, is the rung above it: **given an authenticated verdict
and the full live log, an agent decides what to DO** — and that decision may be "this is not a memory
problem at all", stated with reasons, instead of a directive keyed off an enum.

Three corollaries, each of which has already cost real time here when ignored:

1. **Authenticate the evidence, then let the agent read it.** Ambiguity is resolved by more context,
   not by trusting text the candidate controls. An agent reading an authenticated signal is safe; an
   agent reading a sentinel the candidate printed is a route around every gate.
2. **A wider action space must not widen the trusted set.** Adding an action the agent may take is
   cheap. Adding an input the agent's word alone can move into the record is not. Keep them separate
   and say which one a change is.
3. **The stop condition is itself a judgment.** A fixed retry count is the same category error as a
   fixed error classifier: it answers "should we keep going?" without looking at whether progress is
   being made. See F8 below.

## F8 · Repair without a fixed bound, stopped by judgment instead of by a counter

**Asked, 2026-08-13:** *"I'd like repair to be effectively infinite, but stopped by some kind of LLM
critic — and by the Developer itself saying: I have no idea how to fix this."*

**And the structural half of the same ask:** the Debug node goes away, and so does any `draft`/
`improve` node that is a Debug node wearing another name — i.e. a new node created to have another
go at an experiment that failed. **Everything is fixed inside the one node, for as long as it takes.**

Why this is the same principle: today the transition from "keep repairing" to "give up and make a
new node" is a COUNTER. A counter cannot tell a run that is converging on a fix from a run that has
been rewriting the same line for an hour — and the recorded 2,345-repair runaway plus the three
rounds of batch-halving above are both cases where the counter was the only thing looking. Two
signals that actually bear on the question already exist and are unused for it: the Developer knows
when it is out of ideas, and a critic can see whether successive attempts are addressing different
causes or circling one.

What it must not become: an unbounded spend with no floor. The bound moves from a count to a budget
plus a judgment, and `systemic_failure_stop` (a run where nothing has ever worked stops) stays as the
floor beneath both.

## Worked example, 2026-08-20 · the failure CLASSIFIER, split down the middle

The paragraph above says `c862045c` "leaves open the rung above it". This is that rung, and it is
recorded here because the *shape of the answer* is the reusable part, not the feature.

**What happened.** `runs/e5small-dr-unified-v3` finished with three nodes, zero metrics and the
systemic-failure stop. All three died of `torch.OutOfMemoryError`. `_failure_reason`'s `oom` branch
recognises exactly one signature — the KERNEL kill, `exit_code in (-9, 137)` with no `Traceback` —
and an allocator OOM is its mirror image: it raises, prints a full traceback, exits 1. So every one
of the eight `node_repaired` rows read `reason: crash`, the Developer got "diagnose the root cause"
instead of the memory-reduction directive that exists one branch away, and two of its repairs
returned byte-identical files. Measured across `runs/`: **26 of the 41 text-read failures in the
five modern runs were out-of-memory failures recorded as `crash`.**

**And the measured win is not this change's.** A marker list landed the same day
(`triage.py::_is_torch_oom`), it scans the whole 64,000-byte `res.stderr` clamp, and replayed over
`runs/` it resolves **all 26** — including the 9 rows where `torchrun`/`accelerate` swallowed the
child exception and the recorded 500-character `error_in` is nothing but a `Root Cause … exitcode:
1` block. This rung adds **zero further reclassifications on today's corpus**, and saying so is the
point of the entry: the principle is not "an agent would have caught it", because here a literal
did. What the principle buys is what the literal cannot be extended to — a question with more than
one right answer per program (`crash` vs `no_metric`), a reader that can open the stage log rather
than the captured stream, and a rule that does not need a new incident and a new string for the
next allocator. That case is argued, not measured, and the enforcement below is what makes it safe
to take on those terms.

**The line that was drawn.** Not "the classifier becomes a model". `_failure_reason` answers two
different kinds of question and only one of them was ever a guess:

| | who decides | why |
|---|---|---|
| `diverged` · `stalled` · `timeout` · `drift` · `setup` · `needs_failed` · `expect_failed` · `check_failed` | **the engine, finally** | its own record of what IT did — `run_argv`'s out-of-band `signals`, its own clock, the cross-reader's refusal, the structural stage-contract statuses. The judge is not asked and could not be read if it answered |
| `crash` · `oom` · `no_metric` | **the triage judge**, over a closed vocabulary | the three residual buckets, inferred from text the candidate wrote. Nothing out of band saw them |

**Why the split IS the safety argument, and not a promise.** `reason` is not only a "what next"
input — `metric_salvage.NEVER_SALVAGED_REASONS` reads it, and salvage decides whether a number
enters the record. Every member of that set is on the authenticated side and none of the three
judged reasons is in it, so a model's answer cannot move a node into or out of the salvage refusal
in either direction. That is corollary 2 as a checkable property: the action space widened, the
trusted set did not, and a test asserts the disjointness rather than a comment claiming it.

**The record half.** A model-chosen reason does reach the durable rows, so the rows say who chose
it: `reason_source` (`engine`/`triage`) and `engine_reason` (the deterministic classification, kept
whatever the model said) ride beside `reason` on `node_repaired` and `node_failed`. That is the
`at_pending` shape — an ENGINE-written column recording what the engine independently held beside a
model-derived value — and deliberately not the `TrainingVerdict.fault` shape, which is a field the
model emits and therefore cannot witness that a model wrote it.

**The cost was zero.** The judge is already consulted once per failed attempt and already holds the
evidence (the error text, the repair history, the stage logs), so the classification rides on that
emit as a `failure_kind` property rather than as a second call.

## Where to look for more of these

The operator's standing instruction is to find these sites and improve them, not to wait to be
asked. The shape to grep for: **a fixed threshold, enum, counter or signature match that decides
something whose right answer depends on context.** Known candidates, none of them yet assessed:

- the repair-attempt bound and the inline-repair reason set (F8 above);
- `engine/triage.py`'s verdict vocabulary — a closed enum choosing the fate of a failed node;
  **the sibling question, "what FAILED", was assessed and split on 2026-08-20 — see the worked
  example above;**
- the stall/timeout ladders, which read elapsed time and not whether anything is progressing;
- the anti-stuck counter, defeated once already by an error whose signature changed every attempt;
- stage-contract failures (`needs_failed`, `expect_failed`), where "the declaration is wrong and I
  can fix it here" is a legitimate reading that nothing can currently express;
- the futility / stagnation cadences.

Each one is a candidate, not a defect. The test to apply before changing any of them is the table
above: does this decision determine what happens NEXT, or what goes into the RECORD?
