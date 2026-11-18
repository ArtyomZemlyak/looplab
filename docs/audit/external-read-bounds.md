# Bounded external reads and exact continuation

RESULT 2026-11-11 — isolated implementation and synthetic CPU/UI acceptance. No live deployment claim.

The former connection bootstrap read full state under the generic client response cap. A long run
could fail before useful work. `/harness-identity` now returns only versioned bounded run identity,
generation, event sequence, complete-source status and external mode. Connection checks reject malformed
or inconsistent UID/generation/mode responses before proceeding; no full-state fallback is attempted.
The existing global cap is unchanged. A real HTTP fixture with a large historical hint payload checks
the identity response stays below 2 KiB, uses only supported reads and leaves journals unchanged.

The map formerly bounded each collection independently, so the aggregate could exceed the transport
cap; UI offset+20 navigation could skip rows when fewer were returned, and report history stopped at
200. The shared pagination kernel now enforces exact whole JSON byte and item limits, explicit field
selection/omissions, whole bounded rows and collection-specific content-bound cursors. Oversized
required source-health metadata fails explicitly. Optional section omission and row abbreviation are
visible, without shortening source URLs into invented links. All report versions can be paged.

`tests/test_research_map_paging.py` round-trips all eight unequal collections without missing or
repeating rows, verifies UTF-8 sizes, cross-run refusal, changed-collection refusal, unrelated-source
continuation, whole-row rollback and explicit oversized summaries/rows. A real API fixture pages
205 historical reports at 4096 bytes per response. The registered MCP tool and local CLI traverse an
exact cursor through the real HTTP route and perform reads only. Mounted UI tests verify unequal
cursors, Back navigation and report-only continuation; catalogue tests cover added notices.

This acceptance proves response boundedness and synthetic recovery of all projected rows, not
production latency, constant-memory source scans, a consistent snapshot across journals or scientific
comparability. Existing full-object/history routes and native journals remain authoritative. Cursor
changes require refresh rather than an inferred continuation through a modified collection.

A follow-up consumer check verifies that export also preserves a valid omitted goal/overview rather
than indexing an optional summary. Export carries the map budget and independent-source observation
and explicitly marks bounded context in Markdown. The regression feeds a valid omission envelope to
the export consumer; it is contract-compatibility acceptance, not a claimed production incident.

## Operator direction text previews

**Follow-up 12 November 2026 — isolated source, no deployment claim.** The earlier aggregate map
bound and scoped-direction lifecycle did not fix legacy hint previews. A legacy hint may contain
up to 20000 characters, but progress showed only its first 2000 while `truncated` described only the
six-item window. A successful command therefore did not imply that an external reader saw the
newest overriding tail. Scoped directions already refuse text above 2000; that contract remains.

The observed live-old-source incident at 04:52 UTC had a succeeded command at native seq 1998,
one current direction and failed exact progress readback. The concise replacement at 04:53 UTC,
seq 2000 passed readback. Historical sanitized receipts remain in the Root capacity package;
this record does not infer that the underlying native hint lost its text. Live old source and this
isolated candidate are different versions; no service was changed for this fix.

Progress and the research map keep the original six text previews. `truncated` now covers a
shortened window **or text**; `window_truncated` and `text_truncated` distinguish these cases.
Aligned `item_details` identify the normalized text's full character count and UTF-8 SHA-256,
its native hint sequence and a generation-bound `full_ref` to the existing bounded `/log-page`.
A shortened item produces an explicit `read_warning`. Read the exact hint event and compare its
sequence and stripped-text digest before acting; refresh current progress afterwards, because
an old event is historical content, not renewed operator authority. The existing page byte cap
still applies: an omitted oversized imported event is unavailable, never a complete instruction.
Model-written or retired directions remain excluded. No hint event, state schema, publication
limit, candidate gate, model call or internal-role prompt authority changes.

Public-command tests accept a long Unicode legacy hint whose final override lies beyond 2000,
restart the app, recover its exact tail through HarnessAPI and the bounded native timeline,
verify compact/full/map parity and digest, and leave the event journal byte-identical. Boundary,
window-only omission, model exclusion, scoped-credential access and stale-generation refusal are
covered separately. This is deduplicated follow-up acceptance of bounded reads and direction
projection, not another OPEN item or evidence of deployment/production decision quality.

Final follow-up acceptance:177 relevant CPU checks passed, strict MkDocs and diff check passed.
