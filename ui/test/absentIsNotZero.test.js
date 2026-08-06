// ABSENT IS NOT ZERO, and ZERO IS NOT ABSENT — the two directions of one defect class.
//
// The Card board's `||`-chain zero (120 stray `0`s across 46 real runs) was the loudest instance, but
// the same confusion appears wherever a falsy test stands in for a "do we have this number?" test.
// Two shapes, both fixed together and pinned here:
//
//   * `{epochSeconds && <div/>}` — the falsy value is a REAL instant (1970-01-01T00:00:00Z), so React
//     renders a bare `0` where the element should be.
//   * `{count || 'Multiple'}` / `{count ?? 0}` — a recorded 0 is rewritten into reassuring prose, or
//     an ABSENT count is rewritten into the hard claim "0". The second is the worse one: it asserts
//     something the missing key cannot support, and it did so two lines below `costPricing`, which
//     declines to assert exactly that about the same object.
//
// Driven against the shipped source rather than a re-implementation: these are one-line guards, and a
// test that re-typed them would pass against either version. `sourceOf` reads the real file and the
// assertions describe what the guard must NOT be.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src')
const sourceOf = name => readFileSync(join(SRC, name), 'utf8')

test('epoch-second guards test for a NUMBER, so 1970 renders the element and not a bare 0', () => {
  const runList = sourceOf('RunList.jsx')
  assert.ok(runList.includes("typeof r.mtime === 'number' && <div className=\"muted run-when\""),
    'RunList run-when must be number-guarded; `r.mtime && …` renders `0` at the epoch')
  assert.ok(!runList.includes('{r.mtime && <div className="muted run-when"'),
    'the falsy guard came back')

  const shared = sourceOf('SharedAssistant.jsx')
  assert.ok(shared.includes("typeof expiry === 'number' && <span className=\"muted\">Expires"),
    'the share-expiry line must be number-guarded')
  assert.ok(!shared.includes('{expiry && <span className="muted">Expires'),
    'the falsy guard came back')
})

test('a RECORDED seed count is printed, including zero — only an absent one becomes prose', () => {
  for (const [file, needle] of [
    ['Inspector.jsx', "typeof n.confirmed_seeds === 'number' ? n.confirmed_seeds : 'Multiple'"],
    ['panels.jsx', "typeof robust.confirmed_seeds === 'number' ? robust.confirmed_seeds : 'Multiple'"],
  ]) {
    const src = sourceOf(file)
    assert.ok(src.includes(needle), `${file}: a recorded 0 must print as 0, not as "Multiple"`)
  }
  // `confirmed_seeds: 0` beside a `confirmed_mean` is a CONTRADICTION; the point of the change is
  // that the operator sees it. `||` hid it behind the most reassuring word on the panel.
  assert.ok(!sourceOf('Inspector.jsx').includes("n.confirmed_seeds || 'Multiple'"))
  assert.ok(!sourceOf('panels.jsx').includes("robust.confirmed_seeds || 'Multiple'"))
  // The same rule for the sample-length fallback: only an ABSENT count may fall through to it.
  assert.ok(!sourceOf('Inspector.jsx').includes('n.confirmed_seeds || vals.length'))
})

test('the priced-call row does not assert a zero its own verdict declines to assert', () => {
  const src = sourceOf('Inspector.jsx')
  assert.ok(!src.includes('c.priced_calls ?? 0'),
    'an absent priced_calls rendered as "0 of N" contradicts costPricing on the same object')
  assert.ok(src.includes("fmtInt(typeof c.priced_calls === 'number' ? c.priced_calls : null)"),
    'absent must reach fmtInt as null so it renders an em dash, not a claim')
})
