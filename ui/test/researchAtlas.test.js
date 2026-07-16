import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import {
  ATLAS_RENDER_LIMITS,
  boundedAtlasText,
  buildResearchAtlasView,
  mergeCurationLogs,
  mergeResearchAtlasPayload,
  reconcileAtlasSourceStatuses,
} from '../src/researchAtlasModel.js'
import {
  getCrossRunAtlas, getCrossRunClaims, getCrossRunCurationLog, getCrossRunClaimCurationLog,
} from '../src/api.js'

const source = name => readFile(new URL(`../src/${name}`, import.meta.url), 'utf8')

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
})

test('Atlas projection reconciles concurrent totals without trusting malformed text or counts', () => {
  const view = buildResearchAtlasView({
    n_runs: 0,
    n_concepts: 0,
    n_contested: 0,
    explored: [{ concept: '\n  robust\tsearch ', n_runs: 0, runs: [
      { run_id: 'run/one\n', direction: 'max', metric: 0.75 },
    ] }],
    thin_coverage: ['robust\nsearch'],
    contradictions: [{ ...claim(1), n_oppose: -10, oppose: ['run-1:node-3'] }],
  }, { n: 0, claims: [claim(0)] }, {
    n: 0,
    entries: [{ run_id: 'run-0', proposals: { merges: [{}], splits: 2 },
      receipt: { applied: [{}], skipped: [] } }],
  })

  assert.deepEqual(view.totals, { runs: 3, concepts: 1, claims: 1, contested: 1, curation: 1 })
  assert.equal(view.empty, false)
  assert.equal(view.concepts[0].concept, 'robust search')
  assert.equal(view.concepts[0].runs[0].runId, 'run/one')
  assert.equal(view.claims[0].nSupport, 1)
  assert.equal(view.claims[0].nUnverified, 1)
  assert.deepEqual(view.claims[0].unverified, ['run-0:node-2'])
  assert.equal(view.curation[0].merges, 1)
  assert.equal(view.curation[0].splits, 2)
  assert.equal(boundedAtlasText({ unsafe: 'shape' }), '')
})

test('Atlas derives evidence balance from sanitized totals instead of payload labels', () => {
  const contradictory = [
    { ...claim(0), epistemic: 'refuted', n_support: 2, support: [], n_oppose: 0, oppose: [] },
    { ...claim(1), epistemic: 'supported', n_support: 0, support: [], n_oppose: 3, oppose: [] },
    { ...claim(2), epistemic: 'inconclusive', n_support: 1, support: [], n_oppose: 1, oppose: [] },
    { ...claim(3), epistemic: 'mixed', n_support: -4, support: [], n_oppose: 'bad', oppose: [] },
  ]
  const view = buildResearchAtlasView({}, { claims: contradictory }, {})

  assert.deepEqual(view.claims.map(row => [row.nSupport, row.nOppose, row.epistemic]), [
    [2, 0, 'supported'],
    [0, 3, 'refuted'],
    [1, 1, 'mixed'],
    [0, 0, 'inconclusive'],
  ])
})

