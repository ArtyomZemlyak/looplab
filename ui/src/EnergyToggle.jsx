import React, { useEffect, useState } from 'react'
import { FX_LEVELS, readFx, applyFx } from './fx.js'
import { OpIcon } from './icons.jsx'

// Topbar control for the Energy / Reactor FX mode (Off / Subtle / Full). Mirrors ThemeSwitcher's
// popover so it reads as a sibling of the theme picker. Mounted in both the run-view and run-list
// topbars; only one is on screen at a time (separate routes), so they never fight over the level.
export default function EnergyToggle() {
  const [open, setOpen] = useState(false)
  const [level, setLevel] = useState(readFx)
  useEffect(() => { applyFx(level) }, [level])
  // stay in sync if some other surface flips the level (e.g. the other topbar, another tab)
  useEffect(() => {
    const on = (e) => setLevel(e && typeof e.detail === 'string' ? e.detail : readFx())
    window.addEventListener('ll-fx', on)
    return () => window.removeEventListener('ll-fx', on)
  }, [])

  const on = !!level
  const cur = FX_LEVELS.find(l => l.id === level) || FX_LEVELS[0]
  const pick = (id) => { setLevel(id); setOpen(false) }

  return <div className="fx-switch">
    <button className={'btn sm ghost' + (on ? ' primary' : '')} title="Energy / Reactor FX — animated graph"
            onClick={() => setOpen(o => !o)}><OpIcon name="bolt" size={12} /> Energy{on ? `: ${cur.name}` : ''}</button>
    {open && <>
      <div className="th-backdrop" onClick={() => setOpen(false)} />
      <div className="th-menu fx-menu">
        <div className="th-menu-h">Energy FX</div>
        {FX_LEVELS.map(l => <button key={l.id || 'off'} className={'th-opt' + (l.id === level ? ' on' : '')}
          onClick={() => pick(l.id)}>
          <span className="th-name"><b>{l.name}</b><span className="th-sub">{l.sub}</span></span>
          {l.id === level && <span className="th-check">✓</span>}
        </button>)}
      </div>
    </>}
  </div>
}
