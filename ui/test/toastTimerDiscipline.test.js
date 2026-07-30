import test from 'node:test'
import assert from 'node:assert/strict'
import { readdir, readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src/', import.meta.url))

// Every toast in this app is a self-hiding banner armed with setTimeout. Arm one without clearing
// the previous timer and the SECOND toast is hidden early by the FIRST one's countdown — the user
// sees a confirmation flash and vanish, or misses it entirely. Settings.jsx shipped exactly that
// while RunView.jsx carried the fix and a comment explaining it, so this is a checked invariant
// rather than a convention: a new toast site that forgets is a failing test, not a silent bug.
test('every toast timer is cleared before it is re-armed and torn down on unmount', async () => {
  const files = (await readdir(SRC)).filter(name => name.endsWith('.jsx'))
  const sites = []
  for (const name of files) {
    const source = await readFile(SRC + name, 'utf8')
    // `<ref>.current = setTimeout(... setToast(null) ...)` — the arming site and the ref holding it.
    for (const match of source.matchAll(/(\w+)\.current = setTimeout\(\([^)]*\) =>[^\n]*setToast\(null\)/g)) {
      sites.push({ name, source, ref: match[1] })
    }
    // A bare `setTimeout(() => setToast(null), …)` keeps no handle at all, so it can never be
    // cleared. That is the exact shape this test exists to reject.
    assert.equal(/(?<!= )setTimeout\(\(\) => setToast\(null\)/.test(source), false,
      `${name}: toast timer is armed without keeping its handle, so it can never be cleared`)
  }
  assert.ok(sites.length >= 2, 'expected the known toast sites to be found')
  for (const { name, source, ref } of sites) {
    assert.ok(source.includes(`clearTimeout(${ref}.current)`),
      `${name}: ${ref} is armed but never cleared — a second toast would inherit the first's timer`)
  }
})