test('Atlas projection applies hard caps before React receives portfolio collections', () => {
  const concepts = Array.from({ length: ATLAS_RENDER_LIMITS.concepts + 3 }, (_, index) => ({
    concept: `concept-${index}`,
    n_runs: 1,
    runs: Array.from({ length: ATLAS_RENDER_LIMITS.conceptRuns + 2 }, (__, run) => ({
      run_id: `run-${index}-${run}`, direction: 'min', metric: run,
    })),
  }))
  const claims = Array.from({ length: ATLAS_RENDER_LIMITS.claims + 4 }, (_, index) => ({
    ...claim(index),
    support: Array.from({ length: ATLAS_RENDER_LIMITS.evidence + 5 }, (__, ref) => `run-${index}:node-${ref}`),
  }))
  const contradictions = Array.from(
    { length: ATLAS_RENDER_LIMITS.contradictions + 2 }, (_, index) => claim(index))
  const entries = Array.from({ length: ATLAS_RENDER_LIMITS.curation + 7 }, (_, index) => ({
    run_id: `run-${index}`, proposals: {}, receipt: {},
  }))
  const view = buildResearchAtlasView({ explored: concepts, contradictions,
    thin_coverage: concepts.map(row => row.concept) }, { claims }, { entries })

  assert.equal(view.concepts.length, ATLAS_RENDER_LIMITS.concepts)
  assert.equal(view.concepts[0].runs.length, ATLAS_RENDER_LIMITS.conceptRuns)
  assert.equal(view.claims.length, ATLAS_RENDER_LIMITS.claims)
  assert.equal(view.claims[0].support.length, ATLAS_RENDER_LIMITS.evidence)
  assert.equal(view.contradictions.length, ATLAS_RENDER_LIMITS.contradictions)
  assert.equal(view.curation.length, ATLAS_RENDER_LIMITS.curation)
  assert.equal(view.hiddenConcepts, 3)
  assert.equal(view.hiddenContradictions, 2)
  assert.equal(view.hiddenClaims, 4)
  assert.equal(view.hiddenCuration, 7)
})

test('Atlas source-revision reduces without an unbounded spread on a huge ledger', () => {
  // sourceRevision (via reconcileAtlasSourceStatuses) reads the RAW, un-take()'d `entries` array; a
  // pathologically large / mis-limited curation ledger must not throw RangeError via Math.max(...spread)
  // and crash the Atlas route — it must degrade and still fingerprint the max revision.
  const huge = { conceptCuration: { entries: Array.from({ length: 200000 }, (_, i) => ({ revision: i })) } }
  const statuses = reconcileAtlasSourceStatuses({}, huge, 'now')
  assert.equal(statuses.conceptCuration.state, 'current')
  assert.equal(statuses.conceptCuration.revision, String(199999))   // max revision, computed without crashing
})

test('concept and claim steward logs merge deterministically by time, revision, then kind', () => {
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
    ['claim', 'claim-newest'],
    ['concept', 'concept-new'],
    ['claim', 'claim-rev'],
    ['concept', 'concept-rev'],
  ])
  const view = buildResearchAtlasView({}, {}, merged)
  assert.equal(view.curation[0].kind, 'claim')
  assert.equal(view.curation[2].autoRequested, true)
  assert.equal(view.curation[2].outcome, 'unavailable')
})

test('a partial refresh replaces successful slices and preserves last-good failed slices', () => {
  const previous = {
    atlas: { n_runs: 2, explored: [{ concept: 'kept coverage' }] },
    claims: { claims: [claim(1)] },
    conceptCuration: { entries: [{ run_id: 'kept-concept-log' }] },
    claimCuration: { entries: [{ run_id: 'kept-claim-log' }] },
  }
  const freshClaims = { claims: [claim(2)] }
  const merged = mergeResearchAtlasPayload(previous, { claims: freshClaims })
  assert.equal(merged.atlas, previous.atlas)
  assert.equal(merged.claims, freshClaims)
  assert.equal(merged.conceptCuration, previous.conceptCuration)
  assert.equal(merged.claimCuration, previous.claimCuration)
})

