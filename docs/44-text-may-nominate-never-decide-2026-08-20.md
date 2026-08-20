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
