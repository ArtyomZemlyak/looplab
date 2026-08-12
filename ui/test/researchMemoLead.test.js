// "Format the deep research properly, it's a wall of text."
//
// It was — and precisely one element was responsible. Everything on the memo card is already
// structured (findings as a list, claims with verdicts, next actions with steer buttons, sources and
// reasoning behind disclosures) EXCEPT the collapsed header, which rendered `summary` verbatim. A
// real memo's summary is ~1,600 characters, measured on the live run, so a list of memos was a stack
// of paragraphs with no scannable line.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { MEMO_LEAD_CHARS, memoLead, memoLeadIsPartial } from '../src/researchMemoModel.js'

// The opening of the real memo this was measured on.
const REAL = 'This run is effectively a fresh start on the ESCI v2 rubert-tiny-lite dense-retrieval '
  + 'task: all 3 nodes (a draft + a debug child + a pending debug grandchild) carry the SAME single '
  + 'idea. The champion recipe is still the 0.8835 DCL+R-Drop result from a sibling run.'

test('the header gets a scannable lead, not the paragraph', () => {
  const lead = memoLead(REAL)
  assert.ok(lead.length <= MEMO_LEAD_CHARS + 1, `lead was ${lead.length} chars`)
  assert.ok(lead.startsWith('This run is effectively a fresh start'))
  assert.ok(!lead.includes('champion recipe'), 'the second sentence is not the headline')
})

test('a lead that had to shorten says so, so truncation never reads as the whole answer', () => {
  assert.equal(memoLeadIsPartial(REAL), true)
  assert.equal(memoLeadIsPartial('Short and complete.'), false)
  assert.equal(memoLeadIsPartial(''), false)
})

test('a decimal or an abbreviation does not end the headline', () => {
  // "0.8835 recall" and "vs. the baseline" both contain a period. Splitting on any period would cut
  // the lead to two words on exactly the memos worth reading.
  const decimals = 'Recall reached 0.8835 on the held-out split which beats the 0.81 baseline clearly'
  assert.equal(memoLead(decimals), decimals)
  const abbrev = 'Mining beat vs. the in-batch baseline by a wide margin on every measured split'
  assert.equal(memoLead(abbrev), abbrev)
})

test('a sentence break only counts before a capital', () => {
  const text = 'The loss diverged after epoch three. Temperature was the decisive factor here.'
  assert.equal(memoLead(text), 'The loss diverged after epoch three.')
})

test('an over-long first sentence is cut on a word boundary, never mid-token', () => {
  const long = `${'alpha '.repeat(60)}omega.`
  const lead = memoLead(long)
  assert.ok(lead.endsWith('…'))
  assert.ok(!/\balph…$/.test(lead), 'the ellipsis landed inside a word')
  assert.ok(lead.length <= MEMO_LEAD_CHARS + 1)
})

test('whitespace and empties degrade to nothing rather than to a blank headline', () => {
  assert.equal(memoLead('   \n\t  '), '')
  assert.equal(memoLead(null), '')
  assert.equal(memoLead(undefined), '')
  assert.equal(memoLead('one\n\ntwo   three'), 'one two three', 'newlines collapse')
})

test('the card shows the lead in the header AND the full conclusion in the body', () => {
  // Without `showSummary` the full text would exist nowhere on the card — the shortening would be a
  // silent loss instead of a layout.
  const source = readFileSync(new URL('../src/ResearchMemoCard.jsx', import.meta.url), 'utf8')
  assert.ok(source.includes('memoLead(value.summary)'))
  assert.ok(source.includes('memoLeadIsPartial(value.summary)'))
  assert.ok(/<ResearchMemoBody memo=\{value\} normalized showSummary/.test(source),
    'the body must carry the full conclusion now that the header does not')
  assert.ok(!source.includes('>{value.summary || \'No conclusion was recorded.\'}</span>'),
    'the verbatim-paragraph header came back')
})
