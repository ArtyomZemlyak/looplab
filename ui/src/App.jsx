import React, { useEffect, useState } from 'react'
import RunList from './RunList.jsx'
import RunView from './RunView.jsx'
import Settings from './Settings.jsx'
import AssistantChat from './AssistantChat.jsx'
import SharedAssistant from './SharedAssistant.jsx'
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
  if (route.view === 'run') return <RunView key={route.id} runId={route.id} onBack={back} />
  if (route.view === 'settings') return <Settings onBack={back} />
  if (route.view === 'assistant') return <AssistantChat onBack={back} />
  if (route.view === 'shared') return <SharedAssistant sid={route.id} onBack={back} />
  return <RunList onOpen={open} onSettings={settings} onAssistant={assistant} />
}
