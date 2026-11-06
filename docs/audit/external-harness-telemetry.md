# External Harness Identity and Spend — protocol, no result

**Status:** protocol for the related open item in doc 83; no telemetry acceptance has run. This concerns an
independently driven `external_harness` session, not the existing delegated `cli_agent` usage accounting.

## Required identity

For every attempt record a Harness Revision: provider/model identifier and redacted endpoint identity,
generation parameters, CLI and prompt/skill/tool versions, authority scopes, resource budgets and language.
Store only non-secret fingerprints; raw endpoint credentials, prompts, transcripts and chain-of-thought are
excluded. Link usage and outcomes to that exact revision and attempt.

## Required cases

1. Two attempts differ only in prompt/skill configuration. Their revisions, outcomes and costs must be
   distinguishable even when candidate code and scorer are identical.
2. A provider reports token usage and cost. Preserve them as `provider_observed` with source and coverage.
3. An external agent supplies only a usage summary. Preserve it as `agent_declared`, never upgrade it to an
   independently observed fact. Missing usage remains unknown, not zero.
4. A delegated `cli_agent` uses its existing accounting. Do not create a second total or count it again as
   independent external-harness spend.
5. A restarted run reads the same revision and evidence. No historical record is rewritten with the current
   agent's model or settings.

## Acceptance

UI/API/export can separate model/LLM, GPU and wall-clock quantities without double counting, with explicit
unknown and coverage fields. A harness comparison can attribute effects and costs to exact revisions rather
than to the candidate's code alone. No raw sensitive payload is required for this acceptance.
