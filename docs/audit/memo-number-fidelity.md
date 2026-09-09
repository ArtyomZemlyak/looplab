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
