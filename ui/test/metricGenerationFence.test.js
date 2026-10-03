import test from 'node:test'
import assert from 'node:assert/strict'
import { click, fetchStub, jsonResponse, mountLive, until } from './_mount.js'

const oldGeneration = 'a'.repeat(64), generation = 'b'.repeat(64)
const series = name => ({ [name]: [{ step: 1, value: .5 }] })
const payload = (runGeneration, name) => ({ node_id: 1, attempt: 0,
  run_generation: runGeneration, metrics: series(name) })

test('metric curves require the requested generation and recover only on an explicit retry', async () => {
  const requests = []
  let response = payload(oldGeneration, 'wrong/loss')
  const h = await mountLive({ visible: true, routes: {
    '/api/runs/r/nodes/1/metrics': ({ url }) => {
      requests.push(url); return response
    },
  } })
  try {
    const { MetricCurves } = await h.load('/src/Inspector.jsx')
    const view = await h.mount(MetricCurves, { runId: 'r', nodeId: 1, attempt: 0,
      status: 'evaluated', expectedGeneration: generation })
    try {
      await until(() => view.container.textContent.includes('Metric curves unavailable'), 'wrong generation refused')
      assert.equal(requests[0].searchParams.get('expected_generation'), generation)
      assert.doesNotMatch(view.container.textContent, /wrong.*1 metric|no metric curves logged yet/)
      for (const invalid of [payload(undefined, 'missing/loss'),
        { ...payload(generation, 'other/loss'), node_id: 2 },
        { ...payload(generation, 'other/loss'), attempt: 1 },
        { ...payload(generation, 'invalid/loss'), metrics: 'not a series map' },
        { ...payload(generation, 'invalid/loss'), metrics: [] }]) {
        response = invalid
        await click(view.container.querySelector('button'))
        await until(() => view.container.textContent.includes('Metric curves unavailable'), 'incomplete evidence refused')
        assert.doesNotMatch(view.container.textContent, /missing.*1 metric|other.*1 metric|no metric curves logged yet/)
      }
      response = payload(generation, 'current/loss')
      await click(view.container.querySelector('button'))
      await until(() => view.container.textContent.includes('current'), 'retry reads current curves')
      assert.equal(requests.length, 7)
      assert.ok(h.fetch.calls.every(call => call.method === 'GET'))
    } finally { await view.unmount() }
  } finally { await h.close() }
})

test('changing generation clears curves before the replacement read, fencing late responses', async () => {
  const pending = [], urls = []
  const h = await mountLive({ visible: true })
  const backend = fetchStub({ '/api/runs/r/nodes/1/metrics': ({ url }) => {
    urls.push(url)
    if (urls.length === 1) return payload(oldGeneration, 'old/loss')
    return new Promise(resolve => pending.push(resolve))
  } })
  globalThis.fetch = backend
  try {
    const { MetricCurves } = await h.load('/src/Inspector.jsx')
    const props = expectedGeneration => ({ runId: 'r', nodeId: 1, attempt: 0,
      status: 'evaluated', expectedGeneration })
    const view = await h.mount(MetricCurves, props(oldGeneration))
    try {
      await until(() => view.container.textContent.includes('old'), 'initial measured curves')
      await view.rerender(props(generation))
      assert.doesNotMatch(view.container.textContent, /old.*1 metric/)
      await until(() => pending.length === 1, 'new generation read starts')
      assert.equal(urls[1].searchParams.get('expected_generation'), generation)
      await view.rerender(props(oldGeneration))
      await until(() => pending.length === 2, 'third owned read')
      pending[0](jsonResponse(payload(generation, 'late/other-run')))
      pending[1](jsonResponse(payload(oldGeneration, 'fresh/loss')))
      await until(() => view.container.textContent.includes('fresh'), 'latest scope owns evidence')
      assert.doesNotMatch(view.container.textContent, /late.*1 metric|old.*1 metric/)
    } finally { await view.unmount() }
  } finally { await h.close() }
})
