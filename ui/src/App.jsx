import React, { useEffect, useState } from 'react'
import RunList from './RunList.jsx'
import RunView from './RunView.jsx'

// Tiny hash router so node/run views are deep-linkable: #/run/<id>
function parseHash() {
  const m = location.hash.match(/^#\/run\/(.+)$/)
  return m ? decodeURIComponent(m[1]) : null
}

export default function App() {
  const [run, setRun] = useState(parseHash())
  useEffect(() => {
    const on = () => setRun(parseHash())
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])
  const open = (id) => { location.hash = `#/run/${encodeURIComponent(id)}` }
  const back = () => { location.hash = '' }
  return run ? <RunView key={run} runId={run} onBack={back} /> : <RunList onOpen={open} />
}
