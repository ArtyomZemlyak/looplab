import React, { useEffect, useState } from 'react'
import RunList from './RunList.jsx'
import RunView from './RunView.jsx'
import Settings from './Settings.jsx'
import SharedAssistant from './SharedAssistant.jsx'
import AssistantBar from './AssistantBar.jsx'
import OwnerAuth from './OwnerAuth.jsx'
import { reviewManifest, reviewTokenFromLocation } from './api.js'
import { initTheme } from './ThemeSwitcher.jsx'
import { initFx } from './fx.js'
import { routeHashPath } from './runRouteState.js'

initTheme()   // restore the saved design theme before first paint
initFx()      // restore the saved Energy/Reactor FX level (data-fx) before first paint

// Tiny hash router: #/run/<id> opens a run, #/settings opens the settings page. The assistant is NOT a
// route anymore — it's one persistent component (AssistantBar) with three views (bar/drawer/full) that
// stays mounted across every route, so its conversation is never reset by navigation.
const safeDecode = (s) => { try { return decodeURIComponent(s) } catch { return s } }
function parseHash() {
  if (/\/review\/?$/.test(location.pathname)) return { view: 'review', token: reviewTokenFromLocation() }
  const h = routeHashPath(location.hash)
  if (h === '#/settings') return { view: 'settings' }
  const sh = h.match(/^#\/assistant\/shared\/(.+)$/)
  if (sh) return { view: 'shared', id: safeDecode(sh[1]) }
  const m = h.match(/^#\/run\/(.+)$/)
  return m ? { view: 'run', id: safeDecode(m[1]) } : { view: 'list' }
}

function ReviewRoute({ token }) {
  const [resource, setResource] = useState({ status: 'loading', data: null, error: '' })
  const [retry, setRetry] = useState(0)
  useEffect(() => {
    let active = true
    if (!token) {
      setResource({ status: 'gone', data: null,
        error: 'This review link is incomplete or invalid. Ask the owner for a new link.' })
      return () => { active = false }
    }
    setResource({ status: 'loading', data: null, error: '' })
    reviewManifest()
      .then(data => { if (active) setResource({ status: 'ready', data, error: '' }) })
      .catch(error => { if (active) setResource({
        status: error?.status === 401 || error?.status === 410 ? 'gone' : 'error', data: null,
        error: error?.status === 401 || error?.status === 410
          ? 'This review link is invalid, expired, or was revoked. Ask the owner for a new link.'
          : (error?.message || 'This review link is unavailable.'),
      }) })
    return () => { active = false }
  }, [token, retry])
  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      if (!document.querySelector('[aria-modal="true"]')) document.querySelector('[data-route-main]')?.focus()
    })
    return () => cancelAnimationFrame(frame)
  }, [resource.status])
  if (resource.status !== 'ready') return <main className="auth-gate" data-route-main tabIndex={-1} aria-live="polite">
    <div className="auth-card">
      <div className="auth-mark" aria-hidden="true">{resource.status === 'gone' ? '×' : '◉'}</div>
      <h1>{resource.status === 'loading' ? 'Opening review…' : resource.status === 'gone' ? 'Review link unavailable' : 'Could not open review'}</h1>
      <p>{resource.status === 'loading' ? 'Validating this read-only capability.' : resource.error}</p>
      {resource.status === 'error' && <button className="btn primary" onClick={() => setRetry(n => n + 1)}>Retry</button>}
    </div>
  </main>
  return <RunView key={`${resource.data.id || token}:${resource.data.run_id}`} runId={resource.data.run_id} onBack={null}
    reviewMode reviewMeta={resource.data} />
}

function RouteFocus({ label, routeKey, children }) {
  useEffect(() => {
    document.title = `${label} · LoopLab`
    const frame = requestAnimationFrame(() => {
      if (!document.querySelector('[aria-modal="true"]')) document.querySelector('[data-route-main]')?.focus()
    })
    return () => cancelAnimationFrame(frame)
  }, [routeKey, label])
  return <>
    <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">{label}</div>
    {children}
  </>
}

export default function App() {
  const [route, setRoute] = useState(parseHash())
  useEffect(() => {
    const on = () => setRoute(parseHash())
    window.addEventListener('hashchange', on)
    window.addEventListener('popstate', on)
    return () => {
      window.removeEventListener('hashchange', on)
      window.removeEventListener('popstate', on)
    }
  }, [])
  const routeLabel = route.view === 'run' ? `Run ${route.id}`
    : route.view === 'settings' ? 'Settings'
    : route.view === 'shared' ? 'Shared Assistant chat'
    : route.view === 'review' ? 'Read-only run review' : 'Runs'
  const open = (id) => { location.hash = `#/run/${encodeURIComponent(id)}` }
  const back = () => { location.hash = '' }
  const settings = () => { location.hash = '#/settings' }

  // The shared read-only view owns the whole screen (it IS a public chat), so the persistent assistant
  // is hidden there; everywhere else (list / run / settings) the assistant stays available.
  let content
  const routeKey = `${route.view}:${route.id || route.token || ''}`
  if (route.view === 'review') return <RouteFocus label={routeLabel} routeKey={routeKey}>
    <ReviewRoute key={route.token || 'invalid-review'} token={route.token} />
  </RouteFocus>
  // The shared read-only chat is a PUBLIC surface: the backend serves /api/assistant/shared/ WITHOUT
  // the owner token (server.py::_unauth_api_ok), so it must bypass the OwnerAuth unlock gate exactly
  // like the review route. Falling through into <OwnerAuth> made a token-protected deployment show
  // recipients the "Unlock LoopLab controls" screen instead of the chat, defeating the share link.
  if (route.view === 'shared') return <RouteFocus label={routeLabel} routeKey={routeKey}>
    <SharedAssistant sid={route.id} onBack={back} />
  </RouteFocus>
  if (route.view === 'run') content = <RunView key={route.id} runId={route.id} onBack={back} />
  else if (route.view === 'settings') content = <Settings onBack={back} />
  else content = <RunList onOpen={open} onSettings={settings} />

  return <OwnerAuth label={routeLabel}><RouteFocus label={routeLabel} routeKey={routeKey}><div className="app-shell">
      <div className="app-shell-main">{content}</div>
      {/* Mounted once, outside the route switch → the assistant persists across all navigation. */}
      <AssistantBar runId={route.view === 'run' ? route.id : null} />
    </div></RouteFocus></OwnerAuth>
}
