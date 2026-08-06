// THE MINIFIER IS PART OF THE UI — second verse. `minifierBooleanGuards.test.js` covers the flag
// that made `false` render as `0`; this file covers the one that makes an UNKNOWN number take the
// wrong branch.
//
// `terserOptions.compress.unsafe_comps` lets Terser negate a comparison — rewrite `a <= 0` as
// `a > 0` — so it can swap a ternary's branches and emit fewer bytes. Terser's own documentation
// states the assumption out loud: "assuming none of the operands can be (coerced to) NaN". In this
// UI that assumption is false by construction. `undefined`, a field a projection did not fill, and
// a non-numeric string all coerce to NaN, and "this number is not known" is the normal shape of
// half these payloads — it is the thing the house honesty rules exist for. `a <= 0` and `!(a > 0)`
// disagree for exactly those values, so the SHIPPED build takes the other branch from the source on
// precisely the inputs the branch was written for.
//
// This was found by executing the REAL production pipeline (Rolldown + Terser, the real config) in
// SSR mode over 44 models and 6 components, 183,008 calls, and diffing it against the same tree with
// one flag removed. `unsafe_comps` was the ONLY flag in that block with a behavioural difference —
// `unsafe`, `unsafe_arrows`, `unsafe_methods`, `unsafe_proto`, `unsafe_regexp`, `pure_getters`,
// `keep_fargs` and `hoist_props` each produced zero — and it had 42, in two shipped surfaces:
//
//   traceProjection.conversationWindowNotice({visibleTurns: 5})
//     source  "Showing the most recent 5 of undefined steps."
//     shipped "Trace projection is partial."
//   runMapModel.gridColumns(undefined)          source NaN     shipped 1
//
// Both of those flips happened to land on the tidier text. That is luck, not safety: the direction
// is decided by which branch the SOURCE put first, so a guard written `known ? show : withhold`
// flips the other way and PRINTS a number the source withheld. The probes below carry both
// spellings so the file cannot be read as "the flag is fine when it helps".
//
// Every assertion is paired with a control arm that forces the option back on. A green test whose
// control does not go red is not evidence.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { dirname, join } from 'node:path'
import { minify } from 'terser'

const UI_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')

// The exact shape of `traceProjection.js::conversationWindowNotice` — the withhold-first spelling,
// which is how every "we could not measure this" guard in this tree is written. Plain ES, no JSX,
// so the test needs no transform of its own.
const WITHHOLD_FIRST = `
export const notice = view => (view.visibleTurns <= 0 || view.totalTurns <= 0
  ? 'Trace projection is partial.'
  : \`Showing the most recent \${view.visibleTurns} of \${view.totalTurns} steps.\`)
`

// The mirror spelling: the SHOW branch first. Same rewrite, opposite consequence — this is the one
// where a flipped branch prints a number nobody measured.
const SHOW_FIRST = `
export const line = row => (row.runs > 0
  ? \`best \${row.value} over \${row.runs} run(s)\`
  : 'not measured')
`

const terserOptionsFromViteConfig = async () => {
  const config = (await import(pathToFileURL(join(UI_ROOT, 'vite.config.js')).href)).default
  const options = config?.build?.terserOptions
  assert.ok(options?.compress, 'vite.config.js must still configure terser compression')
  return options
}

const loadMinified = async (source, options) => {
  const { code } = await minify(source, options)
  const mod = await import('data:text/javascript;base64,' + Buffer.from(code, 'utf8').toString('base64'))
  return { code, mod }
}

// The unminified source is the reference: what the author wrote is what must reach the operator.
const loadSource = async source =>
  import('data:text/javascript;base64,' + Buffer.from(source, 'utf8').toString('base64'))

// A payload whose `totalTurns` never arrived. Not a contrived value — a projection that could not
// count its own totals leaves exactly this, and it is why the guard is there.
const UNKNOWN_TOTAL = { visibleTurns: 5, totalTurns: undefined }
const UNKNOWN_RUNS = { runs: undefined, value: 0.94 }

