import React, { useEffect, useState } from 'react'
import { get, runApiPath } from './util.js'
import { useAssistantLanguage } from './useAssistantLanguage.js'
import { useScopedResource } from './useScopedResource.js'
import { hashWithRunRouteState } from './runRouteState.js'
import { resultCaveatText, resultNoticeQuestion, resultNoticeText, validResultNotices } from './resultNoticeModel.js'
import './assistant-run-result.css'

// Reset the navigation trail synchronously when the run incarnation changes.
export default function AssistantResults(props) {
  return <ResultPages key={`${props.runId}:${props.generation}`} {...props} />
}

function ResultPages({ runId, generation, onOpen, onReady, onAsk, askDisabled, askDisabledReason }) {
  const [language] = useAssistantLanguage()
  const [cursors, setCursors] = useState([])
  const cursor = cursors.at(-1) || null
  const resource = useScopedResource(signal => get(runApiPath(runId, '/result-notices')
    + `?expected_generation=${generation}&limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`, { signal, cache: 'no-store' }), {
    scope: `${runId}:${generation}:${cursor || 'latest'}`, pollMs: 5000, timeout: 8000,
    validate: value => validResultNotices(value, generation, cursor, 50) ? '' : 'Invalid result notices',
    classifyFailure: ({ error }) => ({ error: error?.code === 'result_notice_cursor_changed' ? 'cursor_changed' : '' }),
  })
  const rows = resource.status === 'ready' ? resource.data.items : []
  const identity = rows.map(r => r.id + ':' + r.evidence_token + ':' + (r.commentary || '')).join('|')
  // Reading older pages must not trigger the transcript's new-result autoscroll.
  useEffect(() => { if (identity && !cursor) onReady?.() }, [identity, onReady, cursor])
  const ru = language === 'ru'
  const render = row => {
    const text = resultNoticeText(row, language)
    const base = `#/run/${encodeURIComponent(runId)}`
    const href = hashWithRunRouteState(base, row.kind === 'run' ? { generation, view: 'report' }
      : { generation, nodeId: row.node_id, nodeGeneration: row.attempt,
        inspectTab: ['failed', 'aborted'].includes(row.status) ? 'Trace' : 'Metrics' })
    const link = (target, label, key) => <a key={key} className="btn sm ghost" href={target}
      onClick={onOpen ? event => onOpen(event, target) : undefined}>{label}</a>
    return <article key={row.id} className="asst-result-notice">
      <div className="asst-run-result-head"><strong>{text.title}</strong>
        <span className={`asst-result-status ${row.status}`}>{text.stateLabel}</span></div>
      <p>{text.outcome}</p>
      {text.comparison && <p className="asst-result-comparison"><strong>{ru ? 'Сравнение: ' : 'Comparison: '}</strong>{text.comparison}</p>}
      {text.caution && <p className="asst-run-result-caution"><strong>{ru ? 'Надёжность: ' : 'Reliability: '}</strong>{text.caution}</p>}
      {row.kind === 'run' && row.caveats.length > 0 && <p className="asst-run-result-caution">
        {ru ? 'Ограничения: ' : 'Caveats: '}{row.caveats.map(code => resultCaveatText(code, language)).join(' · ')}</p>}
      {row.commentary && <div className="asst-result-commentary">
        <strong>{ru ? 'Внешний агент · интерпретация' : 'External agent · interpretation'}</strong>
        <p>{row.commentary}</p></div>}
      <p className="asst-result-next"><strong>{ru ? 'Дальше: ' : 'Next: '}</strong>{text.next}</p>
      <div className="asst-run-result-actions">
        {onAsk && <button className="btn sm ghost" disabled={askDisabled}
          title={askDisabled ? askDisabledReason : ru ? 'Подготовить вопрос в поле сообщения' : 'Prepare a question in the composer'}
          onClick={() => onAsk(resultNoticeQuestion(row, language))}>
          {ru ? row.status === 'failed' ? 'Разобрать ошибку в чате' : row.status === 'aborted' ? 'Разобрать остановку в чате' : 'Объяснить результат в чате'
            : row.status === 'failed' ? 'Discuss failure in chat' : row.status === 'aborted' ? 'Discuss stop in chat' : 'Explain result in chat'}</button>}
        {link(href, text.actionLabel)}
        {row.kind === 'node' && row.parents.map(parent => link(hashWithRunRouteState(base, {
          generation, nodeId: parent.node_id, nodeGeneration: parent.attempt, inspectTab: 'Metrics',
        }), `${ru ? 'Метрики' : 'Metrics'} #${parent.node_id} · ${ru ? 'попытка' : 'attempt'} ${parent.attempt}`, parent.node_id))}
      </div>
    </article>
  }
  return <section className="asst-result-feed" aria-label={ru ? 'Итоги экспериментов в чате' : 'Experiment results in chat'}>
    <div className="asst-result-feed-head"><span>{ru ? 'Краткие итоги · без вызова модели' : 'Completion briefs · no model call'}</span></div>
    <div className="asst-run-result-actions" aria-busy={resource.status === 'loading'}>
      {cursor && <>
        <button className="btn sm ghost" onClick={() => setCursors(current => current.slice(0, -1))}>
          {ru ? 'Новее' : 'Newer'}</button>
        <button className="btn sm ghost" onClick={() => setCursors([])}>
          {ru ? 'К последним итогам' : 'Latest results'}</button>
      </>}
      <button className="btn sm ghost" disabled={resource.status !== 'ready' || !resource.data.has_more}
        onClick={() => setCursors(current => (current.at(-1) || null) === cursor
          ? [...current, resource.data.next_cursor] : current)}>{ru ? 'Раньше' : 'Earlier'}</button>
      {resource.status === 'ready' && <span className="muted">
        {cursor ? ru ? 'Предыдущая страница' : 'Earlier page' : ru ? 'Последние итоги' : 'Latest page'}
        {' · '}{rows.length} / {resource.data.total}</span>}
    </div>
    {onAsk && rows.length > 0 && <p className="muted">
      {ru ? 'Кнопка подготовит вопрос в поле сообщения. Отправьте его, когда будете готовы.'
        : 'The button prepares a question in the composer. Send it when you are ready.'}</p>}
    {['error', 'stale'].includes(resource.status) && <p role="status">
      {resource.error === 'cursor_changed'
        ? ru ? 'Записи изменились. Вернитесь к последним итогам.' : 'Results changed. Return to latest results.'
        : ru ? 'Не удалось обновить итоги. Проверьте состояние run.' : 'Could not refresh results. Check run state.'}{' '}
      <button className="btn sm ghost" onClick={() => resource.retry()} disabled={!!resource.pending}>
        {ru ? 'Повторить' : 'Retry'}</button></p>}
    {rows.length === 0 && resource.status === 'ready' && <p className="muted">
      {cursor ? ru ? 'Более ранних текущих итогов нет.' : 'No earlier current results.'
        : ru ? 'Итог появится после завершения оценки эксперимента.' : 'A brief appears after an experiment finishes evaluation.'}</p>}
    {rows.length > 3 && <details open={!!cursor}><summary>{ru ? 'Предыдущие итоги' : 'Earlier results'} · {rows.length - 3}</summary>
      {rows.slice(0, -3).map(render)}</details>}
    {rows.slice(-3).map(render)}
  </section>
}
