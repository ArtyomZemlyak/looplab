import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import React, { act } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { JSDOM } from 'jsdom'
import { createServer } from 'vite'
import {
  LEDGER_RENDER_LIMITS,
  boundedLedgerText,
  buildClaimsCurationView,
  isValidLedgerSourceEnvelope,
  mergeCurationLogs,
  mergeLedgerPayload,
  projectLedgerSource,
  reconcileLedgerSourceStatuses,
  ledgerPayloadPortfolioId,
} from '../src/claimsCurationModel.js'
import {
  getCrossRunAtlas, getCrossRunClaims, getCrossRunCurationLog, getCrossRunClaimCurationLog,
} from '../src/api.js'

const source = name => readFile(new URL(`../src/${name}`, import.meta.url), 'utf8')
const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const PORTFOLIO_ID = `portfolio-sha256:${'a'.repeat(64)}`
const REPLACEMENT_PORTFOLIO_ID = `portfolio-sha256:${'b'.repeat(64)}`
const completeReadSegment = Object.freeze({
  read_complete: true,
  rows_total: 1,
  rows_retained: 1,
  rows_quarantined: 0,
  malformed_rows: 0,
  invalid_rows: 0,
})
const legacyCompleteResearchSource = Object.freeze({
  source_complete: true,
  producer_receipt_known: true,
  producer_complete: true,
  producer_runs: 1,
  producer_partial_runs: 0,
  producer_unknown_runs: 0,
  producer_claims_total: 1,
  producer_claims_retained: 1,
  producer_claims_omitted: 0,
})
const completeResearchSource = Object.freeze({
  ...legacyCompleteResearchSource,
  read_health_v: 1,
  ...completeReadSegment,
  snapshot_digest: 'a'.repeat(64),
})
const completeClaimSource = Object.freeze({
  v: 1,
  receipt_known: true,
  source_complete: true,
  read_complete: true,
  research_source_complete: true,
  lessons: completeReadSegment,
  research: completeReadSegment,
  snapshot_digest: 'c'.repeat(64),
})
const partialClaimSource = Object.freeze({
  ...completeClaimSource,
  source_complete: false,
  research_source_complete: false,
  snapshot_digest: 'd'.repeat(64),
})
const curationEnvelope = (entries = [], overrides = {}) => ({
  portfolio_id: PORTFOLIO_ID,
  v: 1, status: 'complete', complete: true, entries, n: entries.length, limit: 20,
  ...overrides,
})

const claim = (index = 0) => ({
  claim_uid: `claim-${index}`,
  statement: `claim ${index}`,
  epistemic: index % 2 ? 'mixed' : 'supported',
  maturity: 'machine-proposed',
  n_support: 0,
  support: [`run-${index}:node-1`],
  oppose: [],
  unverified: [`run-${index}:node-2`],
  scopes: ['task-a'],
  runs: [`run-${index}`],
  research_source: completeResearchSource,
  claim_source: completeClaimSource,
})

test('the ledger projection reconciles concurrent totals without trusting malformed text or counts', () => {
  const view = buildClaimsCurationView({
    n_runs: 0,
    n_contested: 0,
    contradictions: [{ ...claim(1), n_oppose: -10, oppose: ['run-1:node-3'] }],
  }, { n: 0, claims: [claim(0)] }, {
    n: 0,
    entries: [{ run_id: 'run-0', proposals: { merges: [{}], splits: 2 },
      receipt: { applied: [{}], skipped: [] } }],
  })

  // `runs` is now inferred from the CLAIM rows alone — run-0 from the claims page and run-1 from the
  // mixed-evidence record. Until F7 the concept rows contributed their own run references here.
  assert.deepEqual(view.totals, { runs: 2, claims: 1, contested: 1, curation: 1 })
  assert.equal(view.empty, false)
  assert.equal(Object.hasOwn(view, 'concepts'), false,
    'the concepts section moved to the run list Concepts view; nothing may re-derive it here')
  assert.equal(view.claims[0].nSupport, 1)
  assert.equal(view.claims[0].nUnverified, 1)
  assert.deepEqual(view.claims[0].unverified, ['run-0:node-2'])
  assert.equal(view.curation[0].proposals, 3)
  assert.equal(view.curation[0].applied, 1)
  assert.equal(boundedLedgerText({ unsafe: 'shape' }), '')
  assert.equal(boundedLedgerText('task\u202e-name\u2066 scope\u2069'), 'task -name scope',
    'bidi formatting controls cannot visually reorder model-authored comparison context')
})

test('the ledger reports what a bounded mixed-evidence projection did not show', () => {
  const view = buildClaimsCurationView({
    n_contested: 5,
    contradictions: [claim(1)],
    contradictions_total: 5,
    contradictions_omitted: 4,
  }, {}, {})

  assert.equal(view.totals.contested, 5)
  assert.equal(view.hiddenContradictions, 4)

  // A server total that LAGS the rows in this very response must never hide one of them.
  const lagging = buildClaimsCurationView({ n_contested: 0, contradictions: [claim(1)] }, {}, {})
  assert.equal(lagging.totals.contested, 1)
  assert.equal(lagging.hiddenContradictions, 0)
})

test('the ledger derives evidence balance from sanitized totals instead of payload labels', () => {
  const contradictory = [
    { ...claim(0), epistemic: 'refuted', n_support: 2, support: [], n_oppose: 0, oppose: [] },
    { ...claim(1), epistemic: 'supported', n_support: 0, support: [], n_oppose: 3, oppose: [] },
    { ...claim(2), epistemic: 'inconclusive', n_support: 1, support: [], n_oppose: 1, oppose: [] },
    { ...claim(3), epistemic: 'mixed', n_support: -4, support: [], n_oppose: 'bad', oppose: [] },
  ]
  const view = buildClaimsCurationView({}, {
    research_source: completeResearchSource,
    claim_source: completeClaimSource,
    claims: contradictory,
  }, {})

  assert.deepEqual(view.claims.map(row => [row.nSupport, row.nOppose, row.epistemic]), [
    [2, 0, 'supported'],
    [0, 3, 'refuted'],
    [1, 1, 'mixed'],
    [0, 0, 'inconclusive'],
  ])
})

test('the ledger never coerces booleans or numeric strings into evidence and ledger totals', () => {
  const view = buildClaimsCurationView({
    n_runs: true,
    n_contested: '3',
  }, {
    n: '9',
    claims: [{
      ...claim(20), support: [], oppose: [], unverified: [], contradicts: [],
      n_support: true, n_oppose: '2', n_unverified: false, n_contradicts: '4',
    }],
  }, { n: true, entries: [] })

  assert.deepEqual(view.totals, {
    runs: 1, claims: 1, contested: 0, curation: 0,
  })
  assert.deepEqual(
    [view.claims[0].nSupport, view.claims[0].nOppose,
      view.claims[0].nUnverified, view.claims[0].nContradicts],
    [0, 0, 0, 0],
  )
  assert.equal(view.claims[0].epistemic, 'inconclusive')
})

