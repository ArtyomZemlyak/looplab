# Prior-injection citation rate — the number, and why it is UNDEFINED rather than low

*2026-09-18. Doc 52 row 17, marker `prior-injection-hit-rate-unmeasured`. Scope: does a cross-run
prior (lesson, skill, capsule, claim) that a run was SHOWN reach the proposal that followed it. The
instrument shipped on 2026-09-06 (`events/prior_citations.py`, `looplab prior-citations <run_dir>`);
this page is the number it was shipped to produce, taken over the 161-probe corpus that survived the
2026-09-10 wipe in `runs-archive/model-probes`.*

## 0. The reading, in one table

```
runs read: 161;  runs with any prior INJECTED: 14
proposals 439, injections 32, reads 461
shown pairs 0, cited pairs 0
POOLED CITATION RATE: undefined -- no (prior, proposal) pair exists on this corpus
```

The rate is not zero and not low. **It is undefined**, and the reason is one field:

```
prior_injected: {"role": "researcher", "at_node": 0, "rows": 0, "notes": 0, "case": false,
                 "quarantined_useless": 0, "chars": 227}
```

`rows: 0` on every one of the 32 injections. The priors fired EMPTY — 227 characters of header and
no lesson in them — so there was never anything for a proposal to cite. `notes: 0` says the same
about the meta-notes half.

## 1. What that is a fact about, and what it is not

It is a fact about the CORPUS, not about the rank term or the forgetting rung the row worries over.
Every probe ran with its own memory dir, so the shared store each one read was empty: there were no
lessons to select five of. The join the instrument performs is sound — checked by hand on `pgr2`,
where `prior_injected` (researcher, then developer) sits at event 7 and the first `node_created` at
event 161, i.e. exactly the order the join needs. It found no pair because no row was shown, not
because the ordering failed.

So the row's question — *is an injected prior cited?* — **cannot be answered from this corpus at
all**, and the honest close is to say so with the number that shows it rather than to report a 0 %
that would read as "priors are ignored". The read-side rank term and the forgetting rung do still
ship on an unmeasured signal; what this page changes is that the measurement now has a stated
prerequisite instead of an open question.

**The prerequisite: a run whose memory dir already holds lessons.** 14 of 161 runs even attempted an
injection; the other 147 had `reflection_priors` off or no store at all. An arm that wants this
number has to run against a SEEDED store — and it has to check `rows` on its own
`prior_injected` rows before reading any citation rate, because a corpus of empty priors produces a
clean-looking zero.

## 2. What it changed downstream, the same day

docs/60 §60.9 B2 ships a second prior — the measured regime contrast
(`engine/regime_contrast.py`, doc 56 §419.1) — behind `regime_prior`. It reads
`<memory_dir>/regime_contrast.jsonl`, which is written at finalize by each finished run. On a fresh
stand that file does not exist yet, so the block would render *"Implementation regimes measured on
this task: none yet"* and the treatment would be empty for exactly the reason the 32 injections
above were empty.

That is the same shape as doc 56 §422, where B1's floor sat above what 87 % of runs produce: a
treatment that cannot fire cannot be measured. `benchmarks/algotune/PREREGISTERED-B2.txt` therefore
carries a SEEDING clause, and the seed is derivable from the archive rather than invented --
`benchmarks/regime_table.py --seed-ledger` writes the corpus's own per-task contrasts as ledger
rows, so the prior's first sentence is true on the arm's first probe.

## 3. How to re-take this number

```bash
for d in runs-archive/model-probes/*/runs/*/run; do
    python -m looplab.cli prior-citations "$d" --json
done
```

Sum `shown_pairs` and `cited_pairs`; the rate is the second over the first, and it is **undefined
while `rows` is 0 on every injection**. Check that field first — it is the difference between "the
prior was ignored" and "there was no prior".
