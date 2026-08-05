import React, { useRef } from 'react'
import { useDialogFocus } from './useDialogFocus.js'

// `wide` is a READING width: it was picked for memo drawers, trust reports and tables, all of which
// lay out fine — and read better — inside ~1100px. It is the wrong width for content whose intrinsic
// minimum GROWS with the data, and the Card kanban is exactly that: `grid-auto-flow: column` at a
// 225px floor needs ~1390px for six lanes, so the board overflowed its own panel at every viewport
// (measured 1623px of lanes inside a 1070px content box). `size="board"` is that second width; it is
// still a percentage-capped `min()` so the JupyterHub proxy's narrower window gets a panel that fits
// rather than one clipped by the browser edge.
const PANEL_WIDTHS = { wide: 'min(1100px, 95%)', board: 'min(1560px, 96%)' }

/** Shared modal shell kept separate so a small public-safe panel need not download the owner hub. */
export default function PanelShell({ title, sub, onClose, children, wide, size }) {
  const dialogRef = useRef(null)
  useDialogFocus(dialogRef, onClose)
  const width = PANEL_WIDTHS[size] || (wide ? PANEL_WIDTHS.wide : null)
  return <div className="overlay"
    onMouseDown={event => { if (event.target === event.currentTarget) onClose?.() }}>
    <div ref={dialogRef} className="panel" role="dialog" aria-modal="true" aria-label={title}
      tabIndex={-1} style={width ? { width } : {}}>
      <div className="panel-h"><span className="ttl panel-title">{title}</span>
        {sub && <span className="pill panel-sub" title={typeof sub === 'string' ? sub : undefined}>{sub}</span>}<span className="right" />
        <button className="btn sm ghost panel-close" aria-label={`Close ${title}`} onClick={onClose}>✕</button>
      </div>
      <div className="panel-b">{children}</div>
    </div>
  </div>
}
