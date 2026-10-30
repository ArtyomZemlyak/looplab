import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { fetchStub, mountLive, until, click } from './_mount.js'

test('profile edit sends expected digest and preserves draft on competing change', async () => {
  const h = await mountLive()
  try {
    const { default: Editor } = await h.load('/src/ProjectMetricProfileEditor.jsx')
    let body
    globalThis.fetch = fetchStub({ 'PUT /api/projects/p1/metric-profile': ({ init }) => {
      body = JSON.parse(init.body)
      return new Response(JSON.stringify({ detail: { code: 'metric_profile_changed' } }),
        { status: 409, headers: { 'content-type': 'application/json' } })
    } })
    const context = { project_id: 'p1', digest: 'a'.repeat(64), profile: { name: 'Original',
      metrics: [{ key: 'IoU', label: 'IoU', role: 'primary', unit: '', denominator: 'pixels' }], slices: [], notes: '' } }
    const view = await h.mount(Editor, { context, onSaved: () => assert.fail('conflict cannot save') })
    try {
      const input = view.container.querySelector('input')
      await React.act(async () => {
        Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(input, 'My draft')
        input.dispatchEvent(new window.Event('input', { bubbles: true }))
      })
      await click([...view.container.querySelectorAll('button')].find(b => b.textContent === 'Save profile'))
      await until(() => view.container.textContent.includes('changed elsewhere'), 'conflict')
      assert.equal(body.expected_digest, 'a'.repeat(64))
      assert.equal(body.profile.name, 'My draft')
      assert.equal(input.value, 'My draft')
      assert.equal([...view.container.querySelectorAll('button')].find(b => b.textContent === 'Save profile').disabled, true)
    } finally { await view.unmount() }
  } finally { await h.close() }
})
