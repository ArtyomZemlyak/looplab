# Research workflow: implementation evidence and boundaries

This table connects the workflow backlog to source, tests and user-facing behavior. The starting
revision for this isolated development copy is a local source-export snapshot, not an upstream Git SHA.
All integration fixtures are synthetic and CPU-only. They do not prove live GPU execution, network
latency or storage flush.

| Ticket | Implementation and proof | Important boundary |
| --- | --- | --- |
| L01 | `external-research-workflow.md`, `skills/looplab-research/SKILL.md`, harness obligations and onboarding; `test_harness_handoff.py` | Read live contract and task, not the copied skill alone. |
| L02 | `harness/briefing.py`, `progress_summary.py`; `test_research_workflow_integration.py` and CLI/schema fixture | Components are independent observations, not one atomic transaction or authority. |
| L03 | `harness/research_map.py`, `campaign.py`, `ResearchMap.jsx`; `test_campaign_protocol.py`, Chromium fixture | Old relationships remain unknown unless explicitly recorded. |
| L04 | `metric_profile.py`, `ProjectMetricProfileEditor.jsx`, `research_quality.py`; `test_projects.py`, language override integration | Project authoring language is a preference, explicit run language wins; historic prose is not translated. |
| L05 | Campaign overview in `advisory_payloads.py`, `report_history.py`, `ResearchMap.jsx`; report history tests and browser smoke | Overview is authored and may be stale, so its publication watermark is shown. |
| L06 | `agent_handoff.py`, `agent_ack.py`, `agent_events.py`; `test_agent_events.py`, `test_agent_event_ack.py` | Hook crash window is at-least-once, consumer deduplicates event identity; ACK is self-attested, not authorization or proof of completed action. |
| L07 | `readiness_binding.py`, `briefing.py`; `test_campaign_protocol.py` producer event/manifest/artifact, scope and retry tests | Binding verifies bytes at read time only; source, protocol, budget and lease admission stay independent. |
| L08 | `ResearchMemoDraft.jsx`, `research_quality.py`; memo tests and integration for separate technical comment | Suggestions are advisory; a link does not prove a claim. |
| L09 | Decision receipt prior-node, alternatives and information-gain fields; `test_campaign_protocol.py`, decisions tests | Authored reasoning is not independent scientific review. |
| L10 | `ConceptView.jsx`, `conceptSearch.js`; concept search tests, existing lens/graph | Alias search uses existing governed renames. Definitions are authored in rationale or memo, not a new shared taxonomy. |
| L11 | Research memo claim/lesson fields and existing separate review journal; `test_research_map.py`, memo tests | Author statuses and claimed evidence are not verified merely by being filled. |
| L12 | `report_bindings.py`, `ReportMetricBindings.jsx`, `reportModel.js`; binding, real synthetic per-query and key metric tests | Numeric agreement and test identifiers do not prove matching ground truth, cohort or policy; stale attempts are unchecked. |
| L13 | `profile_measurements.py`, `ProfileMeasurements.jsx`, `ResearchComparison.jsx`; profile, paired comparison and UI tests | Profile and additive groups are declarations; plots use only present recorded keys; missing values are N/A, never inferred. |
| L14 | `research_export.py`, project manual adapter, `ReportExtras.jsx`; export tests and Chromium download | Manual adapter performs no network publication. Automatic Confluence adapter is not supplied; figures are hashed references, not copied payloads. |
| L15 | `research_quality.py`, `research_notices.py`, `ResearchNotices.jsx`; map, notice, receipt and UI tests | Engineering classification is explicit; self-attested notice disposition is optional. Unrecorded transient/historical notices remain unknown. No completeness gate. |
| L16 | Generation fences, existing native command receipts and append-only journals; protocol, ACK, replay and lost-reply tests | Outbox delivery is at least once across the process crash window; source reads are separately fenced. |
| L17 | Research map, overview, comparison, board, history and exports; `tests/browser/run_research_smoke.sh` | Browser test uses isolated synthetic run and local port, not the active service. |
| L18 | `tests/bench/benchmark_research_context.py` and saved benchmark | Context size is measured smaller in the fixture; briefing CPU latency is higher, not a speedup. No real external-agent or network latency was measured. |
| L19 | `research-workflow-migration.md`, canonical workflow, skill, OpenAPI/CLI fixture | Rollout and deployment remain a separate operator-controlled action. |
| L20 | `test_research_workflow_integration.py`, `test_campaign_protocol.py`, CPU fixture and full UI suite | Producer/score events and data are synthetic; no model or GPU is run. Existing protocol/lease test fixtures are isolated. |

Reproduce examples: `PYTHONPATH=. python tests/bench/export_synthetic_example.py /tmp/looplab-example`.
Reproduce performance with `PYTHONPATH=. python tests/bench/benchmark_research_context.py`. Browser
setup and command are described in `tests/browser/README.md`. For rollout, preserve the active service
and export, review this patch and the immutable source provenance, run the tests in a clean environment,
and only switch service at a separately authorized stopped boundary. Do not restart a healthy engine.
