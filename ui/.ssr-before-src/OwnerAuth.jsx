import React, { useCallback, useEffect, useRef, useState } from 'react'
import { authStatus, clearOwnerToken, verifyOwnerToken } from './api.js'
import { deadlineRequest } from './requestDeadline.js'

const AUTH_REQUEST_TIMEOUT_MS = 10_000

const ownerAccessState = result => {
  if (!result || typeof result !== 'object'
      || typeof result.required !== 'boolean' || typeof result.authenticated !== 'boolean') return null
  return result.required === false || result.authenticated === true ? 'ready' : 'locked'
}

export default function OwnerAuth({ children, label = 'LoopLab' }) {
  const [resource, setResource] = useState({ status: 'loading', error: '' })
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  const mountedRef = useRef(false)
  const statusRequestRef = useRef(null)
  const unlockRequestRef = useRef(null)
  const inputRef = useRef(null)
  const headingRef = useRef(null)
  const errorRef = useRef(null)

  const check = useCallback(async () => {
    if (statusRequestRef.current) return
    const timed = deadlineRequest(signal => authStatus({ signal }), AUTH_REQUEST_TIMEOUT_MS)
    statusRequestRef.current = timed
    setResource({ status: 'loading', error: '' })
    try {
      const result = await timed.promise
      if (!mountedRef.current || statusRequestRef.current !== timed) return
      const status = ownerAccessState(result)
      if (!status) throw new Error('invalid owner access response')
      setResource({ status, error: '' })
    } catch (error) {
      if (!mountedRef.current || statusRequestRef.current !== timed) return
      setResource({ status: 'error', error: error?.name === 'TimeoutError'
        ? 'Owner access check timed out. Check your connection and retry.'
        : 'Owner access could not be checked. Retry when the service is reachable.' })
    } finally {
      if (statusRequestRef.current === timed) {
        statusRequestRef.current = null
      }
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    check()
    return () => {
      mountedRef.current = false
      statusRequestRef.current?.controller.abort()
      unlockRequestRef.current?.controller.abort()
      statusRequestRef.current = null
      unlockRequestRef.current = null
    }
  }, [check])
  useEffect(() => {
    document.title = `${label} · LoopLab`
  }, [label])
  useEffect(() => {
    if (resource.error) errorRef.current?.focus({ preventScroll: true })
    else if (resource.status === 'locked') inputRef.current?.focus({ preventScroll: true })
    else if (resource.status !== 'ready') headingRef.current?.focus({ preventScroll: true })
  }, [resource.status, resource.error])

  const unlock = async event => {
    event.preventDefault()
    if (!token || unlockRequestRef.current) return
    setBusy(true)
    setResource({ status: 'locked', error: '' })
    const submittedToken = token
    const timed = deadlineRequest(signal => verifyOwnerToken(submittedToken, { signal }),
      AUTH_REQUEST_TIMEOUT_MS)
    unlockRequestRef.current = timed
    try {
      const result = await timed.promise
      if (!mountedRef.current || unlockRequestRef.current !== timed) return
      if (!result || typeof result !== 'object' || result.ok !== true) {
        throw new Error('invalid owner verification response')
      }
      setToken('')
      setResource({ status: 'ready', error: '' })
    } catch (error) {
      if (!mountedRef.current || unlockRequestRef.current !== timed) return
      if (error?.status === 401) clearOwnerToken()
      setResource({ status: 'locked', error: error?.status === 401
        ? 'That owner token was not accepted.'
        : error?.name === 'TimeoutError'
          ? 'Token verification timed out. Your entry was kept; retry when the service is reachable.'
          : 'The owner token could not be verified. Your entry was kept; try again.' })
    } finally {
      if (unlockRequestRef.current === timed) {
        unlockRequestRef.current = null
        if (mountedRef.current) setBusy(false)
      }
    }
  }

  if (resource.status === 'ready') return children
  const title = resource.status === 'locked' ? 'Unlock LoopLab controls'
    : resource.status === 'error' ? 'Could not check owner access' : 'Connecting to LoopLab'
  return <main className="auth-gate" data-route-main tabIndex={-1}
    aria-busy={resource.status === 'loading' ? 'true' : undefined}>
    <div className="auth-card">
      <div className="auth-mark" aria-hidden="true">◉</div>
      <h1 ref={headingRef} tabIndex={-1}>{title}</h1>
      {resource.status === 'loading' && <p role="status" aria-live="polite">Checking owner access…</p>}
      {resource.status === 'error' && <>
        <div ref={errorRef} className="auth-error" role="alert" tabIndex={-1}>{resource.error}</div>
        <button className="btn primary" type="button" onClick={check}>Retry access check</button>
      </>}
      {resource.status === 'locked' && <form onSubmit={unlock} aria-busy={busy ? 'true' : undefined}>
        <p id="owner-token-help">This deployment protects run controls. Enter the <code>LOOPLAB_UI_TOKEN</code> set by the operator.</p>
        <label htmlFor="owner-token">Owner token</label>
        <input ref={inputRef} id="owner-token" className="auth-input" type="password"
          autoComplete="off" autoCapitalize="none" spellCheck={false}
          aria-describedby={`owner-token-help${resource.error ? ' owner-token-error' : ''}`}
          value={token} onChange={event => {
            setToken(event.target.value)
            if (resource.error) setResource({ status: 'locked', error: '' })
          }} />
        {resource.error && <div ref={errorRef} id="owner-token-error" className="auth-error"
          role="alert" tabIndex={-1}>{resource.error}</div>}
        <button className="btn primary" type="submit" disabled={!token || busy}>
          {busy ? 'Unlocking…' : 'Unlock this tab'}
        </button>
        <p className="muted">The token stays in this browser tab and is never embedded in shared pages.</p>
      </form>}
    </div>
  </main>
}
