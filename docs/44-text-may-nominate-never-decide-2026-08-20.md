# Text may nominate. It may never decide.

*2026-08-20. Written while replacing the regex failure classifier with an agent diagnostician
(operator directive, same date). The rule below was not invented for that change — it was found
already implemented in the dependency path, having been learned there from an incident. This page
states it, and lists every place that does and does not follow it.*

## The rule

When the engine must answer a question about the CANDIDATE's code, the test for whether it may read
the candidate's text is **not** "is this pattern reliable". It is two questions, in order:

1. **Does an out-of-band channel exist?** If the engine caused the outcome or evaluated it itself,
   it already holds the fact. Use the fact. Minting a text sentinel for something you know
   out-of-band is strictly worse than reading what you know: it launders an authenticated fact
   through a channel the candidate also writes to, and you can never get the authentication back.

2. **If no out-of-band channel can exist — does the resulting action establish the truth anyway?**
   Text may then NOMINATE a candidate answer, provided something downstream can still contradict it.
   Text may never be the LAST word.

The failure mode this rule prevents is not "an ugly regex". It is a **confident wrong answer with
nothing downstream to catch it**, which is the shape that has cost us nodes.

## The precedent: the dependency path already does this

`runtime/deps.py::triage_install_candidates` reads tracebacks with regexes — and is correct, because
of what it refuses to conclude from them. Its own docstring:

> Free rationale text alone can NEVER mint a candidate. […] The CALLER adds the last and decisive
> condition: the distribution must actually be ABSENT from the eval interpreter (`is_present`).

`is_present` spawns the eval interpreter and asks `find_spec`. The regex proposes a name; the world
answers. Path B additionally requires the name be pointed at by the traceback AND named by the
triage, *independently* — corroboration across two sources that cannot collude.

It learned this from an incident, recorded at `engine/crash_repair.py:635`: the regex reduced
`No module named 'pytorch_lightning.utilities.cloud_io'` to `pytorch_lightning`, which was installed;
`pip install pytorch-lightning` answered "Requirement already satisfied" with returncode 0 in 2.19 s;
`InstallResult.ok` is `returncode == 0`, so the engine wrote a `deps_installed` receipt, spent a dep
round, and re-ran into the byte-identical exception. The receipt was FALSE, and a false receipt is
worse than a wasted round: it says the environment was just fixed, so the failure that follows reads
as the agent's code being wrong. The fix was not a better regex. It was `is_present` — an
out-of-band check that lets the world contradict the text.

## Where the rule was not applied *(both fixed — see the resolution below)*

`engine/triage.py::_failure_reason` is the one place where text gets the **last** word. Nothing
downstream re-checks the reason it returns, and that reason routes the repair direction, gates metric
salvage via `NEVER_SALVAGED_REASONS`, and can end a node. Confidence there is unearned by
construction, however good the patterns are.

Two sites deserve naming, because they fail *different* halves of the rule.

**Fails question 1 — the fact was held and thrown away.**
`runtime/command_eval.py:2791` returns `RunResult(..., stderr="setup failed:\n" + err, ...)` on the
branch where the engine has just observed `rc != 0 or timed_out` from the setup step *it ran*. The
knowledge is out-of-band at that instant. It is then written into stderr — a stream the candidate
also writes — and read back at `engine/triage.py:210` with `.startswith("setup failed:")`. Since
`setup` is in `NEVER_SALVAGED_REASONS`, a candidate whose stderr happens to begin with that literal
has a metric it really produced suppressed. `RunResult` already carries `timed_out`, `diverged` and
`stalled` as out-of-band fields; this one belongs beside them. The module's own watchdog comment
already warns that stderr sentinels "are mixed into the candidate's own output and are forgeable" —
the warning was written, and then not applied one branch below it.

**Fails question 2 — no out-of-band channel can exist, and nothing re-checks.**
The allocator OOM (`_is_torch_oom`, `engine/triage.py:162`) is the honest case: the engine did not
cause the exit, the candidate's own process raised and died, and device-level free memory is not a
substitute because it is sampled after the allocation is released. Text is the only evidence there
is. What was missing was the second half — something downstream that could contradict it. That is
what the diagnostician's evidence field is for: a verdict that cites the file, the line and the log
record it stands on is a verdict that can be checked afterwards. **An agent whose verdict cannot be
re-checked is only a more expensive regex.**

