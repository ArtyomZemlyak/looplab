# Memo number fidelity — the instrument, what it may be read for, and the corpus rate still owed

*2026-09-08. Doc 52 row 32, marker `memo-quoted-numbers-unmatched-against-cited-metrics`. Scope:
where the decimals a deep-research memo QUOTES come from. This page holds the INSTRUMENT, what a
number from it is evidence for, and the one measurement this checkout could make; the corpus rate
the row asks for needs the box that holds `runs/`, and the authoring session had one real memo.*

## 0. Why the verifier declined, and why that reasoning still stands

`trust/memo_verify.py::check_claims` checks that a claim cites something and that the citation
resolves. Its NOTE says, and this page does not overturn it:

> this layer deliberately does NOT try to match numbers quoted in the statement against node
> metrics — a research claim legitimately quotes non-metric decimals (arXiv ids like 2506.12928,
> percentages like 37.9, dataset sizes, p-values), and a regex can't tell those from a metric

That is a refusal to CLASSIFY, and it is correct: a "confabulation" heuristic over unclassified
decimals labels well-supported claims fabricated. What it left undone is a different question.
MLReplicate's 59 % — the share of numbers in ACCEPTED write-ups that no artifact supports — is
about the numbers, and asking *"is this decimal one the cited experiments recorded?"* needs no
classifier at all. It needs a MATCH.

## 1. The instrument

`core/research_record.py::number_fidelity` — pure, deterministic, no model, no provider call.

1. **Scan** the claim's statement for decimal literals. Integers are not counted (a memo quotes
   `bs 8192`, `10 epochs`, `positive_threshold=1` by the dozen; a recorded metric is a measured
   decimal), and two shapes are excluded by LEXICAL span rather than by judging the number —
   anything inside a URL/DOI, and an arXiv id (`\d{4}\.\d{4,5}`). The count of what was excluded is
   recorded beside the rest, so the denominator is checkable rather than quietly shrunk.
2. **Match** each decimal against the metrics of the experiments the claim CITES, at the precision
   the memo quoted: `0.88` IS a recorded 0.87764 to two places, `0.8776` is not 0.8835. A trailing
   `%` is read as scale (0.87764 matches `87.8%`). The sign is part of the number.
3. **Report** the channel each decimal landed in: `cited` (a number the cited experiments
   recorded), `run` (a metric of THIS run, of an experiment the claim does not cite) and `none`.

`trust/memo_verify.py::number_fidelity_report` aggregates that over a memo's claims — one row per
claim, the totals beside them, `fidelity` = matched / quoted — using the verifier's own lifecycle
rule (`_is_terminal_evidence`) for which experiments count, so the instrument and the verdicts
beside it measure the same population. `engine/research_cadence.py::_record_deep_research` computes
it for every memo with claims, independently of `Settings.research_verify`, because it is free.

## 2. What a number from this instrument is evidence for

**`unmatched` is not `fabricated`, and nothing in the code says otherwise.** A memo quoting a
paper's 37.9, a sibling run's plateau, a number derived from two metrics (a delta, a ratio), or a
value printed in a log the run never recorded produces an unmatched decimal and is entirely honest.
The verdicts do not move: a claim whose every number is unmatched is still `cited` and still
reaches the LLM rubric exactly as before. Nothing reads the block to decide anything — the same
posture as `provenance_coverage`, and for the same reason (`docs/36`: deterministic code owns the
RECORD, a model decides what happens next).

**The `run` channel is the finding.** A decimal that is a real metric of an experiment the claim
does NOT cite is a mis-attribution, and it is the one shape a reader cannot get from the statement
and the verdict together.

**`fidelity` has hyperparameters in its denominator, by construction.** See §3. Read the channels,
not the share.

## 3. What this checkout could measure

`tests/data/v8_research_memo.json` is the one real memo preserved in the tree — the `at_node: 0`
memo of `rubertlite-dr-unified-v8`, 8 claims. Re-derived by
`tests/test_memo_number_fidelity.py::test_the_real_memo_in_this_tree_measures_what_the_audit_page_reports`:

| | count |
|---|---:|
| claims | 8 |
| decimals quoted | **21** |
| excluded (arXiv id / URL span) | 0 |
| `cited` / `run` / `none` | 0 / 0 / **21** |

And the composition of those 21, by inspection of the statements:

| kind | count | examples |
|---|---:|---|
| result metrics | 6 | 0.8776, 0.8835 (×2), 0.8173, 0.852, 0.728 |
| deltas | 2 | +0.03, 0.04 |
| hyperparameter values | 13 | wd 0.1, temp 0.05, alpha 0.5, pct_start 0.2, threshold 0.264 |

