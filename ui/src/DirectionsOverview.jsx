import React, { useMemo } from 'react'
import { fmt } from './util.js'
import { directionProfit } from './report.js'

// Directions overview (Workstream E1): the "summary first" lens over the search — a treemap/heat-grid
// of themes where cell AREA = #experiments and cell COLOR = direction profit (how much that theme's
// best beat the run baseline). Click a theme to drill the tree to it. Mirrors MAP-Elites illumination
// + dbt's high-level-first lineage. Pure: derives everything from the folded node set.

// Squarified-ish treemap: greedy row packing into a fixed box. Good enough for a handful of themes and
// avoids a layout dependency. Returns [{...row, x, y, w, h}].
function treemap(rows, W, H) {
  const total = rows.reduce((s, r) => s + r.weight, 0) || 1
  const area = W * H
  const items = rows.map(r => ({ ...r, a: (r.weight / total) * area }))
  const out = []
  let x = 0, y = 0, rowH = 0, rowItems = [], rowArea = 0, remW = W
  const flushRow = () => {
    let rx = x
    const h = rowArea / (remW || 1)
    rowItems.forEach(it => { const w = it.a / (h || 1); out.push({ ...it, x: rx, y, w, h }); rx += w })
    y += h; rowH = 0; rowItems = []; rowArea = 0
  }
  // Simple fixed-width rows: pack until a row's height would exceed remaining; keeps it dependency-free.
  const targetRows = Math.max(1, Math.round(Math.sqrt(items.length)))
  const perRow = Math.ceil(items.length / targetRows)
  for (let i = 0; i < items.length; i++) {
    rowItems.push(items[i]); rowArea += items[i].a
    if (rowItems.length >= perRow || i === items.length - 1) flushRow()
  }
  return out
}

function profitColor(gain, maxAbs) {
  if (gain == null) return 'var(--bg-2)'
  const t = Math.max(-1, Math.min(1, gain / (maxAbs || 1)))
  // green for positive profit, red for negative, neutral at 0
  if (t >= 0) return `color-mix(in srgb, var(--ok) ${Math.round(18 + t * 55)}%, var(--bg-2))`
  return `color-mix(in srgb, var(--alarm) ${Math.round(18 + (-t) * 55)}%, var(--bg-2))`
}

const W = 880, H = 132

export default function DirectionsOverview({ state, active, onPick }) {
  const rows = useMemo(() => directionProfit(state), [state])
  // All hooks must run unconditionally (rules of hooks) — compute before any early return, since
  // `rows` goes from empty → non-empty as the first themed node lands mid-run.
  const cells = useMemo(() => treemap(rows.map(r => ({ ...r, weight: Math.max(1, r.count) })), W, H), [rows])
  if (!rows.length) return null
  const maxAbs = Math.max(0.0001, ...rows.map(r => Math.abs(r.gain ?? 0)))
  return (
    <div className="directions-overview">
      <div className="do-head">
        <b>Directions</b> <span className="muted">— area = #experiments · color = profit vs baseline · click to focus</span>
        {active && <button className="btn sm ghost" onClick={() => onPick(null)}>← all directions ({active})</button>}
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="do-map" preserveAspectRatio="none">
        {cells.map(c => {
          const sel = active === c.theme
          return (
            <g key={c.theme} className={'do-cell' + (sel ? ' on' : '')} onClick={() => onPick(active === c.theme ? null : c.theme)}>
              <rect x={c.x + 1} y={c.y + 1} width={Math.max(0, c.w - 2)} height={Math.max(0, c.h - 2)} rx="6"
                    fill={profitColor(c.gain, maxAbs)} stroke={sel ? 'var(--accent)' : 'var(--line-2)'} strokeWidth={sel ? 2 : 1} />
              {c.w > 64 && c.h > 26 && <text x={c.x + 8} y={c.y + 18} className="do-label">{c.theme}</text>}
              {c.w > 64 && c.h > 42 && <text x={c.x + 8} y={c.y + 34} className="do-sub">
                {c.count}× · best {fmt(c.best)}{c.gain != null ? ` · ${c.gain >= 0 ? '+' : ''}${fmt(c.gain)}` : ''}</text>}
            </g>
          )
        })}
      </svg>
    </div>
  )
}