## Resolution *(2026-08-20, same day)*

Both items above shipped, and the two OPEN markers that stood here are deleted rather than ticked —
closing an open item is a deletion, or the index stops being the backlog.

**Question 1 (`setup`).** `RunResult.setup_failed` is now the out-of-band field, set on the branch in
`runtime/command_eval.py` where the engine has just read `rc` from the setup step it ran. The stderr
prefix stays, because it is what a human reads in the durable row — but nothing DECIDES from it any
more. `setup` is engine-final for a stronger reason than before, not a weaker one.

**Question 2 (the allocator OOM).** `engine/failure_diagnosis.py` is the diagnostician, and it is the
existing crash-triage call rather than a second agent: that call already spends 8.82 provider calls
per failure (335 calls / 38 decisions over v8+v9+v3), so a separate one would double the
failure-path cost and could contradict the directive it is building. Its verdict carries
`{source, locator, quote}`, and the engine re-resolves the locator inside the workdir fence and
stamps `reason_evidence_resolved`.

### What that verdict RECORDS (2026-08-21)

One citation was not the record, and the corpus says so: replayed over the committed 122-row
`failure_triage.v1`, **not one** preserved stderr tail contains a torch allocator marker
(`oom_marker_in_evidence` 0/16, `allocator_message_in_stderr` 0/7), because a tqdm bar pads the last
~440 characters of an OOM'd training run and `_eval_failure_text` keeps 500. Widening the evidence
to this role's own log reads moves **16 rows** (88/118 → 104/118 for the frozen classifier).

**The obvious fix — preserve more stderr — was refused, and the refusal is the design.** The bytes
were never lost: `sandbox._tee_drain` writes `<workdir>/<stage>.log` and nothing deletes it, 787 MB
across the eight preserved runs including one that finished long ago. Preserving the engine's whole
64 KB `res.stderr` clamp on every failure was measured at **+8.8 MB, +27 %** over the 32.7 MB of
existing event log for the 138 failure-bearing rows — and it still reaches none of those 16, whose
answer is in a stage log rather than in stderr at all.

