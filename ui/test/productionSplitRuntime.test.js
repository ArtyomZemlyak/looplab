import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { build } from 'vite'
import config from '../vite.config.js'
import { installDom } from './_mount.js'

const root = fileURLToPath(new URL('..', import.meta.url))

// SSR removes the client split graph. A native import of the shipped chunks
// catches initialization-order/TDZ failures hidden by dev's module runner.
test('all production client chunks initialize without starting API work', async () => {
  installDom({ visible: true })
  const directory = await mkdtemp(join(root, 'test/.split-runtime-'))
  const previousFetch = globalThis.fetch
  const calls = []
  try {
    await build({ ...config, root, configFile: false, logLevel: 'silent',
      build: { ...config.build, outDir: directory, reportCompressedSize: false } })
    const manifest = JSON.parse(await readFile(join(directory, '.vite/manifest.json'), 'utf8'))
    const bootstrap = manifest['index.html']?.file
    assert.ok(bootstrap, 'exercise the real client manifest')
    const chunks = [...new Set(Object.values(manifest).map(row => row.file))]
      .filter(file => file.endsWith('.js') && file !== bootstrap)
    assert.ok(chunks.length > 20, 'exercise the split client, not a single SSR bundle')
    globalThis.fetch = async (...args) => {
      calls.push(args)
      // icons.jsx intentionally reads its public SVG sprite at initialization.
      // Serve only that built asset; any API/other network request is a failure.
      const path = fileURLToPath(String(args[0]))
      assert.ok(path.startsWith(join(directory, 'assets') + '/') || path.startsWith(join(directory, 'assets') + '\\'))
      assert.match(path, /looplab-icons-v1-[^/\\]+\.svg$/)
      return new Response(await readFile(path), { headers: { 'Content-Type': 'image/svg+xml' } })
    }
    for (const file of chunks) await import(pathToFileURL(join(directory, file)).href)
    assert.ok(calls.every(([url]) => /\/assets\/looplab-icons-v1-[^/]+\.svg$/.test(String(url))))
    assert.equal(document.querySelectorAll('[role="dialog"]').length, 0)
  } finally {
    globalThis.fetch = previousFetch
    assert.ok(resolve(directory).startsWith(resolve(root, 'test/.split-runtime-')))
    await rm(directory, { recursive: true, force: true })
  }
})