test('the ledger never reconstructs a positive from a partial D8 prefix and renders its warning', async () => {
  const partial = {
    source_complete: false,
    producer_receipt_known: true,
    producer_complete: false,
    producer_runs: 1,
    producer_partial_runs: 1,
    producer_unknown_runs: 0,
    producer_claims_total: 257,
    producer_claims_retained: 256,
    producer_claims_omitted: 1,
  }
  const view = buildClaimsCurationView({}, {
    research_source: partial, claim_source: partialClaimSource, claims: [{
    ...claim(30), epistemic: 'inconclusive', n_support: 1, research_source: partial,
    claim_source: partialClaimSource,
  }, {
    ...claim(31), epistemic: 'inconclusive', n_support: 0, support: [],
    n_oppose: 1, oppose: ['run-31:node-3'], research_source: partial,
    claim_source: partialClaimSource,
  }] }, {})

  assert.equal(view.claims[0].epistemic, 'inconclusive')
  assert.equal(view.claims[1].epistemic, 'inconclusive')
  assert.equal(view.claimSource.status, 'partial')

  const contradictory = buildClaimsCurationView({}, { research_source: {
    ...completeResearchSource, producer_runs: 1, producer_unknown_runs: 1,
  }, claims: [] }, {})
  assert.equal(contradictory.claimSource.status, 'unknown')

  const forgedRow = buildClaimsCurationView({}, {
    research_source: partial, claim_source: partialClaimSource, claims: [{
    ...claim(32), epistemic: 'supported', n_support: 1,
    research_source: completeResearchSource,
  }] }, {})
  assert.equal(forgedRow.claims[0].epistemic, 'inconclusive')
  assert.equal(forgedRow.claimSource.status, 'unknown')

  const splitSnapshot = buildClaimsCurationView({
    research_source: completeResearchSource,
    claim_source: completeClaimSource,
    contradictions: [{ ...claim(33), epistemic: 'mixed', n_support: 1,
      contradicts: ['opposite'], research_source: completeResearchSource }],
  }, { research_source: partial, claim_source: partialClaimSource, claims: [{
    ...claim(34), epistemic: 'inconclusive', n_support: 1, research_source: partial,
    claim_source: partialClaimSource,
  }] }, {})
  assert.equal(splitSnapshot.claims[0].epistemic, 'inconclusive')
  assert.equal(splitSnapshot.contradictions[0].epistemic, 'mixed')
  assert.equal(splitSnapshot.claimSource.status, 'unknown')

  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const { EvidenceSourceNotice } = await vite.ssrLoadModule('/src/ClaimsCuration.jsx')
    const markup = renderToStaticMarkup(React.createElement(EvidenceSourceNotice, {
      claims: view.claimSource,
    }))
    assert.match(markup, /Evidence source incomplete/)
    assert.match(markup, /Claims partial/)
    assert.match(markup, /Absence and one-sided claim state withheld/)
  } finally {
    await vite.close()
  }
})

test('the ledger validates the additive D8 read-health extension atomically', () => {
  const quarantined = {
    ...completeResearchSource,
    source_complete: false,
    read_complete: false,
    rows_total: 3,
    rows_retained: 1,
    rows_quarantined: 2,
    malformed_rows: 1,
    invalid_rows: 1,
    snapshot_digest: 'b'.repeat(64),
  }
  const partial = buildClaimsCurationView({}, { research_source: quarantined }, {})
  assert.equal(partial.claimSource.status, 'unknown')
  assert.equal(Object.hasOwn(partial, 'researchSource'), false)

  // CODEX AGENT: producer-only diagnostics are never promoted into combined claim authority.
  assert.equal(buildClaimsCurationView({}, {
    research_source: legacyCompleteResearchSource,
  }, {}).claimSource.status, 'unknown')
  const { invalid_rows: _omitted, ...torn } = quarantined
  for (const receipt of [
    torn,
    { ...quarantined, snapshot_digest: 'B'.repeat(64) },
    { ...quarantined, rows_total: 4 },
    { ...quarantined, read_complete: true },
    { ...quarantined, source_complete: true },
  ]) {
    assert.equal(buildClaimsCurationView({}, { research_source: receipt }, {}).claimSource.status,
      'unknown')
  }
})

test('the ledger uses combined claim-source authority when lesson rows are quarantined', async () => {
  const quarantinedLessons = {
    ...completeClaimSource,
    source_complete: false,
    read_complete: false,
    lessons: {
      read_complete: false, rows_total: 2, rows_retained: 1,
      rows_quarantined: 1, malformed_rows: 1, invalid_rows: 0,
    },
    snapshot_digest: 'e'.repeat(64),
  }
  const row = {
    ...claim(35), n_support: 1, epistemic: 'supported',
    claim_source: quarantinedLessons,
  }
  const view = buildClaimsCurationView({}, {
    research_source: completeResearchSource,
    claim_source: quarantinedLessons,
    claims: [row],
  }, {})

  assert.equal(view.claimSource.status, 'partial', 'the lessons store is not healthy')
  assert.equal(view.claims[0].epistemic, 'inconclusive',
    'a quarantined lesson could contain the missing opposite side')
  assert.deepEqual(view.claims[0].support, ['run-35:node-1'],
    'retained references remain visible as a lower bound')

  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const { EvidenceSourceNotice } = await vite.ssrLoadModule('/src/ClaimsCuration.jsx')
    const markup = renderToStaticMarkup(React.createElement(EvidenceSourceNotice, {
      claims: view.claimSource,
    }))
    assert.match(markup, /Claims partial/)
    assert.match(markup, /Absence and one-sided claim state withheld/)
  } finally {
    await vite.close()
  }
})

test('the ledger requires one combined claim snapshot across envelopes and every visible row', () => {
  const laterClaimSource = { ...completeClaimSource, snapshot_digest: 'f'.repeat(64) }
  const mixed = {
    ...claim(42), epistemic: 'mixed', n_support: 1,
    n_oppose: 1, oppose: ['run-42:node-3'],
  }
  const previous = {
    atlas: {
      research_source: completeResearchSource,
      claim_source: completeClaimSource,
      contradictions: [claim(40), mixed],
    },
    claims: {
      research_source: completeResearchSource,
      claim_source: completeClaimSource,
      claims: [claim(46)],
    },
  }
  const refreshed = mergeLedgerPayload(previous, {
    claims: {
      research_source: completeResearchSource,
      claim_source: laterClaimSource,
      claims: [{
        ...claim(43), n_support: 0, support: [], n_oppose: 1,
        oppose: ['run-43:node-4'], claim_source: laterClaimSource,
      }],
    },
  })
  const split = buildClaimsCurationView(refreshed.atlas, refreshed.claims, {})
  assert.equal(split.claimSource.status, 'unknown')
  assert.equal(split.contradictions[0].epistemic, 'inconclusive')
  assert.equal(split.contradictions[1].epistemic, 'mixed',
    'retained two-sided evidence remains mixed even across a split read epoch')
  assert.equal(split.claims[0].epistemic, 'inconclusive')

  const coherent = buildClaimsCurationView(previous.atlas, previous.claims, {})
  assert.equal(coherent.claimSource.status, 'complete')
  assert.equal(coherent.claims[0].epistemic, 'supported')

  const forgedRow = buildClaimsCurationView({}, {
    research_source: completeResearchSource,
    claim_source: completeClaimSource,
    claims: [{ ...claim(44), claim_source: laterClaimSource }],
  }, {})
  assert.equal(forgedRow.claimSource.status, 'unknown')
  assert.equal(forgedRow.claims[0].epistemic, 'inconclusive')

  const producerPartial = {
    ...completeResearchSource,
    source_complete: false,
    producer_complete: false,
    producer_partial_runs: 1,
    producer_claims_total: 2,
    producer_claims_omitted: 1,
  }
  const contradictoryDiagnostics = buildClaimsCurationView({}, {
    research_source: producerPartial,
    claim_source: completeClaimSource,
    claims: [{ ...claim(50), research_source: producerPartial }],
  }, {})
  assert.equal(contradictoryDiagnostics.claimSource.status, 'complete',
    'the server-validated combined receipt is the sole claim authority')
  assert.equal(contradictoryDiagnostics.claims[0].epistemic, 'supported')
})

test('the ledger never accepts a legacy research receipt as combined claim authority', () => {
  const legacyRow = { ...claim(48), research_source: legacyCompleteResearchSource }
  delete legacyRow.claim_source
  const view = buildClaimsCurationView({}, {
    research_source: legacyCompleteResearchSource,
    claims: [legacyRow],
  }, {})

  assert.equal(view.claimSource.status, 'unknown')
  assert.equal(view.claims[0].epistemic, 'inconclusive')

  for (const receipt of [
    { ...completeClaimSource, snapshot_digest: 'C'.repeat(64) },
    { ...completeClaimSource, read_complete: false },
    { ...completeClaimSource, lessons: { ...completeReadSegment, invalid_rows: 1 } },
    { ...completeClaimSource, receipt_known: false },
  ]) {
    assert.equal(buildClaimsCurationView({}, { claim_source: receipt }, {}).claimSource.status,
      'unknown')
  }
})

