import test from 'node:test'
import assert from 'node:assert/strict'
import { readdir } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

// The generalization of `assistantBarResourceTruth.test.js`'s "AssistantBar is still a module that
// compiles". That guard was written after a dropped brace left `vite build` refusing the tree while
// all 767 tests passed — but it was scoped to the ONE file the near-miss happened in, so the hole it
// documents stayed open everywhere else. It reopened: a merge resolution put a `{/* … */}` comment
// between `return (` and the root element in panels.jsx, and the only reason the suite noticed is
// that other tests happen to import that file. 17 of the 134 modules under src/ are imported by no
// test at all, and for those a parse error is invisible until someone runs `npm run build`.
//
// Parsing is the property, not mounting: `transformRequest` runs the JSX transform and stops, so
// this stays cheap and needs no DOM, no storage and no fake timers. A module that cannot parse
// cannot be built, whatever its runtime behaviour would have been.
const SRC = new URL('../src/', import.meta.url)

const sourceModules = async () => {
  const entries = await readdir(fileURLToPath(SRC), { withFileTypes: true, recursive: true })
  return entries
    .filter(entry => entry.isFile() && /\.jsx?$/.test(entry.name))
    .map(entry => {
      const dir = entry.parentPath ?? entry.path
      const rel = dir.slice(fileURLToPath(SRC).length)
      return '/src/' + (rel ? rel.replaceAll('\\', '/') + '/' : '') + entry.name
    })
    .sort()
}

test('every module under src/ still parses', async () => {
  const modules = await sourceModules()
  // A guard that scanned nothing would pass silently; the tree has ~134 modules today.
  assert.ok(modules.length > 100, `expected the src/ sweep to find the tree, got ${modules.length}`)

  const vite = await createServer({
    root: fileURLToPath(new URL('..', import.meta.url)),
    configFile: false, appType: 'custom', logLevel: 'silent', server: { middlewareMode: true },
  })
  try {
    // Transform bounded batches concurrently. The old fully-serial sweep took ~22s in isolation and
    // crossed the suite's 30s deadline under ordinary parallel test load; that made a parse guard
    // flaky without finding a parse defect. Sixteen keeps pressure bounded while preserving coverage.
    const broken = []
    for (let offset = 0; offset < modules.length; offset += 16) {
      const batch = await Promise.all(modules.slice(offset, offset + 16).map(async url => {
        try {
          const result = await vite.transformRequest(url, { ssr: true })
          return result === null ? `${url}: vite resolved nothing for this module` : null
        } catch (error) {
          return `${url}: ${error?.message ?? error}`
        }
      }))
      broken.push(...batch.filter(Boolean))
    }
    assert.deepEqual(broken, [], `modules that do not parse:\n${broken.join('\n\n')}`)
  } finally {
    await vite.close()
  }
})
