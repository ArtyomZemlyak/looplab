import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { fetchStub, mountLive, unanswered, until } from './_mount.js'
import { campaignOverviewView } from '../src/campaignOverviewModel.js'

const gen = 'a'.repeat(64)
const overview = {
  published_seq: 3, at_node: 1,
  content: { summary: 'A/B findings remain visible', recommendation: 'Continue the old fit',
    recommendation_limits: 'Only two directions are covered', learnings: ['Keep negative results'] },
  freshness: { status: 'stale', reasons: [
    { code: 'newer_terminal', event_seq: 7 }, { code: 'newer_operator_direction', event_seq: 8 },
  ] },
}

test('freshness covers later terminals and directions, not only a larger node count', () => {
  const view = campaignOverviewView(overview, { visibleNodeCount: 1, mapEventSeq: 9, snapshotSeq: 9 })
  assert.equal(view.status, 'stale')
  assert.deepEqual(view.reasons.map(row => row.code), ['newer_terminal', 'newer_operator_direction'])
  assert.equal(campaignOverviewView(overview, { mapEventSeq: 10, snapshotSeq: 9 }).status, 'inconsistent')
  assert.equal(campaignOverviewView(null).status, 'missing')
})

test('Report keeps the cumulative overview separate from the latest local result', async () => {
  const h = await mountLive({ visible: true, routes: {
    '/api/runs/r/research-map': { run_id: 'r', generation: gen, event_seq: 9,
      nodes: { total: 2 }, campaign_overview: overview },
  } })
  try {
    const { default: Report } = await h.load('/src/Report.jsx')
    const state = { run_id: 'r', goal: 'Study', direction: 'max', nodes: { 0: { id: 0 }, 1: { id: 1 } },
      report: { published_seq: 9, at_node: 2, headline: 'Local stop', summary: 'STOP is the current local decision' } }
    const view = await h.mount(Report, { state, runId: 'r', readOnly: true,
      expectedGeneration: gen, observedSeq: 9 })
    try {
      await until(() => view.container.textContent.includes('A/B findings remain visible'), 'cumulative overview')
      const text = view.container.textContent
      assert.match(text, /Continue the old fit/)
      assert.match(text, /STOP is the current local decision/)
      assert.match(text, /terminal result was recorded after this overview/)
      assert.match(text, /operator direction was recorded after this overview/)
      assert.match(text, /latest local result/)
      assert.ok(text.indexOf('A/B findings remain visible') < text.indexOf('STOP is the current local decision'))
    } finally { await view.unmount() }
  } finally { await h.close() }
})

test('changing the run aborts the old overview request instead of publishing it elsewhere', async () => {
  const h = await mountLive({ visible: true, routes: {
    '/api/runs/old/research-map': ({ init }) => unanswered(init),
    '/api/runs/new/research-map': { run_id: 'new', generation: gen, event_seq: 9,
      nodes: { total: 0 }, campaign_overview: null },
  } })
  try {
    const { default: CampaignOverview } = await h.load('/src/CampaignOverview.jsx')
    const view = await h.mount(CampaignOverview, { runId: 'old', generation: gen, snapshotSeq: 9 })
    await until(() => h.fetch.calls.some(call => call.path === '/api/runs/old/research-map'), 'old read')
    await view.rerender({ runId: 'new', generation: gen, snapshotSeq: 9 })
    await until(() => view.container.textContent.includes('No cumulative campaign overview'), 'new empty overview')
    assert.doesNotMatch(view.container.textContent, /A\/B findings remain visible/)
    assert.deepEqual(h.fetch.calls.map(call => call.path), [
      '/api/runs/old/research-map', '/api/runs/new/research-map',
    ])
  } finally { await h.close() }
})
