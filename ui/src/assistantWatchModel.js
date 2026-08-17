// The browser half of standing assistant watches (BACKLOG §F4) — a PURE model beside its React
// half, per the house pattern: every decision about what a watch STRIP says lives here with no
// React and no I/O, so `node --test` drives it directly and the component keeps only the
// choreography (the poll, the abort, the stop button's optimistic state).
//
// The one job of this surface, and the reason it exists at all: a watch that is waiting must never
// be confusable with a watch that has stopped. That confusion is the whole complaint the durable
// record was built to answer — "I asked it to monitor and I have no idea whether it still is" — so
// the honesty rules here are about which of those two a row is, and they are stated rather than
// spread across JSX conditionals.

import { durationLabel } from './format.js'

// Mirrors `serve/assistant_watch.py::WATCH_TERMINAL_STATUSES` field for field. A status this
// module has never heard of is treated as NOT terminal on purpose: an unknown status from a newer
// server must read as "still going, we do not recognise this" and keep the row visible, never as
// "finished" — hiding a live watch is the failure mode, and showing a dead one merely looks untidy.
export const WATCH_TERMINAL = new Set(['done', 'blocked', 'cancelled', 'expired', 'failed', 'interrupted'])

// What each status MEANS, in the words an operator uses. Attention statuses get explicit recovery
// copy because they ask something of the operator — everything else is just news.
const STATUS_TEXT = {
  armed: 'waiting',
  waking: 'running now',
  done: 'fired, and finished',
  blocked: 'blocked — review the handoff',
  cancelled: 'stopped',
  expired: 'ran out of its budget',
  failed: 'failed',
  interrupted: 'interrupted by a restart — check and re-arm it',
}

export function isTerminal(watch) {
  return WATCH_TERMINAL.has(String(watch?.status || ''))
}

// Whether this row needs the operator to DO something. `interrupted` refuses to re-enter a mutating
// wake-up whose outcome is unknown; `blocked` refuses to invent a missing work handoff. A refusal
// nobody is told about is exactly as invisible as the dropped monitoring it replaced.
export function needsAttention(watch) {
  return ['blocked', 'interrupted'].includes(String(watch?.status || ''))
}

export function statusText(watch) {
  return STATUS_TEXT[String(watch?.status || '')] || String(watch?.status || 'unknown')
}

// "in 45s" / "in 12m" / "now" — never a bare timestamp. `next_due` is server wall-clock seconds and
// the two clocks disagree by whatever the browser's does, so a countdown is honest only to the
// nearest unit; it is a reassurance that something is scheduled, not a promise about the second.
export function nextCheckText(watch, now = Date.now() / 1000) {
  if (!watch || isTerminal(watch)) return ''
  const due = Number(watch.next_due)
  // `> 0` matters as much as `isFinite`: `Number(null)` and `Number('')` are 0, which is finite and
  // far in the past, so an ABSENT `next_due` would read as "now" — i.e. a watch whose schedule the
  // server never wrote would claim to be about to fire.
  if (!Number.isFinite(due) || due <= 0) return ''
  const delta = due - now
  if (delta <= 1) return 'now'
  // The tiering is `format.js::durationLabel`, the same one the run's live-status age and the node
  // episode picker print — this strip owns only the `in …` wording and the `now` floor above. The
  // top tier used to be a bare `in 3h` here; rounding to the minute inside the hour is still the
  // "honest to the nearest unit" this function's own rule asks for (the clocks disagree by seconds,
  // not by half-hours), and it is what stops one interval reading two ways on one screen.
  return `in ${durationLabel(delta)}`
}

// The wake-up budget, shown as a fraction and ONLY when it can move. A one-shot run-state watch has
// no meaningful "1 of 24" — it fires once by construction — and printing a budget it will never
// spend invites the reading that it is going to keep waking.
export function budgetText(watch) {
  if (!['schedule', 'work'].includes(watch?.trigger?.kind)) return ''
  const done = Number(watch?.wakeups || 0)
  const max = Number(watch?.max_wakeups || 0)
  if (!Number.isFinite(max) || max <= 0) return ''
  return watch?.trigger?.kind === 'work' ? `${done}/${max} cycles` : `${done}/${max} wake-ups`
}

// One row's full description. `waitingFor` is the server's own sentence and is never re-derived
// here: the record stamps it at arm time, and a browser that recomputed it from `trigger` would
// silently disagree with the server the day either vocabulary moves.
export function describeWatch(watch, now = Date.now() / 1000) {
  const waitingFor = String(watch?.waiting_for || '').trim() || 'an unstated condition'
  return {
    id: String(watch?.id || ''),
    waitingFor,
    status: String(watch?.status || ''),
    statusText: statusText(watch),
    terminal: isTerminal(watch),
    attention: needsAttention(watch),
    // The INSTRUCTION is part of the row, not a hidden detail. A monitoring surface that will not
    // say what it is going to do when it wakes is the same "what is it even waiting for" problem
    // one level down.
    instruction: String(watch?.instruction || ''),
    nextCheck: nextCheckText(watch, now),
    budget: budgetText(watch),
    checkpoint: String(watch?.checkpoint?.summary || ''),
    note: String(watch?.last_error || ''),
    stoppable: !isTerminal(watch),
  }
}

// What the strip shows. Active watches first (they are the live fact), each in arm order so the
// list does not reshuffle under the operator on every poll; terminals are kept only while they are
// still news — a `done` watch that already reported is history the transcript holds better.
export function watchStrip(watches, { now = Date.now() / 1000, recentSeconds = 600 } = {}) {
  const rows = Array.isArray(watches) ? watches : []
  const active = []
  const recent = []
  for (const watch of rows) {
    if (!watch || !watch.id) continue
    if (!isTerminal(watch)) { active.push(watch); continue }
    // Attention statuses are never aged out: they ask the operator for something, and a request
    // that expires on a timer is a request nobody made.
    const updated = Number(watch.updated)
    if (needsAttention(watch)
        || (Number.isFinite(updated) && now - updated <= recentSeconds)) recent.push(watch)
  }
  const byCreated = (a, b) => (Number(a.created) || 0) - (Number(b.created) || 0)
  active.sort(byCreated)
  recent.sort(byCreated)
  const items = [...active, ...recent].map(watch => describeWatch(watch, now))
  return {
    items,
    activeCount: active.length,
    attentionCount: items.filter(item => item.attention).length,
    // The strip is HIDDEN when there is nothing standing, rather than shown empty: a permanent
    // "no watches" row is chrome, and chrome is what an operator learns to stop reading.
    visible: items.length > 0,
    summary: active.length
      ? `${active.length} standing watch${active.length === 1 ? '' : 'es'}`
      : 'no standing watches',
  }
}

// How often the strip re-reads. Deliberately unrelated to the server's own poll cadence: this is a
// browser refreshing a list, not the thing doing the watching, so a closed tab costs the monitoring
// nothing. Slower when nothing is armed, because then the only thing that can change the list is
// the operator themselves — and they are already causing a refresh when they do it.
export function stripPollMs(strip) {
  return strip?.activeCount ? 5000 : 30000
}
