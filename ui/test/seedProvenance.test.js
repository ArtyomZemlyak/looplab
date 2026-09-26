import assert from 'node:assert/strict'
import test from 'node:test'

import { SEED_CONTRACT_VERDICTS, seedContractText, seedSource } from '../src/seedProvenance.js'

test('a sibling source links by run id; a launch seed from outside the runs root names its directory', () => {
  assert.deepEqual(seedSource({ run_id: 'src1', node_id: 6 }), { name: 'src1', linkable: true, nodeId: 6 })
  assert.deepEqual(
    seedSource({ seed_from_run: true, run_dir: '/data/archive/e5-v12', node_id: 3 }),
    { name: 'e5-v12', linkable: false, nodeId: 3, dir: '/data/archive/e5-v12' })
  assert.equal(seedSource({ seed_from_run: true, node_id: 3 }), null, 'no run id and no directory')
  assert.equal(seedSource({ run_dir: '/x', node_id: 3 }), null, 'a directory alone is not a seed receipt')
  assert.equal(seedSource(null), null)
})

test('the contract verdict is shown with its sentence, and an absent verdict is not "same"', () => {
  assert.deepEqual(SEED_CONTRACT_VERDICTS, ['same', 'different', 'unknown'])
  assert.equal(seedContractText({ eval_contract: 'same' }), ' · evaluation contract: same')
  assert.equal(
    seedContractText({ eval_contract: 'different', eval_contract_note: ' DIFFERENT DIRECTION: … ' }),
    ' · evaluation contract: different — DIFFERENT DIRECTION: …')
  assert.equal(seedContractText({ run_id: 'src1' }), '', 'the server import records no verdict')
  assert.equal(seedContractText({ eval_contract: 'maybe' }), '', 'an unknown word is not rendered')
})