test('the ledger preserves bounded structured contradictions and cannot render them support-only', () => {
  const contradicts = Array.from(
    { length: LEDGER_RENDER_LIMITS.evidence + 3 },
    (_, index) => `opposite claim ${index}\n${'x'.repeat(500)}`,
  )
  const view = buildClaimsCurationView({}, { claims: [
    {
      ...claim(8), epistemic: 'mixed', n_support: 1, support: ['run-8:node-1'],
      n_oppose: 0, oppose: [], contradicts,
    },
    {
      ...claim(9), epistemic: 'mixed', n_support: 1, support: ['run-9:node-1'],
      n_oppose: 0, oppose: [], contradicts: [],
    },
  ] }, {})

  assert.equal(view.claims[0].epistemic, 'mixed')
  assert.equal(view.claims[0].nContradicts, contradicts.length)
  assert.equal(view.claims[0].contradicts.length, LEDGER_RENDER_LIMITS.evidence)
  assert.ok(view.claims[0].contradicts.every(value => value.length <= 300 && !value.includes('\n')))
  assert.equal(view.claims[1].epistemic, 'mixed', 'explicit structured mixed state must survive')
})

test('the ledger requires support before structured contradictions become mixed evidence', () => {
  const view = buildClaimsCurationView({}, { claims: [{
    ...claim(10), epistemic: 'mixed', n_support: 0, support: [],
    n_oppose: 0, oppose: [], contradicts: ['opposite assertion'],
  }] }, {})

  assert.equal(view.claims[0].nSupport, 0)
  assert.equal(view.claims[0].nContradicts, 1)
  assert.equal(view.claims[0].epistemic, 'inconclusive')
})

test('the ledger preserves decision freshness and warns only on stale or unknown governed decisions', async () => {
  const rows = [
    { ...claim(20), maturity: 'operator-ratified', decision_fresh: false },
    { ...claim(21), maturity: 'operator-pinned' },
    { ...claim(22), maturity: 'operator-rejected', decision_fresh: true },
    { ...claim(23), maturity: 'machine-proposed' },
  ]
  const view = buildClaimsCurationView({}, { claims: rows }, {})
  assert.deepEqual(view.claims.map(row => row.decisionFresh), [false, null, true, null])

  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const { ClaimCard } = await vite.ssrLoadModule('/src/ClaimsCuration.jsx')
    const markup = view.claims.map(row => renderToStaticMarkup(
      React.createElement(ClaimCard, { claim: row }),
    ))
    assert.match(markup[0], /operator ratified · ⚠ stale/)
    assert.match(markup[1], /operator pinned · ⚠ freshness unknown/)
    assert.doesNotMatch(markup[2], /⚠/)
    assert.doesNotMatch(markup[3], /⚠/)
  } finally {
    await vite.close()
  }
})

test('the ledger projection applies hard caps before React receives portfolio collections', () => {
  const claims = Array.from({ length: LEDGER_RENDER_LIMITS.claims + 4 }, (_, index) => ({
    ...claim(index),
    support: Array.from({ length: LEDGER_RENDER_LIMITS.evidence + 5 }, (__, ref) => `run-${index}:node-${ref}`),
  }))
  const contradictions = Array.from(
    { length: LEDGER_RENDER_LIMITS.contradictions + 2 }, (_, index) => claim(index))
  const entries = Array.from({ length: LEDGER_RENDER_LIMITS.curation + 7 }, (_, index) => ({
    run_id: `run-${index}`, outcome: 'empty', proposals: {}, receipt: {},
  }))
  const view = buildClaimsCurationView({ contradictions }, { claims }, { entries })

  assert.equal(view.claims.length, LEDGER_RENDER_LIMITS.claims)
  assert.equal(view.claims[0].support.length, LEDGER_RENDER_LIMITS.evidence)
  assert.equal(view.contradictions.length, LEDGER_RENDER_LIMITS.contradictions)
  assert.equal(view.curation.length, LEDGER_RENDER_LIMITS.curation)
  assert.equal(view.hiddenContradictions, 2)
  assert.equal(view.hiddenClaims, 4)
  assert.equal(view.hiddenCuration, 7)
})

test('a source revision touches only the bounded visible slice of a huge raw ledger', () => {
  let indexedReads = 0
  const entries = new Proxy(new Array(200000), {
    get(target, property, receiver) {
      if (/^\d+$/u.test(String(property))) {
        indexedReads += 1
        return { revision: 200000 - Number(property) }
      }
      return Reflect.get(target, property, receiver)
    },
  })
  const huge = { conceptCuration: { entries } }
  const statuses = reconcileLedgerSourceStatuses({}, huge, 'now')
  assert.equal(statuses.conceptCuration.state, 'current')
  assert.equal(statuses.conceptCuration.revision, '200000')
  assert.ok(indexedReads <= LEDGER_RENDER_LIMITS.curation)
})

test('every source is projected to a bounded allowlist before React state', () => {
  let evidenceReads = 0
  const support = new Proxy(new Array(200000), {
    has(target, property) {
      if (/^\d+$/u.test(String(property))) return true
      return Reflect.has(target, property)
    },
    get(target, property, receiver) {
      if (/^\d+$/u.test(String(property))) {
        evidenceReads += 1
        return `run-1:node-${property}\n${'x'.repeat(1000)}`
      }
      return Reflect.get(target, property, receiver)
    },
  })
  const raw = {
    portfolio_id: PORTFOLIO_ID,
    claims: [{ ...claim(1), statement: 's'.repeat(5000), support,
      debug_blob: { payload: 'x'.repeat(2_000_000) } }],
    n: 1, revision: 4,
    debug_blob: { payload: 'x'.repeat(2_000_000) },
  }
  assert.equal(isValidLedgerSourceEnvelope('claims', raw), true)
  const projected = projectLedgerSource('claims', raw)

  assert.equal(projected.claims.length, 1)
  assert.equal(projected.claims[0].support.length, LEDGER_RENDER_LIMITS.evidence)
  assert.ok(projected.claims[0].support.every(value => value.length <= 500 && !value.includes('\n')))
  assert.equal(projected.claims[0].statement.length, 500)
  assert.equal(Object.hasOwn(projected, 'debug_blob'), false)
  assert.equal(Object.hasOwn(projected.claims[0], 'debug_blob'), false)
  assert.ok(evidenceReads <= LEDGER_RENDER_LIMITS.evidence)

  assert.equal(isValidLedgerSourceEnvelope('claims', {
    portfolio_id: PORTFOLIO_ID,
    claims: new Array(LEDGER_RENDER_LIMITS.claims + 1).fill({}),
  }), false, 'an oversized top-level page is rejected before projection/state')
})

test('concept and claim steward preview keeps each ledger newest-first without inventing cross-ledger order', () => {
  const merged = mergeCurationLogs({ n: 3, entries: [
    { run_id: 'concept-new', at: '2026-07-16T01:00:00Z', revision: 1, outcome: 'proposed',
      proposals: { merges: [{}] } },
    { run_id: 'concept-rev', revision: 4, outcome: 'empty', proposals: {} },
  ] }, { n: 2, entries: [
    { run_id: 'claim-newest', at: '2026-07-16T02:00:00Z', revision: 1, outcome: 'proposed',
      proposals: { decisions: [{}] } },
    { run_id: 'claim-rev', revision: 4, outcome: 'unavailable', auto_requested: true, proposals: {} },
  ] })

  assert.equal(merged.n, 5)
  assert.deepEqual(merged.entries.map(entry => [entry.kind, entry.run_id]), [
    ['concept', 'concept-new'],
    ['claim', 'claim-newest'],
  ])
  const view = buildClaimsCurationView({}, {}, merged)
  assert.deepEqual(view.curation.map(entry => entry.kind), ['concept', 'claim'])
  assert.deepEqual(view.curation.map(entry => entry.outcome), ['proposed', 'proposed'])
})

