import { useEffect, useState } from 'react'

// Energy / Reactor FX — an opt-in animated layer on top of ANY design theme. Stored separately from
// the palette theme (ll.theme): a level string written to <html data-fx="…"> so every heavy effect
// scopes under :root[data-fx]. Empty string = OFF → the attribute is removed and there is ZERO cost
// (no rule matches), so the default look and the five themes are untouched.
//   '' = off · 'subtle' = glow + light motion · 'full' = energy flows + living nodes + animated bg
export const FX_LEVELS = [
  { id: '', name: 'Off', sub: 'no effects' },
  { id: 'subtle', name: 'Subtle', sub: 'glow, light motion' },
  { id: 'full', name: 'Full', sub: 'energy flows + living nodes' },
]

const KEY = 'll.fx'
const VALID = new Set(['subtle', 'full'])

// Apply a level to <html> + persist + notify in-page listeners (so a toggle in one topbar updates the
// graph live without a reload). An unknown/empty level clears the attribute (OFF).
export function applyFx(level) {
  const root = document.documentElement
  const lvl = VALID.has(level) ? level : ''
  if (lvl) root.dataset.fx = lvl
  else delete root.dataset.fx
  try { localStorage.setItem(KEY, lvl) } catch { /* private mode */ }
  try { window.dispatchEvent(new CustomEvent('ll-fx', { detail: lvl })) } catch { /* old browser */ }
}

export function readFx() {
  try { const v = localStorage.getItem(KEY) || ''; return VALID.has(v) ? v : '' } catch { return '' }
}

// Call once at module load (App.jsx), before first paint — restores the saved level.
export function initFx() {
  const lvl = readFx()
  applyFx(lvl)
  return lvl
}

// True when the user has NOT requested reduced motion. Gate JS-mounted animations (the edge particles)
// on this so we never inject moving SVG for people who opted out — the CSS @media is the backstop.
export function motionOK() {
  try { return !window.matchMedia('(prefers-reduced-motion: reduce)').matches } catch { return true }
}

// Reactive hook: a component reads the live FX level and re-renders when it flips (same-tab via the
// 'll-fx' CustomEvent, cross-tab via 'storage'). Used by Dag to swap edge type / mount the backdrop.
export function useFx() {
  const [level, setLevel] = useState(readFx)
  useEffect(() => {
    const on = (e) => setLevel(e && typeof e.detail === 'string' ? e.detail : readFx())
    window.addEventListener('ll-fx', on)
    window.addEventListener('storage', on)
    return () => { window.removeEventListener('ll-fx', on); window.removeEventListener('storage', on) }
  }, [])
  return level
}
