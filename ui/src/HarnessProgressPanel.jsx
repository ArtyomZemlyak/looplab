import { uiText, useUILanguage } from './uiLanguage.js'
import React, { useState } from 'react'
import { get, runApiPath } from './util.js'
import { useScopedResource } from './useScopedResource.js'
import Panel from './PanelShell.jsx'
import { validHarnessNextStep } from './HarnessNextStep.jsx'
import HarnessConnection from './HarnessConnection.jsx'
import AgentActivity from './AgentActivity.jsx'
import { invalidPanelPayload, isRecord, PANEL_REQUEST_TIMEOUT_MS, RUN_GENERATION_RE } from './panelPrimitives.js'
import { PanelResourceNotice } from './PanelResourceNotice.jsx'
import { useAssistantUILanguage } from './useAssistantLanguage.js'
import { validHarnessProgress } from './harnessProgressModel.js'
import LazyBoundary from './LazyBoundary.jsx'

const CycleBody = React.lazy(() => import('./HarnessCycleBody.jsx'))

// External decisions live in three durable sidecars, not in the folded event log. This read model
// keeps the intermediate reasoning receipts inspectable after a client/server restart and labels
// old evidence explicitly; it never pretends that a receipt for one Idea satisfies another Idea.
export function HarnessProgressPanel({ runId, expectedGeneration, seq, externalMode, configStatus,
  onOpenEvents, onClose, compact = false, engineRunning, onOpen }) {
  useUILanguage()

  const [offset, setOffset] = useState(0)
  const language = useAssistantUILanguage()
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
    return <section className="topbar" aria-label={((ru ? 'Состояние внешнего агента' : uiText('External agent status')))}>
      <span className="muted">{((ru ? 'Внешний агент' : uiText('External agent')))}</span>
      {fresh ? <details className="spacer"><summary><b>{uiText(progress.next_step.title)}</b></summary>
        <p>{progress.next_step.detail}</p>
        {ru && !progress.next_step.language && <p className="muted">Подсказка старого сервера приведена на исходном языке.</p>}</details>
        : <span role="status">{((ru ? 'Следующий шаг недоступен' : uiText('Next step unavailable')))}</span>}
      {!fresh && <span className="spacer" />}
      <AgentActivity activity={progress?.agent_activity} fresh={fresh} />
      {!fresh && <button type="button" className="btn sm ghost"
        onClick={() => resource.retry({ supersede: true })}>{((ru ? 'Повторить чтение' : uiText('Retry')))}</button>}
      <button type="button" className="btn sm" onClick={onOpen}>{((ru ? 'Обязательства агента' : uiText('Agent obligations')))}</button>
    </section>
  }
  const fresh = resource.status === 'ready' && !(seq > progress?.event_seq)
  return <Panel title={((ru ? 'Обязательства внешнего агента' : uiText('External agent obligations')))} sub={progress ? `${ru ? 'событие' : 'event'} #${progress.event_seq}` : runId}
    onClose={onClose} wide>
    {configStatus === 'ready' && externalMode !== true && <p role="status">{((ru ? 'В этом запуске работает встроенный цикл LoopLab. Журналы внешнего агента применяются к запускам с external_harness=true.' : uiText("This run uses LoopLab's built-in agent cycle. The external agent journals apply to runs launched with external_harness=true.")))}</p>}
    {configStatus !== 'ready' && <p role="status">{((ru ? 'Ожидаем настройки запуска…' : uiText('Waiting for run settings…')))}</p>}
    {!validGeneration && <p className="muted" role="status">{((ru ? 'Ожидаем сохранённую generation запуска…' : uiText('Waiting for a durable run generation…')))}</p>}
    {scope && <PanelResourceNotice resource={resource} label={((ru ? 'Обязательства агента' : uiText('Agent obligations')))} language={readLanguage}
      onRetry={() => resource.retry()} />}
    {scope && <p className="report-section-intro">{ru
      ? 'Что внешний агент должен сделать сейчас, перед следующим экспериментом и перед завершением: исследования, новизна идеи, вопросы оценки и отчёт. Журналы и подключение агента — в свёрнутых разделах ниже. Панель сама не запускает агента и не управляет обучением.'
      : uiText('What the external agent must do now, before another experiment and before finalization: research, novelty, evaluation questions and reports. Journals and the agent connection are folded below. This panel does not start an agent or control training.')}</p>}
    {scope && <LazyBoundary label={((ru ? 'Требования и история' : uiText('Requirements and history')))}
      resetKey={`${runId}:${expectedGeneration}`} language={readLanguage} focusOnFailure={false}
      loadingFallback={<p role="status">{((ru ? 'Загружаем требования и историю…' : uiText('Loading requirements and history…')))}</p>}
      failureContent={<p>{((ru ? 'Требования не показаны. Перед решением прочитайте актуальный harness-progress через внешнего агента или перезагрузите LoopLab.' : uiText('Requirements are not displayed. Before deciding, read current harness-progress through the external agent or reload LoopLab.')))}</p>}>
      {progress && <CycleBody progress={progress} fresh={fresh} runId={runId} offset={offset} setOffset={setOffset} onOpenEvents={onOpenEvents} />}
    </LazyBoundary>}
    {/* The connection instructions stay in the panel SHELL, outside the lazily loaded body, so a
        failed body never takes the reconnect controls with it — but below the obligations and
        folded: the agent is usually already connected (review 2026-10-14 §1). */}
    {scope && <details className="harness-audit harness-connection-details">
      <summary>{((ru ? 'Подключение внешнего агента' : uiText('External agent connection')))}</summary>
      <HarnessConnection runId={runId} generation={expectedGeneration} seq={seq} />
    </details>}
  </Panel>
}
