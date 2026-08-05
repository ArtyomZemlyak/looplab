import React, { createContext, useContext, useRef } from 'react'
import { useDialogFocus } from './useDialogFocus.js'

// `wide` is a READING width: it was picked for memo drawers, trust reports and tables, all of which
// lay out fine — and read better — inside ~1100px. It is the wrong width for content whose intrinsic
// minimum GROWS with the data, and the Card kanban is exactly that: `grid-auto-flow: column` at a
// 225px floor needs ~1390px for six lanes, so the board overflowed its own panel at every viewport
// (measured 1623px of lanes inside a 1070px content box). `size="board"` is that second width; it is
// still a percentage-capped `min()` so the JupyterHub proxy's narrower window gets a panel that fits
// rather than one clipped by the browser edge.
const PANEL_WIDTHS = { wide: 'min(1100px, 95%)', board: 'min(1560px, 96%)' }

// A panel opened from the RUN menu is an overlay over that run's workspace — it is a detour, and
// Escape returns you to the graph. The same component reached as a LoopLab DESTINATION (`#/memory`,
// `#/knowledge`, `#/gpu`) is not a detour: it is the whole screen, and rendering it `aria-modal`
// would make the LoopLab menu in its own header inert. One context switches presentation without
// forking any panel body. The width above is an OVERLAY concern only: a page fills its container,
// so `size`/`wide` are deliberately not consulted on that branch.
export const PanelPresentationContext = createContext('overlay')

/** Shared modal shell kept separate so a small public-safe panel need not download the owner hub. */
export default function PanelShell({ title, sub, onClose, children, wide, size }) {
  const page = useContext(PanelPresentationContext) === 'page'
  const dialogRef = useRef(null)
  useDialogFocus(dialogRef, onClose, !page)
  const width = PANEL_WIDTHS[size] || (wide ? PANEL_WIDTHS.wide : null)
  if (page) return <section className="panel panel-page" aria-label={title}>
    <div className="panel-h"><span className="ttl panel-title">{title}</span>
      {sub && <span className="pill panel-sub" title={typeof sub === 'string' ? sub : undefined}>{sub}</span>}
      <span className="right" />
    </div>
    <div className="panel-b">{children}</div>
  </section>
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
