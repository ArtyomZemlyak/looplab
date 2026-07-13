import test from 'node:test'
import assert from 'node:assert/strict'

import { apiPrefix, get, isReviewLocation, post, reviewReadPath, reviewTokenFromLocation } from '../src/api.js'

const TOKEN = 'rv_0123456789ab_abcdefghijklmnopqrstuvwxyzABCDEFG'

test('review bearer is read only from the fragment, never the HTTP path', () => {
  assert.equal(isReviewLocation({ pathname: '/proxy/review', hash: '' }), true)
  assert.equal(reviewTokenFromLocation({ pathname: '/proxy/review', hash: `#/${TOKEN}` }), TOKEN)
  assert.equal(reviewTokenFromLocation({ pathname: '/proxy/review',
    hash: `#/${TOKEN}?gen=${'a'.repeat(64)}&node=4&tab=code` }), TOKEN)
  assert.equal(reviewTokenFromLocation({ pathname: `/proxy/review/${TOKEN}`, hash: '' }), '')
  assert.equal(reviewTokenFromLocation({ pathname: '/proxy/', hash: `#/${TOKEN}` }), '')
  assert.equal(reviewTokenFromLocation({ pathname: '/proxy/review', hash: `#/${TOKEN}/extra?node=4` }), '')
})

test('review reads use the capability namespace and preserve query parameters', () => {
  const previous = globalThis.location
  globalThis.location = { pathname: '/user/a/proxy/8765/review', hash: `#/${TOKEN}` }
  try {
    assert.equal(apiPrefix(), '/user/a/proxy/8765')
    assert.equal(reviewReadPath('/api/runs/demo/state?seq=29'), '/api/review/state?seq=29')
    assert.equal(reviewReadPath('/api/runs/demo/nodes/4/metrics'), '/api/review/nodes/4/metrics')
    assert.equal(reviewReadPath('/api/settings'), '/api/settings')
  } finally {
    if (previous === undefined) delete globalThis.location
    else globalThis.location = previous
  }
})

test('review pathname never falls back to an owner credential, even with a broken fragment', async () => {
  const previous = {
    location: globalThis.location,
    sessionStorage: globalThis.sessionStorage,
    fetch: globalThis.fetch,
  }
  const calls = []
  globalThis.location = { pathname: '/proxy/review', hash: '#/not-a-capability' }
  globalThis.sessionStorage = { getItem: () => 'owner-secret-that-must-not-cross-the-boundary' }
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, options })
    return { ok: true, json: async () => ({ ok: true }) }
  }
  try {
    assert.equal(reviewReadPath('/api/runs/demo/state'), '/api/review/state')
    await get('/api/review')
    assert.equal(calls.length, 1)
    assert.equal(calls[0].options.headers['X-LoopLab-Token'], undefined)
    assert.equal(calls[0].options.headers['X-LoopLab-Review'], undefined)
    assert.equal(calls[0].options.cache, 'no-store')

    await assert.rejects(post('/api/settings', {}), error => error.code === 'REVIEW_READ_ONLY')
    assert.equal(calls.length, 1, 'a blocked review mutation must not reach fetch')
  } finally {
    for (const [name, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[name]
      else globalThis[name] = value
    }
  }
})

test('valid review bearer takes precedence over stale owner state', async () => {
  const previous = {
    location: globalThis.location,
    sessionStorage: globalThis.sessionStorage,
    fetch: globalThis.fetch,
  }
  let requestOptions
  globalThis.location = { pathname: '/review/', hash: `#/${TOKEN}` }
  globalThis.sessionStorage = { getItem: () => 'stale-owner-token' }
  globalThis.fetch = async (_url, options = {}) => {
    requestOptions = options
    return { ok: true, json: async () => ({ ok: true }) }
  }
  try {
    await get('/api/review')
    assert.equal(requestOptions.headers['X-LoopLab-Review'], TOKEN)
    assert.equal(requestOptions.headers['X-LoopLab-Token'], undefined)
    assert.equal(requestOptions.cache, 'no-store')
  } finally {
    for (const [name, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[name]
      else globalThis[name] = value
    }
  }
})
