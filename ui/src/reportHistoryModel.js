// The run report's version history and figures — the pure half beside `ReportExtras.jsx`.
//
// The fold keeps ONE report (latest wins). `GET /report/versions` lists every `report_generated`
// row through the fold's own sanitizer (`looplab/serve/report_history.py`), marking the current
// version and the repeats (`duplicate_of`). This module reads that list defensively and compares
// two versions field by field, so "what changed since the last report" is a statement the page
// derives from two recorded versions — never a summary a model wrote.

import { normalizeRunReport } from './reportModel.js'

const _text = (value, max = 300) => (typeof value === 'string' ? value.slice(0, max) : '')
const _seq = value => (Number.isSafeInteger(value) && value >= 0 ? value : null)
const FIGURE_ID = /^[0-9a-f]{64}$/
const FIGURE_MIMES = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp'])

export const REPORT_FIGURES_MAX = 12
export const REPORT_DIFF_MAX_LINES = 400

export function normalizeReportFigures(value) {
  if (!Array.isArray(value)) return []
  const out = []
  const seen = new Set()
  for (const row of value) {
    if (!row || typeof row !== 'object' || !FIGURE_ID.test(row.figure_id || '')) continue
    if (seen.has(row.figure_id)) continue
    seen.add(row.figure_id)
    const source = row.source && typeof row.source === 'object' ? {
      root: _text(row.source.root, 200), path: _text(row.source.path, 500),
      nodeId: Number.isSafeInteger(row.source.node_id) ? row.source.node_id : null,
    } : null
    out.push({
      figureId: row.figure_id,
      caption: _text(row.caption),
      mime: FIGURE_MIMES.has(row.mime) ? row.mime : '',
      source: source && (source.root || source.path) ? source : null,
    })
    if (out.length >= REPORT_FIGURES_MAX) break
  }
  return out
}

export function normalizeReportVersions(payload) {
  const rows = payload && typeof payload === 'object' && Array.isArray(payload.versions)
    ? payload.versions : []
  const versions = []
  for (const row of rows) {
    if (!row || typeof row !== 'object' || _seq(row.seq) == null) continue
    versions.push({
      seq: row.seq,
      ts: typeof row.ts === 'number' && Number.isFinite(row.ts) ? row.ts : null,
      atNode: _seq(row.at_node),
      trigger: _text(row.trigger, 64),
      headline: _text(row.headline),
      publisher: _text(row.publisher, 64),
      authorClaim: _text(row.author_claim, 120),
      figures: _seq(row.figures) ?? 0,
      duplicateOf: _seq(row.duplicate_of),
      current: row.current === true,
    })
  }
  const total = _seq(payload?.total) ?? versions.length
  return { versions, total, distinct: _seq(payload?.distinct) ?? versions.length,
    truncated: payload?.truncated === true || total > versions.length }
}

// A bounded line diff (LCS). Over REPORT_DIFF_MAX_LINES lines a side, the comparison says so
// instead of computing a quadratic table in the browser.
export function lineDiff(before, after) {
  const a = (before || '').split('\n'), b = (after || '').split('\n')
  if (a.length > REPORT_DIFF_MAX_LINES || b.length > REPORT_DIFF_MAX_LINES) return null
  const table = Array.from({ length: a.length + 1 }, () => new Uint16Array(b.length + 1))
  for (let i = a.length - 1; i >= 0; i -= 1) {
    for (let j = b.length - 1; j >= 0; j -= 1) {
      table[i][j] = a[i] === b[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1])
    }
  }
  const ops = []
  let i = 0, j = 0
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) { ops.push({ op: 'same', text: a[i] }); i += 1; j += 1 }
    else if (table[i + 1][j] >= table[i][j + 1]) { ops.push({ op: 'del', text: a[i] }); i += 1 }
    else { ops.push({ op: 'add', text: b[j] }); j += 1 }
  }
  while (i < a.length) ops.push({ op: 'del', text: a[i++] })
  while (j < b.length) ops.push({ op: 'add', text: b[j++] })
  return ops
}

const TEXT_FIELDS = ['headline', 'verdict', 'summary', 'champion_summary']
const LIST_FIELDS = ['caveats', 'what_worked', 'what_didnt', 'learnings', 'next_directions']

// What changed from `older` to `newer` (raw report contents). Unchanged fields are listed as such
// so the page can say "the same" rather than leave the reader to compare by eye.
export function reportDiff(older, newer) {
  const a = normalizeRunReport(older) || {}, b = normalizeRunReport(newer) || {}
  const fields = []
  for (const field of TEXT_FIELDS) {
    const before = a[field] || '', after = b[field] || ''
    if (!before && !after) continue
    fields.push({ field, kind: 'text', status: before === after ? 'same'
      : !before ? 'added' : !after ? 'removed' : 'changed', before, after })
  }
  for (const field of LIST_FIELDS) {
    const before = a[field] || [], after = b[field] || []
    if (!before.length && !after.length) continue
    const added = after.filter(item => !before.includes(item))
    const removed = before.filter(item => !after.includes(item))
    fields.push({ field, kind: 'list', status: added.length || removed.length ? 'changed' : 'same',
      added, removed })
  }
  const bodyBefore = a.body_markdown || '', bodyAfter = b.body_markdown || ''
  if (bodyBefore || bodyAfter) {
    fields.push({ field: 'body_markdown', kind: 'lines', status: bodyBefore === bodyAfter ? 'same' : 'changed',
      ops: bodyBefore === bodyAfter ? [] : lineDiff(bodyBefore, bodyAfter) })
  }
  const figBefore = normalizeReportFigures(older?.figures).map(f => f.figureId)
  const figAfter = normalizeReportFigures(newer?.figures).map(f => f.figureId)
  if (figBefore.length || figAfter.length) {
    const added = figAfter.filter(id => !figBefore.includes(id))
    const removed = figBefore.filter(id => !figAfter.includes(id))
    fields.push({ field: 'figures', kind: 'count', status: added.length || removed.length ? 'changed' : 'same',
      added: added.length, removed: removed.length })
  }
  if (JSON.stringify(a.key_metrics || null) !== JSON.stringify(b.key_metrics || null)) {
    fields.push({ field: 'key_metrics', kind: 'flag', status: 'changed' })
  }
  return { fields, changed: fields.some(row => row.status !== 'same') }
}
