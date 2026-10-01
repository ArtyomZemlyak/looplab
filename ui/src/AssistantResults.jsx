import React, { useEffect, useState } from 'react'
import { get, runApiPath } from './util.js'
import { useAssistantLanguage } from './useAssistantLanguage.js'
import { useScopedResource } from './useScopedResource.js'
import { hashWithRunRouteState } from './runRouteState.js'
import { resultNoticeQuestion, resultNoticeText, validResultNotices } from './resultNoticeModel.js'
import './assistant-run-result.css'

export default function AssistantResults({ runId, generation, onOpen, onReady, onAsk, askDisabled, askDisabledReason }) {
  const [language] = useAssistantLanguage()
  const [limit, setLimit] = useState(50)
  const resource = useScopedResource(signal => get(runApiPath(runId, '/result-notices')
    + `?expected_generation=${generation}&limit=${limit}`, { signal, cache: 'no-store' }), {
    scope: `${runId}:${generation}:${limit}`, pollMs: 5000, timeout: 8000,
    validate: value => validResultNotices(value, generation) ? '' : 'Invalid result notices',
  })
  const rows = resource.status === 'ready' ? resource.data.items : []
  const identity = rows.map(r => r.id + ':' + r.evidence_token + ':' + (r.commentary || '')).join('|')
  useEffect(() => { if (identity) onReady?.() }, [identity, onReady])
  const ru = language === 'ru'
  const render = row => {
    const text = resultNoticeText(row, language)
    const base = `#/run/${encodeURIComponent(runId)}`
    const href = hashWithRunRouteState(base, row.kind === 'run' ? { generation, view: 'report' }
      : { generation, nodeId: row.node_id, nodeGeneration: row.attempt,
        inspectTab: row.status === 'failed' ? 'Trace' : 'Metrics' })
    return <article key={row.id} className="asst-result-notice">
      <div className="asst-run-result-head"><strong>{text.title}</strong>
        <span>{ru ? 'Ассистент · запись LoopLab' : 'Assistant · LoopLab record'}</span></div>
      <p>{text.outcome}</p>
      {text.caution && <p className="asst-run-result-caution">{text.caution}</p>}
      {row.kind === 'run' && row.caveats.length > 0 && <p className="asst-run-result-caution">
        {ru ? 'Ограничения: ' : 'Caveats: '}{row.caveats.join(' · ')}</p>}
      {row.commentary && <div className="asst-result-commentary">
        <strong>{ru ? 'Внешний агент · интерпретация' : 'External agent · interpretation'}</strong>
        <p>{row.commentary}</p></div>}
      <div className="asst-run-result-actions">
        {onAsk && <button className="btn sm ghost" disabled={askDisabled}
          title={askDisabled ? askDisabledReason : ru ? 'Подготовить вопрос в поле сообщения' : 'Prepare a question in the composer'}
          onClick={() => onAsk(resultNoticeQuestion(row, language))}>
          {ru ? row.status === 'failed' ? 'Разобрать ошибку в чате' : 'Объяснить результат в чате'
            : row.status === 'failed' ? 'Discuss failure in chat' : 'Explain result in chat'}</button>}
        <a href={href} onClick={onOpen ? event => onOpen(event, href) : undefined}>{text.next}</a>
      </div>
    </article>
  }
  return <section className="asst-result-feed" aria-label={ru ? 'Итоги экспериментов в чате' : 'Experiment results in chat'}>
    <div className="asst-result-feed-head"><span>{ru ? 'Краткие итоги · без вызова модели' : 'Completion briefs · no model call'}</span></div>
    {onAsk && rows.length > 0 && <p className="muted">
      {ru ? 'Кнопка подготовит вопрос в поле сообщения. Отправьте его, когда будете готовы.'
        : 'The button prepares a question in the composer. Send it when you are ready.'}</p>}
    {['error', 'stale'].includes(resource.status) && <p role="status">
      {ru ? 'Не удалось обновить итоги. Проверьте состояние run.' : 'Could not refresh results. Check run state.'}{' '}
      <button className="btn sm ghost" onClick={() => resource.retry()} disabled={!!resource.pending}>
        {ru ? 'Повторить' : 'Retry'}</button></p>}
    {rows.length === 0 && resource.status === 'ready' && <p className="muted">
      {ru ? 'Итог появится после завершения оценки эксперимента.' : 'A brief appears after an experiment finishes evaluation.'}</p>}
    {rows.length > 3 && <details><summary>{ru ? 'Предыдущие итоги' : 'Earlier results'} · {rows.length - 3}</summary>
      {rows.slice(0, -3).map(render)}</details>}
    {rows.slice(-3).map(render)}
    {resource.status === 'ready' && resource.data.has_more && <p className="muted">
      {ru ? 'Показано' : 'Showing'} {rows.length} / {resource.data.total}.{' '}
      {limit < 200 ? <button className="btn sm ghost" onClick={() => setLimit(200)}>
        {ru ? 'Показать до 200' : 'Show up to 200'}</button>
        : ru ? 'Полная история — в Events.' : 'Full history is in Events.'}</p>}
  </section>
}
