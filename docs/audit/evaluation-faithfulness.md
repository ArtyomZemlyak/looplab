# Evaluation Faithfulness — protocol, no result

**Status:** protocol for the related open item in doc 83; no acceptance has run. This checks the meaning of
an evaluation contract, not whether a scorer merely executed or an agent agrees with its own conclusion.

## Contract under test

Record the scientific question, intended decision, target/label, cohort/split, primary metric and its role,
scoring policy, feature availability at inference, scorer/source digests and comparability facets. Technical
recovery may explicitly mark scientific fields not applicable. Do not interrupt a healthy active fit for a
new paperwork requirement.

## Required cases

1. A deliberately different target: immediate next event versus next converted event. The contract must not
   silently treat them as the same scientific question.
2. A cohort, label, split or scoring-policy change that preserves the scalar shape. Comparison must be unknown
   or refused, never inferred from matching keys or numbers.
3. An event-time input that was not actually available at inference. It must not be relabelled as a
   production-faithful feature merely because event time precedes the target.
4. A protected scorer whose implementation is unchanged but whose stated scientific meaning is wrong.
   Execution integrity alone must not produce an attested verdict.
5. A technical retry with no literature or new research question. It must remain possible without a warning
   storm or a fabricated scientific claim.

An independent reviewer sees the original question and evaluator before the candidate's explanation. Reviewer
agreement is an evidence tier, not a proof of truth. Any post-result contract edit creates a new version.

## Acceptance

Every case resolves to attested, rejected or explicitly unknown with a reason and exact evidence. Unknown
results cannot enter a common scientific ranking as comparable. Machine validation, independent semantic
review and operator attestation remain distinct and auditable.
