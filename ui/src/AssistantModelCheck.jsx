import React, { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { deadlineGet } from './util.js'
import { LlmHealth, readHealthRecovery } from './LlmHealth.jsx'
import { loadSettingsSchema } from './settingsSchema.js'
import { validateSettingsResource } from './settingsModel.js'
import { claimPublisher, releasePublisher, captureAuthoritativeRead,
  confirmAuthoritativeRead, publish } from './settingsLaunchGuard.js'

// Same saved configuration, provider operation and recovery record as Settings.
// This surface never edits settings or credentials, and opening it never calls a model.
export default function AssistantModelCheck({ onSettings }) {
  const ownerRef = useRef(null)
  const rootRef = useRef(null)
  const requestRef = useRef(null)
  const mutationRef = useRef(null)
  const mountedRef = useRef(false)
  const [snapshot, setSnapshot] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [recovery, setRecovery] = useState(false)
  const [loading, setLoading] = useState(false)
  const load = async (explicit = false) => {
    if (mutationRef.current) return false
    requestRef.current?.controller.abort()
    const request = deadlineGet('/api/settings', 15_000)
    requestRef.current = request
    const read = captureAuthoritativeRead(ownerRef.current, { explicit })
    setLoading(true)
    try {
      const [data, schema] = await Promise.all([request.promise, loadSettingsSchema()])
      if (!mountedRef.current || requestRef.current !== request) return false
      validateSettingsResource(data, schema)
      if (!confirmAuthoritativeRead(read)) throw new Error('reconciliation required')
      setSnapshot(data)
      setError('')
      return true
    } catch {
      if (mountedRef.current && requestRef.current === request) {
        setSnapshot(null)
        setError('Saved model settings need a fresh read. Refresh here or review Model settings; no provider request was started by this read.')
      }
      return false
    } finally {
      if (mountedRef.current && requestRef.current === request) setLoading(false)
    }
  }
  useLayoutEffect(() => {
    ownerRef.current = claimPublisher()
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      requestRef.current?.controller.abort()
      releasePublisher(ownerRef.current, { retain: !!mutationRef.current || !!readHealthRecovery() })
    }
  }, [])
  useEffect(() => {
    if (document.activeElement === document.body) rootRef.current?.focus({ preventScroll: true })
    load()
  }, [])
  useLayoutEffect(() => {
    const unresolved = recovery || !!readHealthRecovery()
    const blocked = !snapshot || loading || !!error || !!busy || unresolved
    publish(ownerRef.current, { active: true, blocked,
      status: blocked ? 'model-check' : 'ready',
      reason: unresolved ? 'A model check has an unresolved outcome. Check its previous result before starting a run.'
        : busy ? 'A model connection check is still running.'
          : blocked ? 'Load the saved model configuration before starting a run.' : '',
      retainable: unresolved || !!error || !!busy,
      resolved: !!snapshot && !loading && !error && !busy && !unresolved })
  }, [snapshot, loading, error, busy, recovery])
  const beginAction = kind => {
    if (mutationRef.current || !snapshot || loading || error) return null
    const token = { kind }
    mutationRef.current = token
    setBusy(kind)
    return token
  }
  const finishAction = token => {
    if (mutationRef.current !== token) return
    mutationRef.current = null
    if (mountedRef.current) setBusy('')
  }
  return <div ref={rootRef} className="asst-model-check" role="region" tabIndex={-1} aria-label="Model connection check">
    <p>Checks the server-resolved active model. Your message stays in the composer.
      A provider request may be billed. Leaving does not stop provider work or billing.</p>
    {loading && <p role="status">Reading saved model settings…</p>}
    {error && <div role="alert"><p>{error}</p>
      <button type="button" className="btn sm" disabled={loading} onClick={() => load(true)}>Refresh saved settings</button>
    </div>}
    <LlmHealth savedSettingsRevision={snapshot?.settings_revision}
      savedSecretRevision={snapshot?.secret_revision} actionBlocked={loading || !!error}
      actionKind={busy} beginAction={beginAction} finishAction={finishAction}
      reloadSavedSettings={() => load(true)} onRecoveryChange={setRecovery} />
    <p><button type="button" className="btn sm ghost" onClick={onSettings}>Edit model settings</button></p>
  </div>
}
