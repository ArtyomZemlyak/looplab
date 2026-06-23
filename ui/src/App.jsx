import React, { useEffect, useState } from 'react'
import RunList from './RunList.jsx'
import RunView from './RunView.jsx'
import Settings from './Settings.jsx'

// Tiny hash router: #/run/<id> opens a run, #/settings opens the settings page.
function parseHash() {
  const h = location.hash
  if (h === '#/settings') return { view: 'settings' }
  const m = h.match(/^#\/run\/(.+)$/)
  return m ? { view: 'run', id: decodeURIComponent(m[1]) } : { view: 'list' }
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
  if (route.view === 'run') return <RunView key={route.id} runId={route.id} onBack={back} />
  if (route.view === 'settings') return <Settings onBack={back} />
  return <RunList onOpen={open} onSettings={settings} />
}
