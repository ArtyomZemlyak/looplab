import React, { useEffect, useRef, useState } from 'react'
import { authStatus, clearOwnerToken, verifyOwnerToken } from './api.js'

export default function OwnerAuth({ children, label = 'LoopLab' }) {
  const [resource, setResource] = useState({ status: 'loading', error: '' })
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  const inputRef = useRef(null)
  const gateRef = useRef(null)

  const check = () => {
    setResource({ status: 'loading', error: '' })
    authStatus()
      .then(result => setResource({
        status: result?.required === false || result?.authenticated === true ? 'ready' : 'locked', error: '',
      }))
      .catch(error => setResource({ status: 'error', error: error.message || 'Could not reach LoopLab' }))
  }
  useEffect(check, [])
  useEffect(() => {
    document.title = `${label} · LoopLab`
  }, [label])
  useEffect(() => {
    if (resource.status === 'locked') inputRef.current?.focus({ preventScroll: true })
    else gateRef.current?.focus({ preventScroll: true })
  }, [resource.status])

  const unlock = async event => {
    event.preventDefault()
    if (!token || busy) return
    setBusy(true)
    try {
      await verifyOwnerToken(token)
      setToken('')
      setResource({ status: 'ready', error: '' })
    } catch (error) {
      clearOwnerToken()
      setResource({ status: 'locked', error: error?.status === 401
        ? 'That owner token was not accepted.' : (error.message || 'Could not verify the token.') })
    } finally { setBusy(false) }
  }

  if (resource.status === 'ready') return children
  return <main ref={gateRef} className="auth-gate" data-route-main tabIndex={-1} aria-live="polite">
    <div className="auth-card">
      <div className="auth-mark" aria-hidden="true">◉</div>
      <h1>{resource.status === 'locked' ? 'Unlock LoopLab controls' : 'Connecting to LoopLab'}</h1>
      {resource.status === 'loading' && <p>Checking owner access…</p>}
      {resource.status === 'error' && <>
        <p role="alert">{resource.error}</p>
        <button className="btn primary" onClick={check}>Retry</button>
      </>}
      {resource.status === 'locked' && <form onSubmit={unlock}>
        <p>This deployment protects run controls. Enter the <code>LOOPLAB_UI_TOKEN</code> set by the operator.</p>
        <label htmlFor="owner-token">Owner token</label>
        <input ref={inputRef} id="owner-token" className="auth-input" type="password"
          autoComplete="off" autoCapitalize="none" spellCheck={false}
          value={token} onChange={event => setToken(event.target.value)} />
        {resource.error && <div className="auth-error" role="alert">{resource.error}</div>}
        <button className="btn primary" type="submit" disabled={!token || busy}>
          {busy ? 'Unlocking…' : 'Unlock this tab'}
        </button>
        <p className="muted">The token stays in this browser tab and is never embedded in shared pages.</p>
      </form>}
    </div>
  </main>
}