test('a partial refresh replaces successful slices and preserves last-good failed slices', () => {
  const previous = {
    atlas: { n_runs: 2, explored: [{ concept: 'kept coverage' }] },
    claims: { claims: [claim(1)] },
    conceptCuration: { entries: [{ run_id: 'kept-concept-log' }] },
    claimCuration: { entries: [{ run_id: 'kept-claim-log' }] },
  }
  const freshClaims = { claims: [claim(2)] }
  const merged = mergeLedgerPayload(previous, { claims: freshClaims })
  assert.equal(merged.atlas, previous.atlas)
  assert.equal(merged.claims, freshClaims)
  assert.equal(merged.conceptCuration, previous.conceptCuration)
  assert.equal(merged.claimCuration, previous.claimCuration)
})

test('malformed fulfilled envelopes are rejected and retain last-good source data', () => {
  assert.equal(isValidLedgerSourceEnvelope('claims', {}), false)
  assert.equal(isValidLedgerSourceEnvelope('claims', { claims: [] }), false)
  assert.equal(isValidLedgerSourceEnvelope('claims', {
    portfolio_id: PORTFOLIO_ID, claims: [],
  }), true)
  assert.equal(isValidLedgerSourceEnvelope('atlas', {
    portfolio_id: PORTFOLIO_ID,
    explored: [], thin_coverage: [], contradictions: [],
  }), true, 'the server still SENDS the concept sections; carrying them is not a defect')
  assert.equal(isValidLedgerSourceEnvelope('atlas', {
    portfolio_id: PORTFOLIO_ID, contradictions: [],
  }), true, 'and it is fenced only on the section it renders')
  assert.equal(isValidLedgerSourceEnvelope('atlas', {
    portfolio_id: PORTFOLIO_ID, explored: [], thin_coverage: [],
  }), false, 'an envelope without the mixed-evidence rows is not usable here')
  assert.equal(isValidLedgerSourceEnvelope('atlas', {
    portfolio_id: PORTFOLIO_ID, contradictions: new Array(25).fill({}),
  }), false, 'more rows than this client asked for is a torn or hostile response')
  assert.equal(isValidLedgerSourceEnvelope('conceptCuration', { entries: [] }), false)

  const previousPayload = { claims: { revision: 7, claims: [claim(7)] } }
  const previousStates = reconcileLedgerSourceStatuses({}, previousPayload, 'before')
  const successful = isValidLedgerSourceEnvelope('claims', {}) ? { claims: {} } : {}
  const merged = mergeLedgerPayload(previousPayload, successful)
  const states = reconcileLedgerSourceStatuses(previousStates, successful, 'after')
  assert.equal(merged.claims, previousPayload.claims)
  assert.equal(states.claims.state, 'retained-stale')
  assert.equal(states.claims.loadedAt, 'before')
})

test('curation source health is atomic, bounded, and rejects legacy envelopes', () => {
  const entry = { run_id: 'run-1', outcome: 'proposed' }
  const healthy = {
    portfolio_id: PORTFOLIO_ID,
    v: 1, status: 'complete', complete: true, entries: [entry], n: 1, limit: 20,
  }
  assert.equal(isValidLedgerSourceEnvelope('conceptCuration', healthy), true)
  assert.equal(isValidLedgerSourceEnvelope('claimCuration', { entries: [entry] }), false)
  assert.equal(isValidLedgerSourceEnvelope('claimCuration', {
    entries: [entry], n: 5, limit: 20, legacy_extra: true,
  }), false)

  for (const envelope of [
    { ...healthy, entries: [null] },
    { ...healthy, entries: [[]] },
    { ...healthy, status: 'unavailable', complete: false },
    { ...healthy, v: '1' },
    { ...healthy, n: '1' },
    { ...healthy, n: 0 },
    { ...healthy, limit: 0 },
    { ...healthy, limit: 201 },
    { ...healthy, entries: [entry, entry], n: 2, limit: 1 },
    { entries: [], v: 1 },
    { entries: [], status: 'complete' },
    { entries: [], complete: true },
    { entries: [entry], n: 0 },
    { entries: [entry], limit: '20' },
  ]) {
    assert.equal(isValidLedgerSourceEnvelope('conceptCuration', envelope), false)
  }

  const previous = { conceptCuration: healthy }
  const malformed = { ...healthy, complete: false }
  const successful = isValidLedgerSourceEnvelope('conceptCuration', malformed)
    ? { conceptCuration: malformed } : {}
  const merged = mergeLedgerPayload(previous, successful)
  const before = reconcileLedgerSourceStatuses({}, previous, 'before')
  const after = reconcileLedgerSourceStatuses(before, successful, 'after', ['conceptCuration'])
  assert.equal(merged.conceptCuration, healthy)
  assert.equal(after.conceptCuration.state, 'retained-stale')
  assert.equal(after.conceptCuration.loadedAt, 'before')
})

test('portfolio identity prevents partial mixing and permits an atomic full-refresh switch', () => {
  const previous = {
    atlas: { portfolio_id: PORTFOLIO_ID, explored: [] },
    claims: { portfolio_id: PORTFOLIO_ID, claims: [claim(1)] },
  }
  const replacementClaims = {
    portfolio_id: REPLACEMENT_PORTFOLIO_ID, claims: [claim(2)],
  }

  const partial = mergeLedgerPayload(previous, { claims: replacementClaims })
  assert.equal(partial.claims, previous.claims)
  assert.equal(partial.atlas, previous.atlas)
  assert.equal(ledgerPayloadPortfolioId(partial), PORTFOLIO_ID)

  const switched = mergeLedgerPayload(
    previous, { claims: replacementClaims }, { allowPortfolioSwitch: true })
  assert.deepEqual(switched.atlas, {})
  assert.equal(switched.claims, replacementClaims)
  assert.equal(ledgerPayloadPortfolioId(switched), REPLACEMENT_PORTFOLIO_ID)
})

test('source provenance distinguishes current, retained-stale, and failed slices', () => {
  const first = reconcileLedgerSourceStatuses({}, {
    claims: { revision: 7, claims: [claim(1)] },
  }, '2026-07-16T03:00:00Z')
  assert.deepEqual(Object.fromEntries(Object.entries(first).map(([key, value]) => [key, value.state])), {
    atlas: 'failed', claims: 'current', conceptCuration: 'failed', claimCuration: 'failed',
  })
  assert.equal(first.claims.loadedAt, '2026-07-16T03:00:00Z')
  assert.equal(first.claims.revision, '7')

  const second = reconcileLedgerSourceStatuses(first, {
    atlas: { revisions: { concept_governance: 4, claims: 9 } },
  }, '2026-07-16T04:00:00Z')
  assert.deepEqual(Object.fromEntries(Object.entries(second).map(([key, value]) => [key, value.state])), {
    atlas: 'current', claims: 'retained-stale', conceptCuration: 'failed', claimCuration: 'failed',
  })
  assert.equal(second.claims.loadedAt, first.claims.loadedAt)
  assert.equal(second.claims.revision, first.claims.revision)
  assert.equal(second.atlas.revision, 'concept 4 · claims 9')

  const failedRefresh = reconcileLedgerSourceStatuses(second, {}, '2026-07-16T05:00:00Z')
  assert.deepEqual(Object.fromEntries(Object.entries(failedRefresh).map(([key, value]) => [key, value.state])), {
    atlas: 'retained-stale', claims: 'retained-stale', conceptCuration: 'failed', claimCuration: 'failed',
  })

  const allCurrent = reconcileLedgerSourceStatuses(failedRefresh, {
    atlas: { revisions: { concept_governance: 5, claims: 10 } },
    claims: { revision: 8 },
    conceptCuration: { entries: [{ revision: 11 }, { revision: 12 }] },
    claimCuration: { entries: [{ revision: 13 }] },
  }, '2026-07-16T06:00:00Z')
  assert.deepEqual(Object.fromEntries(Object.entries(allCurrent).map(([key, value]) => [key, value.state])), {
    atlas: 'current', claims: 'current', conceptCuration: 'current', claimCuration: 'current',
  })
  assert.equal(allCurrent.conceptCuration.revision, '12')
  assert.equal(allCurrent.claimCuration.revision, '13')

  const claimsOnly = reconcileLedgerSourceStatuses(allCurrent, {
    claims: { revision: 9 },
  }, '2026-07-16T07:00:00Z')
  assert.equal(claimsOnly.claims.state, 'current')
  assert.equal(claimsOnly.claims.loadedAt, '2026-07-16T07:00:00Z')
  for (const key of ['atlas', 'conceptCuration', 'claimCuration']) {
    assert.equal(claimsOnly[key].state, 'retained-stale')
    assert.equal(claimsOnly[key].loadedAt, '2026-07-16T06:00:00Z')
  }
})

