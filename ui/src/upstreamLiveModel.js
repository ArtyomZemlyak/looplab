// The LIVE upstream lane as the server projected it (`state.upstream_live`, doc 73 §2.5): the mode
// the run serves and why, its queue of propose/check/advance operations with each receipt, and the
// automated author's rows. Absent on a run that declares no upstream block — `null` here, and the
// panel says nothing about a live lane. A malformed row is dropped, never shown as a receipt.
const MODES = new Set(['off', 'propose', 'auto'])
const RECENT = 5

const count = (value, fallback) => (Number.isSafeInteger(value) && value >= 0 ? value : fallback)

const queueRow = row => row && typeof row === 'object' && typeof row.op === 'string'
  && typeof row.action_id === 'string' && Number.isSafeInteger(row.idx)

const authoredRow = row => row && typeof row === 'object' && typeof row.outcome === 'string'
  && Number.isSafeInteger(row.seq) && Number.isSafeInteger(row.source_node_id)

// A step the engine held back at a cap (doc 73 §4.2 G3/G4, the diagnostic `lane_held`).
const heldRow = row => row && typeof row === 'object' && typeof row.op === 'string'
  && typeof row.reason === 'string' && Number.isSafeInteger(row.seq)

// `engineRunning` is the payload's present-time `engine_running` (the projection itself caches with
// the log, so it cannot know): `false` with an engine-armed mode means nobody serves the lane NOW —
// the mode shown is what the last engine served, not a promise (`idle`).
export function upstreamLiveSummary(live, engineRunning) {
  if (!live || typeof live !== 'object' || !MODES.has(live.mode)) return null
  const queue = live.queue && typeof live.queue === 'object' ? live.queue : {}
  const rows = (Array.isArray(queue.rows) ? queue.rows : []).filter(queueRow)
  const authored = (Array.isArray(live.authored) ? live.authored : []).filter(authoredRow)
  return {
    mode: live.mode,
    reason: typeof live.reason === 'string' ? live.reason : '',
    // `configured`: no engine has armed the lane yet, so this is what the settings SAY, not what
    // an engine serves (`upstream_serve.py::upstream_live_view`).
    configured: live.configured === true,
    idle: live.configured !== true && engineRunning === false,
    author: live.author === true,
    pending: count(queue.pending, rows.filter(row => row.status === 'pending').length),
    total: count(queue.total, rows.length),
    recent: rows.slice(-RECENT).reverse(),
    authored: authored.slice(-RECENT).reverse(),
    authoredTotal: count(live.authored_total, authored.length),
    // The operator's kill switch (`upstream_auto_set`), the caps that held a step back, and what
    // the author spent — doc 73 §4.2 G2-G4. Absent on an older payload: not stopped, nothing held.
    autoPaused: live.auto_paused === true,
    held: (Array.isArray(live.held) ? live.held : []).filter(heldRow).slice(-RECENT).reverse(),
    authorSpentUsd: Number.isFinite(live.author_spent_usd) && live.author_spent_usd >= 0 ? live.author_spent_usd : 0,
  }
}

// English labels only: the panel renders each through `uiText`, so the Russian lives in ru.json
// alone. An unknown value reads as `unknown`, never as an empty cell.
const LABELS = {
  mode: {
    off: 'off — proposing, checking and promoting need a paused run',
    propose: 'propose — operations are queued and run between turns',
    auto: 'auto — the engine checks and promotes on its own',
  },
  status: { pending: 'waiting', succeeded: 'succeeded', failed: 'check did not pass', refused: 'refused by the lane', settled: 'settled' },
  // A `failed` receipt means what its OPERATION failed at: only a check has a gate to not pass.
  failed: { propose: 'proposal failed', check: 'check did not pass', advance: 'advance failed' },
  outcome: {
    drafted: 'drafted → proposal', declined: 'declined by its critic', skipped: 'not drafted', failed: 'drafting failed',
    rejected: 'the draft could not be absorbed', refused: 'refused by the lane',
  },
  track: { repair: 'fix from a repair', champion: 'champion' },
  held: { advance: 'advance held at the hourly cap', author: 'author stopped at its budget' },
  // `lane_held {reason: "refused:<code>"}`: the lane refused an automatic step for a reason about the
  // proposal itself, so the engine never asks it again (`upstream_serve.py::refused_for_good`).
  refused: { check: 'automatic check refused by the lane, not asked again', advance: 'automatic advance refused by the lane, not asked again' },
}

export function upstreamLiveLabel(kind, value, op) {
  if (kind === 'status' && value === 'failed' && typeof op === 'string' && LABELS.failed[op]) return LABELS.failed[op]
  return LABELS[kind]?.[value] ?? 'unknown'
}

// One held row's label: a cap that held the step back, or a refusal that retired it.
export function upstreamHeldLabel(row) {
  const refused = typeof row?.reason === 'string' && row.reason.startsWith('refused:')
  return upstreamLiveLabel(refused ? 'refused' : 'held', row?.op)
}
