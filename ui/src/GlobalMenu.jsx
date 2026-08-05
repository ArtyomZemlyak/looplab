import React, { useEffect, useRef, useState } from 'react'
import { followClientRoute, nextRovingIndex } from './accessibility.jsx'
import { GLOBAL_DESTINATIONS } from './globalNav.js'

// The LoopLab menu: every surface that is true for the WHOLE installation, in one place, on every
// owner route. It is deliberately the twin of the run workspace's panel hubs (same `.more-wrap` /
// `.run-menu.more-menu` / roving `[role=menuitem]` mechanics, no new CSS) so the two menus read as
// two scopes of one system rather than two unrelated widgets — the run bar answers "what about THIS
// run", this one answers "what about LoopLab".
//
// Items are anchors, not buttons: the hash each one carries is the same URL the operator can
// bookmark, and middle-click / copy-link therefore work. `onNavigate` exists only so the run LIST
// can preserve its scroll/filter snapshot on the way out (App.jsx::navigateWithListState); a route
// without that concern omits it and the plain href navigates.
export default function GlobalMenu({ current = null, onNavigate = null, disabled = false,
  buttonRef = null }) {
  const [open, setOpen] = useState(false)
  const ownTriggerRef = useRef(null)
  const triggerRef = buttonRef || ownTriggerRef
  const menuRef = useRef(null)
  const close = (restoreFocus = false) => {
    setOpen(false)
    if (restoreFocus) requestAnimationFrame(() => {
      const target = triggerRef.current
      if (target?.isConnected) target.focus({ preventScroll: true })
    })
  }
  const onKeyDown = event => {
    if (event.key === 'Escape') { event.preventDefault(); close(true); return }
    // Menu items are roving-focus targets rather than Tab stops, exactly like the run panel hubs.
    if (event.key === 'Tab' && event.shiftKey) { event.preventDefault(); close(true); return }
    const items = [...(menuRef.current?.querySelectorAll('[role="menuitem"]') || [])]
    const next = nextRovingIndex(event.key,
      Math.max(0, items.indexOf(document.activeElement)), items.length)
    if (next == null) return
    event.preventDefault(); items[next]?.focus()
  }
  useEffect(() => {
    if (!open) return undefined
    const frame = requestAnimationFrame(() =>
      menuRef.current?.querySelector('[role="menuitem"]')?.focus({ preventScroll: true }))
    return () => cancelAnimationFrame(frame)
  }, [open])
  return <div className="more-wrap global-menu-wrap">
    <button ref={triggerRef} type="button" className="btn sm ghost global-menu-btn" disabled={disabled}
      aria-haspopup="menu" aria-expanded={open} aria-controls="looplab-global-menu"
      title="Installation-wide surfaces — not scoped to any one run"
      onClick={() => setOpen(value => !value)}>LoopLab ▾</button>
    {open && <>
      <div className="menu-backdrop" aria-hidden="true" onClick={() => close(true)} />
      <div ref={menuRef} id="looplab-global-menu" className="run-menu more-menu" role="menu"
        aria-label="LoopLab — installation-wide" onClick={event => event.stopPropagation()}
        onKeyDown={onKeyDown}
        onBlur={event => {
          if (event.relatedTarget !== triggerRef.current
            && !event.currentTarget.contains(event.relatedTarget)) close(false)
        }}>
        <div className="mi-label">Whole installation</div>
        {GLOBAL_DESTINATIONS.map(entry => <a key={entry.key} role="menuitem" tabIndex={-1}
          className={'mi' + (current === entry.key ? ' on' : '')} href={entry.hash} title={entry.title}
          aria-current={current === entry.key ? 'page' : undefined}
          onClick={event => {
            close(false)
            // followClientRoute leaves a modified click (new tab / window) as a real link and only
            // intercepts the plain one, which is when the list snapshot has to be handed over.
            if (onNavigate) followClientRoute(event, () => onNavigate(entry.hash, entry.key))
          }}>{entry.label}</a>)}
      </div>
    </>}
  </div>
}
