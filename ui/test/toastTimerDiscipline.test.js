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
//
// The three copies are now one hook (doc 25 UI-13), so the invariant is stronger than "each site
// remembers": there is ONE arming site, it lives in `useToast.js`, and no surface may grow a second.
test('exactly one toast timer exists, and it is cleared before re-arming', async () => {
  const files = (await readdir(SRC)).filter(name => name.endsWith('.jsx') || name.endsWith('.js'))
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
  assert.deepEqual(sites.map(site => site.name), ['useToast.js'],
    'a surface re-grew its own toast timer instead of using the shared hook')
  for (const { name, source, ref } of sites) {
    // The clear must live in the SAME function body as the arm, not merely somewhere in the file.
    // Two weaker spellings both passed while the reset was missing: a file-wide `includes` (the
    // early-retire path also calls clearTimeout) and a bounded forward match (the unmount teardown
    // sits a few lines above the arm). Walk back to the enclosing body and look there.
    const arm = source.indexOf(`${ref}.current = setTimeout`)
    let depth = 0
    let start = arm
    while (start > 0) {
      const char = source[--start]
      if (char === '}') depth += 1
      else if (char === '{') { if (depth === 0) break; depth -= 1 }
    }
    const body = source.slice(start, arm)
    assert.ok(body.includes(`clearTimeout(${ref}.current)`),
      `${name}: ${ref} is armed without first clearing the pending timer in the same function — ` +
      "a second toast would inherit the first's countdown")
  }
})

test('the shared hook does not clear a toast after unmount', async () => {
  // The second guard the copies disagreed about: a 5-second timer outlives a closed drawer or a
  // route change. RunView's copy had no unmount teardown at all; the Assistant bar's had one but
  // spelled it inside an unrelated effect. Firing into a dead tree is a React warning at best.
  const source = await readFile(SRC + 'useToast.js', 'utf8')
  assert.match(source, /useEffect\(\(\) => \(\) => \{[\s\S]*?mounted\.current = false[\s\S]*?clearTimeout\(timer\.current\)/,
    'the hook must both mark itself unmounted and drop the pending timer')
  assert.match(source, /setTimeout\(\(\) => \{ if \(mounted\.current\) setToast\(null\) \}/,
    'the fire path must re-check mounted — teardown alone loses an in-flight callback')
})

test('every toast surface takes its timer from the hook', async () => {
  // Named explicitly rather than derived, because the failure this prevents is a NEW surface
  // hand-rolling a fourth copy: the list is what a reviewer compares a new toast against.
  for (const name of ['RunView.jsx', 'AssistantBar.jsx', 'Settings.jsx']) {
    const source = await readFile(SRC + name, 'utf8')
    assert.match(source, /import \{ useToast \} from '\.\/useToast\.js'/, `${name} lost the hook`)
    assert.equal(/useState\(null\)\s*$/m.test(source.split('\n').filter(
      line => line.includes('toast') && line.includes('useState')).join('\n')), false,
      `${name} re-declared its own toast state`)
  }
})
