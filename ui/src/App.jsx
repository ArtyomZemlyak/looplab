import React, { useEffect, useState } from 'react'
import RunList from './RunList.jsx'
import RunView from './RunView.jsx'
import Settings from './Settings.jsx'
import AssistantChat from './AssistantChat.jsx'
import SharedAssistant from './SharedAssistant.jsx'
import AssistantBar from './AssistantBar.jsx'
import { initTheme } from './ThemeSwitcher.jsx'
import { initFx } from './fx.js'

initTheme()   // restore the saved design theme before first paint
initFx()      // restore the saved Energy/Reactor FX level (data-fx) before first paint

// Tiny hash router: #/run/<id> opens a run, #/settings opens the settings page.
const safeDecode = (s) => { try { return decodeURIComponent(s) } catch { return s } }
function parseHash() {
  const h = location.hash
  if (h === '#/settings') return { view: 'settings' }
  if (h === '#/assistant') return { view: 'assistant' }
  const sh = h.match(/^#\/assistant\/shared\/(.+)$/)
  if (sh) return { view: 'shared', id: safeDecode(sh[1]) }
  // #/assistant/s/<sid> — the full-page assistant opened on a specific session (carried over from the
  // bottom command bar so the conversation continues rather than starting fresh).
  const as = h.match(/^#\/assistant\/s\/(.+)$/)
  if (as) return { view: 'assistant', sid: safeDecode(as[1]) }
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
  const assistant = () => { location.hash = '#/assistant' }

  // The full-page assistant and the shared read-only view own the whole screen (they ARE the chat), so
  // the docked command bar is hidden there; everywhere else it stays pinned to the bottom.
  let content, showBar = true
  if (route.view === 'run') content = <RunView key={route.id} runId={route.id} onBack={back} />
  else if (route.view === 'settings') content = <Settings onBack={back} />
  else if (route.view === 'assistant') { content = <AssistantChat key={route.sid || 'new'} initialSid={route.sid} onBack={back} />; showBar = false }
  else if (route.view === 'shared') { content = <SharedAssistant sid={route.id} onBack={back} />; showBar = false }
  else content = <RunList onOpen={open} onSettings={settings} onAssistant={assistant} />

  return <div className="app-shell">
    <div className="app-shell-main">{content}</div>
    {showBar && <AssistantBar runId={route.view === 'run' ? route.id : null} />}
  </div>
}
