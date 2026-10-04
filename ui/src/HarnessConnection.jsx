import React, { useState } from 'react'
import { useScopedResource } from './useScopedResource.js'
import { useAssistantLanguage } from './useAssistantLanguage.js'
import { PANEL_REQUEST_TIMEOUT_MS } from './panelPrimitives.js'

// Configuration, translations and recovery form load only after the operator
// opens connection help. The normal progress read needs none of that code.
export default function HarnessConnection({ runId, generation, seq }) {
  const [wanted, setWanted] = useState(false)
  const [language] = useAssistantLanguage()
  const ru = language === 'ru'
  const scope = `${runId}:${generation}`
  const resource = useScopedResource(() => import('./HarnessHandoff.jsx')
    .then(module => ({ Component: module.default })), {
    scope, gate: wanted ? null : 'idle', timeout: PANEL_REQUEST_TIMEOUT_MS,
  })
  if (resource.status === 'ready') {
    const Component = resource.data.Component
    return <Component key={scope} runId={runId} generation={generation} seq={seq} defaultOpen />
  }
  return <div>
    <button type="button" className="btn" disabled={!!resource.pending} onClick={() => {
      if (wanted) resource.retry()
      else setWanted(true)
    }}>{ru ? 'Подключить внешнего агента' : 'Connect external agent'}</button>
    {resource.pending && <p role="status">{ru ? 'Загружаем инструкцию подключения…' : 'Loading connection help…'}</p>}
    {['error', 'stale'].includes(resource.status) && <p role="status">{ru
      ? 'Инструкция не загрузилась. Нажмите кнопку, чтобы повторить чтение.'
      : 'Connection help unavailable. Press the button to retry loading.'}</p>}
  </div>
}
