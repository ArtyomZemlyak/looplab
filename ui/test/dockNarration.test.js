import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

test('timeline narration stays renderable for malformed and forward-compatible events', async () => {
  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const { eventNarration } = await vite.ssrLoadModule('/src/Dock.jsx')
    assert.equal(eventNarration({ type: 'future_event' }), '{}')
    assert.equal(eventNarration({ type: 'node_created', data: null }),
      'node_created — details could not be summarized')
    // A non-string rationale (malformed data) no longer throws to the generic fallback: the feed narration
    // now runs rationale through `stripMd`, which coerces it to a string, so node_created still renders its
    // node info gracefully (rationale is always a string in practice — models.py Idea.rationale: str).
    assert.equal(eventNarration({ type: 'node_created', data: {
      node_id: 3, operator: 'improve', idea: { rationale: 7 },
    } }), 'node #3 via improve — 7')
    assert.equal(eventNarration({ type: 'node_failed', data: {
      node_id: 4, reason: 'guard against undefined behavior',
    } }), 'node #4 failed (guard against undefined behavior)')
    for (const [type, data] of [
      ['node_building', { operator: 'improve' }],
      ['data_leakage', {}],
      ['run_setup_finished', {}],
      ['node_confirmed', { node_id: 2, mean: 1, seeds: 3 }],
      ['strategy_decision', { strategy: {} }],
      ['train_monitor_alert', { node_id: 3 }],           // missing status -> no coerced verdict
    ]) {
      assert.equal(eventNarration({ type, data }),
        `${type} — details could not be summarized`, `${type} must not coerce a missing field`)
    }
    assert.equal(eventNarration({ type: 'run_started', data: {
      task_id: 'task-a', direction: 'max',
    } }), 'run started — task-a (max)')
    assert.equal(eventNarration({ type: 'train_monitor_alert', data: {
      node_id: 3, status: 'broken', reason: 'loss diverged', confidence: 0.9,
    } }), 'training monitor: #3 looks broken — loss diverged (90% conf)')
    assert.equal(eventNarration({ type: 'asha_rank', data: {
      node_id: 3, intermediate: 0.42, quantile: 0.5, population: 4,
    } }), 'ASHA: #3 0.42 endpoint rank warning')
    assert.equal(eventNarration({ type: 'asha_rank', data: {
      node_id: 3, intermediate: 0.42, quantile: 0.5, population: 4,
      endpoint_underperforming: false, resource_underperforming: true,
      comparable_population: 3,
    } }), 'ASHA: #3 0.42 same-resource rank warning')
    assert.equal(eventNarration({ type: 'future_event', data: {
      text: 'params.x was undefined at eval',
    } }), '{"text":"params.x was undefined at eval"}')
    assert.match(eventNarration({ type: 'future_event', data: { text: 'bounded' },
      _log_page: { truncated: true, raw_bytes: 2048 } }),
    /details omitted \(2.048 source bytes exceed page limit\)/)

    const completeSource = {
      source_complete: true, partial_capsules: 0, source_unknown_capsules: 0,
      source_concepts_omitted: 0, source_outcomes_omitted: 0,
    }
    const completeRunSource = {
      concept_evidence_nodes_total: 2, concept_evidence_nodes_incomplete: 0,
      concept_evidence_complete: true,
      concepts_total: 2, concepts_omitted: 0, concepts_complete: true,
      concept_outcomes_total: 2, concept_outcomes_omitted: 0, concept_outcomes_complete: true,
    }
    const prior = overrides => ({
      type: 'cross_run_prior', data: {
        v: 2, matched_concepts: ['loss/target'], prior_runs_total: 1,
        prior_runs_omitted: 0, prior_runs_complete: true, concept_source: completeSource,
        prior_runs: [{
          run_id: 'old', best_metric: 0.9, run_best_metric: 0.9,
          matched_concepts: ['loss/target'], source_receipt: completeRunSource,
          matched_concept_outcomes: [
            { concept: 'loss/target', outcome_retained: true, outcome: 0.1 },
          ],
        }],
        ...overrides,
      },
    })
    const exact = eventNarration(prior())
    assert.match(exact, /loss\/target.*1 retained run.*evidence completeness unknown/)
    assert.doesNotMatch(exact, /matched outcome|run best|PARTIAL/)

    const runBest = eventNarration(prior({ prior_runs: [{
      run_id: 'old', best_metric: 0.9, run_best_metric: 0.9,
      matched_concepts: ['loss/target'], source_receipt: completeRunSource,
      matched_concept_outcomes: [
        { concept: 'loss/target', outcome_retained: false, outcome: null },
      ],
    }] }))
    assert.match(runBest, /1 retained run.*evidence completeness unknown/)
    assert.doesNotMatch(runBest, /matched outcome|run best/)

    const partial = eventNarration(prior({
      concept_source: { ...completeSource, source_complete: false, partial_capsules: 1,
        source_unknown_capsules: 1 },
    }))
    assert.match(partial, /evidence completeness unknown/)

    const knownPartial = eventNarration(prior({
      concept_source: { ...completeSource, source_complete: false, partial_capsules: 1 },
      prior_runs: [{
        ...prior().data.prior_runs[0],
        source_receipt: {
          ...completeRunSource, concept_evidence_nodes_incomplete: 1,
          concept_evidence_complete: false, concepts_complete: false,
          concept_outcomes_complete: false,
        },
      }],
    }))
    // # CODEX AGENT: a bounded live event remains an audit preview even when its embedded receipt
    // claims a known partial source; only the governed Atlas endpoint may present source authority.
    assert.match(knownPartial, /evidence completeness unknown/)
    assert.doesNotMatch(knownPartial, /PARTIAL source|matched outcome|run best/)

    const preProducerReceipt = eventNarration(prior({
      prior_runs: [{
        ...prior().data.prior_runs[0],
        source_receipt: {
          concepts_total: 2, concepts_omitted: 0, concepts_complete: true,
          concept_outcomes_total: 2, concept_outcomes_omitted: 0,
          concept_outcomes_complete: true,
        },
      }],
    }))
    assert.match(preProducerReceipt, /evidence completeness unknown/)

    const quarantined = eventNarration(prior({
      concept_source: {
        ...completeSource, source_complete: false, source_store_complete: false,
        source_rows_total: 2, source_rows_quarantined: 1, source_malformed_rows: 1,
        source_invalid_capsule_rows: 0, source_duplicate_run_rows: 0,
      },
    }))
    assert.match(quarantined, /evidence completeness unknown/)
    assert.doesNotMatch(quarantined, /PARTIAL source/)

    for (const corrupt of [
      { matched_concepts: ['loss/target', ' loss/target'] },
      { concept_source: { ...completeSource, source_concepts_omitted: 1 } },
      { concept_source: { ...completeSource, source_complete: false,
        source_concepts_omitted: 1 } },
      { concept_source: { ...completeSource, source_store_complete: false } },
      { concept_source: {
        ...completeSource, source_complete: false, source_store_complete: false,
        source_rows_total: 1, source_rows_quarantined: 1, source_malformed_rows: 0,
        source_invalid_capsule_rows: 0, source_duplicate_run_rows: 0,
      } },
      { prior_runs_total: Number.MAX_SAFE_INTEGER + 1 },
      { prior_runs: [{
        run_id: 'old', run_best_metric: 0.9, matched_concepts: ['loss/target'],
        source_receipt: { ...completeRunSource, concepts_omitted: 1 },
        matched_concept_outcomes: [
          { concept: 'loss/target', outcome_retained: true, outcome: 0.1 },
        ],
      }] },
      { prior_runs: [{
        run_id: 'old', run_best_metric: 0.9, matched_concepts: ['loss/target'],
        source_receipt: { ...completeRunSource, concepts_total: 0 },
        matched_concept_outcomes: [
          { concept: 'loss/target', outcome_retained: true, outcome: 0.1 },
        ],
      }] },
      { prior_runs: [{
        run_id: 'old', run_best_metric: 0.9, matched_concepts: ['loss/target'],
        source_receipt: { ...completeRunSource, concept_outcomes_total: 0 },
        matched_concept_outcomes: [
          { concept: 'loss/target', outcome_retained: true, outcome: 0.1 },
        ],
      }] },
      { prior_runs: [{
        run_id: 'old', run_best_metric: 0.9, matched_concepts: ['loss/target'],
        source_receipt: completeRunSource, matched_concept_outcomes: [
          { concept: 'loss/target', outcome_retained: true, outcome: 0.1 },
          { concept: 'loss/target', outcome_retained: true, outcome: 0.2 },
        ],
      }] },
      { prior_runs: [{
        run_id: 'old', run_best_metric: 0.9,
        matched_concepts: ['loss/target', ' loss/target'],
        source_receipt: completeRunSource, matched_concept_outcomes: [
          { concept: 'loss/target', outcome_retained: true, outcome: 0.1 },
          { concept: ' loss/target', outcome_retained: false, outcome: null },
        ],
      }] },
    ]) {
      const narration = eventNarration(prior(corrupt))
      assert.match(narration, /evidence completeness unknown/)
      if (corrupt.prior_runs) assert.doesNotMatch(narration, /matched outcome/)
    }
    const corruptSibling = eventNarration(prior({ prior_runs_total: 2, prior_runs: [
      prior().data.prior_runs[0],
      { run_id: 'older', matched_concepts: ['loss/target'], source_receipt: null },
    ] }))
    assert.doesNotMatch(corruptSibling, /matched outcome|run best/)
    assert.match(corruptSibling, /evidence completeness unknown/)

    const bidi = eventNarration(prior({ matched_concepts: ['loss/\u202etarget'] }))
    assert.match(bidi, /evidence completeness unknown/)
    assert.doesNotMatch(bidi, /\u202e|matched outcome/)

    assert.equal(eventNarration({ type: 'cross_run_prior', data: {
      matched_concepts: 'loss/target', prior_runs: [],
    } }), 'cross_run_prior — details could not be summarized')
    assert.equal(eventNarration({ type: 'cross_run_prior', data: {
      matched_concepts: ['loss/target'], prior_runs: 'corrupt',
    } }), 'cross_run_prior — details could not be summarized')
    assert.equal(eventNarration({ type: 'cross_run_prior', data: {
      v: 2, matched_concepts: [], prior_runs: [], prior_runs_total: 0,
    } }), 'cross_run_prior — details could not be summarized')
    assert.match(eventNarration({ type: 'cross_run_prior', data: {
      matched_concepts: ['loss/target'], prior_runs: [{ best_metric: 0.9 }],
    } }), /1 retained run · evidence completeness unknown/)
  } finally {
    await vite.close()
  }
})
