import React, { Suspense, useEffect, useRef } from 'react'

const reloadPage = () => window.location.reload()

function LoadSurface({ label, mode, failed = false, onReload = reloadPage }) {
  const surfaceRef = useRef(null)
  const reloadRef = useRef(null)
  useEffect(() => {
    if (mode === 'inline' && !failed) return undefined
    const frame = requestAnimationFrame(() => {
      const target = failed ? reloadRef.current : surfaceRef.current
      target?.focus({ preventScroll: true })
    })
    return () => cancelAnimationFrame(frame)
  }, [failed, mode])

  const body = <>
    {mode === 'route' && <h1>{failed ? `${label} unavailable` : `Opening ${label}…`}</h1>}
    {mode !== 'route' && <b>{failed ? `${label} could not be opened.` : `Loading ${label}…`}</b>}
    {failed
      ? <><p>This section failed while loading or rendering. Reload LoopLab to fetch a consistent build and retry.</p>
          <button ref={reloadRef} type="button" className="btn primary" onClick={onReload}>Reload LoopLab</button></>
      : mode === 'route' && <p>The rest of the application remains available while this route downloads.</p>}
  </>

  if (mode === 'route') return <main ref={surfaceRef} className="auth-gate lazy-route-state"
    data-route-main tabIndex={-1} role={failed ? 'alert' : 'status'} aria-live={failed ? 'assertive' : 'polite'}>
    <div className="auth-card">{body}</div>
  </main>
  if (mode === 'overlay') return <div className="overlay lazy-overlay-state">
    <div ref={surfaceRef} className="panel" role={failed ? 'alertdialog' : 'dialog'} aria-modal="true"
      aria-label={`${failed ? 'Load failure' : 'Loading'}: ${label}`} tabIndex={-1}>
      <div className="panel-b lazy-load-state">{body}</div>
    </div>
  </div>
  return <div ref={surfaceRef} className={`notice lazy-load-state${failed ? ' resource-error' : ''}`}
    role={failed ? 'alert' : 'status'} aria-live={failed ? 'assertive' : 'polite'}>{body}</div>
}

function LoadedFocus({ focusOnReady, children }) {
  useEffect(() => {
    if (!focusOnReady) return undefined
    const frame = requestAnimationFrame(() => {
      if (!document.querySelector('[aria-modal="true"]')) {
        document.querySelector('[data-route-main]')?.focus({ preventScroll: true })
      }
    })
    return () => cancelAnimationFrame(frame)
  }, [focusOnReady])
  return children
}

class LoadErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error) { return { error } }

  componentDidUpdate(previous) {
    if (previous.resetKey !== this.props.resetKey && this.state.error) this.setState({ error: null })
  }

  render() {
    if (this.state.error) return <LoadSurface label={this.props.label} mode={this.props.mode}
      failed onReload={this.props.onReload} />
    return this.props.children
  }
}

/** A local Suspense + error boundary. A failed chunk never blanks the surrounding route. */
export default function LazyBoundary({ label, children, mode = 'inline', focusOnReady = false,
  resetKey = label, onReload = reloadPage }) {
  return <LoadErrorBoundary label={label} mode={mode} resetKey={resetKey} onReload={onReload}>
    <Suspense fallback={<LoadSurface label={label} mode={mode} onReload={onReload} />}>
      <LoadedFocus focusOnReady={focusOnReady}>{children}</LoadedFocus>
    </Suspense>
  </LoadErrorBoundary>
}
