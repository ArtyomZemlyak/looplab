import React, { useEffect, useState } from 'react'
import { get, fmt } from './util.js'

export default function RunList({ onOpen }) {
  const [runs, setRuns] = useState(null)
  useEffect(() => {
    const load = () => get('/api/runs').then(setRuns).catch(() => setRuns([]))
    load(); const t = setInterval(load, 2500); return () => clearInterval(t)
  }, [])
  return (
    <div className="app">
      <div className="topbar"><span className="brand"><span className="dot">◉</span> LoopLab</span>
        <span className="muted">autonomous R&D — live runs</span></div>
      <div className="runlist">
        {runs == null && <div className="notice">Loading runs…</div>}
        {runs && !runs.length && <div className="notice">No runs found under the run-root. Start one with
          <code> python -m looplab.cli run examples/toy_task.json --out runs/demo</code>, then it appears here live.</div>}
        {runs && runs.map(r => (
          <div className="run-card" key={r.run_id} onClick={() => onOpen(r.run_id)}>
            <span className="pill phase">{r.phase}</span>
            <div>
              <div><b>{r.run_id}</b> <span className="muted">· {r.task_id}</span></div>
              <div className="goal">{r.goal}</div>
            </div>
            <span className="right" />
            <div style={{ textAlign: 'right' }}>
              <div>best <b>{fmt(r.best_confirmed ?? r.best_metric)}</b></div>
              <div className="muted">{r.nodes} nodes · {r.direction}</div>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
