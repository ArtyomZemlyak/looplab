// `_mount.js::sharedVite` — one Vite server per test file (review 2026-09-22, UI-05) — keeps the
// isolation a server per test used to give: every caller gets FRESH module instances, so no test
// sees module state another test left, while the transforms are shared. A "tidier" version that
// reused one runner would still pass every converted file today and leak module state between
// their tests tomorrow, so the guarantee is driven here, on a module that HOLDS state:
// `runMode.js` keeps the published run-access envelope in a module-level variable.
import test from 'node:test'
import assert from 'node:assert/strict'

import { sharedVite } from './_mount.js'

test('each sharedVite() caller gets fresh module instances over one shared server', async () => {
  const first = await sharedVite()
  const second = await sharedVite()
  try {
    const a = await first.ssrLoadModule('/src/runMode.js')
    const again = await first.ssrLoadModule('/src/runMode.js')
    const b = await second.ssrLoadModule('/src/runMode.js')
    assert.equal(again, a, 'one caller loads one instance, as one server did')
    assert.notEqual(b, a, 'a second caller gets its own instance, as a new server did')

    // Module STATE, not just identity: what one caller's instance published, the other's never saw.
    a.setRunAccess('run-a', { readOnly: true, seq: 7 })
    assert.deepEqual(a.getRunAccess('run-a'), { readOnly: true, seq: 7, mode: 'history' })
    assert.deepEqual(b.getRunAccess('run-a'), { readOnly: false, seq: null, mode: 'live' },
      'the second instance holds no state the first one wrote')

    // And the transforms are the server's, shared: one module graph behind both callers.
    assert.equal(second.environments, first.environments)
    assert.ok([...first.environments.ssr.moduleGraph.idToModuleMap.keys()]
      .some(id => id.endsWith('/src/runMode.js')), 'the module was transformed by the server')
  } finally {
    await first.close()
    await second.close()
  }
})
