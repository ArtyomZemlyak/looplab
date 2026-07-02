import React, { useEffect, useState } from 'react'
import RunList from './RunList.jsx'
import RunView from './RunView.jsx'
import Settings from './Settings.jsx'
import SharedAssistant from './SharedAssistant.jsx'
import AssistantBar from './AssistantBar.jsx'
import { initTheme } from './ThemeSwitcher.jsx'
import { initFx } from './fx.js'

initTheme()   // restore the saved design theme before first paint
initFx()      // restore the saved Energy/Reactor FX level (data-fx) before first paint

// Tiny hash router: #/run/<id> opens a run, #/settings opens the settings page. The assistant is NOT a
// route anymore — it's one persistent component (AssistantBar) with three views (bar/drawer/full) that
// stays mounted across every route, so its conversation is never reset by navigation.
const safeDecode = (s) => { try { return decodeURIComponent(s) } catch { return s } }
function parseHash() {
  const h = location.hash
  if (h === '#/settings') return { view: 'settings' }
  const sh = h.match(/^#\/assistant\/shared\/(.+)$/)
  if (sh) return { view: 'shared', id: safeDecode(sh[1]) }
  const m = h.match(/^#\/run\/(.+)$/)
  return m ? { view: 'run', id: safeDecode(m[1]) } : { view: 'list' }
}

export default function App() {
  const [route, setRoute] = useState(parseHash())
  useEffect(() => {
    const on = () => setRoute(parseHash())
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])
  const open = (id) => { location.hash = `#/run/${encodeURIComponent(id)}` }
  const back = () => { location.hash = '' }
  const settings = () => { location.hash = '#/settings' }

  // The shared read-only view owns the whole screen (it IS a public chat), so the persistent assistant
  // is hidden there; everywhere else (list / run / settings) the assistant stays available.
  let content, hideAssistant = false
  if (route.view === 'run') content = <RunView key={route.id} runId={route.id} onBack={back} />
  else if (route.view === 'settings') content = <Settings onBack={back} />
  else if (route.view === 'shared') { content = <SharedAssistant sid={route.id} onBack={back} />; hideAssistant = true }
  else content = <RunList onOpen={open} onSettings={settings} />

  return <div className="app-shell">
    <div className="app-shell-main">{content}</div>
    {/* Mounted once, outside the route switch → the assistant persists across all navigation. */}
    <AssistantBar runId={route.view === 'run' ? route.id : null} hidden={hideAssistant} />
  </div>
}
