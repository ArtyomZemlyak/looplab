import { uiText, uiMessage, useUILanguage, effectiveUILanguage } from './uiLanguage.js'
import React, { Suspense, useEffect, useRef } from 'react'
import { useDialogFocus } from './useDialogFocus.js'

const reloadPage = () => window.location.reload()

function LoadSurface({ label, mode, failed = false, onReload = reloadPage, onClose,
    language = 'en', focusOnFailure = true, failureContent }) {
  useUILanguage()

  const ru = language === 'ru' || effectiveUILanguage() === 'ru'
  const surfaceRef = useRef(null)
  const reloadRef = useRef(null)
  useDialogFocus(surfaceRef, onClose, mode === 'overlay')
  useEffect(() => {
    if (mode === 'overlay' || (mode === 'inline' && (!failed || !focusOnFailure))) return undefined
    const frame = requestAnimationFrame(() => {
      // Opt-in recovery for a removed focused control; a later failure must not
      // take focus from another control the operator has already reached.
      if (mode === 'inline' && focusOnFailure === 'if-lost'
          && document.activeElement && document.activeElement !== document.body) return
      const target = failed ? reloadRef.current : surfaceRef.current
      target?.focus({ preventScroll: true })
    })
    return () => cancelAnimationFrame(frame)
  }, [failed, mode, focusOnFailure])

  const body = <>
    {mode === 'route' && <h1>{(failed ? (ru ? `Недоступно: ${label}` : uiMessage("{0} unavailable", [label])) : (ru ? `Открываем: ${label}…` : uiMessage("Opening {0}…", [label])))}</h1>}
    {mode !== 'route' && <b>{(failed ? (ru ? `Не удалось открыть «${label}».` : uiMessage("{0} could not be opened.", [label])) : (ru ? `Загрузка раздела «${label}»…` : uiMessage("Loading {0}…", [label])))}</b>}
    {failed && failureContent != null && <div>{failureContent}</div>}
    {failed
      ? <><p>{((ru ? 'Не удалось загрузить или отобразить этот раздел. Перезагрузите LoopLab и попробуйте снова.' : uiText('This section failed while loading or rendering. Reload LoopLab to fetch a consistent build and retry.')))}</p>
          <button ref={reloadRef} type="button" className="btn primary" onClick={onReload}>
            {((ru ? 'Перезагрузить LoopLab' : uiText('Reload LoopLab')))}</button></>
      : mode === 'route' && <p>{((ru ? 'Пока эта страница загружается, остальные разделы приложения остаются доступны.' : uiText('The rest of the application remains available while this route downloads.')))}</p>}
  </>

  if (mode === 'route') return <main ref={surfaceRef} className="auth-gate lazy-route-state"
    data-route-main tabIndex={-1} role={failed ? 'alert' : 'status'} aria-live={failed ? 'assertive' : 'polite'}>
    <div className="auth-card">{body}</div>
  </main>
  if (mode === 'overlay') return <div className="overlay lazy-overlay-state">
    <div ref={surfaceRef} className="panel" role={failed ? 'alertdialog' : 'dialog'} aria-modal="true"
      aria-label={`${failed ? ru ? 'Ошибка загрузки' : 'Load failure' : ru ? 'Загрузка' : 'Loading'}: ${label}`} tabIndex={-1}>
      <div className="panel-b lazy-load-state"><button className="btn sm ghost"
        onClick={onClose}>{((ru ? 'Закрыть' : uiText('Close')))}</button>{body}</div>
    </div>
  </div>
  return <div ref={surfaceRef} className={`notice lazy-load-state${failed ? ' resource-error' : ''}`}
    role={failed ? 'alert' : 'status'} aria-live={failed ? 'assertive' : 'polite'}>{body}</div>
}

function LoadedFocus({ focusOnReady, children }) {
  useUILanguage()

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
    if (this.state.error) return <LoadSurface label={uiText(this.props.label)} mode={this.props.mode}
      failed onReload={this.props.onReload} onClose={this.props.onClose}
      language={this.props.language} focusOnFailure={this.props.focusOnFailure}
      failureContent={this.props.failureContent} />
    return this.props.children
  }
}

/** A local Suspense + error boundary. A failed chunk never blanks the surrounding route. */
export default function LazyBoundary({ label, children, mode = 'inline', focusOnReady = false,
    resetKey = label, onReload = reloadPage, onClose, language = 'en', focusOnFailure = true,
    loadingFallback, failureContent }) {
  useUILanguage()

  return <LoadErrorBoundary label={uiText(label)} mode={mode} resetKey={resetKey} onReload={onReload}
    onClose={onClose} language={language} focusOnFailure={focusOnFailure} failureContent={failureContent}>
    <Suspense fallback={loadingFallback === undefined
      ? <LoadSurface label={uiText(label)} mode={mode} onReload={onReload} onClose={onClose}
          language={language} focusOnFailure={focusOnFailure} /> : loadingFallback}>
      <LoadedFocus focusOnReady={focusOnReady}>{children}</LoadedFocus>
    </Suspense>
  </LoadErrorBoundary>
}
