# Structured-claims setting retirement

**RESULT — isolated implementation and CPU compatibility acceptance, 11 November 2026.**
No live runtime, source snapshot, calibration experiment, GPU job or historical evidence was changed.

`cross_run_structured_claims` had no runtime consumer since phase1: claim identity is unconditionally
structured. Keeping its switch in the editor suggested an available behavioral choice. This change
removes the active Settings field, legacy-default insertion, calibration-profile override, catalogue
row and configuration-table entry. New snapshots contain no such field. The curated editor has
264 fields, Settings299 and the active calibration profile296; the only removed name in each set
is `cross_run_structured_claims`.

Original configuration snapshots, including either historical boolean value, remain readable via
`RETIRED_SETTINGS`. Input dictionaries and files are not rewritten. The old input no longer becomes
an attribute or active output field. Unknown future settings retain the original refusal policy.

## Calibration identity compatibility

V1 had pinned this already-inert field to False. Removing its **identity slot** would revoke old
receipts despite unchanged behavior. The slot therefore remains reserved in the v1 digest schema,
outside active Settings and the profile override. It may name only a retired Settings field.

For the historical 296-field envelope, retirement alone leaves the profile digest exactly
`sha256:398f9b6eb20702f6dc2c4aa557abc7a9efb7d07fc1d2dceae7c951d37e9b541a`.
For the complete profile with max_nodes2/depth1, the original v1 function from d4773b66 and both the
legacy False document and the new omitted-field document return exactly
`sha256:6388061e07db62b9495a57c163239ff18ede2161e3b71d94e099ac6f5e0fd22e`.
The runtime digest supplies the reserved False slot only when absent; it never replaces an explicit
value. True, a wrong type, an active-setting change or a future key stays distinct/refused. The
quality reader allows only this specific retired slot at its original False value, rather than
all retired settings. Active setting coverage remains exact.

This compatibility is not a new receipt grant. All existing implementation, environment, task,
GPU-identity, source/runtime pins, drift checks, thresholds and self-digest validation remain.
A stale implementation or previously invalid receipt gains no authority. Mixed-document paired
calibration evidence remains refused; original homogeneous documents and new homogeneous documents
both pass the same complete reader. No source or receipt guard was disabled to make the test pass.

## Subsequent schema growth, 17 November integration

The accepted source series subsequently adds `best_of_n_pilot=None` and `cluster_executor=None`.
The active profile therefore has 298 fields and the full profile digest is now
`sha256:e2acfc2f8a57cf35822dfa01e32dc1f639bbe707218f572010f2151f925cd472`.
Its max_nodes2/depth1 runtime digest is
`sha256:09e3fdcc823cc9c43760e3e29fe43ca08e1fab8d5da446323f30c030f0bfc655`.
Tests reconstruct the historical envelope by excluding exactly these two later selectors and
verify both historical pins above. The wider schema normally invalidates old receipts; retirement
compatibility does not authorize ignoring added execution selectors or any other identity field.

## Acceptance

New CPU tests cover original snapshots without mutation; exact historical profile and runtime hash
pins; full native calibration-evidence reader on original/new formats; wrong retired values, changed
active fields and unknown future fields; unchanged mixed-pair refusal; and the actual served settings
catalogue. Existing runtime digest tests mutate every active behavior field independently. Browser
schema-resource tests verify the removed switch and the new exact count. The evidence fixtures use
synthetic GPU identity and do not execute a CUDA probe or measure training.

The retest also found a pre-existing documentation wording mismatch: CLI inspect already prints its
raw snapshot under `--config`, while an exact documentation contract phrase had disappeared. The
paragraph was clarified to match that existing behavior; no inspect runtime change or live incident
is claimed.
