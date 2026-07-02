import React, { useEffect, useState } from 'react'

// Selectable design themes — each is a CSS-variable re-skin applied via <html data-theme>.
export const THEMES = [
  { id: 'current', name: 'Dark', sub: 'default', bg: '#0c0e12', ac: '#4aa3ff', fg: '#e6e9ef' },
  { id: 'retrowave', name: 'Retro Wave', sub: 'neon synthwave', bg: '#150c28', ac: '#ff2e97', fg: '#f6e9ff' },
  { id: 'paper', name: 'Paper', sub: 'warm light', bg: '#f4f1ea', ac: '#346ba3', fg: '#2b2722' },
  { id: 'white-scifi', name: 'White Sci-Fi', sub: 'clean HUD', bg: '#f6f9fd', ac: '#0aa6c9', fg: '#0f1b2d' },
  { id: 'old-green', name: 'Old Green', sub: 'phosphor CRT', bg: '#030a05', ac: '#5bff95', fg: '#3ff06d' },
  { id: 'reactor', name: 'Reactor', sub: 'sci-fi · pair with Energy FX', bg: '#05060f', ac: '#36e6ff', fg: '#eaf2ff' },
]

const KEY = 'll.theme'

// Apply a theme to <html> (and persist). 'current' clears the attribute (the default :root palette).
export function applyTheme(id) {
  const root = document.documentElement
  if (!id || id === 'current') root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', id)
  try { localStorage.setItem(KEY, id || 'current') } catch { /* private mode */ }
}

// Restore the theme on load — call once at app start. A `?theme=<id>` query param wins (handy for
// sharing / previewing a design via a link); otherwise the saved choice; otherwise the default.
export function initTheme() {
  let id = 'current'
  try {
    const q = new URLSearchParams(location.search).get('theme')
    id = (q && THEMES.some(t => t.id === q)) ? q : (localStorage.getItem(KEY) || 'current')
  } catch { /* ignore */ }
  applyTheme(id)
  return id
}

export default function ThemeSwitcher() {
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(() => {
    try { return localStorage.getItem(KEY) || 'current' } catch { return 'current' }
  })
  useEffect(() => { applyTheme(active) }, [active])

  const cur = THEMES.find(t => t.id === active) || THEMES[0]
  const pick = (id) => { setActive(id); setOpen(false) }

  return <div className="theme-switch">
    <button className="btn sm ghost" title="UI theme" onClick={() => setOpen(o => !o)}>
      <span className="th-dot" style={{ background: cur.ac, boxShadow: `0 0 0 2px ${cur.bg}` }} /> Theme
    </button>
    {open && <>
      <div className="th-backdrop" onClick={() => setOpen(false)} />
      <div className="th-menu">
        <div className="th-menu-h">Design</div>
        {THEMES.map(t => <button key={t.id} className={'th-opt' + (t.id === active ? ' on' : '')}
          onClick={() => pick(t.id)}>
          <span className="th-sw" style={{ background: t.bg, borderColor: t.ac }}>
            <span className="th-sw-ac" style={{ background: t.ac }} />
            <span className="th-sw-fg" style={{ background: t.fg }} />
          </span>
          <span className="th-name"><b>{t.name}</b><span className="th-sub">{t.sub}</span></span>
          {t.id === active && <span className="th-check">✓</span>}
        </button>)}
      </div>
    </>}
  </div>
}
