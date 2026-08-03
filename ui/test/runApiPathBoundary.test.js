import test from 'node:test'
import assert from 'node:assert/strict'
import { readdir, readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

import { runApiPath, runNodeApiPath } from '../src/api.js'

const SRC = fileURLToPath(new URL('../src/', import.meta.url))

// `runApiPath` calls itself "one constructor for every owner-style per-run endpoint", and until
// doc 25 UI-11 about fifteen call sites in the same file said otherwise. Every one of them happened
// to encode correctly, so nothing was broken — which is exactly why an unenforced invariant like
// this decays: the next author copies whichever neighbour they land on, and the first inline site
// that forgets `encodeURIComponent` produces a URL that still works for ordinary run ids and
// silently mis-routes for the ones that carry URL syntax.
//
// Run ids are FILESYSTEM names, not slugs. `#` truncates the path at a fragment; a literal `%2F`
// decodes into a second path segment and reaches a different route entirely.

test('a run id carrying URL syntax survives the constructor intact', () => {
  const hostile = 'run#frag/../other%2Fseg?x=1&y=2'
  const path = runApiPath(hostile, '/state')
  assert.equal(path, `/api/runs/${encodeURIComponent(hostile)}/state`)
  assert.equal(path.includes('#'), false, 'a fragment would truncate the request path')
  assert.equal(path.includes('?'), false,
    'the run id must not introduce a query string the server would parse as parameters')
  // ['', 'api', 'runs', <id>, 'state'] — a `/` that survived encoding would add a sixth.
  assert.equal(path.split('/').length, 5, 'the run id must stay ONE path segment')
  assert.equal(runApiPath(hostile, '/comments?limit=5').split('?').length, 2,
    'only the caller-supplied suffix may introduce a query')
})

test('the node constructor keeps the run boundary it composes on', () => {
  assert.equal(runNodeApiPath('a/b', 7, '/trace'), '/api/runs/a%2Fb/nodes/7/trace')
})

test('a non-string run id is coerced before encoding, not after', () => {
  // `encodeURIComponent(undefined)` is the string "undefined" — a REQUEST to a run named
  // "undefined" rather than an obvious failure. Coercing inside the constructor keeps every call
  // site's behaviour identical instead of depending on what the caller happened to pass.
  assert.equal(runApiPath(42, '/state'), '/api/runs/42/state')
})

test('no module builds a per-run path by hand', async () => {
  // The grep guard the finding asked for. `api.js` line 18 IS the constructor, and a comment that
  // merely mentions the URL shape is not a call site.
  const files = (await readdir(SRC)).filter(name => name.endsWith('.js') || name.endsWith('.jsx'))
  const offenders = []
  for (const name of files) {
    const source = await readFile(SRC + name, 'utf8')
    source.split('\n').forEach((line, index) => {
      if (!line.includes('`/api/runs/${')) return
      if (name === 'api.js' && line.includes('${suffix}')) return   // the constructor itself
      offenders.push(`${name}:${index + 1}`)
    })
  }
  assert.deepEqual(offenders, [],
    'a per-run endpoint was built inline instead of through runApiPath/runNodeApiPath')
})

test('the constructor is the only place the run segment is encoded', async () => {
  const api = await readFile(SRC + 'api.js', 'utf8')
  const constructor = api.slice(api.indexOf('export const runApiPath'),
                                api.indexOf('export function isReviewLocation'))
  assert.match(constructor, /encodeURIComponent\(String\(runId\)\)/)
  assert.match(constructor, /encodeURIComponent\(String\(nodeId\)\)/,
    'the node segment must be encoded too — numeric today is not a contract')
})
