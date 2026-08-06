import React, { useEffect, useRef, useState } from 'react'
import { followClientRoute, nextRovingIndex } from './accessibility.jsx'
import { GLOBAL_DESTINATIONS } from './globalNav.js'

// The LoopLab mark, in its ONE spelling. Every topbar in the app renders it from here rather than
// hand-writing `<span className="brand">…`, because the word used to appear TWICE side by side in the
// run header — once as this inert mark, once as the menu button labelled "LoopLab ▾" — and six
// screens each spelled the mark out themselves, which is how two of them would eventually disagree.
//
// It stays a `<span>` even though `GlobalMenu` below wraps it in a real `<button>`: the mark is the
// part of the trigger that is ALLOWED to disappear. `styles.css` hides `.run-head .brand` outright at
// ≤900px to make room in the run header, so if the trigger itself carried the class, the whole
// installation menu would vanish from every phone-width run view. Keeping the class on the inner span
// means the narrow run header collapses to a bare disclosure control that still opens the menu — and
// is why the arrow lives outside this element and the button below carries its own `aria-label`
// (a `display:none` label is removed from the accessibility tree, name and all).
export function BrandMark() {
  return <span className="brand"><span className="dot">◉</span> LoopLab</span>
}

// The LoopLab menu: every surface that is true for the WHOLE installation, in one place, on every
// owner route. It is deliberately the twin of the run workspace's panel hubs (same `.more-wrap` /
// `.run-menu.more-menu` / roving `[role=menuitem]` mechanics, no new CSS) so the two menus read as
// two scopes of one system rather than two unrelated widgets — the run bar answers "what about THIS
// run", this one answers "what about LoopLab".
//
// The mark IS the trigger. The alternative — a mark that navigates to `#/` and an arrow beside it
// that opens the menu — was rejected: `Runs (#/)` is already the first item of this menu, so a mark
// that both navigated and disclosed would have two meanings for one click and no way to spell that
// in `aria-haspopup="menu"`, whose whole contract is that activation opens the menu. One control,
// one meaning; the destination the mark used to imply is the first thing the menu offers.
//
// The arrow follows the word rather than replacing the `◉` or sitting before it: `◉` is the identity
// glyph (the `reactor` theme styles `.brand` specifically), and a disclosure arrow that PRECEDES its
// label reads as a collapsed tree node, not a menu. Following the word it also falls outside
// `.brand`, which is what keeps the control visible at the widths that hide the mark.
//
// Items are anchors, not buttons: the hash each one carries is the same URL the operator can
// bookmark, and middle-click / copy-link therefore work. `onNavigate` exists only so the run LIST
// can preserve its scroll/filter snapshot on the way out (App.jsx::navigateWithListState); a route
// without that concern omits it and the plain href navigates.
//
// Public routes never mount this component at all — they render `<BrandMark />` instead, so a
// reviewer sees an inert mark rather than a dead button or a button that opens an empty menu. The
// branch is spelled at the call site (RunView), where `reviewMode` is known.
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
    {/* The accessible name is explicit and starts with the visible word (WCAG 2.5.3 Label in Name):
        the mark inside is `display:none` on a narrow run header, and a hidden label would otherwise
        leave this button nameless exactly where it is reduced to a lone arrow. */}
    <button ref={triggerRef} type="button" className="btn sm ghost global-menu-btn" disabled={disabled}
      aria-haspopup="menu" aria-expanded={open} aria-controls="looplab-global-menu"
      aria-label="LoopLab — installation-wide surfaces"
      title="Installation-wide surfaces — not scoped to any one run"
      onClick={() => setOpen(value => !value)}>
      <BrandMark />
      <span className="global-menu-caret" aria-hidden="true">▾</span>
    </button>
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
