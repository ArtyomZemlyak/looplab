// Whether the SERVER that answered this poll is still running the code on disk.
//
// THE CASE, measured 2026-09-03. The operator reported the question ladder drawing twelve questions
// with nothing attached and every row "not measured yet". Four layers were checked and every one was
// correct: the fold on disk held 17 `parent_card_id` edges and gave 7 questions children;
// `public_cards()` over that same log published all 17; this bundle contained the fixed reader; and
// the lattice model, driven in node over the real wire, drew experiments under 10 of 15 rows. The
// payload the RUNNING SERVER returned carried `parent_card_id` on 0 of 34 cards and `child_rollup`
// on 0 of 12 questions — a 30-field DTO against the tree's 55. That process had been up nine days,
// since before the fold learned to keep the edge, and restarting it restored every number at once.
//
// So the browser was drawing exactly what it was given, and no test of this code could ever have
// caught it. `looplab/serve/code_freshness.py` publishes the server's own code identity so the one
// layer that CAN say it does: this reader turns that into the sentence the operator acts on.
//
// A NOTICE, NEVER A REFUSAL. A stale server still serves, and its numbers are still that server's
// honest fold — they are simply older than the tree. Hiding rows or blanking values would replace a
// legible smaller truth with no truth at all.
import { isRecord } from './panelPrimitives.js'

export function serverCodeNotice(state) {
  const report = isRecord(state) && isRecord(state.server_code) ? state.server_code : null
  if (!report || report.stale !== true) return null
  // The COUNT is what the operator acts on and is exact even when the sample below is clipped; the
  // server sends at most a handful of paths so this notice cannot become the payload.
  const count = Number.isSafeInteger(report.changed_count) ? report.changed_count : 0
  const sample = Array.isArray(report.changed)
    ? report.changed.filter(p => typeof p === 'string' && p.trim()) : []
  const more = report.changed_truncated === true || sample.length < count
  return {
    text: `server code ${count} file${count === 1 ? '' : 's'} behind`,
    detail: 'this server process loaded its code at startup and the tree has moved since — every'
      + ' fix merged after it started is absent from what you are looking at. Restart the UI server.'
      + (sample.length ? ` Changed: ${sample.join(', ')}${more ? ', …' : ''}` : ''),
  }
}
