/**
 * A leakage scan that recorded boundary TIES must not render as, simply, `clean`.
 *
 * `trust/leakage.py::temporal_leakage` counts train rows whose timestamp EQUALS the first test
 * timestamp separately from the ones strictly after it. The equality is ordinary on a coarse clock
 * (a daily bucket puts the last train row and the first test row together) and is a real interleave
 * on an exact-instant one, and the detector deliberately cannot tell those apart — so it records the
 * tie and does not convict, because this gate is WIRED and a conviction aborts the run.
 *
 * That trade only holds if the number reaches somebody. It did not: `ties` had zero readers in
 * `looplab/` or `ui/`, and this renderer printed `clean` off `leak` alone, so a split whose boundary
 * instant holds four thousand train rows read as assurance.
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

test('boundary ties are named beside `clean`, and a real leak still wins', async () => {
  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const { eventNarration } = await vite.ssrLoadModule('/src/narration.js')
    const scan = (data) => eventNarration({ type: 'data_leakage', data })

    // Nothing qualified: the sentence is unchanged, byte for byte.
    assert.equal(scan({ leak: false, verdicts: [{ detector: 'temporal_leakage', ties: 0 }] }),
      'leakage scan: clean')
    assert.equal(scan({ leak: false }), 'leakage scan: clean')

    // MUTATION: render off `leak` alone -> this is `leakage scan: clean` and the operator is told
    // nothing about a split that shares four thousand instants with its own test set.
    assert.equal(
      scan({ leak: false, verdicts: [{ detector: 'temporal_leakage', ties: 4000 }] }),
      'leakage scan: clean (4000 train rows share the first test timestamp)')
    assert.equal(scan({ leak: false, verdicts: [{ ties: 1 }] }),
      'leakage scan: clean (1 train row shares the first test timestamp)')

    // A real overlap is still the headline: the tie is a qualifier, never a competing verdict.
    assert.equal(scan({ leak: true, verdicts: [{ ties: 4000 }] }), 'leakage scan: LEAK DETECTED')

    // Total over junk, like every other renderer here — a malformed row may not break the feed.
    assert.equal(scan({ leak: false, verdicts: 'nope' }), 'leakage scan: clean')
    assert.equal(scan({ leak: false, verdicts: [null, { ties: 'many' }, { ties: -3 }] }),
      'leakage scan: clean')
  } finally {
    await vite.close()
  }
})
