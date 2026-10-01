import React from 'react'
import { fmt } from './util.js'
import { terminalReady, sourceIncomplete, sourceIntegrityNotice,
  bestMetricCaveats, bestMetricCaveatLabel, bestMetricCaveatNotice } from './runIndex.js'
import { hashWithRunRouteState } from './runRouteState.js'
import { useAssistantLanguage } from './useAssistantLanguage.js'
import './assistant-run-result.css'

const measured = value => value && Number.isSafeInteger(value.node_id) && value.node_id >= 0
  && Number.isSafeInteger(value.attempt) && value.attempt >= 0
  && typeof value.value === 'number' && Number.isFinite(value.value)
  && typeof value.confirmed === 'boolean'

export default function AssistantRunResult({ run, onOpen, onAsk, onReady, askDisabled = false, askDisabledReason }) {
  const [language] = useAssistantLanguage()
  const text = (en, ru) => language === 'ru' ? ru : en
  React.useEffect(() => { onReady?.() }, [onReady, run?.run_id, run?.generation])
  if (!run || !terminalReady(run) || run.finalization_incomplete) return null
  const generation = /^[0-9a-f]{64}$/.test(run.generation || '') ? run.generation : null
  const base = `#/run/${encodeURIComponent(run.run_id)}`
  const report = generation ? hashWithRunRouteState(base, { generation, view: 'report' }) : base
  const evidence = run.result_summary
  const complete = !sourceIncomplete(run) && generation
    && measured(evidence?.first) && measured(evidence?.selected)
  const selected = complete ? evidence.selected : null
  const first = complete ? evidence.first : null
  const caveats = bestMetricCaveats(run)
  const direction = run.direction === 'min' ? text('lower is better', 'меньше — лучше')
    : run.direction === 'max' ? text('higher is better', 'больше — лучше') : text('direction not recorded', 'направление не указано')
  const sameNode = first && first.node_id === selected.node_id && first.attempt === selected.attempt
  const nodeHref = node => hashWithRunRouteState(base, {
    generation, nodeId: node.node_id, nodeGeneration: node.attempt, inspectTab: 'Code',
  })
  const link = (href, text) => <a className="btn sm" href={href}
    onClick={onOpen ? event => onOpen(event, href) : undefined}>{text}</a>
  return <section className="asst-run-result" aria-label={text('Run result summary', 'Итог запуска')}>
    <div className="asst-run-result-head"><strong>{text('Run result', 'Итог запуска')}</strong>
      <span>{text('Free to read', 'Без вызова модели')}</span></div>
    <p>{run.stop_reason === 'error' ? text('The run ended with an error. Review partial results and failures.', 'Запуск завершился с ошибкой. Проверьте частичные результаты и причины сбоев.')
      : text('Review the recorded result before planning another experiment.', 'Проверьте полученный результат перед следующим экспериментом.')}</p>
    {selected ? <>
      <div className="asst-run-result-metric">{run.objective_key || text('Objective', 'Целевая метрика')} · {direction}</div>
      <dl><div><dt>{text('First eligible experiment', 'Первый пригодный для сравнения эксперимент')} · #{first.node_id} · {first.confirmed ? text('mean', 'среднее') : text('score', 'оценка')}</dt><dd>{fmt(first.value)}</dd></div>
        <div><dt>{text('Selected result', 'Выбранный результат')} · #{selected.node_id} · {selected.confirmed ? text('mean', 'среднее') : text('score', 'оценка')}</dt><dd>{fmt(selected.value)}</dd></div></dl>
      <p>{sameNode ? text('The selected result is the first eligible experiment.', 'Выбран результат первого пригодного для сравнения эксперимента.')
        : text('Read Report to compare these values, their evaluation conditions, and confirmation.', 'В отчёте сравните значения, условия оценки и подтверждение результата.')}</p>
      <p className="asst-run-result-caution">{selected.confirmed
        ? Number.isSafeInteger(selected.seeds) && selected.seeds >= 2
          ? text(`Selected mean from ${selected.seeds} seeds. Check spread and trust evidence in Report.`, `Среднее по ${selected.seeds} случайным инициализациям. Проверьте разброс и надёжность оценки в отчёте.`)
          : text('Confirmation mean recorded; multiple successful seeds are not established.', 'Среднее сохранено; несколько успешных случайных инициализаций не подтверждены.')
        : text('Selected result has no multi-seed confirmation. Treat it as exploratory.', 'Результат не подтверждён на нескольких случайных инициализациях. Это предварительная оценка.')}</p>
      {caveats.length > 0 && <p className="asst-run-result-caution" title={bestMetricCaveatNotice(run)}>
        {text('Recorded caveats:', 'Ограничения:')} {caveats.map(bestMetricCaveatLabel).join(' · ')}. {text('Review Report and Trust.', 'Проверьте отчёт и раздел Trust.')}
      </p>}
    </> : <p className="asst-run-result-caution">{sourceIncomplete(run)
      ? sourceIntegrityNotice(run) : text('No complete result summary is available. Open Report to inspect the recorded evidence.', 'Полного итога пока нет. Откройте отчёт и проверьте сохранённые данные.')}</p>}
    <div className="asst-run-result-actions">
      {link(report, text('Read Report', 'Открыть отчёт'))}
      {selected && link(nodeHref(selected), text('Open selected code', 'Открыть выбранный код'))}
      {generation && link(hashWithRunRouteState(base, { generation, panel: 'artifacts' }), text('Find artifacts', 'Найти артефакты'))}
      {onAsk && <button className="btn sm ghost" disabled={askDisabled} onClick={onAsk}
        title={askDisabled ? askDisabledReason || 'Wait for the current action before preparing a question'
          : text('Prepare a question in this chat; review it before Send', 'Подготовить вопрос в чате; проверьте его перед отправкой')}>{text('Ask about this result', 'Спросить об этом результате')}</button>}
    </div>
    {onAsk && <div className="asst-run-result-note">{text('Ask prepares a message. Sending it may use a paid model.', 'Кнопка готовит текст вопроса. Отправка может вызвать платную модель.')}</div>}
  </section>
}
