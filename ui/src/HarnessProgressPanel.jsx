import React, { useState } from 'react'
import { get, runApiPath } from './util.js'
import { useScopedResource } from './useScopedResource.js'
import Panel from './PanelShell.jsx'
import { validHarnessNextStep } from './HarnessNextStep.jsx'
import HarnessConnection from './HarnessConnection.jsx'
import AgentActivity from './AgentActivity.jsx'
import { invalidPanelPayload, isRecord, PANEL_REQUEST_TIMEOUT_MS, RUN_GENERATION_RE } from './panelPrimitives.js'
import { PanelResourceNotice } from './PanelResourceNotice.jsx'
import { useAssistantLanguage } from './useAssistantLanguage.js'
import { validHarnessProgress } from './harnessProgressModel.js'

const CycleBody = React.lazy(() => import('./HarnessCycleBody.jsx'))

// External decisions live in three durable sidecars, not in the folded event log. This read model
// keeps the intermediate reasoning receipts inspectable after a client/server restart and labels
// old evidence explicitly; it never pretends that a receipt for one Idea satisfies another Idea.
export function HarnessProgressPanel({ runId, expectedGeneration, seq, externalMode, configStatus,
  onOpenEvents, onClose, compact = false, engineRunning, onOpen }) {
  const [offset, setOffset] = useState(0)
  const [language] = useAssistantLanguage()
  const ru = language === 'ru'
  const readLanguage = ru ? 'ru' : 'en'
  const validGeneration = RUN_GENERATION_RE.test(expectedGeneration || '')
  const scope = validGeneration && externalMode === true
    ? `${runId}:${expectedGeneration}:${readLanguage}:${compact ? `brief:${engineRunning}` : offset}` : ''
  const resource = useScopedResource(signal => get(
    runApiPath(runId, '/harness-progress')
      + `?expected_generation=${expectedGeneration}&language=${readLanguage}&`
      + (compact ? 'brief=true' : `offset=${offset}&limit=20`),
    { cache: 'no-store', signal }).then(value => {
    if (!isRecord(value) || value.generation !== expectedGeneration
        || !validHarnessNextStep(value.next_step, readLanguage)
        || (compact ? !value.next_step || !Number.isSafeInteger(value.event_seq) || value.event_seq < 0
          || ![true, false, null].includes(value.execution?.engine_running)
          : !validHarnessProgress(value, offset))) {
      invalidPanelPayload()
    }
    return value
  }), { scope, gate: scope ? null : 'idle', timeout: PANEL_REQUEST_TIMEOUT_MS,
    pollMs: 10_000, deps: [seq, engineRunning] })
  const progress = resource.data
  // Engine observations can change without an event. The periodic read owns this label;
  // an engine prop change fences the old scope but must not override a newer server probe.
  if (compact) {
    const fresh = resource.status === 'ready' && progress?.event_seq >= seq
    return <section className="topbar" aria-label={ru ? 'Состояние внешнего агента' : 'External agent status'}>
      <span className="muted">{ru ? 'Внешний агент' : 'External agent'}</span>
      {fresh ? <details className="spacer"><summary><b>{progress.next_step.title}</b></summary>
        <p>{progress.next_step.detail}</p>
        {ru && !progress.next_step.language && <p className="muted">Подсказка старого сервера приведена на исходном языке.</p>}</details>
        : <span role="status">{ru ? 'Следующий шаг недоступен' : 'Next step unavailable'}</span>}
      {!fresh && <span className="spacer" />}
      <AgentActivity activity={progress?.agent_activity} fresh={fresh} />
      {!fresh && <button type="button" className="btn sm ghost"
        onClick={() => resource.retry({ supersede: true })}>{ru ? 'Повторить чтение' : 'Retry'}</button>}
      <button type="button" className="btn sm" onClick={onOpen}>{ru ? 'Цикл агента' : 'Agent cycle'}</button>
    </section>
  }
  const fresh = resource.status === 'ready' && !(seq > progress?.event_seq)
  return <Panel title={ru ? 'Цикл внешнего агента' : 'External agent cycle'} sub={progress ? `${ru ? 'событие' : 'event'} #${progress.event_seq}` : runId}
    onClose={onClose} wide>
    {configStatus === 'ready' && externalMode !== true && <p role="status">{ru
      ? 'В этом запуске работает встроенный цикл LoopLab. Журналы внешнего агента применяются к запускам с external_harness=true.'
      : "This run uses LoopLab's built-in agent cycle. The external agent journals apply to runs launched with external_harness=true."}</p>}
    {configStatus !== 'ready' && <p role="status">{ru ? 'Ожидаем настройки запуска…' : 'Waiting for run settings…'}</p>}
    {!validGeneration && <p className="muted" role="status">{ru ? 'Ожидаем сохранённую generation запуска…' : 'Waiting for a durable run generation…'}</p>}
    {scope && <PanelResourceNotice resource={resource} label={ru ? 'Цикл агента' : 'Agent cycle'} language={readLanguage}
      onRetry={() => resource.retry()} />}
    {scope && <HarnessConnection runId={runId} generation={expectedGeneration} seq={seq} />}
    {progress && <React.Suspense fallback={<p role="status">{ru ? 'Загружаем требования и историю…' : 'Loading requirements and history…'}</p>}>
      <CycleBody progress={progress} fresh={fresh} runId={runId} offset={offset} setOffset={setOffset} onOpenEvents={onOpenEvents} />
    </React.Suspense>}
  </Panel>
}
