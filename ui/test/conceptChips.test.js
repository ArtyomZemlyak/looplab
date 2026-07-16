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
})

test('chipsAtPath surfaces level-exact tags as a trailing "here" chip', () => {
  const chips = chipsAtPath(NC, {}, 'loss')
  // node 4 (tagged exactly "loss") has no next-level child -> it must not vanish; it becomes the
  // trailing atLevel chip so the user sees where the remainder of the parent count went.
  const here = chips.find(c => c.atLevel)
  assert.ok(here, 'expected an atLevel chip after drilling into loss')
  assert.equal(here.id, 'loss')
  assert.equal(here.label, 'loss')
  assert.equal(here.count, 1)                       // node 4 only
  assert.equal(chips[chips.length - 1], here)       // rendered last (child chips first)
  // top level has no exact-at-root tags -> no atLevel chip there
  assert.equal(chipsAtPath(NC, {}, '').some(c => c.atLevel), false)
})

test('chipsAtPath canonicalizes via rename', () => {
  const chips = chipsAtPath({ 0: ['raw'] }, { raw: 'loss/x' }, '')
  assert.equal(chips[0].id, 'loss')
})

test('chipsAtPath survives prototype-key tags', () => {
  // LLM-authored ids can collide with Object.prototype keys — must not read the chain or crash.
  const chips = chipsAtPath({ 0: ['__proto__/x'], 1: ['constructor'], 2: ['loss/a'] }, {}, '')
  const byId = Object.fromEntries(chips.map(c => [c.id, c.count]))
  assert.equal(byId['__proto__'], 1)
  assert.equal(byId['constructor'], 1)
  assert.equal(byId['loss'], 1)
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
