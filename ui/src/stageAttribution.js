// The eval pipeline's stage strip, ATTRIBUTED — the pure half of `Inspector.jsx::StagePipeline`.
//
// THE DEFECT THIS EXISTS FOR. `stage_finished` is appended once per stage per ATTEMPT of the
// engine's inline-repair loop and the fold keeps the rows last-wins BY STAGE NAME
// (`events/replay.py::_on_stage_finished`). An inline repair does NOT bump the lifecycle
// generation, so after a repair the surviving rows still describe the attempt the repair replaced
// — and the strip drew them exactly as it draws a live result. Measured on
// `runs/rubertlite-dr-unified-v9` at 2026-08-17 12:48 UTC: experiment #5 had NINE `vectorsearch
// .train` processes alive in its workdir and had been running under repair #3 for 177 minutes,
// while its newest recorded stage statements were `mine expect_failed` (recorded under repair 2)
// and `train fail` (recorded under repair 1) — two red ✗ chips, over a node that was training.
// #6 was the same at 56 minutes. Over the four runs whose stage rows are written inside the
// attempt loop (v6-v9; a pre-2026-08-07 log wrote them all at the terminal, after every repair, so
// it cannot express this shape at all) there are 44 such windows — median 66.1 minutes, 89.8 hours
// in total, 23 of them over an hour — and that is a LOWER bound, because the window is closed as
// soon as ANY stage speaks again even though the other stages' rows stay stale.
//
// WHAT THE SIGNAL IS. `Node.repairs` is the fold's count of inline repairs applied to the current
// lifecycle, and each stage row carries the `repairs` value it was recorded at. A row whose epoch
// is strictly smaller is one that NO LATER ATTEMPT HAS SPOKEN ABOUT — including a `reused` marker,
// which advances the row's epoch precisely because a reuse is the later attempt's own statement
// that the result stands. Both numbers are DERIVED BY THE FOLD FROM LOG ORDER; no event gained a
// field, so the four runs already on disk are attributed retroactively.
//
// WHAT IT IS NOT, and this bound is the reason the copy below says what it says: it answers "has a
// later attempt reported on this stage", never "is a process alive". `narration.js::pendingWork`
// draws the same line for the same reason. A node between stages, waiting on a GPU lease, or being
// repaired all read as superseded-and-unreported, and none of them is proof of a running process.

// Mirrors `core/models.py::stage_row_superseded` — keep the two in step; `ui/test/
// stageAttribution.test.js` pins the shared truth table against the python one's own cases.
// STRICTLY less-than: EQUAL is the current attempt (the negative control — a node whose rows are
// its state must render byte-for-byte as before, and that includes every unrepaired node, where
// both numbers are 0). GREATER happens after a `node_reset`, which re-stamps the rows it retains.
// Either side ABSENT answers false: an old projection carries no `repairs` key, and "I cannot
// tell" must render as the historical view rather than assert that a real result is stale.
export const stageRowSuperseded = (row, repairs) =>
  Number.isSafeInteger(row?.repairs) && Number.isSafeInteger(repairs)
  && row.repairs < repairs

// The statuses `StagePipeline` paints with `--fail` — i.e. the ones an operator reads as "this
// experiment is broken". Derived from the tone/icon tables below rather than restated, so a new
// stage status cannot end up red in one place and amber in the other.
export const STAGE_OK_STATUSES = ['ok', 'timeout', 'reused']

// The strip's historical tone/icon tables, for a row that IS the node's current state.
export const stageTone = (s) => s?.status === 'ok' ? 'var(--ok)'
  : s?.status === 'timeout' ? 'var(--working)'
    : s?.status === 'reused' ? 'var(--fg-mut)' : 'var(--fail)'
export const stageIcon = (s) => s?.status === 'ok' ? '✓'
  : s?.status === 'timeout' ? '⧗' : s?.status === 'reused' ? '↺' : '✗'

// THE SUPERSEDED GLYPH, and why the row does NOT keep its own outcome glyph.
//
// This module shipped with the opposite rule — "a superseded row keeps its OWN outcome glyph, the
// row still records what that attempt did, and replacing the glyph would be a second claim" — and
// that reasoning is wrong in one specific way: the glyph is not a caption on the row, it is the
// row's HEADLINE. It is read before the tone and instead of the title. Measured on the live
// `runs/e5small-dr-unified-v2` node 9 on 2026-08-19: `Node.repairs` 1, both stage rows recorded at
// `repairs` 0, so `stageRowSuperseded` answers true for BOTH — and the strip drew
// `✓ mine → ✗ train`, which the operator read as "training failed". It had not: that `train`
// crashed at 223.871 s, a repair landed, and the retry trained all 2,109 steps and went on to
// evaluation. Tone said "stale" (`var(--fg-mut)`) and glyph said "failed" — one row making two
// opposite claims, and the glyph won.
//
// So the glyph is REPLACED, by the one thing the fold proves about a superseded row: a later
// attempt exists and has not reported on this stage. It deliberately does NOT vary with the row's
// status. The two live rows here are exactly `✓ mine` and `✗ train` and BOTH are superseded, so
// a glyph that kept the outcome for the OK half would still assert an outcome that has been
// replaced, and one that promoted it to a success would claim a retry nothing has reported on.
// `⋯` is "no current statement" — the checkable sentence `stageRowTitle` already writes — and it
// is distinct from every member of `stageIcon`'s table, so no row can be mistaken for a `reused` one.
export const STAGE_SUPERSEDED_ICON = '⋯'

