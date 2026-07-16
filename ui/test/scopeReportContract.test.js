import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const source = readFileSync(new URL('../src/ScopeReport.jsx', import.meta.url), 'utf8')

test('ScopeReport keeps stale and indeterminate comparison authority visible', () => {
  assert.match(source, /!data\.stale && group\?\.indeterminate === null/)
  assert.match(source, /trusted && !data\.stale/)
  assert.match(source, /data\?\.authoritative === true/)
  assert.match(source, /stale snapshot — regenerate/)
  assert.match(source, /tied_winners[\s\S]*tied best/)
  assert.match(source, /incomplete_runs[\s\S]*run incomplete/)
  assert.match(source, /note={status\(item\?\.comparison_status\)/)
  assert.match(source, /contract_authority[\s\S]*unverified/)
  assert.match(source, /typeof value\?\.exists === 'boolean'[\s\S]*!Array\.isArray\(value\.content\)/)
  assert.match(source, /e\?\.status === 400/)
  assert.doesNotMatch(source, /\/400\/\.test\(e\.message\)|'Generation failed: ' \+ e\.message/)
})