What was missing was the ACCOUNT. By the time the diagnostician answers it has already read the
logs, the config and the code the eval ran, and every bit of that was discarded when the call
returned. It now writes two additive, fold-ignored columns on `node_repaired` and `node_failed`
(invariant #5):

* **`reason_summary`** — what failed and BECAUSE OF WHAT, with the numbers and names INLINE: the
  allocation size, the parameter and its value, the stage, the epoch, the exception type. The bar is
  about CONTENT, not length: *"see train.log:41233"* is a failed summary even when that citation is
  exactly right.
* **`reason_findings`** — the trail, `{source, locator, quote, means}` per entry, each re-resolved
  by CALLING `evidence_citation_resolves` rather than restating it (one fence, not a second
  spelling) and stamped `resolved` (True / False / None).

**Why the summary carries the weight, and why there is no machinery around dead links.** A citation
may die. That is not a reason to build a content digest, a gone-versus-changed discriminator or a
pruning policy — it is the reason the summary must stand alone. A finding whose citation does not
resolve is MARKED and KEPT: the finding stands on its own text, and a reader owed the account is not
owed a working link. The inverse ordering — a pointer where the account should be — is the record
that rots, and no amount of link machinery saves it.

**And a summary is the agent's ACCOUNT, not the evidence.** If the diagnostician is wrong, its
summary is wrong in exactly the same way and no reader can tell from the summary alone. That is what
the citations are for and why the two halves are kept in separate blocks on the row: `summary` and
`means` are for READING, `quote` and `locator` are for CHECKING, `resolved` is the engine's and not
the model's.

Cost, both directions: **no prompt growth** (the 500-character `err` handed to the repair prompt and
to the judge's history is byte-identical — `tests/test_diagnosis_record.py` pins it as an equality,
not a budget) and a bounded record (`FINDINGS_CAP` 6 × 3 × 300 plus a 1,200-character summary,
~6.8 KB worst case per failure row, +2.9 % over the preserved corpus if every diagnosis fills every
field, which no real one does).

**One defect was found and closed on the way through.** `reason_evidence` was NOT redacted —
`evidence_quote` is by its own schema description "the one line that settles it, quoted", i.e. bytes
a model copied out of a stage log, landing on a durable row that travels into `events.jsonl`, the
trace, the UI and every export. That is the eighth persisted output channel and the same defect the
C2 sweep closed for `node_failed.triage_rationale` one field over. All of `summary` / `locator` /
`quote` / `means` now go through `Engine._redact` BEFORE their cap, which matters: measured over the
257 preserved stage and console logs, a 500-character window carries **0** redaction masks while a
wider read carries 3 at 8 KB, 36 at 16 KB (including a real `password`) and 384 at 64 KB — and this
role reads with TOOLS, so its quotable window is the whole file.

**What is NOT measured, and by what.** `score-triage` is byte-identical in every arm after this
change (recorded 76/118, frozen 88/118, frozen-widened 104/118, live 88/118) — correctly, because
the classifier did not move and this is about the RECORD, not about who decides. What DID move is
what a candidate replaying the record can reach: **0 of 122 corpus rows carry an allocator marker in
the durable 500-character record, and 16 carry one in what the diagnostician read** and now writes
down; 36 of the 122 labels rest on evidence the durable record cannot show at all (16
`oom_marker_in_evidence`, 10 `reused_stage_later_scored`, 7 `allocator_message_in_stderr`, 3
`nonfinite_loss_in_log`). Those numbers are a PROXY: `triage_corpus.build_dataset` reads `error_in`
and the triage spans, not `reason_summary`/`reason_findings`, so no cut of the bench can yet score a
candidate on the new columns directly. Teaching the extractor to carry them is the one follow-up
that would make this claim checkable rather than argued — it is not done here, and until it is, the
`frozen-widened` arm is the closest thing to a measurement of the new record.

**What did NOT get solved, stated because the rule above demands it.** There is still no out-of-band
probe of a failure KIND. Every candidate is either the text rule that was deleted, or a fact already
known false of the case that motivated this (a `torch.OutOfMemoryError` RAISES — full traceback, exit
1), or sampled after the process is gone. So the CONCLUSION cannot be re-checked, and the second half
of the rule is satisfied in the weaker form available: what is checkable is whether the diagnostician
LOOKED. That check RECORDS and never gates, because refusing an uncited-but-correct diagnosis would
lose it and nobody has measured how often a live model mis-formats a citation. **Do not promote it to
a gate without that number.**

Two corrections to the analysis above, both worth keeping visible:

* `oom` did not merely move — it became ANSWER-ONLY. Both of its engine producers were text rules,
  so no engine path can name an out-of-memory failure at all; it is a `crash` until something looks.
* the engine must still HAND OVER what it holds. Deleting the kernel-OOM rule was right on
  question 1, but `_eval_failure_text` surfaces `exit=` only when stderr is blank, so a pod cgroup
  kill leaving a `Killed` line reached the judge as that one word. `engine_observed_facts` states the
  exit status and says what it excluded (no watchdog claimed the run) — the fact, never the
  conclusion. That is question 1 applied a second time, one rung along from `setup`.

And one place the rule was found violated *by this very change*, which is the most useful entry here:
letting a diagnostician choose `node_repaired.reason` handed the F8 critic's cause column to a model,
and that column is a TRUSTED input to a stop decision (`c862045c`). The rule extracted from fixing it
generalises past this page:

> A **gate** keyed on the cause reads the ENGINE's column. Only the **directive** and the **record**
> read the diagnosis.

## The ownership test, stated once

The engine classifies what it **did** or **computed**: its own clock ran out and it killed; its
health watchdog saw a non-finite value and killed; its stall watchdog killed; it read two metrics
itself and compared them; it ran a step and saw the return code. Everything at the level of the
experiment's own code is a matter for investigation, not for pattern matching — and the investigation
must leave evidence behind, or it has not improved on the pattern it replaced.