// The glyph's text equivalent, and the superseded row is the ONLY row that gets one — because it
// is the only row whose glyph stopped naming its own status. The outcome is still true of the
// attempt that recorded it and must stay reachable to a reader who cannot see a tone. Every other
// row is unchanged, label included (there is none), which is what keeps the negative control exact.
export const stageSupersededLabel = (s) =>
  `${s?.status} under an earlier attempt; superseded — no later attempt has reported on this stage`

const seconds = (s) => s?.seconds != null ? ` · ${s.seconds}s` : ''
const exit = (s) => s?.exit_code != null ? ` · exit ${s.exit_code}` : ''

// The row's own sentence. The superseded suffix names BOTH numbers, because "stale" without the
// epochs is a claim the operator cannot check, and checking it is the whole point.
export const stageRowTitle = (s, { superseded = false, repairs = null } = {}) => {
  const base = `${s?.name}: ${s?.status}${seconds(s)}${exit(s)}`
  if (!superseded) return base
  return `${base} — recorded under repair ${s?.repairs} of ${repairs}; `
    + 'no later attempt has reported on this stage'
}

// ONE row, ready to render. A strip with nothing superseded is byte-identical to the historical one:
// `superseded` is false, `iconLabel` is null, and `icon`/`tone` are the historical tables verbatim.
export const stageRowView = (s, repairs) => {
  const superseded = stageRowSuperseded(s, repairs)
  return {
    row: s,
    name: s?.name,
    status: s?.status,
    superseded,
    failed: !STAGE_OK_STATUSES.includes(s?.status),
    tone: superseded ? 'var(--fg-mut)' : stageTone(s),
    icon: superseded ? STAGE_SUPERSEDED_ICON : stageIcon(s),
    // `null` on every row that is not superseded, so the component renders the markup it always did.
    iconLabel: superseded ? stageSupersededLabel(s) : null,
    title: stageRowTitle(s, { superseded, repairs }),
  }
}

// THE NOTICE, and every clause of it is something the fold proves.
//
// It leads with the count because that is the operator's question ("are these chips about now?"),
// names the repair ordinal because that is what makes it checkable against the event feed, and
// stops at what is known: a pending node has NOT BEEN SCORED SINCE (which is a fact about the
// absence of a terminal, not a claim about a live process — see the module header), and a settled
// one recorded its outcome under a later attempt than these rows describe.
//
// Returns null when nothing is superseded — the negative control: the label above the strip is
// then exactly the string it has always been.
export const stageSupersessionNotice = (views, { repairs = null, status = null } = {}) => {
  const stale = (views || []).filter(v => v?.superseded)
  if (!stale.length) return null
  const failed = stale.filter(v => v.failed).length
  const subject = stale.length === 1 ? 'result is' : 'results are'
  const settled = status === 'evaluated' || status === 'failed'
  return {
    superseded: stale.length,
    failed,
    repairs,
    text: `${stale.length} of ${views.length} stage ${subject} from an earlier attempt`
      + ` — repair ${repairs} was applied after ${stale.length === 1 ? 'it' : 'them'} and `
      + (settled
        ? 'this experiment was settled by a later attempt these rows do not describe.'
        : 'this experiment has not been scored since.'),
    // Said separately because it is the operator's actual complaint and the sentence above is
    // about staleness in general: N of the stale rows are the RED ones.
    failureText: failed
      ? (failed === 1
        ? '1 of them is a failure that a later attempt has not repeated.'
        : `${failed} of them are failures that a later attempt has not repeated.`)
      : null,
  }
}

// The whole strip. One call, so the component holds no rule of its own.
export const stagePipelineView = (node) => {
  const stages = Array.isArray(node?.stages) ? node.stages : []
  const repairs = Number.isSafeInteger(node?.repairs) ? node.repairs : null
  const rows = stages.map(s => stageRowView(s, repairs))
  return {
    rows,
    notice: stageSupersessionNotice(rows, { repairs, status: node?.status }),
    // The label the strip has always printed, kept here so the component reads ONE object.
    failedStage: node?.failed_stage || null,
  }
}
