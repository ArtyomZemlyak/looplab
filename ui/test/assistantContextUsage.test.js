// "The context in the assistant is counted strangely — 20k, then 50k, then 25k, then 18k in one
// conversation."
//
// The accounting was right and the LABEL was wrong. The chip said "tokens in the assistant's context
// (grows with the conversation)", a promise the number cannot keep for two honest reasons: it shows
// the last turn's peak single prompt (a forty-tool-call turn carries more than the one-liner after
// it), and the agent loop COMPACTS the history when it outgrows the budget, so the context really
// does shrink — the loop working, presented as a fault.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { contextChipTitle, contextUsage } from '../src/assistantChromeModel.js'

const turn = (context, total) => ({ tokens: { context, total } })

test('the sequence the operator saw is reported as last + peak, not as a mystery', () => {
  const usage = contextUsage([turn(20000, 25000), turn(50000, 90000), turn(25000, 30000),
                              turn(18000, 20000)])
  assert.equal(usage.last, 18000, 'what the last turn actually carried')
  assert.equal(usage.peak, 50000, 'the monotonic number the old label was describing')
  assert.equal(usage.spent, 165000)
  assert.equal(usage.compacted, true)
})

test('`prompt` is never read as a window number', () => {
  // `prompt` SUMS the same context re-sent by every call in a turn — O(calls²) and billed. Using it
  // as "how full is the window" is what made a tool-heavy turn look like a 50k context.
  const usage = contextUsage([{ tokens: { prompt: 900000, total: 900000 } }])
  assert.equal(usage.last, 0)
  assert.equal(usage.peak, 0)
  assert.equal(usage.spent, 900000, 'it is still a real cost number')
})

test('a chat that only grew is not reported as compacted', () => {
  const usage = contextUsage([turn(1000, 1000), turn(5000, 6000), turn(9000, 9000)])
  assert.equal(usage.compacted, false)
  assert.equal(usage.last, 9000)
  assert.equal(usage.peak, 9000)
})

test('the tooltip explains a DROP instead of leaving it to be read as a fault', () => {
  const title = contextChipTitle(contextUsage([turn(50000, 50000), turn(18000, 18000)]))
  assert.match(title, /carried by the last turn/)
  assert.match(title, /compacted the history/)
  assert.match(title, /not a fault/)
  assert.match(title, /billed across this chat/)
})

test('an empty or junk chat yields no chip rather than a zero', () => {
  assert.deepEqual(contextUsage([]), { last: 0, peak: 0, spent: 0, compacted: false })
  assert.equal(contextChipTitle(contextUsage([])), '')
  const junk = contextUsage([{ tokens: { context: 'lots', total: null } }, {}, null])
  assert.equal(junk.last, 0)
  assert.equal(junk.spent, 0)
})

test('the chip reads the model rather than re-deriving the numbers inline', async () => {
  const { readFileSync } = await import('node:fs')
  const source = readFileSync(new URL('../src/AssistantBar.jsx', import.meta.url), 'utf8')
  assert.ok(source.includes('contextUsage(msgs)'))
  assert.ok(!source.includes("m.tokens?.context || m.tokens?.prompt"),
    'the prompt-as-window fallback came back')
  assert.ok(!source.includes('grows with the conversation'), 'the promise the number cannot keep')
})
