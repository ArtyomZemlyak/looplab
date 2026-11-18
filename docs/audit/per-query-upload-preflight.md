# Per-query metadata preflight — acceptance and local result

At the audited baseline, `looplab/harness/composite.py::upload_per_query` read and spooled the file; server-side
`looplab/harness/per_query.py::publish_upload` decoded arrays, ordered keys and reduced statistics before
checking the registered experiment/arm and node binding. A rejected unknown arm correctly preserves the
scientific boundary, but the inexpensive metadata error is discovered after payload work.

## Required behavior

Provide a bounded read-only preflight using the existing protocol, node lifecycle and upload schema.
Validate generation, node/attempt, experiment/arm binding, declared metric names/specs and test label before
opening the arrays. Return actual revision/context and specific remediation. Do not auto-register an arm,
alter a preregistration, change targets or claim payload/cohort verification without reading that payload.

## Required cases

1. Missing experiment, missing arm, arm bound to another node, stale generation and malformed metric spec
   return an actionable refusal before file read/decode, statistical reduction or large spool copy. Instrument
   these operations in the integration fixture rather than inferring their absence from elapsed time.
2. A valid declared arm passes metadata only. Mark actual query keys, clusters, test digest and metric values
   unverified until the real upload. Missing scalar consistency remains unknown/no measured value; preflight
   must not manufacture a node scalar to make a genuine saved-vector receipt look independently measured.
3. A protocol amendment or node reset occurs after preflight. The real publication revalidates current
   generation, lifecycle and arm binding under the existing write fences; a preflight receipt grants no write
   or scientific authority.
4. Lost replies and existing spooled requests use the original body/action ID and reconcile server receipts.
   An exact replay of a previously accepted upload remains that receipt even when later metadata differs.
   No valid vectors, failed attempts or old receipts are deleted to repair a binding.
5. A source/test/scoring-policy change is distinct from an experiment-registration or budget-only amendment.
   Keep the current conservative protocol warning unless an explicit evidence-backed facet comparison exists.
   Matching keys or numbers alone cannot prove scientific comparability.

## Acceptance

Drive the actual upload and preflight public APIs against an isolated CPU fixture, including a race and an
exact replay. Count payload reads and verify the resulting manifest, test ID and paired comparison. A client
warning alone or a dry run that still reads the entire file does not pass. No GPU rescore is needed.

## RESULT 2026-11-11 — isolated CPU acceptance

Implemented `POST /api/runs/{run_id}/per-query/preflight` and `upload_per_query(dry_run=true)` in
existing MCP orchestration. Metadata validation runs before payload reads and again under the existing
publication sequencer. Normal composite uploads carry the observed node attempt; reset between preflight
and publication refuses. An incomplete or unsupported successful preflight response fails closed. Metadata
receipts explicitly leave payload validation unchecked, and never create an experiment or arm.

Validated with `tests/test_per_query_preflight.py`, `tests/test_campaign_protocol.py`,
`tests/test_campaign_composite.py` and `tests/test_per_query_upload_spool.py`: **74 passed**. The real
isolated HTTP app and MCP composite prove bad metadata causes no payload read, decode or spool; successful
publication retains test IDs, paired comparisons and genuine-vector scalar consistency flags. Reset is
injected both before the binary POST and during decode. Accepted exact requests replay their original
receipt after reset without re-decoding; old requests preserve their original digest without an attempt
field. Lost binary replies still replay the original client spool when the source file is gone.

Scope: local source and toy CPU runs. No live runtime, GPU, saved scientific output or published master
was changed. Deployment remains unverified until a separately authorized rollout checks the deployed API.