test('Atlas source provenance distinguishes current, retained-stale, and failed slices', () => {
  const first = reconcileAtlasSourceStatuses({}, {
    claims: { revision: 7, claims: [claim(1)] },
  }, '2026-07-16T03:00:00Z')
  assert.deepEqual(Object.fromEntries(Object.entries(first).map(([key, value]) => [key, value.state])), {
    atlas: 'failed', claims: 'current', conceptCuration: 'failed', claimCuration: 'failed',
  })
  assert.equal(first.claims.loadedAt, '2026-07-16T03:00:00Z')
  assert.equal(first.claims.revision, '7')

  const second = reconcileAtlasSourceStatuses(first, {
    atlas: { revisions: { concept_governance: 4, claims: 9 } },
  }, '2026-07-16T04:00:00Z')
  assert.deepEqual(Object.fromEntries(Object.entries(second).map(([key, value]) => [key, value.state])), {
    atlas: 'current', claims: 'retained-stale', conceptCuration: 'failed', claimCuration: 'failed',
  })
  assert.equal(second.claims.loadedAt, first.claims.loadedAt)
  assert.equal(second.claims.revision, first.claims.revision)
  assert.equal(second.atlas.revision, 'concept 4 · claims 9')

  const failedRefresh = reconcileAtlasSourceStatuses(second, {}, '2026-07-16T05:00:00Z')
  assert.deepEqual(Object.fromEntries(Object.entries(failedRefresh).map(([key, value]) => [key, value.state])), {
    atlas: 'retained-stale', claims: 'retained-stale', conceptCuration: 'failed', claimCuration: 'failed',
  })

  const allCurrent = reconcileAtlasSourceStatuses(failedRefresh, {
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

  const claimsOnly = reconcileAtlasSourceStatuses(allCurrent, {
    claims: { revision: 9 },
  }, '2026-07-16T07:00:00Z')
  assert.equal(claimsOnly.claims.state, 'current')
  assert.equal(claimsOnly.claims.loadedAt, '2026-07-16T07:00:00Z')
  for (const key of ['atlas', 'conceptCuration', 'claimCuration']) {
    assert.equal(claimsOnly[key].state, 'retained-stale')
    assert.equal(claimsOnly[key].loadedAt, '2026-07-16T06:00:00Z')
  }
})

test('Atlas has a discoverable owner-only route and complete resource states', async () => {
  const [app, runList, atlas, api, css] = await Promise.all([
    source('App.jsx'), source('RunList.jsx'), source('ResearchAtlas.jsx'), source('api.js'),
    source('research-atlas.css'),
  ])

  assert.match(app, /lazy\(\(\) => import\('\.\/ResearchAtlas\.jsx'\)\)/)
  assert.match(app, /h === '#\/research-atlas'[\s\S]*canonicalHash: '#\/atlas'/)
  assert.match(app, /const researchAtlas = \(\) => \{ location\.hash = '#\/atlas' \}/)
  assert.match(app, /history\.replaceState\(history\.state, '', route\.canonicalHash\)/)
  assert.match(app, /route\.view === 'research-atlas'/)
  assert.match(app, /onResearchAtlas=\{researchAtlas\}/)
  assert.ok(app.lastIndexOf("route.view === 'research-atlas'") < app.indexOf('<OwnerAuth label={routeLabel}>'),
    'Atlas content must be wrapped by the owner authentication gate')
  assert.match(runList, /aria-label="Open Research Atlas preview"/)
  assert.match(atlas, /Promise\.allSettled/)
  for (const state of [/Loading Research Atlas preview/, /Research Atlas preview unavailable/,
    /No cross-run memory yet/, /Degraded view; some sources have not loaded\./]) assert.match(atlas, state)
  assert.match(atlas, /Research Atlas preview[\s\S]*Experimental · bounded · read-only/)
  // Keep the implicit list role: role="region" would erase list semantics for assistive technology.
  assert.match(atlas, /<ul className="atlas-concepts" tabIndex=\{0\} aria-label="Bounded explored concepts">/)
  assert.doesNotMatch(atlas, /<ul[^>]*role="region"/)
  assert.match(atlas, /aria-label="Bounded mixed-evidence claim records"/)
  assert.doesNotMatch(atlas, /aria-label="[^"]*[Cc]ontradictory claims"/)
  assert.match(atlas, /Some portfolio records were ignored\./)
  assert.match(atlas, /Partial refresh; preserving last-loaded data for failed sources\./)
  assert.match(css, /\.atlas-source-retained-stale \{[^}]*color: var\(--working-text\)/)
  assert.match(css, /\.atlas-source-failed \{[^}]*color: var\(--fail-text\)/)
  assert.match(atlas, /Concept observations[\s\S]*Concepts seen across runs/)
  assert.match(atlas, /Observed in one run/)
  assert.doesNotMatch(atlas, /<p className="atlas-eyebrow">Coverage<\/p>|<h3>Thin coverage/)
  assert.match(atlas, /support-only evidence/)
  assert.match(atlas, /not a proposition verdict or applicability decision/)
  assert.match(atlas, /claim grouping ·/)
  assert.match(atlas, /supporting attempt refs/)
  assert.doesNotMatch(atlas, /scope ·|>support <b>|>oppose <b>/)
  assert.match(atlas, /Steward invocation log[\s\S]*Recent proposals \+ outcomes/)
  assert.doesNotMatch(atlas, /<p className="atlas-eyebrow">Audit trail<\/p>|Concept \+ claim governance/)
  assert.equal((atlas.match(/<SourceWatermark sourceKey=/g) || []).length, 5,
    'every panel must disclose its live/bounded source; the shared Atlas slice appears twice')
  assert.match(atlas, /not a\s+CoverageFrame, frozen snapshot, or completeness estimate/)
  assert.match(atlas, /logged proposals and outcomes; not a current governance snapshot/)
  assert.match(atlas, /data-route-main tabIndex=\{-1\}/)
  assert.match(api, /crossRunRead[\s\S]*cache: 'no-store'/)
  assert.match(api, /cross-run\/claims\?limit=\$\{args\.limit\}&offset=/)
})

test('Atlas reads are no-store, abortable, and clamp every paging input', async () => {
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
  assert.doesNotThrow(() => buildResearchAtlasView(null, { claims: 'not-an-array' }, []))
  const view = buildResearchAtlasView({
    explored: [null], thin_coverage: [{}], contradictions: [{}],
  }, { claims: [{ statement: { unsafe: true } }] }, { entries: [{}] })
  assert.deepEqual(view.concepts, [])
  assert.equal(view.invalidRows.concepts, 1)
  assert.deepEqual(view.thin, [])
  assert.deepEqual(view.contradictions, [])
  assert.deepEqual(view.claims, [])
  assert.deepEqual(view.curation, [])
  assert.equal(view.invalidRows.total, 5)
  assert.equal(view.empty, true)
  assert.deepEqual(buildResearchAtlasView(null, null, null).totals,
    { runs: 0, concepts: 0, claims: 0, contested: 0, curation: 0 })
  assert.equal(buildResearchAtlasView(null, null, null).empty, true)
})

test('thin observations and contested/contradiction records prevent a false empty Atlas', () => {
  assert.equal(buildResearchAtlasView({ thin_coverage: ['one-run-only'] }, {}, {}).empty, false)
  assert.equal(buildResearchAtlasView({ n_contested: 2 }, {}, {}).empty, false)
  assert.equal(buildResearchAtlasView({ contradictions: [claim(1)] }, {}, {}).empty, false)
})

test('curation metadata keeps long run identity and time readable at 360px', async () => {
  const [atlas, css] = await Promise.all([source('ResearchAtlas.jsx'), source('research-atlas.css')])
  assert.match(atlas, /className="atlas-curation-meta"/)
  assert.match(atlas, /className="atlas-curation-run"/)
  assert.match(atlas, /title=\{entry\.runId\}/)
  assert.match(atlas, /className="atlas-curation-time"[\s\S]*?title=\{timeLabel\}/)
  assert.match(css, /\.atlas-curation-run \{ flex: 1 1 auto; min-width: 0; \}/)
  assert.match(css, /\.atlas-curation-run a \{[^}]*overflow: hidden;[^}]*text-overflow: ellipsis;[^}]*white-space: nowrap;/)
  assert.match(css, /@media \(max-width: 600px\) \{[\s\S]*?\.atlas-curation-meta \{ flex-wrap: wrap;/)
  assert.match(css, /\.atlas-curation-time \{ max-width: 100%; text-align: left; \}/)
})
