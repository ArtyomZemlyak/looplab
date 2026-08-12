// The two card-trace fetches, driven against the REAL `deadlineGet` contract.
//
// Found by review, not by the suite: `deadlineGet` returns a HANDLE — `{controller, promise,
// timedOut}` — and both new call sites did `deadlineGet(...).then(...)`. That throws the moment the
// effect runs, so the card's Trace section and the node's research disclosure would both have been
// dead on arrival. Nothing caught it: no test mounts either component, the models beneath them are
// tested in isolation, and `vite build` compiles a runtime type error happily.
//
// So this pins the SHAPE both sites depend on, and that each site reaches `.promise` and aborts
// through `.controller` — the two facts a mounted test would have caught and a source read would not
// have thought to check.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { createServer } from 'vite'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src')

const vite = await createServer({
  root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
  server: { middlewareMode: true },
})
const { deadlineRequest } = await vite.ssrLoadModule('/src/requestDeadline.js')
test.after(() => vite.close())

test('deadlineGet-shaped reads return a HANDLE, and a handle is not thenable', async () => {
  const handle = deadlineRequest(() => new Promise(() => {}), 50)
  assert.equal(typeof handle.then, 'undefined',
    'if this ever becomes thenable, the call sites below may go back to awaiting it directly')
  assert.ok(handle.promise instanceof Promise)
  assert.equal(typeof handle.controller.abort, 'function')
  // Claim the rejection before aborting: an abandoned rejected promise is an unhandled rejection,
  // which node:test reports as a FILE-level failure with three green subtests above it.
  const settled = handle.promise.then(() => 'resolved', () => 'rejected')
  handle.controller.abort()
  assert.equal(await settled, 'rejected')
})

for (const [file, label] of [['CardBoard.jsx', "the card's Trace section"],
                             ['Inspector.jsx', "the node's research disclosure"]]) {
  test(`${label} reads .promise and aborts through .controller`, () => {
    const source = readFileSync(join(SRC, file), 'utf8')
    const at = source.indexOf('/cards/${encodeURIComponent(cardId)}/trace')
    assert.ok(at > 0, `${file} no longer fetches a card trace`)
    const block = source.slice(at, at + 700)
    assert.ok(/request\.promise/.test(block), 'the handle must be awaited through .promise')
    assert.ok(/request\.controller\.abort\(\)/.test(block),
      'the effect must abort its own request on unmount, or a closed panel keeps fetching')
    assert.ok(!/\/trace`\s*\)?\s*,?\s*[^)]*\)\s*\n?\s*\.then/.test(block),
      'the .then-on-the-handle shape came back')
  })
}
