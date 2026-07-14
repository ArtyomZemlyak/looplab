import React, { useRef } from 'react'
import { useDialogFocus } from './useDialogFocus.js'

/** Shared modal shell kept separate so a small public-safe panel need not download the owner hub. */
export default function PanelShell({ title, sub, onClose, children, wide }) {
  const dialogRef = useRef(null)
  useDialogFocus(dialogRef, onClose)
  return <div className="overlay"
    onMouseDown={event => { if (event.target === event.currentTarget) onClose?.() }}>
    <div ref={dialogRef} className="panel" role="dialog" aria-modal="true" aria-label={title}
      tabIndex={-1} style={wide ? { width: 'min(1100px, 95%)' } : {}}>
      <div className="panel-h"><span className="ttl">{title}</span>
        {sub && <span className="pill">{sub}</span>}<span className="right" />
        <button className="btn sm ghost" aria-label={`Close ${title}`} onClick={onClose}>✕</button>
      </div>
      <div className="panel-b">{children}</div>
    </div>
  </div>
}
