import test from 'node:test'
import assert from 'node:assert/strict'
import { fetchStub, mountLive, until, click } from './_mount.js'

test('profile measurements show actual values, N/A and declared denominators', async () => {
  const h = await mountLive()
  try {
    const { default: ProfileMeasurements } = await h.load('/src/ProfileMeasurements.jsx')
    globalThis.fetch = fetchStub({ ['GET /api/runs/r/profile-measurements']: {
      profile: { state: 'configured', profile: { metrics: [{ key: 'Product@200', label: 'Product', denominator: 'all GT', unit: 'ratio' }] } },
      nodes: [{ node_id: 1, name: 'Score', attempt: 0, values: [{ value: .5, channel: 'declared' }] },
        { node_id: 2, name: 'Pending', attempt: 1, values: [{ value: null }] }], offset: 0, limit: 10, has_more: false,
    } })
    const view = await h.mount(ProfileMeasurements, { runId: 'r', generation: 'a'.repeat(64) })
    try {
      await click(view.container.querySelector('button'))
      await until(() => view.container.textContent.includes('all GT'), 'measurements')
      assert.match(view.container.textContent, /0\.5/)
      assert.match(view.container.textContent, /N\/A/)
      assert.match(view.container.textContent, /does not verify cohort/)
      assert.ok(view.container.querySelector('svg[role="img"]'))
    } finally { await view.unmount() }
  } finally { await h.close() }
})

test('additive stacks use only complete nonnegative rows and matching declared denominator', async () => {
  const h = await mountLive()
  try {
    const { default: ProfileMeasurements } = await h.load('/src/ProfileMeasurements.jsx')
    const metrics = [
      { key: 'repeat', label: 'Repeat', stack_group: 'Contributions', denominator: 'all GT', unit: 'ratio' },
      { key: 'explore', label: 'Explore', stack_group: 'Contributions', denominator: 'all GT', unit: 'ratio' },
    ]
    globalThis.fetch = fetchStub({ ['GET /api/runs/r/profile-measurements']: { profile: { state: 'configured', profile: { metrics } },
      nodes: [{ node_id: 1, values: [{ value: .3 }, { value: .2 }] }, { node_id: 2, values: [{ value: .1 }, { value: null }] }],
      offset: 0, limit: 10, has_more: false } })
    const view = await h.mount(ProfileMeasurements, { runId: 'r', generation: 'a'.repeat(64) })
    try {
      await click(view.container.querySelector('button'))
      await until(() => view.container.textContent.includes('Components are declared additive'), 'stack')
      const stack = [...view.container.querySelectorAll('figure')].find(f => f.textContent.includes('Components are declared additive'))
      assert.equal(stack.querySelectorAll('rect').length, 2)
      assert.match(stack.textContent, /all GT/)
      assert.match(stack.querySelector('svg').getAttribute('aria-label'), /#1 0\.5/)
      assert.doesNotMatch(stack.querySelector('svg').getAttribute('aria-label'), /#2/)
    } finally { await view.unmount() }
  } finally { await h.close() }
})
