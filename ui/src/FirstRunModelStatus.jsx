import React, { useEffect, useState } from 'react'
import { usePoll } from './hooks.js'
import { deadlineGet } from './util.js'
import { MODEL_CHECK_EVENT, modelConnectionView, readModelCheck } from './modelConnection.js'

export default function FirstRunModelStatus({ onSettings }) {
  const [snapshot, setSnapshot] = useState(null)
  const [check, setCheck] = useState(readModelCheck)
  const [routeEpoch, setRouteEpoch] = useState(0)
  useEffect(() => {
    const onCheck = event => setCheck(event.detail)
    const onRoute = () => { setSnapshot(null); setRouteEpoch(value => value + 1) }
    window.addEventListener(MODEL_CHECK_EVENT, onCheck)
    window.addEventListener('hashchange', onRoute)
    return () => {
      window.removeEventListener(MODEL_CHECK_EVENT, onCheck)
      window.removeEventListener('hashchange', onRoute)
    }
  }, [])
  usePoll(alive => {
    const request = deadlineGet('/api/settings', 10_000)
    request.promise.then(value => {
      if (alive()) setSnapshot(value?.settings && value?.settings_revision && value?.secret_revision
        ? value : { error: true })
    }).catch(() => { if (alive()) setSnapshot({ error: true }) })
    return request
  }, 30_000, [routeEpoch], { pauseHidden: true })
  const status = modelConnectionView(snapshot, check)
  return <div className="asst-new-run-hint">
    <span role="status" className={status.tone ? `model-connection-${status.tone}` : ''}>{status.text}</span>
    <button type="button" className="btn sm" onClick={onSettings}>Model settings</button>
  </div>
}
