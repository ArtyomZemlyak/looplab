import { uiText, useUILanguage } from './uiLanguage.js'
import React, { useEffect, useState } from 'react'
import { get, runApiPath } from './util.js'
import { useAssistantUILanguage } from './useAssistantLanguage.js'
import { useScopedResource } from './useScopedResource.js'
import { hashWithRunRouteState } from './runRouteState.js'
import { resultCaveatText, resultNoticeBrief, resultNoticeQuestion, resultNoticeText, validResultNotices } from './resultNoticeModel.js'
import './assistant-run-result.css'
import { resultConversation } from './assistantResultTimeline.js'

// Reset the navigation trail synchronously when the run incarnation changes.
export default function AssistantResults(props) {
  useUILanguage()

  return <ResultPages key={`${props.runId}:${props.generation}`} {...props} />
}

function ResultPages({ runId, generation, onOpen, onReady, onAsk, askDisabled, askDisabledReason,
    messages, renderMessage }) {
  useUILanguage()

  const language = useAssistantUILanguage()
  const [cursors, setCursors] = useState([])
  const cursor = cursors.at(-1) || null
  const resource = useScopedResource(signal => get(runApiPath(runId, '/result-notices')
    + `?expected_generation=${generation}&limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`, { signal, cache: 'no-store' }), {
    scope: `${runId}:${generation}:${cursor || 'latest'}`, pollMs: 5000, timeout: 8000,
    validate: value => validResultNotices(value, generation, cursor, 50) ? '' : 'Invalid result notices',
    classifyFailure: ({ error }) => ({ error: error?.code === 'result_notice_cursor_changed' ? 'cursor_changed' : '' }),
  })
  const rows = resource.status === 'ready' ? resource.data.items : []
  const identity = rows.map(r => r.id + ':' + r.evidence_token + ':' + (r.commentary || '') + ':' + (r.commentary_status || '')).join('|')
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
      onClick={onOpen ? event => onOpen(event, target) : undefined}>{uiText(label)}</a>
    return <article key={`result:${row.id}`} className="feed-msg chat assistant asst-result-notice">
      <div className="fm-body">
      <div className="chat-who">{((ru ? 'Ассистент' : uiText('assistant')))} · {uiText(text.title)}</div>
      <div className="chat-bubble">
      {row.commentary ? <p className="asst-result-commentary">{row.commentary}</p>
        : <p>{resultNoticeBrief(row, language)}</p>}
      {row.commentary && (row.trust_flagged || row.trust_advisory || row.parent_trust_advisory
        || row.salvaged || row.feasible === false || row.violations > 0) && <p className="asst-run-result-caution">
        {((ru ? 'Есть ограничения или предупреждения оценки. Проверьте доказательства перед продолжением.' : uiText('Evaluation caveats or warnings remain. Review the evidence before continuing.')))}</p>}
      {row.kind === 'run' && row.caveats.length > 0 && <p className="asst-run-result-caution">
        {row.caveats.map(code => resultCaveatText(code, language)).join(' · ')}</p>}
      <details className="asst-result-evidence"><summary>{((ru ? 'Измерения и ограничения' : uiText('Measurements and caveats')))}</summary>
      <p>{uiText(text.outcome)}</p>
      {text.comparison && <p className="asst-result-comparison"><strong>{((ru ? 'Сравнение: ' : uiText('Comparison: ')))}</strong>{uiText(text.comparison)}</p>}
      {text.caution && <p className="asst-run-result-caution"><strong>{((ru ? 'Надёжность: ' : uiText('Reliability: ')))}</strong>{uiText(text.caution)}</p>}
      {row.kind === 'run' && row.caveats.length > 0 && <p className="asst-run-result-caution">
        {((ru ? 'Ограничения: ' : uiText('Caveats: ')))}{row.caveats.map(code => resultCaveatText(code, language)).join(' · ')}</p>}
      {row.commentary && <p className="muted">{row.commentary_source === 'assistant'
        ? (ru ? 'Ассистент · интерпретация' : uiText('Assistant · interpretation'))
        : (ru ? 'Внешний агент · интерпретация' : uiText('External agent · interpretation'))}</p>}
      {!row.commentary && ['generating', 'ready'].includes(row.commentary_status) && <p className="muted">
        {ru ? 'Ассистент готовит пояснение…' : uiText('Assistant is preparing an explanation…')}</p>}
      {!row.commentary && ['failed', 'interrupted', 'unavailable'].includes(row.commentary_status) && <p className="muted">
        {ru ? 'Пояснение недоступно; измеренный итог сохранён. Автоповтор выключен. Можно обсудить результат в чате.'
          : uiText('Explanation unavailable; measured result saved. No automatic retry. Discuss the result in chat.')}</p>}
      <p className="asst-result-next"><strong>{((ru ? 'Дальше: ' : uiText('Next: ')))}</strong>{uiText(text.next)}</p>
      <div className="asst-run-result-actions">
        {link(href, text.actionLabel)}
        {row.kind === 'node' && row.parents.map(parent => link(hashWithRunRouteState(base, {
          generation, nodeId: parent.node_id, nodeGeneration: parent.attempt, inspectTab: 'Metrics',
        }), `${ru ? 'Метрики' : 'Metrics'} #${parent.node_id} · ${ru ? 'попытка' : 'attempt'} ${parent.attempt}`, parent.node_id))}
      </div>
      </details>
      {onAsk && <div className="asst-result-followup"><button className="btn sm ghost" disabled={askDisabled}
          title={((askDisabled ? uiText(askDisabledReason) : (ru ? 'Подготовить вопрос в поле сообщения' : uiText('Prepare a question in the composer'))))}
          onClick={() => onAsk(resultNoticeQuestion(row, language))}>
          {((ru ? row.status === 'failed' ? 'Разобрать ошибку' : row.status === 'aborted' ? 'Разобрать остановку' : 'Обсудить следующий шаг' : (row.status === 'failed' ? uiText('Discuss failure') : (row.status === 'aborted' ? uiText('Discuss stop') : uiText('Discuss next step')))))}</button></div>}
      </div></div>
    </article>
  }
  return <section className="asst-result-feed" aria-label={((ru ? 'Итоги экспериментов в чате' : uiText('Experiment results in chat')))}>
    {messages && renderMessage ? resultConversation(messages, rows.length > 3 && !cursor ? rows.slice(-3) : rows)
      .map(item => item.result ? render(item.result) : renderMessage(item.message, item.index))
      : rows.slice(-3).map(render)}
    <details className="asst-result-history" open={!!cursor || ['error', 'stale'].includes(resource.status)}><summary>{((ru ? 'История итогов' : uiText('Result history')))}</summary>
    <div className="asst-run-result-actions" aria-busy={resource.status === 'loading'}>
      {cursor && <>
        <button className="btn sm ghost" onClick={() => setCursors(current => current.slice(0, -1))}>
          {((ru ? 'Новее' : uiText('Newer')))}</button>
        <button className="btn sm ghost" onClick={() => setCursors([])}>
          {((ru ? 'К последним итогам' : uiText('Latest results')))}</button>
      </>}
      <button className="btn sm ghost" disabled={resource.status !== 'ready' || !resource.data.has_more}
        onClick={() => setCursors(current => (current.at(-1) || null) === cursor
          ? [...current, resource.data.next_cursor] : current)}>{((ru ? 'Раньше' : uiText('Earlier')))}</button>
      {resource.status === 'ready' && <span className="muted">
        {((cursor ? (ru ? 'Предыдущая страница' : uiText('Earlier page')) : (ru ? 'Последние итоги' : uiText('Latest page'))))}
        {' · '}{rows.length} / {resource.data.total}</span>}
    </div>
    {['error', 'stale'].includes(resource.status) && <p role="status">
      {((resource.error === 'cursor_changed' ? (ru ? 'Записи изменились. Вернитесь к последним итогам.' : uiText('Results changed. Return to latest results.')) : (ru ? 'Не удалось обновить итоги. Проверьте состояние run.' : uiText('Could not refresh results. Check run state.'))))}{' '}
      <button className="btn sm ghost" onClick={() => resource.retry()} disabled={!!resource.pending}>
        {((ru ? 'Повторить' : uiText('Retry')))}</button></p>}
    {rows.length === 0 && resource.status === 'ready' && <p className="muted">
      {((cursor ? (ru ? 'Более ранних текущих итогов нет.' : uiText('No earlier current results.')) : (ru ? 'Итог появится после завершения оценки эксперимента.' : uiText('A brief appears after an experiment finishes evaluation.'))))}</p>}
    {rows.length > 3 && (!messages || !cursor) && <details open={!!cursor}><summary>{((ru ? 'Предыдущие итоги' : uiText('Earlier results')))} · {rows.length - 3}</summary>
      {rows.slice(0, -3).map(render)}</details>}
    </details>
  </section>
}
