import React, { useMemo } from 'react'
import { fmt } from './util.js'
import { directionProfit, optimizationLabel } from './report.js'

// Directions overview (round-6 redesign): the "summary first" lens over the search, now a COMPACT
// row of clickable direction chips instead of the old screen-eating treemap. Each chip carries the
// direction name (stored in the legacy `idea.theme` wire field), experiment count, best metric and signed
// difference from the run baseline — but in ~26px of height. Click a chip to drill the tree to that
// direction. The dot intentionally stays neutral: these aggregates do not establish a causal effect. Pure:
// derives everything from the folded node set via directionProfit (report.js), unchanged.

export default function DirectionsOverview({ state, active, onPick }) {
  const rows = useMemo(() => directionProfit(state), [state])
  if (!rows.length) return null
  return (
    <div className="directions-overview">
      <div className="do-head">
        <strong>Directions</strong>
        {active && <button type="button" className="btn sm ghost" onClick={() => onPick(null)}>← all directions ({active})</button>}
        <span className="muted" id="directions-caveat">Optimization: {optimizationLabel(state.direction)} · Descriptive only; not a causal effect or winner claim.</span>
      </div>
      <div className="do-chips" role="group" aria-label="Research directions" aria-describedby="directions-caveat">
        {rows.map(r => {
          const sel = active === r.direction
          const tone = r.gain == null ? 'var(--line-2)' : 'var(--accent)'
          const difference = r.gain == null ? '' : `${r.gain >= 0 ? '+' : ''}${fmt(r.gain)}`
          return (
            <button key={r.direction} type="button" aria-pressed={sel}
                    className={'do-chip' + (sel ? ' on' : '')} style={{ '--tone': tone }}
                    onClick={() => onPick(sel ? null : r.direction)}
                    title={`${r.direction}: ${r.count} experiment(s) · best observed ${fmt(r.best)}${difference ? ` · difference from run baseline ${difference}` : ''}.`}>
              <span className="do-dot" />
              <span className="do-name">{r.direction}</span>
              <span className="do-meta">{r.count}× · best observed {fmt(r.best)}
                {difference ? ` · Δ from baseline ${difference}` : ''}</span>
            </button>
          )
        })}
      </div>
    </div>
  )
}
