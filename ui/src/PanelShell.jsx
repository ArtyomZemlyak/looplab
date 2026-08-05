import React, { createContext, useContext, useRef } from 'react'
import { useDialogFocus } from './useDialogFocus.js'

// A panel opened from the RUN menu is an overlay over that run's workspace — it is a detour, and
// Escape returns you to the graph. The same component reached as a LoopLab DESTINATION (`#/memory`,
// `#/knowledge`, `#/gpu`) is not a detour: it is the whole screen, and rendering it `aria-modal`
// would make the LoopLab menu in its own header inert. One context switches presentation without
// forking any panel body.
export const PanelPresentationContext = createContext('overlay')

/** Shared modal shell kept separate so a small public-safe panel need not download the owner hub. */
export default function PanelShell({ title, sub, onClose, children, wide }) {
  const page = useContext(PanelPresentationContext) === 'page'
  const dialogRef = useRef(null)
  useDialogFocus(dialogRef, onClose, !page)
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
      tabIndex={-1} style={wide ? { width: 'min(1100px, 95%)' } : {}}>
      <div className="panel-h"><span className="ttl panel-title">{title}</span>
        {sub && <span className="pill panel-sub" title={typeof sub === 'string' ? sub : undefined}>{sub}</span>}<span className="right" />
        <button className="btn sm ghost panel-close" aria-label={`Close ${title}`} onClick={onClose}>✕</button>
      </div>
      <div className="panel-b">{children}</div>
    </div>
  </div>
}