test('the shipped minifier settings leave an unknown count on the same branch the source chose', async () => {
  const options = await terserOptionsFromViteConfig()
  const built = await loadMinified(WITHHOLD_FIRST, options)
  const source = await loadSource(WITHHOLD_FIRST)

  // The property is agreement with the SOURCE, not the prettiness of either string. A minifier that
  // improves the message is still a minifier that decided the message.
  assert.equal(built.mod.notice(UNKNOWN_TOTAL), source.notice(UNKNOWN_TOTAL),
    `the built module took a different branch than the source for an unknown count: ${built.code}`)
  // And the known case must be untouched, or the test would pass by breaking everything equally.
  const known = { visibleTurns: 5, totalTurns: 12 }
  assert.equal(built.mod.notice(known), 'Showing the most recent 5 of 12 steps.')
  assert.equal(built.mod.notice({ visibleTurns: 0, totalTurns: 12 }), 'Trace projection is partial.')
})

test('an unknown count cannot be minified INTO a printed number', async () => {
  const options = await terserOptionsFromViteConfig()
  const built = await loadMinified(SHOW_FIRST, options)
  const source = await loadSource(SHOW_FIRST)

  assert.equal(built.mod.line(UNKNOWN_RUNS), 'not measured')
  assert.equal(built.mod.line(UNKNOWN_RUNS), source.line(UNKNOWN_RUNS))
  assert.equal(built.mod.line({ runs: 3, value: 0.94 }), 'best 0.94 over 3 run(s)')
})

test('CONTROL: with unsafe_comps back on, the withhold-first guard flips branch on an unknown count', async () => {
  const options = await terserOptionsFromViteConfig()
  const forced = { ...options, compress: { ...options.compress, unsafe_comps: true } }
  const built = await loadMinified(WITHHOLD_FIRST, forced)
  const source = await loadSource(WITHHOLD_FIRST)

  // The control arm must actually reproduce the rewrite, or the two tests above prove nothing.
  assert.match(built.code, /visibleTurns>0/,
    `the control arm must actually negate the comparison: ${built.code}`)
  assert.notEqual(built.mod.notice(UNKNOWN_TOTAL), source.notice(UNKNOWN_TOTAL))
  assert.equal(source.notice(UNKNOWN_TOTAL), 'Showing the most recent 5 of undefined steps.')
  assert.equal(built.mod.notice(UNKNOWN_TOTAL), 'Trace projection is partial.')
  // Both spellings still agree when the number IS known — which is exactly why nothing caught this.
  assert.equal(built.mod.notice({ visibleTurns: 5, totalTurns: 12 }),
    source.notice({ visibleTurns: 5, totalTurns: 12 }))
})

// The two REAL surfaces the differential caught, driven through the same pipeline. These are the
// bytes an operator was actually served, not an analogy.
test('the two shipped surfaces the rewrite reached agree with their source again', async () => {
  const options = await terserOptionsFromViteConfig()
  const forced = { ...options, compress: { ...options.compress, unsafe_comps: true } }

  // `runMapModel.js::gridColumns` — the Lineage grid packer's column count.
  const GRID = `
export function gridColumns(count, max = 8) {
  if (count <= 1) return 1
  return Math.min(max, Math.max(2, Math.ceil(Math.sqrt(count * 1.5))))
}
`
  const source = await loadSource(GRID)
  const built = await loadMinified(GRID, options)
  const control = await loadMinified(GRID, forced)

  assert.equal(source.gridColumns(3), 3)
  assert.equal(built.mod.gridColumns(3), 3)
  // A count that never arrived is NaN in the source and must stay NaN in the build: `1` is a
  // fabricated answer, and a packer that silently claims one column is how a Lineage canvas ends up
  // stacking every run in a single line with nothing saying the count was unknown.
  assert.ok(Number.isNaN(source.gridColumns(undefined)))
  assert.ok(Number.isNaN(built.mod.gridColumns(undefined)),
    `the built packer invented a column count: ${built.code}`)
  assert.equal(control.mod.gridColumns(undefined), 1)   // control: the defect returns
})
