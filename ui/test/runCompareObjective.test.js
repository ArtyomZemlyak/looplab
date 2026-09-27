import assert from 'node:assert/strict'
import test from 'node:test'

import { sharedVite } from './_mount.js'

// doc 68 68.2 (critic 2026-09-27, second pass, mutant UF): only `compareObjective` was tested, not
// that the Compare table's `objective` column prints it — the cell could fall back to the bare
// direction and a retargeted run's best would sit under "max" like any task-metric run's.
test("RunCompare's objective column names the ruler a retargeted run was ranked by", async () => {
  const vite = await sharedVite()
  try {
    const { valueFor } = await vite.ssrLoadModule('/src/RunCompare.jsx')
    const names = { projects: {}, supertasks: {} }
    const cell = run => valueFor('objective', run, null, names, v => String(v))
    assert.equal(cell({ run_id: 'rt', direction: 'max', objective_key: 'filtered' }),
                 'max · ranked by filtered (an operator retarget)')
    assert.equal(cell({ run_id: 'plain', direction: 'min' }), 'min')
  } finally {
    await vite.close()
  }
})
