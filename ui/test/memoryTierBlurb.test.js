// "I don't understand what Cases are even for" and "there should be captions so I can tell the kinds
// of memory apart" — the Memory panel showed four tabs with counts and nothing else, while the tiers
// really are different things with different writers and different readers.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { MEMORY_TIER_BLURB, memoryTierBlurb } from '../src/conceptShelf.js'

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src')

test('every tab the panel renders has a blurb', () => {
  // The tab list is the contract: a tier added without a caption puts the panel straight back into
  // the state that prompted this.
  const panels = readFileSync(join(SRC, 'panels.jsx'), 'utf8')
  const tabs = panels.slice(panels.indexOf('  const tabs = ['), panels.indexOf('  const selectedResource'))
  for (const tier of ['lessons', 'cases', 'notes', 'knowledge']) {
    assert.ok(tabs.includes(`['${tier}'`), `${tier} is still a tab`)
    assert.ok(memoryTierBlurb(tier).length > 40, `${tier} needs a caption`)
  }
})

test('the Cases caption answers the question that was actually asked', () => {
  // Not "what a case is" in the abstract — why there are ten of them and what reads them.
  const cases = MEMORY_TIER_BLURB.cases
  assert.match(cases, /one row per task/i)
  assert.match(cases, /one per task, not one per experiment/i)
})

test('the Knowledge caption states the distinction from the other three', () => {
  // "How is authoring different from memory?" — the answer is WHO WROTE IT, and it must be explicit.
  assert.match(MEMORY_TIER_BLURB.knowledge, /human/i)
  assert.match(MEMORY_TIER_BLURB.knowledge, /Nothing here is written by a run/i)
})

test('lessons and notes are told apart by what they claim', () => {
  assert.match(MEMORY_TIER_BLURB.lessons, /transferable claim/i)
  assert.match(MEMORY_TIER_BLURB.notes, /not a claim in itself/i)
})

test('an unknown tier renders nothing rather than a wrong caption', () => {
  assert.equal(memoryTierBlurb('nope'), '')
  assert.equal(memoryTierBlurb(undefined), '')
})
