import React, { useCallback, useEffect, useRef, useState } from 'react'
import { authStatus, clearOwnerToken, verifyOwnerToken } from './api.js'

const AUTH_REQUEST_TIMEOUT_MS = 10_000

const withDeadline = (request, controller) => {
  let timer
  let timedOut = false
  let onAbort
  const deadline = new Promise((_, reject) => {
    onAbort = () => {
      if (timedOut) return
      const error = new Error('owner access request was aborted')
      error.name = 'AbortError'
      reject(error)
    }
    controller.signal.addEventListener('abort', onAbort, { once: true })
    timer = setTimeout(() => {
      timedOut = true
      controller.abort()
      const error = new Error('owner access request timed out')
      error.name = 'TimeoutError'
      reject(error)
    }, AUTH_REQUEST_TIMEOUT_MS)
  })
  return Promise.race([request, deadline]).finally(() => {
    clearTimeout(timer)
    controller.signal.removeEventListener('abort', onAbort)
  })
}

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
  const statusBusyRef = useRef(false)
  const unlockBusyRef = useRef(false)
  const statusSequenceRef = useRef(0)
  const unlockSequenceRef = useRef(0)
  const statusRequestRef = useRef(null)
  const unlockRequestRef = useRef(null)
  const inputRef = useRef(null)
  const headingRef = useRef(null)
  const errorRef = useRef(null)

  const check = useCallback(async () => {
    if (statusBusyRef.current) return
    statusBusyRef.current = true
    const id = ++statusSequenceRef.current
    const controller = new AbortController()
    statusRequestRef.current = { id, controller }
    setResource({ status: 'loading', error: '' })
    try {
      const result = await withDeadline(authStatus({ signal: controller.signal }), controller)
      if (!mountedRef.current || statusRequestRef.current?.id !== id) return
      const status = ownerAccessState(result)
      if (!status) throw new Error('invalid owner access response')
      setResource({ status, error: '' })
    } catch (error) {
      if (!mountedRef.current || statusRequestRef.current?.id !== id) return
      setResource({ status: 'error', error: error?.name === 'TimeoutError'
        ? 'Owner access check timed out. Check your connection and retry.'
        : 'Owner access could not be checked. Retry when the service is reachable.' })
    } finally {
      if (statusRequestRef.current?.id === id) {
        statusRequestRef.current = null
        statusBusyRef.current = false
      }
    }
  }, [])

  useEffect(() => {
    mountedRef.current = true
    check()
    return () => {
      mountedRef.current = false
      statusBusyRef.current = false
      unlockBusyRef.current = false
      statusSequenceRef.current += 1
      unlockSequenceRef.current += 1
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
    if (!token || unlockBusyRef.current) return
    unlockBusyRef.current = true
    setBusy(true)
    setResource({ status: 'locked', error: '' })
    const submittedToken = token
    const id = ++unlockSequenceRef.current
    const controller = new AbortController()
    unlockRequestRef.current = { id, controller }
    try {
      const result = await withDeadline(
        verifyOwnerToken(submittedToken, { signal: controller.signal }), controller,
      )
      if (!mountedRef.current || unlockRequestRef.current?.id !== id) return
      if (!result || typeof result !== 'object' || result.ok !== true) {
        throw new Error('invalid owner verification response')
      }
      setToken('')
      setResource({ status: 'ready', error: '' })
    } catch (error) {
      if (!mountedRef.current || unlockRequestRef.current?.id !== id) return
      if (error?.status === 401) clearOwnerToken()
      setResource({ status: 'locked', error: error?.status === 401
        ? 'That owner token was not accepted.'
        : error?.name === 'TimeoutError'
          ? 'Token verification timed out. Your entry was kept; retry when the service is reachable.'
          : 'The owner token could not be verified. Your entry was kept; try again.' })
    } finally {
      if (unlockRequestRef.current?.id === id) {
        unlockRequestRef.current = null
        unlockBusyRef.current = false
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
