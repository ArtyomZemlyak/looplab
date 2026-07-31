import React, { useEffect, useState } from 'react'
import { FX_LEVELS, readFx, applyFx } from './fx.js'
import { OpIcon } from './icons.jsx'
import { useRovingRadioMenu } from './accessibility.jsx'

// Topbar control for the Energy / Reactor FX mode (Off / Subtle / Full). Mirrors ThemeSwitcher's
// popover so it reads as a sibling of the theme picker. Mounted in both the run-view and run-list
// topbars; only one is on screen at a time (separate routes), so they never fight over the level.
export default function EnergyToggle() {
  const [open, setOpen] = useState(false)
  const [level, setLevel] = useState(readFx)
  const { triggerRef, menuRef, close, onKeyDown } = useRovingRadioMenu(open, setOpen)
  useEffect(() => { applyFx(level) }, [level])
  // stay in sync if some other surface flips the level (e.g. the other topbar, another tab)
  useEffect(() => {
    const on = (e) => setLevel(e && typeof e.detail === 'string' ? e.detail : readFx())
    window.addEventListener('ll-fx', on)
    window.addEventListener('storage', on)
    return () => { window.removeEventListener('ll-fx', on); window.removeEventListener('storage', on) }
  }, [])

  const on = !!level
  const cur = FX_LEVELS.find(l => l.id === level) || FX_LEVELS[0]
  const pick = (id) => { setLevel(id); close(true) }

  return <div className="fx-switch">
    <button type="button" ref={triggerRef} className={'btn sm ghost' + (on ? ' primary' : '')}
      title="Energy / Reactor FX — animated graph" aria-haspopup="menu" aria-expanded={open}
      aria-controls="energy-switcher-menu" aria-label={`Energy effects: ${cur.name}`}
      onClick={() => setOpen(!open)}>
      <OpIcon name="bolt" size={12} />
      <span className="fx-switch-label">Energy{on ? `: ${cur.name}` : ''}</span>
    </button>
    {open && <>
      <div className="th-backdrop" aria-hidden="true" onClick={() => close(true)} />
      <div ref={menuRef} id="energy-switcher-menu" className="th-menu fx-menu" role="menu" aria-label="Energy effects"
        onKeyDown={onKeyDown} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) close(false) }}>
        <div className="th-menu-h">Energy FX</div>
        {FX_LEVELS.map(l => <button type="button" key={l.id || 'off'} role="menuitemradio" aria-checked={l.id === level}
          tabIndex={-1} className={'th-opt' + (l.id === level ? ' on' : '')}
          onClick={() => pick(l.id)}>
          <span className="th-name"><b>{l.name}</b><span className="th-sub">{l.sub}</span></span>
          {l.id === level && <span className="th-check">✓</span>}
        </button>)}
      </div>
    </>}
  </div>
}
