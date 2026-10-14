// Pure projections for the cross-run orchestration surfaces (doc 78): the host GPU queue, the
// agent wake-up ledger and a failed node's forensic record. No React, no fetch — `node --test`
// drives every branch (`test/orchestrationModel.test.js`).

const ago = (ts, now) => {
  if (typeof ts !== 'number' || !Number.isFinite(ts)) return null
  const s = Math.max(0, Math.round(now - ts))
  if (s < 90) return `${s}s`
  if (s < 5400) return `${Math.round(s / 60)}m`
  return `${Math.round(s / 3600)}h`
}

// The GPU pool: who holds it, who waits (in FIFO order) and which runs are gated.
export function gpuQueueView(status, now = Date.now() / 1000) {
  if (!status || typeof status !== 'object') return { known: false, holder: null, queue: [], gates: [] }
  const holder = status.holder
    ? { runId: status.holder.run_id || null, pid: status.holder.pid ?? null,
        since: ago(status.holder.acquired_at, now), unverified: !!status.holder.unverified }
    : null
  const queue = (Array.isArray(status.queue) ? status.queue : []).map((row, i) => ({
    position: i + 1, runId: row.run_id || null, pid: row.pid ?? null,
    waited: ago(row.created_at, now),
  }))
  const gates = (Array.isArray(status.gates) ? status.gates : []).map(g => ({
    runId: g.run_id,
    hold: !!g.hold,
    after: Array.isArray(g.start_after_ids) ? g.start_after_ids : [],
    open: !(Array.isArray(g.blockers) && g.blockers.length),
    waitingFor: (Array.isArray(g.blockers) ? g.blockers : [])
      .filter(b => b && b.kind === 'start_after').map(b => b.run_id),
    reason: typeof g.reason === 'string' ? g.reason : '',
    malformed: !!g.malformed,
  }))
  return { known: true, held: status.lease_held, holder, queue, gates }
}

// The notification hook's delivery ledger, newest first, with a tone per row.
export function wakeLedgerView(status, now = Date.now() / 1000) {
  if (!status || typeof status !== 'object') return { configured: false, command: null, rows: [] }
  const config = status.config || null
  const rows = (Array.isArray(status.ledger) ? status.ledger : []).map((row, i) => ({
    key: `${row.ts}-${i}`,
    when: ago(row.ts, now),
    status: row.status,
    tone: row.status === 'delivered' ? 'ok' : row.status === 'failed' ? 'warn' : 'bad',
    attempt: row.attempt ?? null,
    kinds: Array.isArray(row.kinds) ? row.kinds : [],
    runs: Array.isArray(row.runs) ? row.runs : [],
    events: Array.isArray(row.event_ids) ? row.event_ids.length : 0,
    exitCode: row.exit_code ?? null,
    error: typeof row.error === 'string' ? row.error : '',
    test: !!row.test,
  }))
  return {
    configured: !!status.configured,
    enabled: config ? config.enabled !== false : false,
    command: config && Array.isArray(config.command) ? config.command[0] : null,
    debounce: config ? config.debounce_s : null,
    newestEventId: typeof status.newest_event_id === 'number' ? status.newest_event_id : null,
    rows,
  }
}

// One failed attempt's forensic record, reduced to what a reader scans first.
export function forensicsView(record) {
  if (!record || typeof record !== 'object') return null
  const signals = (Array.isArray(record.signals) ? record.signals : []).map(s =>
    s.ranks && s.ranks.length ? `${s.signal} · ranks ${s.ranks.join(', ')}` : `${s.signal} · ${s.source || ''}`.trim())
  const exitCodes = Object.entries(record.exit_codes || {}).map(([k, v]) => `${k}: ${v}`)
  const root = record.root_exception
    ? `${record.root_exception.type}: ${record.root_exception.message || ''}`.trim()
    : null
  const wrappers = (Array.isArray(record.exceptions) ? record.exceptions : [])
    .filter(e => !root || `${e.type}: ${e.message || ''}`.trim() !== root)
    .map(e => `${e.type}: ${e.message || ''}`.trim())
  const tails = Object.entries(record.log_tails || {}).map(([name, lines]) => ({
    name, text: Array.isArray(lines) ? lines.join('\n') : '' }))
  const ranks = Object.entries(record.rank_tails || {}).map(([name, lines]) => ({
    name, text: Array.isArray(lines) ? lines.join('\n') : '' }))
  return {
    headline: record.headline || '',
    root, wrappers, signals, exitCodes,
    fault: record.faulthandler
      ? [record.faulthandler.error, ...(record.faulthandler.stack || [])].filter(Boolean).join('\n')
      : null,
    hints: Array.isArray(record.hints) ? record.hints : [],
    tails, ranks,
  }
}
