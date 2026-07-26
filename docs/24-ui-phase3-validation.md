# UI Phase 3 validation

This document is the acceptance contract for the product-usability phase that adds comfortable
density, saved portfolio views, and cross-run comparison. Automated tests may prove behavior and
data-safety contracts; they do **not** count as moderated usability evidence or pixel-level browser
acceptance.

## Implemented product contract

- Compact remains the default. Comfortable density is an explicit, device-local preference restored
  before the first paint and shared by every owner route.
- A saved portfolio view contains the current project scope, compound filters, sort, List/Map/Compare
  representation, selected comparison runs, and comparison columns.
- Stored views are schema-filtered and bounded to 12; comparison selection is deduplicated and
  bounded to 8 runs. Blocked/full browser storage is an explicit failed save, never false success.
- Compare always pins the run identity and supports persisted custom evidence columns.
- Objective values are ranked only when every selected run has the same known task and the same
  known `min`/`max` objective. Mixed or unknown contracts remain visible but unranked.
- Run detail and configuration are joined only when a post-read lifecycle probe confirms the exact
  run generation. A reset during the read produces partial/unavailable detail instead of a mixed
  snapshot.
- Configuration differences are owner-only, bounded to 160 rendered rows, and disclose truncation.
  Champion cells deep-link to the exact experiment.
- The compare implementation is an on-demand chunk with a 3 KiB incremental gzip ceiling. The
  measured whole-product baseline is 355,455 B gzip under a 348 KiB ceiling; existing route budgets
  remain unchanged.

## Automated acceptance

Run from `ui/`:

```bash
npm test
npm run build
npm run check:bundle
```

The phase is not releasable if any command fails. The suite covers storage corruption and bounds,
view replacement/round-trip, column allow-listing, task/objective compatibility, configuration
evidence bounds, global density initialization, lazy compare reachability, and existing UI
regressions.

## Deploy-level browser matrix

Use a production build served by `looplab ui`; do not substitute Storybook, a static mock, or
screenshots of a different implementation. Seed at least six non-sensitive fixture runs: three
comparable runs for one minimize task, two comparable runs for one maximize task, and one run with
an intentionally different or incomplete comparison contract.

Capture every accepted screenshot only after the state has settled. Reject blank, loading, cropped,
authentication-blocked, or wrong-state captures.

| Dimension | Required coverage |
|---|---|
| Viewports | 320, 768, 1280, and 1440 CSS px |
| Zoom | 100% and 200% |
| Themes | default dark, Paper light, and forced-colors |
| Input | pointer/touch and keyboard-only |
| Portfolio states | first/empty, populated list, filtered empty, Map, 1 selected, 2 selected, 8 selected, mixed-task Compare |
| Detail states | compare loading, verified snapshot, partial/reset-during-read, config truncation, storage failure |
| Run states | active, paused/stalled, reset/repaired generation, terminal, and read-only review |

### Browser journey

1. Open Runs and confirm Compact is the default with no horizontal page overflow.
2. Switch to Comfortable, navigate to a run and Settings, reload, then confirm the mode persisted
   and critical metadata/form help is at least 12 px.
3. Apply a project + task + status filter, change sorting and representation, and save a named view.
4. Change one field and confirm `Modified`; restore the saved view, reload, and verify the exact state.
5. Select one run and verify Compare remains disabled with guidance. Select a second run and open it.
6. Customize columns, refresh, leave Compare, return, and verify the column layout persisted.
7. Compare same-task runs and verify only the objective-best row is highlighted.
8. Add a different-task run and verify values remain visible but no run is ranked as best.
9. Reset one selected fixture run during detail loading and verify configuration is withheld as
   partial rather than joined across generations.
10. Follow a champion link and verify the exact run and experiment open.
11. At every viewport, repeat steps 3–10 with keyboard only; focus must remain visible and follow the
    visual order. At 200% zoom, controls and evidence must reflow or use named internal scrolling
    without two-dimensional page scrolling.

Record screenshot path, browser/version, viewport, zoom, theme, input method, fixture generation,
result, and issue link for every row. A source/DOM test is not a substitute for an unchecked row.

## Moderated ML/DS usability sessions

Recruit 5–7 practitioners who routinely inspect experiment runs; include at least two people who
did not build LoopLab. Use non-sensitive fixture data and record role/experience bands, not employer,
customer, or dataset identifiers.

Give each participant these tasks without leading them to a control:

1. Find the best run and prove why it is best.
2. Explain the failure of one exact experiment attempt.
3. Stop one research direction without stopping the whole run.
4. Compare two runs and identify the meaningful configuration differences.
5. Open a review link and leave a comment on one exact experiment attempt.

For each task record:

- completion without help (`yes` / `no`);
- time to completion;
- wrong turns and unsafe near-misses;
- the first label or surface the participant expected;
- confidence from 1–5 and one short reason;
- facilitator help, if any;
- issue severity and an anonymized observation.

Success criteria:

- at least 5 sessions completed;
- at least 80% unassisted completion for tasks 1, 4, and 5;
- no participant confuses mixed-task metric values with a valid ranking;
- no participant mistakes Card-scoped abandonment for a broader belief/run action;
- no generation-mixed evidence is observed;
- every critical/high issue has an owner and follow-up issue before release.

Do not mark this section complete from automated tests, internal dogfooding alone, or invented
participant results. Attach the anonymized session log and issue links when the sessions actually
occur.

## Current evidence boundary

The automated acceptance is complete when the three commands above pass. Browser screenshots and
the moderated-session exit criteria remain separately evidenced gates until their recorded matrix
and session log exist.
