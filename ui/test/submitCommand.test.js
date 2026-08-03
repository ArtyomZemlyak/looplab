// One command submitter behind every panel button (doc 25 UI-07).
//
// A dozen panels wrote the same try/await/commandFeedback/onToast/catch-onToast block. The failure a
// drifted copy produces is silence: no toast at all, which an operator reads as "it worked". These
// pin the four properties the copies encoded, so a future call site inherits them instead of
// re-deriving them:
//
//   * exactly one toast, on EVERY outcome including a transport throw;
//   * the transport arm is an `error` feedback, never a success — otherwise a network failure clears
//     the operator's draft or leaves an optimistic strike-through in place;
//   * `labels.transport` is honoured for the surfaces that deliberately withhold the thrown text;
//   * the feedback is RETURNED, because success-gated side effects and optimistic rollbacks live at
//     the call site.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { submitCommand } from '../src/api.js'

const LABELS = {
  success: 'Card added', noop: 'That hypothesis was already tracked',
  executing: 'Card requested — waiting for the run', failure: 'Could not add Card',
}
const src = name => readFileSync(fileURLToPath(new URL(`../src/${name}`, import.meta.url)), 'utf8')

const collect = () => { const seen = []; return [seen, m => seen.push(m)] }

test('a succeeded record toasts the success label once and reports success', async () => {
  const [seen, onToast] = collect()
  const feedback = await submitCommand(Promise.resolve({ status: 'succeeded' }), LABELS, onToast)
  assert.equal(feedback.kind, 'success')
  assert.deepEqual(seen, ['Card added'])
})

test('a noop is a success outcome, so a success-gated draft clear still fires', async () => {
  // The whole reason `noop` is not an error: the postcondition already holds. A call site that
  // treated it as failure would leave the operator's input populated after a command that worked.
  const [seen, onToast] = collect()
  const feedback = await submitCommand(Promise.resolve({ status: 'noop' }), LABELS, onToast)
  assert.equal(feedback.kind, 'success')
  assert.deepEqual(seen, ['That hypothesis was already tracked'])
})

test('an executing record is PENDING, not success — the run has not applied it yet', async () => {
  const [seen, onToast] = collect()
  const feedback = await submitCommand(Promise.resolve({ status: 'executing' }), LABELS, onToast)
  assert.equal(feedback.kind, 'pending')
  assert.deepEqual(seen, ['Card requested — waiting for the run'])
})

test('a failed record toasts the failure label with the server reason', async () => {
  const [seen, onToast] = collect()
  const feedback = await submitCommand(
    Promise.resolve({ status: 'failed', error: { message: 'run is not accepting commands' } }),
    LABELS, onToast)
  assert.equal(feedback.kind, 'error')
  assert.match(seen[0], /^Could not add Card: /)
  assert.match(seen[0], /run is not accepting commands/)
})

// ------------------------------------------------------------------ the transport arm

test('a THROWN transport still produces exactly one toast', async () => {
  // The copy-paste failure this replaces: a panel whose catch arm drifted showed nothing at all, and
  // an operator reads no feedback as "it worked".
  const [seen, onToast] = collect()
  await submitCommand(Promise.reject(new Error('NetworkError')), LABELS, onToast)
  assert.deepEqual(seen, ['Could not add Card: NetworkError'])
})

test('a thrown transport is an ERROR, never a success', async () => {
  // `if (feedback.kind === 'success') setDraft('')` is the live call-site pattern. Returning success
  // here would discard the operator's text on a command the server never received.
  const feedback = await submitCommand(Promise.reject(new Error('offline')), LABELS, () => {})
  assert.equal(feedback.kind, 'error')
  assert.equal(feedback.status, 'transport')
  assert.equal(feedback.transport, true)
})

test('labels.transport WITHHOLDS the thrown message for surfaces that want it withheld', async () => {
  // The Inspector reset menu deliberately says only "could not be submitted, try again" — the
  // actionable half — rather than surfacing a raw fetch error in a dense control strip.
  const [seen, onToast] = collect()
  await submitCommand(Promise.reject(new Error('TypeError: fetch failed')),
                      { ...LABELS, transport: 'Reset #7 could not be submitted. Try again.' }, onToast)
  assert.deepEqual(seen, ['Reset #7 could not be submitted. Try again.'])
})

test('labels.transport is NOT used when the server answered', async () => {
  // A server refusal is a different fact from an unreachable server; collapsing them would tell the
  // operator to retry something the run has already declined.
  const [seen, onToast] = collect()
  await submitCommand(Promise.resolve({ status: 'failed', error: { message: 'paused' } }),
                      { ...LABELS, transport: 'never reached the server' }, onToast)
  assert.match(seen[0], /Could not add Card/)
  assert.doesNotMatch(seen[0], /never reached the server/)
})

test('a non-Error rejection still renders something an operator can read', async () => {
  const [seen, onToast] = collect()
  await submitCommand(Promise.reject('plain string'), LABELS, onToast)
  assert.deepEqual(seen, ['Could not add Card: plain string'])
})

test('a missing onToast is not a crash', async () => {
  // Several panels take `onToast` optionally; the copies all spelled `onToast?.(…)`.
  await submitCommand(Promise.resolve({ status: 'succeeded' }), LABELS, undefined)
  await submitCommand(Promise.reject(new Error('x')), LABELS, undefined)
})

test('submitCommand never rethrows — the caller has already been told', async () => {
  // The call sites are event handlers. An unhandled rejection out of one is an invisible failure
  // twice over: no toast, and no stack anyone sees.
  await submitCommand(Promise.reject(new Error('boom')), LABELS, () => {})
})

// ------------------------------------------------------------------ the call sites use it

test('the panels no longer re-derive the try/feedback/toast/catch block', () => {
  const panels = src('panels.jsx')
  for (const fn of ['Quarantined #', 'Cancelled #', 'Steered the next proposal',
                    'Card added', 'Hypothesis added', 'Hypothesis abandoned']) {
    const at = panels.indexOf(fn)
    assert.ok(at > 0, `${fn} disappeared from panels.jsx`)
    const window = panels.slice(Math.max(0, at - 400), at + 400)
    assert.ok(window.includes('submitCommand('), `${fn} does not go through submitCommand`)
  }
})

test('the Inspector reset paths go through the shared submitter', () => {
  const inspector = src('Inspector.jsx')
  // Guarded by spelling because the property is "no hand-rolled catch arm here", and the two reset
  // surfaces are the ones whose withheld-message wording is easiest to lose in a merge.
  assert.ok(inspector.includes('transport: `Reset #${id} could not be submitted. Try again.`'))
  assert.ok(inspector.includes("transport: 'Re-run could not be submitted. Try again.'"))
  assert.ok(!inspector.includes('} catch { onToast?.('), 'a hand-rolled transport catch came back')
})

test('the optimistic abandon rolls back on anything that is not success', () => {
  // `kind !== 'success'` and not `kind === 'error'`: a still-executing command has not abandoned
  // anything yet, so leaving the strike-through would show an outcome the run may still refuse.
  const panels = src('panels.jsx')
  const at = panels.indexOf('Hypothesis abandoned')
  assert.ok(panels.slice(at, at + 500).includes("feedback.kind !== 'success'"))
})
