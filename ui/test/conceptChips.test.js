// Unit tests for the View 2 concept-chip model. Run with `node --test test/` from ui/.
import test from 'node:test'
import assert from 'node:assert/strict'
import { chipsAtPath, breadcrumb, conceptMatches, matchingNodeIds } from '../src/conceptChips.js'

const NC = {
  0: ['loss/contrastive/dcl', 'architecture/moe'],
  1: ['loss/contrastive/mnr'],
  2: ['loss/mnr'],
  3: ['data/synth'],
  4: ['loss'],                                     // tagged at a coarse level
}

test('chipsAtPath top level = roots with subtree counts', () => {
  const chips = chipsAtPath(NC, {}, '')
  const byId = Object.fromEntries(chips.map(c => [c.id, c.count]))
  assert.equal(byId['loss'], 4)                    // nodes 0,1,2,4 all touch the loss subtree
  assert.equal(byId['architecture'], 1)
  assert.equal(byId['data'], 1)
  assert.equal(chips[0].id, 'loss')                // sorted by count desc
  assert.equal(chips.find(c => c.id === 'loss').label, 'loss')
})

test('chipsAtPath drills into a concept', () => {
  const chips = chipsAtPath(NC, {}, 'loss')
  const byId = Object.fromEntries(chips.map(c => [c.id, c.count]))
  assert.equal(byId['loss/contrastive'], 2)        // nodes 0,1
  assert.equal(byId['loss/mnr'], 1)                // node 2
  assert.equal(byId['loss/contrastive/dcl'], undefined)   // that's two levels down, not shown here
  // node 4 (tagged exactly "loss") produces no child chip under loss
})

test('chipsAtPath canonicalizes via rename', () => {
  const chips = chipsAtPath({ 0: ['raw'] }, { raw: 'loss/x' }, '')
  assert.equal(chips[0].id, 'loss')
})

test('breadcrumb segments', () => {
  assert.deepEqual(breadcrumb(''), [])
  assert.deepEqual(breadcrumb('loss/contrastive'),
    [{ id: 'loss', label: 'loss' }, { id: 'loss/contrastive', label: 'contrastive' }])
})

test('conceptMatches is prefix-aware OR', () => {
  assert.equal(conceptMatches(['loss/contrastive/dcl'], ['loss']), true)      // ancestor selects descendant
  assert.equal(conceptMatches(['loss/mnr'], ['loss/contrastive']), false)     // sibling, no match
  assert.equal(conceptMatches(['architecture/moe'], ['loss', 'architecture']), true)  // OR
  assert.equal(conceptMatches(['data/synth'], ['loss']), false)
  assert.equal(conceptMatches(['x'], []), true)                               // empty selection = all
})

test('matchingNodeIds highlights the union (OR)', () => {
  const ids = matchingNodeIds(NC, ['loss/contrastive'])
  assert.deepEqual([...ids].sort((a, b) => a - b), [0, 1])
  assert.equal(matchingNodeIds(NC, []), null)                                 // no selection -> null
  const both = matchingNodeIds(NC, ['architecture', 'data'])
  assert.deepEqual([...both].sort((a, b) => a - b), [0, 3])
})
