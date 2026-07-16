import React, { useMemo } from 'react'
import { fmt } from './util.js'
import { directionProfit } from './report.js'

// Directions overview (round-6 redesign): the "summary first" lens over the search, now a COMPACT
// row of clickable theme chips instead of the old screen-eating treemap. Each chip carries the same
// descriptive information — the theme, its experiment count, best observed metric and signed
// difference from the run baseline — but in ~26px of height. Click a chip to drill the tree to that
// theme. The dot intentionally stays neutral: these aggregates do not establish a causal effect. Pure:
// derives everything from the folded node set via directionProfit (report.js), unchanged.

export default function DirectionsOverview({ state, active, onPick }) {
  const rows = useMemo(() => directionProfit(state), [state])
  if (!rows.length) return null
  return (
    <div className="directions-overview">
      {/* caption removed (the chips + tooltips are self-explanatory); the header row only appears to
          offer "back to all" while a theme is focused, so it never costs vertical space otherwise. */}
      {active && <div className="do-head">
        <button type="button" className="btn sm ghost" onClick={() => onPick(null)}>← all directions ({active})</button>
      </div>}
      <div className="do-chips" role="group" aria-label="Research directions">
        {rows.map(r => {
          const sel = active === r.theme
          const tone = r.gain == null ? 'var(--line-2)' : 'var(--accent)'
          const difference = r.gain == null ? '' : `${r.gain >= 0 ? '+' : ''}${fmt(r.gain)}`
          return (
            <button key={r.theme} type="button" aria-pressed={sel}
                    className={'do-chip' + (sel ? ' on' : '')} style={{ '--tone': tone }}
                    onClick={() => onPick(sel ? null : r.theme)}
                    title={`${r.theme}: ${r.count} experiment(s) · best observed ${fmt(r.best)}${difference ? ` · difference from run baseline ${difference}` : ''}. Descriptive only; not a causal effect or winner claim.`}>
              <span className="do-dot" />
              <span className="do-name">{r.theme}</span>
              <span className="do-meta">{r.count}× · best observed {fmt(r.best)}
                {difference ? ` · Δ from baseline ${difference}` : ''}</span>
            </button>
          )
        })}
      </div>
    </div>
  )
}
