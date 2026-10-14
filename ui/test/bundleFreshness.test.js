import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { bundleIsStale, buildIdFromHtml, currentBuildId, indexUrl } from '../src/bundleFreshness.js'

test('the served build id is read from index.html and compared only when both are known', () => {
  const html = '<!doctype html><html><head>\n    <meta name="looplab-build" content="0123456789abcdef" />'
  assert.equal(buildIdFromHtml(html), '0123456789abcdef')
  assert.equal(buildIdFromHtml('<html><head></head>'), '')
  assert.equal(buildIdFromHtml('<meta name="looplab-build" content="<script>">'), '')
  assert.equal(bundleIsStale('0123456789abcdef', 'fedcba9876543210'), true)
  assert.equal(bundleIsStale('0123456789abcdef', '0123456789abcdef'), false)
  assert.equal(bundleIsStale('', 'fedcba9876543210'), false)   // dev server / tests: no claim
  assert.equal(bundleIsStale('0123456789abcdef', ''), false)   // old server or failed read
  assert.equal(currentBuildId(), '')                           // no define outside a build
})

test('index.html is read from the page directory, proxy prefixes included', () => {
  assert.equal(indexUrl({ pathname: '/' }), '/')
  assert.equal(indexUrl({ pathname: '/user/a/proxy/8794/' }), '/user/a/proxy/8794/')
  assert.equal(indexUrl({ pathname: '/user/a/proxy/8794/index.html' }), '/user/a/proxy/8794/')
  assert.equal(indexUrl({ pathname: '/review' }), '/')
})

test('the build stamps the same id into the bundle and the page', () => {
  const config = readFileSync(new URL('../vite.config.js', import.meta.url), 'utf8')
  assert.match(config, /__LOOPLAB_BUILD_ID__: JSON\.stringify\(BUILD_ID\)/)
  assert.match(config, /<meta name="looplab-build" content="\$\{BUILD_ID\}"/)
})