Two things follow, and both are why this page exists before a corpus rate does. First, **the all-
unmatched column is the correct answer for that memo**: the run held zero nodes when it was
written, every result number in it comes from a sibling run, and the memo's own summary says so —
which is exactly the population a numeric "fabrication" heuristic would have libelled. Second,
**13 of 21 decimals are hyperparameters**, so a raw match rate over a real corpus is bounded far
below 1.0 by the shape of the writing rather than by anyone's honesty.

## 4. The corpus rate, and what it needs

Not measured. The row asks for the match rate over the memo corpus, and that corpus is the ~119
completed memos across the thirty run dirs on the bench box (`core/advisory_payloads.py::
memo_snapshot_cue` counts them); this checkout has one. The pass is offline and free — fold each
run, call `number_fidelity_report` on every `research_completed` memo, pool the rows — and the
columns to report per run are: memos, claims, decimals quoted, excluded, and the three channels,
with `run` broken out because it is the actionable one.

Filled in by the box run, one dated line (`RESULT <date>: runs=… memos=… quoted=… cited=… run=…
none=…`), and doc 52's `memo-number-fidelity-corpus-rate-unmeasured` marker is deleted in the same
change.

### 4.1 The box run, 2026-09-18

Run on the box that holds `runs/` — the eleven dense-retrieval run directories, not the bench
corpus §4 estimated from. The pass is the one §4 prescribes and nothing more: fold each run, call
`number_fidelity_report` on every `research_completed` memo, pool the rows.

**RESULT 2026-09-18: runs=9 memos=237 quoted=5267 cited=1191 run=250 none=3826**

| run | memos | claims | quoted | excl | cited | run | none |
|---|---|---|---|---|---|---|---|
| e5small-dr-unified-v11 | 10 | 102 | 233 | 8 | 34 | 3 | 196 |
| e5small-dr-unified-v12 | 57 | 730 | 2045 | 14 | 536 | 55 | 1454 |
| e5small-dr-unified-v13 | 7 | 90 | 189 | 3 | 3 | 1 | 185 |
| e5small-dr-unified-v2 | 11 | 79 | 132 | 0 | 21 | 1 | 110 |
| e5small-dr-unified-v3 | — | — | — | — | — | — | — |
| e5small-dr-unified-v4 | 75 | 720 | 930 | 1 | 415 | 123 | 392 |
| rubertlite-dense-retrieval | 27 | 301 | 683 | 1 | 12 | 8 | 663 |
| rubertlite-dr-unified-v6 | 28 | 240 | 603 | 0 | 88 | 44 | 471 |
| rubertlite-dr-unified-v7 | — | — | — | — | — | — | — |
| rubertlite-dr-unified-v8 | 15 | 129 | 312 | 2 | 72 | 14 | 226 |
| rubertlite-dr-unified-v9 | 7 | 46 | 140 | 0 | 10 | 1 | 129 |
| **TOTAL** | **237** | **2437** | **5267** | **29** | **1191** | **250** | **3826** |

`cited/quoted` = **0.226**, run-channel **0.047**, none-channel **0.726**.

**Two runs report no row and that is a refusal, not a zero.** `e5small-dr-unified-v3` and
`rubertlite-dr-unified-v7` produced no metric at all, so their fold yields an empty metric map and
every decimal in their memos is unmatched BY CONSTRUCTION. They are held out of the totals
(`runs=9`, not 11) under the same denominator rule `core/claimpin.py` states: a pass that cannot
tell "nothing matched" from "nothing was checked" is the vacuous green the rule exists to abolish.
The first attempt at this pass is the worked example — it handed `EventStore` the run DIRECTORY
instead of its `events.jsonl`, read 0 events for all eleven runs, and printed
`cited=0 run=0 none=5365`, a headline that was pure artifact. Both channels empty at once is the
signature: v12 has nineteen evaluated nodes and `0.782726` is in its memo text, so the `run`
channel could not have been empty if the join had run at all.

**Do not read `none` as "invented numbers", and this is the main caveat on the table.** The
none-channel is dominated by decimals that are not metrics and are not supposed to be: learning
rates (`1e-3`), temperatures (`0.05`), R-Drop alphas (`0.5`), thresholds (`0.1`), batch sizes and
epoch counts, and the manual benchmark table's own `+0.03-0.04` deltas. A memo that writes
"temp 0.05" quotes a decimal that no node metric should match. Separating hyperparameters from
unsupported result claims needs a second rule this instrument deliberately does not have — it
MATCHES, it does not classify (see `research_record.py::number_fidelity`'s own note on why that
distinction is the whole design).

**The actionable channel is `run` = 250 of 5267 (4.7%)**, exactly as §4 predicted when it asked for
that column to be broken out: a real metric of this run attributed to an experiment the claim does
not cite. It is not uniform — `e5small-dr-unified-v4` carries 123 of the 250 (13.2% of its own
quoted decimals) against v2's 1 and v13's 1 — so the next read is that run, not the corpus.
