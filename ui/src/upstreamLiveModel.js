// The LIVE upstream lane as the server projected it (`state.upstream_live`, doc 73 §2.5): the mode
// the run serves and why, its queue of propose/check/advance operations with each receipt, and the
// automated author's rows. Absent on a run that declares no upstream block — `null` here, and the
// panel says nothing about a live lane. A malformed row is dropped, never shown as a receipt.
const MODES = new Set(['off', 'propose', 'auto'])
const RECENT = 5

const rowsOf = (value, key) => (Array.isArray(value) ? value : [])
  .filter(row => row && typeof row === 'object' && typeof row[key] === 'string')

const count = (value, fallback) => (Number.isSafeInteger(value) && value >= 0 ? value : fallback)

export function upstreamLiveSummary(live) {
  if (!live || typeof live !== 'object' || !MODES.has(live.mode)) return null
  const queue = live.queue && typeof live.queue === 'object' ? live.queue : {}
  const rows = rowsOf(queue.rows, 'op')
  const authored = rowsOf(live.authored, 'outcome')
  return {
    mode: live.mode,
    reason: typeof live.reason === 'string' ? live.reason : '',
    author: live.author === true,
    pending: count(queue.pending, rows.filter(row => row.status === 'pending').length),
    total: count(queue.total, rows.length),
    recent: rows.slice(-RECENT).reverse(),
    authored: authored.slice(-RECENT).reverse(),
    authoredTotal: count(live.authored_total, authored.length),
  }
}

// One label table per vocabulary, both languages, so an unknown value reads as itself.
const LABELS = {
  mode: {
    off: ['off — proposing, checking and promoting need a paused run', 'выключена — предложение, проверка и перенос требуют паузы'],
    propose: ['propose — operations are queued and run between turns', 'propose — операции ставятся в очередь и выполняются между ходами'],
    auto: ['auto — the engine checks and promotes on its own', 'auto — движок сам проверяет и переносит'],
  },
  status: {
    pending: ['waiting', 'ожидает'], succeeded: ['done', 'выполнено'],
    failed: ['failed', 'не прошло'], refused: ['refused', 'отклонено'],
  },
  outcome: {
    drafted: ['drafted → proposal', 'черновик → предложение'], declined: ['declined by its critic', 'отклонён критиком'],
    skipped: ['skipped', 'пропущен'], failed: ['failed', 'ошибка'],
  },
  track: { repair: ['fix from a repair', 'фикс из ремонта'], champion: ['champion', 'чемпион'] },
}

export function upstreamLiveLabel(kind, value, ru) {
  const pair = LABELS[kind]?.[value]
  return pair ? pair[ru ? 1 : 0] : String(value ?? '')
}
