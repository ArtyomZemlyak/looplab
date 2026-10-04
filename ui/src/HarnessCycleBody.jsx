import React from 'react'
import HarnessNextStep from './HarnessNextStep.jsx'
import AgentActivity from './AgentActivity.jsx'
import { useAssistantLanguage } from './useAssistantLanguage.js'

// Reading labels only. Server phase IDs, verdicts and authored journal text stay
// verbatim; the UI neither infers permission nor publishes an answer.
const journalNames = { decisions: 'Решения', reviews: 'Разборы', checkpoints: 'Вопросы оценки' }
const statuses = { recorded: 'записано', pending: 'ожидает ответа', answered: 'ответ записан',
  superseded: 'устарело', current: 'актуально', current_evidence_for_idea: 'актуально для этой идеи' }

export default function HarnessCycleBody({ progress, fresh, runId, offset, setOffset, onOpenEvents }) {
  const [language] = useAssistantLanguage()
  const ru = language === 'ru'
  const t = (en, russian) => ru ? russian : en
  const current = fresh && progress.complete
  const history = progress.history
  const blockers = progress.candidate_blockers_if_expanding
  const perIdea = Object.entries(progress.candidate_decisions_per_idea)
  const hasMore = ['decisions', 'reviews', 'checkpoints'].some(kind => history[kind].has_more)
  const historyRow = (kind, row, index) => {
    const receipt = kind === 'checkpoints' ? row.question : row
    const answer = kind === 'checkpoints' ? row.answer : null
    const status = row.status || row.validity || 'recorded'
    const oldCurrent = !current && ['current', 'current_evidence_for_idea'].includes(status)
    return <li key={`${kind}-${receipt.action_id || receipt.checkpoint_id || index}`} className="ov-row">
      <b>{receipt.phase_id || (ru ? journalNames[kind] : kind)}</b>{' '}
      <span className="chip" title={status}>{ru ? statuses[status] || status : status}
        {oldCurrent ? t(' at last read', ' при последнем чтении') : ''}</span>{' '}
      {row.lifecycle === 'superseded' && <span className="chip">{t('old evaluator attempt', 'предыдущая попытка оценки')}</span>}
      {receipt.at_node != null && <span className="muted">{t('at node', 'на эксперименте')} {receipt.at_node} · </span>}
      {receipt.stage && <span>{receipt.stage} · </span>}
      {receipt.decision || answer?.verdict || ''}
      {(receipt.reason || answer?.reason) && <div>{receipt.reason || answer.reason}</div>}
      {(receipt.expectation || receipt.observation) && <details>
        <summary>{t('Observed checkpoint', 'Записанное наблюдение')}</summary>
        {receipt.expectation && <p>{t('Expected', 'Ожидалось')}: {receipt.expectation}</p>}
        {receipt.observation && <pre className="log-tail">{receipt.observation}</pre>}
      </details>}
      <div className="muted">{receipt.action_id || receipt.checkpoint_id}
        {receipt.action_ref ? ` · ${t('action', 'действие')} ${receipt.action_ref}` : ''}</div>
    </li>
  }
  return <>
    {!progress.complete && <div className="report-inline-state error" role="alert">
      {t('An event, decision, review or checkpoint journal has damaged rows. History below may be partial; inspect source health before treating a missing receipt as never written.',
        'В журнале событий, решений, разборов или вопросов оценки есть повреждённые записи. История ниже может быть неполной. Проверьте источники перед выводом, что отсутствующая квитанция никогда не записывалась.')}
      <pre>{JSON.stringify(progress.source_health, null, 2)}</pre>
    </div>}
    <p className="muted">{t('Measured prefix:', 'Прочитанная часть истории: экспериментов —')} {progress.at_node} {t('nodes, event', ', событие')} #{progress.event_seq}.
      {' '}{t('This is a read of several durable journals; refresh after a new event or response.', 'Данные прочитаны из нескольких журналов. Обновите их после нового события или ответа.')}</p>
    <HarnessNextStep step={progress.next_step} runId={runId} fresh={fresh} />
    <AgentActivity activity={progress.agent_activity} fresh={fresh} />
    <p className="muted">{t('Journal rows: decisions', 'Записей в журналах: решения')} {progress.source_health.decisions.accepted_rows},
      {' '}{t('reviews', 'разборы')} {progress.source_health.reviews.accepted_rows}, {t('checkpoints', 'вопросы оценки')} {progress.source_health.checkpoints.accepted_rows}.
      {' '}{t('The event timeline records node and command transitions separately.', 'Переходы экспериментов и команд записаны отдельно в событиях.')} {' '}
      <button type="button" className="btn sm ghost" onClick={onOpenEvents}>{t('Open events', 'Открыть события')}</button></p>
    {!current && <p role="status" className="report-inline-state">{t('Current requirements unavailable. Refresh a complete source read before deciding; recorded history below grants no permission.',
      'Актуальные требования недоступны. Перед решением обновите чтение полных источников; записанная история ниже не даёт разрешения на действие.')}</p>}
    {current && <>
      <h3>{t('Before another candidate', 'Перед следующим экспериментом')}</h3>
      {blockers.length ? <ul>{blockers.map((item, index) => <li key={`${item.phase_id}-${index}`}>
        <b>{item.phase_id}</b> · {item.action.replaceAll('{run_id}', runId)}
      </li>)}</ul> : <p>{t("No current external-cycle gate. Candidate-specific checks, budgets and the task's edit surface still apply.",
        'В прочитанной истории нет текущих требований цикла. Проверка конкретного кандидата, бюджеты и разрешённые изменения задачи всё равно действуют.')}</p>}
      <h3>{t('For each proposed Idea', 'Для каждой предлагаемой идеи')}</h3>
      {progress.candidate_requirements.effective_concepts && <p>{t('Effective concept tags are required on every submitted candidate.', 'Для каждого кандидата нужны действующие теги концептов.')}</p>}
      {progress.candidate_requirements.hypothesis_statement && <p>{t('A nonempty hypothesis statement is required on every submitted candidate; injection creates a new Card.', 'Для каждого кандидата нужна непустая гипотеза; отправка создаёт новую карточку.')}</p>}
      {perIdea.length ? <ul>{perIdea.map(([name, count]) => <li key={name}>
        {name}: {ru ? `вариантов для разбора: ${count}; для этой конкретной идеи` : `review ${count} option${count === 1 ? '' : 's'} for the exact Idea`}
      </li>)}</ul> : <p>{t('No configured idea-specific review at this node count.', 'На этом числе экспериментов разбор конкретной идеи не требуется.')}</p>}
      <h3>{t('Evaluation questions', 'Вопросы оценки')}</h3>
      <p>{progress.pending_checkpoint_count} {t('pending. Answers remain in the checkpoint history below.', 'без ответа. Ответы сохраняются в истории вопросов ниже.')}</p>
      {progress.pending_checkpoints.map(row => <div key={row.question.checkpoint_id} className="ov-row">
        <b>{row.question.phase_id}</b> · {t('node', 'эксперимент')} {row.question.node_id} · {row.question.stage || t('live observation', 'наблюдение во время выполнения')}
        {row.question.expectation && <div>{row.question.expectation}</div>}
      </div>)}
      {progress.pending_checkpoints_truncated && <p className="muted">{t('More questions: use the harness-checkpoints API.', 'Остальные вопросы доступны через API harness-checkpoints.')}</p>}
      <h3>{t('Before finalizing', 'Перед завершением запуска')}</h3>
      {progress.finish_pending_nodes.length > 0 && <p>{t('Unsettled experiments', 'Незавершённые эксперименты')}: {progress.finish_pending_nodes.join(', ')}.
        {' '}{t('Read state, checkpoints and saved command receipts before choosing recovery or explicit cancellation. A monitor verdict follows that question\'s allowed answers.', 'Перед восстановлением или явной отменой прочитайте состояние, вопросы оценки и квитанции команд. Ответ монитору должен быть разрешён конкретным вопросом.')}</p>}
      <p>{progress.finish_report_due ? t('Current run report required. ', 'Нужен актуальный отчёт запуска. ') : ''}
        {progress.finish_reviews_due.length ? `${t('Reviews due', 'Нужны разборы')}: ${progress.finish_reviews_due.join(', ')}`
          : t('No knowledge reviews due at this measured prefix.', 'В прочитанной части истории разбор знаний не требуется.')}</p>
    </>}
    <h3>{t('Recorded intermediate actions', 'Записанные действия')}</h3>
    <p className="muted">{t('A current decision is still bound to its exact Idea and implementation. Superseded receipts remain visible for audit.',
      'Актуальное решение связано с конкретной идеей и реализацией. Устаревшие квитанции сохраняются для проверки истории.')}</p>
    {['decisions', 'reviews', 'checkpoints'].map(kind => <section key={kind}>
      <h4>{ru ? journalNames[kind] : kind} · {history[kind].total}</h4>
      {history[kind].items.length ? <ul>{history[kind].items.map((row, index) => historyRow(kind, row, index))}</ul>
        : <p className="muted">{t('No entries on this page.', 'На этой странице нет записей.')}</p>}
    </section>)}
    <div className="panel-actions">
      <button type="button" className="btn sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 20))}>{t('Newer', 'Более новые')}</button>
      <button type="button" className="btn sm" disabled={!hasMore} onClick={() => setOffset(offset + 20)}>{t('Older', 'Более ранние')}</button>
      <span className="muted">{t('Page', 'Страница')} {Math.floor(offset / 20) + 1} {t('of each journal', 'каждого журнала')}</span>
    </div>
  </>
}