test('a source-local retry changes only the attempted slice', () => {
  const before = reconcileLedgerSourceStatuses({}, {
    atlas: { explored: [], thin_coverage: [], contradictions: [] },
    claims: { claims: [] },
    conceptCuration: { entries: [] },
    claimCuration: { entries: [{ revision: 2 }] },
  }, 'before')
  const failed = reconcileLedgerSourceStatuses(before, {}, 'failed', ['claimCuration'])

  assert.equal(failed.claimCuration.state, 'retained-stale')
  assert.equal(failed.claimCuration.loadedAt, 'before')
  for (const key of ['atlas', 'claims', 'conceptCuration']) {
    assert.deepEqual(failed[key], before[key], `${key} was not attempted and must remain current`)
  }

  const recovered = reconcileLedgerSourceStatuses(failed, {
    claimCuration: { entries: [{ revision: 3 }] },
  }, 'after', ['claimCuration'])
  assert.equal(recovered.claimCuration.state, 'current')
  assert.equal(recovered.claimCuration.loadedAt, 'after')
  assert.equal(recovered.claimCuration.revision, '3')
})

test('a partial UI never presents an unavailable source as an empty current fact', async () => {
  const [atlas, deadline] = await Promise.all([
    source('ClaimsCuration.jsx'), source('requestDeadline.js'),
  ])

  assert.match(atlas, /const id = \+\+requestId\.current[\s\S]*requestedSources\.forEach[\s\S]*settle\(key/)
  assert.match(atlas, /!active \|\| id !== requestId\.current/,
    'late results must be fenced to the mounted request')
  assert.match(atlas, /deadlineRequest\(read, SOURCE_TIMEOUT_MS\)/)
  assert.match(deadline, /setTimeout\([\s\S]*controller\.abort/,
    'each source needs a bounded liveness escape')
  assert.match(atlas, /loaded \? value : 'not loaded'/,
    'summary values from a never-loaded slice need an explicit unavailable state')
  assert.match(atlas, /view\.empty && <LedgerEmptyState/,
    'empty and partial-empty projections need compact source-level readiness')
  assert.match(atlas, /!view\.empty && <div className="ledger-grid">/,
    'empty projections must not render oversized empty panels')
  assert.match(atlas, /mixedLoaded && \(view\.contradictions\.length > 0[\s\S]*None returned\./)
  assert.match(atlas, /claimsLoaded && \(view\.claims\.length > 0[\s\S]*No claims returned\./)
  assert.match(atlas, /curationCurrent[\s\S]*incomplete merge/)
  assert.doesNotMatch(atlas, /shown · incomplete merge/,
    'an incomplete two-ledger merge must not expose a false combined count')
  assert.doesNotMatch(atlas, /errorText|result\.reason\?\.message/,
    'transport failures must use client-owned copy instead of reflecting internal error text')
  assert.match(atlas, /errors\.every\(error => error\.status === 400\)/)
  // Every non-current watermark keeps its own retry, and a retry in flight locks the OTHER
  // sources out. The active button is deliberately not `disabled` — a disabled button loses focus
  // mid-interaction — so it reports busy through aria instead; that distinction is the property.
  assert.match(atlas, /retryable && state !== 'current'[\s\S]*retry\(sourceKey, 'watermark'/,
    'failed and retained-stale watermarks both need their own retry action')
  assert.match(atlas, /disabled=\{busy && !activeRetry\}[\s\S]*aria-busy=\{activeRetry \|\| undefined\}/,
    'a retry in flight must lock out the other sources without disabling itself')
  for (const key of ['atlas', 'claims']) {
    assert.match(atlas, new RegExp(`sourceKey="${key}"[\\s\\S]*?retry=\\{retry\\}`))
  }
  assert.match(atlas, /\[\['conceptCuration', 'Concept'\], \['claimCuration', 'Claim'\]\][\s\S]*sourceKey=\{sourceKey\}[\s\S]*retry=\{retry\}/)
})

test('the ledger empty state distinguishes evidence runs and each independent source', async () => {
  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  const current = { state: 'current', loadedAt: '2026-07-16T10:00:00Z', revision: '1' }
  const failed = { state: 'failed', loadedAt: '', revision: '' }
  const stale = { state: 'retained-stale', loadedAt: '2026-07-16T09:00:00Z', revision: '1' }
  try {
    const { LedgerEmptyState, EvidenceSourceNotice } = await vite.ssrLoadModule('/src/ClaimsCuration.jsx')
    const allCurrent = renderToStaticMarkup(React.createElement(LedgerEmptyState, {
      sourceStates: {
        atlas: current, claims: current, conceptCuration: current, claimCuration: current,
      }, claimSource: { status: 'complete' },
      pending: [], retry() {}, busy: false,
    }))
    const currentDom = new JSDOM(allCurrent)
    assert.match(currentDom.window.document.querySelector('h2').textContent, /No cross-run evidence/)
    assert.match(currentDom.window.document.body.textContent,
      /runs may still exist/)
    assert.equal(currentDom.window.document.querySelectorAll('.ledger-empty-source').length, 4)
    assert.deepEqual([...currentDom.window.document.querySelectorAll('.ledger-readiness-state')]
      .map(node => node.textContent), ['complete', 'complete', 'complete', 'complete'])
    assert.equal(currentDom.window.document.querySelectorAll('.ledger-empty-source .btn').length, 0)
    // A genuinely empty portfolio offers no action: every source loaded, so neither a settings trip
    // nor a retry would change anything, and offering one would read as "something is broken".
    assert.equal(currentDom.window.document.querySelector('a[href="#/settings"]'), null)
    assert.equal(currentDom.window.document.querySelector('.ledger-empty-actions'), null)

    // ...but when the evidence sources refused because memory is not configured, the one action
    // that CAN fix it is offered, and only then.
    const unconfigured = renderToStaticMarkup(React.createElement(LedgerEmptyState, {
      sourceStates: {
        atlas: current, claims: current, conceptCuration: current, claimCuration: current,
      }, claimSource: { status: 'complete' },
      pending: [], retry() {}, busy: false, memorySettingsNeeded: true,
    }))
    const unconfiguredDom = new JSDOM(unconfigured)
    assert.equal(unconfiguredDom.window.document.querySelector('.ledger-empty-actions a[href="#/settings"]')
      ?.textContent, 'Memory settings')

    const bounded = renderToStaticMarkup(React.createElement(LedgerEmptyState, {
      sourceStates: {
        atlas: current, claims: current, conceptCuration: current, claimCuration: current,
      }, claimSource: { status: 'partial' },
      pending: [], retry() {}, busy: false,
    }))
    const boundedDom = new JSDOM(bounded)
    assert.match(boundedDom.window.document.querySelector('h2').textContent,
      /No retained evidence/)
    assert.match(boundedDom.window.document.body.textContent, /do not prove absence/)
    // Both evidence slices render claim rows read under ONE combined receipt, so both report it.
    assert.deepEqual([...boundedDom.window.document.querySelectorAll('.ledger-readiness-state')]
      .map(node => node.textContent), ['partial', 'partial', 'complete', 'complete'])

    const notice = renderToStaticMarkup(React.createElement(EvidenceSourceNotice, {
      claims: { status: 'partial' },
    }))
    const noticeDom = new JSDOM(notice)
    assert.ok(noticeDom.window.document.querySelector('[role="status"]'))
    assert.match(noticeDom.window.document.body.textContent,
      /Claims partial.*Absence and one-sided claim state withheld/is)
    // A COMPLETE claim source prints nothing. It used to be able to fire on a partial concept-capsule
    // read — a store this screen no longer touches — which read as a degraded claim ledger.
    assert.equal(renderToStaticMarkup(React.createElement(EvidenceSourceNotice, {
      claims: { status: 'complete' },
    })), '')

    const partial = renderToStaticMarkup(React.createElement(LedgerEmptyState, {
      sourceStates: {
        atlas: current, claims: failed, conceptCuration: failed, claimCuration: stale,
      }, claimSource: { status: 'unknown' },
      pending: ['conceptCuration'],
      retry() {}, busy: false,
    }))
    const partialDom = new JSDOM(partial)
    assert.match(partialDom.window.document.querySelector('h2').textContent,
      /Claim evidence unavailable/)
    assert.deepEqual([...partialDom.window.document.querySelectorAll('.ledger-readiness-state')]
      .map(node => node.textContent), ['unknown', 'unavailable', 'loading', 'stale'])
    // Every non-current source keeps a retry control, INCLUDING the one currently reloading. The
    // control used to be removed while loading, which reflowed the row out from under the pointer
    // and blurred it for a keyboard user; it is now held in place and gated by `busy` instead.
    assert.deepEqual([...partialDom.window.document.querySelectorAll('.ledger-empty-source .btn')]
      .map(node => node.getAttribute('aria-label')),
    ['Retry Claim records', 'Retry Concept steward log', 'Retry Claim steward log'])
    assert.ok(partialDom.window.document.querySelector('.ledger-empty-source-loading .btn'),
      'a reloading source keeps its control in place rather than reflowing the row')
  } finally {
    await vite.close()
  }
})

test('the mounted ledger settles sources progressively and fences timed-out or superseded reads', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const realSetTimeout = globalThis.setTimeout
  const realClearTimeout = globalThis.clearTimeout
  const requests = []
  const sourceTimers = []
  const installed = {
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    location: dom.window.location, sessionStorage: dom.window.sessionStorage,
    HTMLElement: dom.window.HTMLElement, Node: dom.window.Node,
    requestAnimationFrame: callback => realSetTimeout(callback, 0),
    cancelAnimationFrame: handle => realClearTimeout(handle),
    IS_REACT_ACT_ENVIRONMENT: true,
    fetch: (url, options = {}) => new Promise(resolve => requests.push({
      url: String(url), options, resolve,
    })),
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  const previousTimers = Object.fromEntries(['setTimeout', 'clearTimeout']
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  let root
  let vite
  const response = (payload, status = 200) => ({
    ok: status < 400, status, headers: { get: () => null }, json: async () => payload,
  })
  const reply = (request, payload, status = 200) => act(async () => {
    request.resolve(response(payload, status))
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve()
  })
  const click = button => act(async () => {
    button.click()
    await Promise.resolve(); await Promise.resolve()
  })
  // The atlas slice contributes the MIXED-EVIDENCE records now; its concept sections are sent and
  // ignored, which is exactly what this envelope models.
  const atlasEnvelope = statement => ({
    portfolio_id: PORTFOLIO_ID,
    explored: [{ concept: 'ignored', n_runs: 1, runs: [] }], thin_coverage: [],
    contradictions: [{ ...claim(71), statement }],
  })
  const claimsEnvelope = statement => ({
    portfolio_id: PORTFOLIO_ID, claims: [{ ...claim(70), statement }],
  })
  const requestFor = (batch, path) => batch.find(item => item.url.includes(path))
  const sourceNote = label => [...document.querySelectorAll('.ledger-source-note')]
    .find(node => node.textContent.includes(label))
  try {
    for (const [key, value] of Object.entries(installed)) {
      Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
    }
    vite = await createServer({
      root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
      server: { middlewareMode: true },
    })
    const [{ createRoot }, { default: ClaimsCuration }] = await Promise.all([
      import('react-dom/client'), vite.ssrLoadModule('/src/ClaimsCuration.jsx'),
    ])
    globalThis.setTimeout = (callback, delay, ...args) => {
      if (delay !== 15_000) return realSetTimeout(callback, delay, ...args)
      const handle = { cleared: false, fire: () => callback(...args) }
      sourceTimers.push(handle)
      return handle
    }
    globalThis.clearTimeout = handle => {
      if (handle && typeof handle === 'object' && 'cleared' in handle) handle.cleared = true
      else realClearTimeout(handle)
    }
    root = createRoot(document.getElementById('root'))
    await act(async () => {
      root.render(React.createElement(ClaimsCuration, { onBack() {} }))
      await Promise.resolve()
    })
    const initial = requests.slice()
    assert.equal(initial.length, 4)
    await reply(requestFor(initial, '/atlas?'), atlasEnvelope('progressive mixed record'))
    await reply(requestFor(initial, '/claims?'), claimsEnvelope('progressive claim'))
    await reply(requestFor(initial, '/curation-log?'), curationEnvelope())

    assert.match(document.body.textContent, /progressive mixed record.*progressive claim/s,
      'settled slices render while the fourth request remains unresolved')
    assert.equal(document.querySelector('main').getAttribute('aria-busy'), 'true')
    assert.match(sourceNote('Claim steward log').textContent, /loading/)
    let retryButton = sourceNote('Claim steward log').querySelector('button')
    assert.equal(retryButton.disabled, true)
    assert.equal(retryButton.getAttribute('aria-label'), 'Retry Claim steward log')

    const hangingTimer = sourceTimers.find(timer => !timer.cleared)
    await act(async () => { hangingTimer.fire(); await Promise.resolve(); await Promise.resolve() })
    assert.equal(document.querySelector('main').getAttribute('aria-busy'), 'false')
    assert.match(sourceNote('Claim steward log').textContent, /unavailable/)
    assert.equal(sourceNote('Claim steward log').querySelector('button').disabled, false,
      'a timed-out slice must release its exact retry')

    const refreshStart = requests.length
    await click(document.querySelector('[aria-label="Refresh all claim and curation sources"]'))
    const refreshBatch = requests.slice(refreshStart)
    assert.equal(refreshBatch.length, 4)
    await reply(requestFor(refreshBatch, '/atlas?'), { detail: 'offline' }, 503)
    await reply(requestFor(refreshBatch, '/claims?'), claimsEnvelope('fresh claim'))
    await reply(requestFor(refreshBatch, '/api/cross-run/curation-log?'), curationEnvelope())
    await reply(requestFor(refreshBatch, '/claim-curation-log?'), { detail: 'offline' }, 503)
    assert.match(document.body.textContent,
      /Refresh incomplete; showing stale last-good data; some sources unavailable\./)
    assert.match(document.body.textContent, /progressive mixed record/)
    assert.match(sourceNote('Mixed claims').textContent, /stale/)

    const localStart = requests.length
    retryButton = sourceNote('Claim steward log').querySelector('button')
    await click(retryButton)
    const localBatch = requests.slice(localStart)
    assert.equal(localBatch.length, 1)
    assert.match(localBatch[0].url, /\/api\/cross-run\/claim-curation-log\?limit=20$/)
    retryButton = sourceNote('Claim steward log').querySelector('button')
    // The button the operator just pressed stays focusable and announces itself busy; a `disabled`
    // attribute here would blur it mid-interaction and drop the keyboard user back to the document.
    assert.equal(retryButton.disabled, false)
    assert.equal(retryButton.getAttribute('aria-disabled'), 'true')
    assert.equal(retryButton.getAttribute('aria-busy'), 'true')
    assert.equal(retryButton.getAttribute('aria-label'), 'Retrying Claim steward log')
    assert.equal(sourceNote('Mixed claims').querySelector('button').disabled, true,
      'every OTHER retry is hard-disabled while a source request is active')
    await reply(localBatch[0], curationEnvelope([{ run_id: 'claim-current', outcome: 'empty' }]))
    assert.match(document.body.textContent, /claim steward.*empty/is)

    await reply(requestFor(initial, '/claim-curation-log?'),
      curationEnvelope([{ run_id: 'late-timeout', outcome: 'empty' }]))
    assert.doesNotMatch(document.body.textContent, /late-timeout/,
      'a response arriving after its timeout cannot overwrite the successful retry')

    const supersededStart = requests.length
    // The mixed-evidence watermark is the atlas slice's ONLY retry now (the concepts panel that used
    // to own it is gone), so a failed mixed read stays recoverable outside the empty state.
    await click(sourceNote('Mixed claims').querySelector('button'))
    const superseded = requests[supersededStart]
    assert.match(superseded.url, /\/api\/cross-run\/atlas\?limit=24$/)
    const replacementStart = requests.length
    await act(async () => {
      root.render(React.createElement(ClaimsCuration, { key: 'replacement', onBack() {} }))
      await Promise.resolve(); await Promise.resolve()
    })
    assert.equal(superseded.options.signal.aborted, true)
    const replacement = requests.slice(replacementStart)
    assert.equal(replacement.length, 4)
    await reply(requestFor(replacement, '/atlas?'), atlasEnvelope('replacement mixed record'))
    await reply(requestFor(replacement, '/claims?'), claimsEnvelope('replacement claim'))
    await reply(requestFor(replacement, '/api/cross-run/curation-log?'), curationEnvelope())
    await reply(requestFor(replacement, '/claim-curation-log?'), curationEnvelope())
    await reply(superseded, atlasEnvelope('superseded mixed record'))
    assert.match(document.body.textContent, /replacement mixed record/)
    assert.doesNotMatch(document.body.textContent, /superseded mixed record/,
      'an aborted late response from a replaced request cannot commit')
  } finally {
    if (root) await act(async () => root.unmount())
    for (const [key, descriptor] of Object.entries(previousTimers)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
    if (vite) await vite.close()
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
    dom.window.close()
  }
})

test('Claims & Curation has a discoverable owner-only route and complete resource states', async () => {
  const [app, runList, ledger, api, css, globalMenu] = await Promise.all([
    source('App.jsx'), source('RunList.jsx'), source('ClaimsCuration.jsx'), source('api.js'),
    source('claims-curation.css'), source('GlobalMenu.jsx'),
  ])
  const { GLOBAL_DESTINATIONS } = await import('../src/globalNav.js')

  assert.match(app, /lazy\(\(\) => import\('\.\/ClaimsCuration\.jsx'\)\)/)
  // BOTH former spellings are aliases now, and both canonicalize to the SAME new hash — `#/atlas`
  // was the canonical form before F7 and is written into operators' bookmarks and into doc links.
  assert.match(app, /h === '#\/research-atlas' \|\| h === '#\/atlas'[\s\S]*canonicalHash: '#\/claims'/)
  assert.match(app, /h === '#\/claims'\) return \{ view: 'claims' \}/)
  // The route helpers go through `navigateWithListState`, which preserves the run-list scroll and
  // selection across the round trip; assigning `location.hash` directly was the old spelling and the
  // assertion below it (the owner gate) never ran once this stopped matching. This surface, Settings
  // and the installation surfaces share ONE departure — the property holds for every LoopLab
  // destination rather than only for the two that remembered to opt in.
  assert.match(app, /const globalNavigate = useCallback\(\(hash, snapshot\) => \{\s*navigateWithListState\(hash, snapshot, null, 'looplab'\)/,
    'the claim ledger must navigate like its sibling owner routes, not by a private mechanism')
  assert.match(app, /history\.replaceState\(history\.state, '', route\.canonicalHash\)/)
  assert.match(app, /route\.view === 'claims'/)
  assert.match(app, /onGlobalNavigate=\{globalNavigate\}/)
  assert.ok(app.lastIndexOf("route.view === 'claims'") < app.indexOf('<OwnerAuth label={routeLabel}>'),
    'claim/curation content must be wrapped by the owner authentication gate')
  // Discoverability moved from a loose header button into the LoopLab menu the run list renders.
  // Assert the whole chain — the list mounts the menu, the menu renders the shared destination list,
  // and that list still contains the canonical hash — so removing any link fails here.
  assert.match(runList, /<GlobalMenu current="list"[\s\S]*onNavigate=\{openGlobal\}/)
  assert.match(globalMenu, /GLOBAL_DESTINATIONS\.map\(entry =>/)
  assert.deepEqual(
    GLOBAL_DESTINATIONS.filter(entry => entry.key === 'claims')
      .map(entry => [entry.hash, entry.label]),
    [['#/claims', 'Claims & Curation']],
    'the claim ledger must stay a canonical, labelled LoopLab destination')
  assert.deepEqual(GLOBAL_DESTINATIONS.filter(entry => /atlas/i.test(`${entry.key}${entry.hash}${entry.label}`)), [],
    'no menu entry may still carry the old name')
  assert.match(ledger, /requestedSources\.forEach/)
  for (const state of [/Loading claims and curation/, /Claims & Curation couldn.t load/,
    /No cross-run evidence/, /Some sources unavailable\./]) assert.match(ledger, state)
  assert.match(ledger, /Claims &amp; Curation[\s\S]*Experimental · bounded · read-only/)
  assert.match(ledger, /D8 receipts cover processed rows,[\s\S]*not every run/)
  assert.match(ledger, /<EvidenceSourceNotice claims=\{view\.claimSource\}/)
  assert.match(ledger, /Referenced runs/)
  assert.match(ledger, /Claim and curation source readiness/)
  assert.doesNotMatch(ledger, /<ul[^>]*role="region"/)
  assert.match(ledger, /aria-label="Bounded mixed-evidence claim records"/)
  assert.doesNotMatch(ledger, /aria-label="[^"]*[Cc]ontradictory claims"/)
  assert.match(ledger, /Some portfolio records were ignored\./)
  assert.match(ledger, /Refresh incomplete; showing stale last-good data/)
  assert.match(css, /\.ledger-source-retained-stale \{[^}]*color: var\(--working-text\)/)
  assert.match(css, /\.ledger-source-loading \{[^}]*color: var\(--working-text\)/)
  assert.match(css, /\.ledger-source-failed \{[^}]*color: var\(--fail-text\)/)
  // F7: the concepts section is GONE, not hidden, and what replaced it is a LINK to the view that
  // outgrew it. Negative pins stay substrings on purpose — what must not come back is the text.
  for (const gone of [/Concepts seen across runs/, /Observed in one run/, /Bounded explored concepts/,
    /thin_coverage/, /conceptSource/, /ledger-concepts/, /ledger-thin/, /ledger-runref/]) {
    assert.doesNotMatch(ledger, gone, 'the weaker concept copy must not return to this surface')
  }
  assert.match(ledger, /<a href="#\/concepts">Concepts view<\/a>/,
    'the section it replaced must leave a way to reach the richer view')
  assert.match(app, /h === '#\/concepts'\) return \{ view: 'list', listView: 'concepts' \}/,
    'that link must resolve to the run list opened ON its Concepts view, not to the List tab')
  assert.match(app, /<RunList key=\{requestedListView \|\| 'runs'\}/,
    'RunList captures its initial navigation once, so the requested view has to re-key it')
  assert.match(ledger, /support-only evidence/)
  assert.match(ledger, /not a verdict or applicability decision/)
  assert.match(ledger, /claim grouping ·/)
  assert.match(ledger, /\['support', claim\.support, claim\.nSupport\]/)
  assert.match(ledger, /kind === 'contradiction' \? 's' : ' refs'/)
  assert.doesNotMatch(ledger, /scope ·|>support <b>|>oppose <b>/)
  assert.match(ledger, /Recent proposals \+ outcomes/)
  assert.equal((ledger.match(/<SourceWatermark (?:key=\{sourceKey\} )?sourceKey=/g) || []).length, 3,
    'every panel must disclose its source; the mapped curation watermark renders twice')
  // The dropped concepts panel owned the atlas slice's only retry; the mixed-evidence watermark
  // inherited it. Driven for real by the mounted test, which clicks it and observes the request.
  assert.match(ledger, /sourceKey="atlas"[\s\S]*?activeRetry=\{busy && request\.key === 'atlas'\}/)
  assert.doesNotMatch(ledger, /retryable=\{false\}/,
    'no watermark may opt out of its retry now that no panel doubles up on a source')
  assert.match(ledger, /history, not current governance/)
  assert.match(ledger, /data-route-main tabIndex=\{-1\}/)
  assert.match(api, /crossRunRead[\s\S]*cache: 'no-store'/)
  assert.match(api, /cross-run\/claims\?limit=\$\{args\.limit\}&offset=/)
  // The HTTP contract deliberately did NOT move with the surface.
  assert.match(api, /cross-run\/atlas\?limit=/)
})

test('cross-run reads are no-store, abortable, and clamp every paging input', async () => {
  const previous = {
    fetch: globalThis.fetch,
    location: globalThis.location,
    sessionStorage: globalThis.sessionStorage,
  }
  const calls = []
  globalThis.location = { pathname: '/', hash: '' }
  globalThis.sessionStorage = { getItem: () => null }
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, options })
    return { ok: true, json: async () => ({}) }
  }
  const controller = new AbortController()
  try {
    await getCrossRunAtlas(Number.POSITIVE_INFINITY, { signal: controller.signal })
    await getCrossRunClaims(500, -7, { signal: controller.signal })
    await getCrossRunCurationLog(0, { signal: controller.signal })
    await getCrossRunClaimCurationLog(999, { signal: controller.signal })
    assert.match(calls[0].url, /\/api\/cross-run\/atlas\?limit=24$/)
    assert.match(calls[1].url, /\/api\/cross-run\/claims\?limit=200&offset=0$/)
    assert.match(calls[2].url, /\/api\/cross-run\/curation-log\?limit=1$/)
    assert.match(calls[3].url, /\/api\/cross-run\/claim-curation-log\?limit=50$/)
    for (const call of calls) {
      assert.equal(call.options.cache, 'no-store')
      assert.equal(call.options.signal, controller.signal)
    }
  } finally {
    for (const [name, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[name]
      else globalThis[name] = value
    }
  }
})

test('empty and malformed envelopes degrade to a stable empty projection', () => {
  assert.doesNotThrow(() => buildClaimsCurationView(null, { claims: 'not-an-array' }, []))
  const view = buildClaimsCurationView({
    contradictions: [{}],
  }, { claims: [{ statement: { unsafe: true } }] }, { entries: [{}] })
  assert.deepEqual(view.contradictions, [])
  assert.deepEqual(view.claims, [])
  assert.deepEqual(view.curation, [])
  assert.equal(view.invalidRows.total, 3)
  assert.equal(view.empty, true)
  assert.deepEqual(buildClaimsCurationView(null, null, null).totals,
    { runs: 0, claims: 0, contested: 0, curation: 0 })
  assert.equal(buildClaimsCurationView(null, null, null).empty, true)
})

test('contested/contradiction and curation records prevent a false empty ledger', () => {
  assert.equal(buildClaimsCurationView({ n_contested: 2 }, {}, {}).empty, false)
  assert.equal(buildClaimsCurationView({ contradictions: [claim(1)] }, {}, {}).empty, false)
  assert.equal(buildClaimsCurationView({}, {}, { n: 3 }, ).empty, false)
  // A concept-only atlas response is now EMPTY here: this surface renders none of it, and claiming
  // otherwise would print "showing 0 of 0" panels beside a "not empty" heading.
  assert.equal(buildClaimsCurationView(
    { explored: [{ concept: 'one' }], thin_coverage: ['one-run-only'] }, {}, {}).empty, true)
})

test('curation preview stays bounded to steward outcome counts', async () => {
  const [atlas, css] = await Promise.all([source('ClaimsCuration.jsx'), source('claims-curation.css')])
  assert.match(atlas, /<b>\{entry\.kind\} steward<\/b>[\s\S]*entry\.proposals[\s\S]*entry\.applied/)
  assert.doesNotMatch(atlas, /entry\.runId|entry\.at|entry\.revision/)
  assert.match(css, /\.ledger-curation-list/)
})

// ---------------------------------------------------------------------------------------------
// `api.js::CROSS_RUN_STATE_FIELDS` is an ALLOWLIST: a wire field absent from it never reaches React
// state. So the list and the fields `claimsCurationModel.normalizeClaim` reads are ONE contract, and
// nothing held them together — a field dropped from the list turns its render branch into code no
// server response can reach, which is exactly what had happened to five of them (`decision`,
// `polarity`, `sources`, `verification`, `evidence_digest`). The Decision line, the polarity half of
// the metric line and the whole "Sources and verification" disclosure in `ClaimsCuration.jsx` were
// unreachable for every possible payload, while the model went on carefully normalizing all five.
//
// DRIVEN through the real projection rather than pinned against the literal: a pin would have passed
// on the same list that dropped them.
const fullyPopulatedClaim = () => ({
  ...claim(1),
  epistemic: 'mixed',
  maturity: 'operator-ratified',
  oppose: ['run-1:node-3'],
  n_oppose: 1,
  contradicts: ['run-2:node-9'],
  n_contradicts: 1,
  polarity: 1,
  metric: 'recall@100',
  sources: ['https://example.invalid/lessons'],
  verification: ['verified by run-4'],
  evidence_digest: 'd'.repeat(64),
  decision_fresh: true,
  decision: { decision: 'ratified', note: 'reproduced twice', by: 'operator', at: '2026-08-10' },
})

test('the claim projection keeps every field the claim card renders', () => {
  const projected = projectLedgerSource('atlas', {
    portfolio_id: PORTFOLIO_ID, n_contested: 1,
    claim_source: completeClaimSource,
    contradictions: [fullyPopulatedClaim()],
  })
  const view = buildClaimsCurationView(projected, {}, {})
  const row = view.contradictions[0]
  assert.ok(row, 'the mixed-evidence row must survive the projection')
  // The steward's verdict, which is the entire point of a "Claims & Curation" screen.
  assert.deepEqual(row.decision,
    { action: 'ratified', note: 'reproduced twice', by: 'operator', at: '2026-08-10' })
  assert.equal(row.polarity, 1)
  assert.deepEqual(row.sources, ['https://example.invalid/lessons'])
  assert.deepEqual(row.verification, ['verified by run-4'])
  assert.equal(row.evidenceDigest, 'd'.repeat(64))
})

test('every wire field the claim model reads is on the api allowlist', async () => {
  // The anti-drift half. `normalizeClaim`/`normalizeClaimSource` read the wire row as `row.<field>` /
  // `receipt.<field>`; `projectCrossRunValue` keeps only names listed in `CROSS_RUN_STATE_FIELDS`. A
  // name read on one side and absent from the other is a render branch no input can reach, and it is
  // silent — the field simply arrives `undefined`.
  const [model, api] = await Promise.all([source('claimsCurationModel.js'), source('api.js')])
  const literal = api.match(/const CROSS_RUN_STATE_FIELDS = `([^`]*)`/)
  assert.ok(literal, 'the allowlist literal must stay findable')
  const allowed = new Set(literal[1].split(/\s+/).filter(Boolean))
  const read = new Set()
  for (const [, name] of model.matchAll(
    /\b(?:row|receipt|segment|envelope|entry|decision)\.([a-z][a-z0-9_]*)\b/g)) read.add(name)
  // `kind` is the ONE name here that is not a wire field: `mergeCurationLogs` stamps it onto each
  // curation entry itself (`{ ...record(envelope.entries[0]), kind }`) after the projection has
  // already run, so the allowlist has no business carrying it.
  read.delete('kind')
  // Only the wire names — a camelCase read is this model's own projected shape, never the envelope's.
  const wire = [...read].filter(name => !/[A-Z]/.test(name))
  const missing = wire.filter(name => !allowed.has(name))
  assert.deepEqual(missing, [],
    `these wire fields are read by claimsCurationModel.js but stripped by the api.js allowlist, so `
    + `every branch that depends on them is unreachable: ${missing.join(', ')}`)
})
