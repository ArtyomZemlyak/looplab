import { useEffect, useRef, useState } from 'react'

// Subscribe to a run's live folded state over SSE. The server emits `event: state` frames whose
// data is { state, seq }. Returns the latest live state + connection status. Auto-reconnects.
export function useRunState(runId) {
  const [live, setLive] = useState(null)
  const [seq, setSeq] = useState(-1)
  const [connected, setConnected] = useState(false)
  const esRef = useRef(null)

  useEffect(() => {
    if (!runId) return
    let stopped = false
    let timer = null
    let lastSeq = -2, lastAlive
    const reconnect = (delay) => { if (stopped) return; clearTimeout(timer); timer = setTimeout(connect, delay) }
    function connect() {
      const es = new EventSource(`/api/runs/${runId}/events`)
      esRef.current = es
      es.addEventListener('state', (e) => {
        let p
        try { p = JSON.parse(e.data) } catch { return }  // ignore a torn/partial SSE frame
        setConnected(true)
        // Re-render on a seq change OR an engine_running flip (a zombie's liveness changes with no
        // new event/seq); track lastAlive in the closure (NOT stale React `live`) to avoid churn.
        const alive = p.state && p.state.engine_running
        if (p.seq === lastSeq && alive === lastAlive) return
        lastSeq = p.seq; lastAlive = alive; setLive(p.state); setSeq(p.seq)
      })
      // `done` = the run reached a terminal state and the server ends the stream. We do NOT treat it
      // as "stop forever": reconnect-poll so a reopen (fork / branch / add-experiment) is picked up
      // within a couple seconds. Closing-and-never-reconnecting is what made those actions invisible
      // until a manual reload (#8). The state handler dedups by seq, so the poll is cheap when idle.
      es.addEventListener('done', () => { es.close(); reconnect(2500) })
      es.onerror = () => {
        setConnected(false); es.close()
        reconnect(1500)
      }
    }
    connect()
    return () => { stopped = true; clearTimeout(timer); esRef.current && esRef.current.close() }
  }, [runId])

  return { live, seq, connected }
}

// Browser notifications for finish / approval / failure-spike.
export function useNotifications(enabled, state) {
  const prev = useRef({ phase: null, fails: 0 })
  useEffect(() => {
    if (!enabled || !state) return
    // `Notification` is absent in insecure/unsupported contexts — referencing it bare throws.
    if (!('Notification' in window)) return
    if (Notification.permission === 'default') Notification.requestPermission()
    const phase = state.phase
    const fails = Object.values(state.nodes || {}).filter(n => n.status === 'failed').length
    const notify = (t, b) => { try { new Notification(t, { body: b }) } catch {} }
    if (prev.current.phase && phase !== prev.current.phase) {
      if (phase === 'finished') notify('LoopLab — run finished', `best=${state.best_node_id ?? '—'}`)
      if (phase === 'approval') notify('LoopLab — approval needed', state.goal || '')
      if (phase === 'spec_approval') notify('LoopLab — eval spec needs ratification', '')
    }
    if (fails - prev.current.fails >= 3) notify('LoopLab — failures spiking', `${fails} failed nodes`)
    prev.current = { phase, fails }
  }, [enabled, state])
}
