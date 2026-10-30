import assert from 'node:assert/strict'
import test from 'node:test'
import { fetchStub, jsonResponse, mountLive, until } from './_mount.js'

test('tmpfs without a successful mirror names the durable-folder setting', async () => {
  const harness = await mountLive({ visible: true })
  globalThis.fetch = fetchStub({ '/api/run-mirror': { volatile: true, dst_root: null } })
  try {
    const { default: Banner } = await harness.load('/src/RunMirrorBanner.jsx')
    const view = await harness.mount(Banner)
    await until(() => view.container.textContent.includes('LOOPLAB_RUN_MIRROR_DST'))
    assert.equal(view.container.querySelector('[role="status"]') !== null, true)
  } finally {
    await harness.close()
  }
})

test('a running mirror with a timed-out pass is shown as needing attention', async () => {
  const harness = await mountLive({ visible: true })
  globalThis.fetch = fetchStub({ '/api/run-mirror': {
    volatile: true, dst_root: '/backup', supervisor: { state: 'running' },
    last_pass_finished_at: 1100, last_successful_pass_finished_at: 1000,
    stuck: { detail: 'timeout' }, errors: 0, diagnostic: { state: 'degraded' },
  } })
  try {
    const { default: Banner } = await harness.load('/src/RunMirrorBanner.jsx')
    const view = await harness.mount(Banner)
    await until(() => view.container.textContent.includes('Mirror needs attention.'))
    assert.match(view.container.textContent, /Last successful mirror:/)
  } finally {
    await harness.close()
  }
})
