# External harness identity and spend

**RESULT — source feature implemented with CPU/API/MCP/UI acceptance; no live deployment claim.**

**Status:** isolated source implementation and CPU acceptance, 11 November 2026.
This concerns an independently driven `external_harness` session, not delegated `cli_agent`
usage accounting. No live client instrumentation, paid provider call, GPU run or deployment was
performed. The original protocol below is retained; source acceptance is recorded separately.

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


## Implementation and evidence

The existing diagnostic `agent_checkpointed` native command accepts exactly one of
`external_work_state` or `external_telemetry`. Its existing generation/idempotency receipt and
NO_SPAWN policy apply. Internal/delegated runs refuse independent external telemetry, so their
existing `llm_usage` ledger is never counted again. No new journal or engine reducer is introduced.

An `external_telemetry` receipt contains a stable `receipt_id`, recorded `node_id`/`attempt`, a
complete schema of revision fields, `usage` and nullable `wall_seconds`. Revision fields are
`provider`, `model`, `cli_version`, `language`, and SHA-256 fingerprints of endpoint identity,
generation parameters, prompt, skills, tools, authority scopes and resource budget. Each field must
be present; unknown values are null. No prompt, tool arguments, endpoint URL, credentials,
transcript, output or reasoning field is accepted. Revision identity includes unknown fields and
changes when only prompt/skill configuration changes. An incomplete revision is explicitly marked.

Usage requires `source` (`agent_summary` or `provider_response`), stable `source_id`, `coverage=delta`,
and nullable input/output/total token counts, cost and currency. A cumulative summary is refused.
Cost has an explicit currency; no price or zero is inferred. `provider_observation` in
`core/external_telemetry.py` extracts only id, model and usage from a provider response object or
mapping, without reading choices/output. SDK pricing estimates and hidden fields are not imported.
Supply the observed provider model ID in the revision; an alias is not assumed equal to a different
resolved model ID. Submit the sanitized returned receipt through the saved native command.

**Observation boundary:** `provider_observed` means the external client adapter supplied usage read
from a provider response. LoopLab does not independently authenticate that response or invoice.
The observer and `provider_attestation=not_independently_attested` are visible in API, UI and export.
An ordinary agent usage summary stays `agent_declared`; naming a provider never makes it independent
billing evidence. Neither tier grants authority or modifies native spend/budget counters.

Exact repeated command receipts append nothing. Repeated provider request / agent segment IDs with
new receipt keys are deduplicated across revisions and attempts. Conflicting duplicates are excluded
from usable subtotals; malformed records make source coverage explicitly unavailable. Only known
subtotals are shown, with unknown counts. The bounded currency summary names any omitted currencies.
Raw records and original revisions remain immutable. A reset never substitutes the current node's
metric for an earlier attempt: its historical lifecycle reference remains visible and the current
outcome is withheld. This is association, not causal attribution to a harness change.

`GET /api/runs/{run_id}/external-telemetry` pages twenty sanitized receipts. The returned
`prefix_digest` binds the exact sanitized receipt content and fences continuation; a changed set returns409 and requires an explicit first-page
read. Progress/briefing carries bounded coverage totals. Research UI shows currencies, unknown
counters, revision fingerprints and attempt references, with passive page reads only. Export embeds
the latest twenty original receipts and a continuation notice, separate native LLM/evaluation ledgers,
and a hash covering the Markdown. GPU hours remain unknown unless independently recorded; evaluation
wall time is never multiplied by an assumed GPU count. Agent wall segments may overlap and are not
elapsed run time. Money, GPU hours and seconds have no combined total.

Acceptance exercises a registered MCP tool through the actual saved-command client and HTTP command
service; exact replay, restarted service and a separate cold Python process reconstruct the same
receipts. Two attempts with unchanged candidate/scorer and a prompt-only revision difference remain
distinguishable, including preserved old identity after reset. Provider usage and an unknown agent
summary remain separate, native LLM totals unchanged. Additional cases cover conflicting aliases,
legacy/malformed records, strict number/secret-field refusal and stable pagination past twenty.
UI tests mount the component, follow exact prefix-bound pages, exercise failure recovery and verify
that every request is GET. Provider adapter fixtures deliberately fail if output/choices are read.

The CPU provider object is a controlled fixture, not an observed production invoice. Production
provider coverage, actual external-client adoption and billing authenticity remain unmeasured; this
source feature adds no mandatory telemetry or launch gate.
