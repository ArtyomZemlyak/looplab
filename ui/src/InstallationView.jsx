import React, { lazy } from 'react'
import GlobalMenu from './GlobalMenu.jsx'
import LazyBoundary from './LazyBoundary.jsx'
import { PanelPresentationContext } from './PanelShell.jsx'
import { useToast } from './useToast.js'
import { globalDestination } from './globalNav.js'

// The three LoopLab destinations that were only ever reachable from INSIDE a run's menu, even though
// none of them takes a run id: `/api/memory` + `/api/knowledge` (cross-run memory), `/api/{kind}`
// (authored prompts/skills/knowledge) and `/api/gpu` (this host's cards). The panel bodies are
// unchanged and still mount in the run workspace for old `?panel=` links — only the way IN is new.
const loadPanels = () => import('./panels.jsx')
const lazyNamed = name => lazy(() => loadPanels().then(module => ({ default: module[name] })))
const MemoryPanel = lazyNamed('MemoryPanel')
const AuthoringPanel = lazyNamed('AuthoringPanel')
const GpuPanel = lazyNamed('GpuPanel')

const BODY = { memory: MemoryPanel, knowledge: AuthoringPanel, gpu: GpuPanel }

export default function InstallationView({ view, onBack }) {
  const destination = globalDestination(view)
  const Body = BODY[view]
  const [toast, showToast] = useToast()
  if (!destination || !Body) return null
  return <div className="app">
    <div className="topbar">
      <GlobalMenu current={view} />
      <button className="btn sm ghost" onClick={onBack}>← runs</button>
      <span className="ttl" style={{ fontWeight: 700, fontSize: 15 }}>{destination.label}</span>
      <span className="muted">{destination.title}</span>
      <span className="spacer" style={{ flex: 1 }} />
    </div>
    <main className="installation-page" data-route-main tabIndex={-1}>
      {/* `page` presentation, not the overlay one: this IS the destination, so it must not render
          aria-modal over the LoopLab menu that got the operator here. */}
      <PanelPresentationContext.Provider value="page">
        <LazyBoundary label={destination.label} resetKey={view}>
          <Body onClose={onBack} onToast={showToast} />
        </LazyBoundary>
      </PanelPresentationContext.Provider>
    </main>
    {toast && <div className="toast" role="status" aria-live="polite">{toast}</div>}
  </div>
}
