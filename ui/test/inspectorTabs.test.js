import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import {
  READ_ONLY_INSPECT_TABS, REVIEW_EVIDENCE_TABS, REVIEW_SUMMARY_TABS, RUN_ROUTE_TABS,
  hashWithRunRouteState, inspectorTabs, parseRunRouteState,
} from '../src/runRouteState.js'

// Review 2026-09-22, UI-04: the Inspector's tab vocabulary lived in four copies and two deciders
// (RunView healing the URL, the Inspector drawing its strip). One function now answers both.

test('the one function: live, review and read-only, with and without a sweep', () => {
  assert.deepEqual(inspectorTabs({ access: 'live', sweep: true }), RUN_ROUTE_TABS)
  assert.deepEqual(inspectorTabs({ access: 'live', sweep: false }),
    ['Overview', 'Comments', 'Trace', 'Code', 'Metrics', 'Trust', 'Cost'])
  assert.deepEqual(inspectorTabs({ access: 'review', evidence: true }), REVIEW_EVIDENCE_TABS)
  assert.deepEqual(inspectorTabs({ access: 'review', evidence: false }), REVIEW_SUMMARY_TABS)
  assert.deepEqual(inspectorTabs({ access: 'read-only', sweep: true }), READ_ONLY_INSPECT_TABS)
  assert.deepEqual(READ_ONLY_INSPECT_TABS, ['Overview', 'Code', 'Trust', 'Cost'])
})

test('a node that has not loaded yet keeps a Trials deep link rather than healing it away', () => {
  assert.ok(inspectorTabs().includes('Trials'))
  assert.ok(inspectorTabs({ access: 'live' }).includes('Trials'))
})

test('every tab any view can offer is one the route can carry, so no deep link is dropped', () => {
  const offered = new Set()
  for (const access of ['live', 'review', 'read-only']) {
    for (const evidence of [false, true]) {
      for (const sweep of [false, true]) {
        for (const tab of inspectorTabs({ access, evidence, sweep })) offered.add(tab)
      }
    }
  }
  for (const tab of offered) {
    assert.ok(RUN_ROUTE_TABS.includes(tab), `${tab} is offered but the route vocabulary drops it`)
    const hash = hashWithRunRouteState('#/run/demo', { generation: 'a'.repeat(64), nodeId: 3, inspectTab: tab })
    assert.equal(parseRunRouteState(hash).state.inspectTab, tab, `${tab} does not survive a reload`)
  }
})

test('neither decider re-spells a tab list (the copies this replaced must not come back)', () => {
  const inspector = readFileSync(new URL('../src/Inspector.jsx', import.meta.url), 'utf8')
  const runView = readFileSync(new URL('../src/RunView.jsx', import.meta.url), 'utf8')
  for (const [name, source] of [['Inspector.jsx', inspector], ['RunView.jsx', runView]]) {
    assert.ok(!source.includes("['Overview', 'Code', 'Trust', 'Cost']"), `${name} re-spells the read-only strip`)
    assert.ok(!source.includes("'Comments', 'Trace', 'Code', 'Metrics'"), `${name} re-spells the live strip`)
    assert.ok(!/const (?:LIVE|READ_ONLY)_INSPECT_TABS\b/.test(source), `${name} owns a tab list again`)
  }
})
