
import { uiText, useUILanguage } from './uiLanguage.js'
import React from 'react'
import { fmt } from './util.js'
import { terminalReady, sourceIncomplete, sourceIntegrityNotice,
  bestMetricCaveats, bestMetricCaveatNotice } from './runIndex.js'
import { resultCaveatText, resultTrustAdvisoryText } from './resultNoticeModel.js'
import { hashWithRunRouteState } from './runRouteState.js'
import { useAssistantUILanguage } from './useAssistantLanguage.js'
import { resultMeasurement, resultSpreadText } from './resultMeasurement.js'
import './assistant-run-result.css'

const measured = value => value && Number.isSafeInteger(value.node_id) && value.node_id >= 0
  && Number.isSafeInteger(value.attempt) && value.attempt >= 0
  && typeof value.value === 'number' && Number.isFinite(value.value)
  && typeof value.confirmed === 'boolean'

export default function AssistantRunResult({ run, onOpen, onAsk, onReady, askDisabled = false, askDisabledReason }) {
  useUILanguage()

  const language = useAssistantUILanguage()
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
  const measurement = selected && resultMeasurement(selected.confirmed, selected.seeds, language)
  const nodeHref = node => hashWithRunRouteState(base, {
    generation, nodeId: node.node_id, nodeGeneration: node.attempt, inspectTab: 'Code',
  })
  const link = (href, text) => <a className="btn sm" href={href}
    onClick={onOpen ? event => onOpen(event, href) : undefined}>{text}</a>
  const value = node => <dd>{fmt(node.value)}{node.confirmed && <div className="muted"><small>
    {Number.isFinite(node.score)
      ? `${text('Evaluation score', 'Основная оценка')}: ${fmt(node.score)}`
      : text('Evaluation score not recorded.', 'Основная оценка не записана.')}</small></div>}</dd>
  return <section className="asst-run-result" aria-label={text('Run result summary', 'Итог запуска')}>
    <div className="asst-run-result-head"><strong>{text('Run result', 'Итог запуска')}</strong>
      <span>{text('Free to read', 'Без вызова модели')}</span></div>
    <p>{run.stop_reason === 'error' ? text('The run ended with an error. Review partial results and failures.', 'Запуск завершился с ошибкой. Проверьте частичные результаты и причины сбоев.')
      : text('Review the recorded result before planning another experiment.', 'Проверьте полученный результат перед следующим экспериментом.')}</p>
    {selected ? <>
      <div className="asst-run-result-metric">{run.objective_key || text('Objective', 'Целевая метрика')} · {uiText(direction)}</div>
      <dl><div><dt>{text('First eligible experiment', 'Первый допустимый эксперимент')} · #{first.node_id} · {uiText(resultMeasurement(first.confirmed, first.seeds, language).label)}</dt>{value(first)}</div>
        <div><dt>{text('Selected result', 'Выбранный результат')} · #{selected.node_id} · {uiText(measurement.label)}</dt>{value(selected)}</div></dl>
      <p>{sameNode ? text('The selected result is the first eligible experiment; it does not establish improvement.', 'Выбран первый допустимый эксперимент; улучшение не установлено.')
        : first.confirmed !== selected.confirmed
          ? text('Different measurement types; improvement is not established. Compare evaluation scores and repeat checks separately in Report.',
            'Разные типы измерений; улучшение не установлено. В отчёте сравните основные оценки отдельно от повторных запусков.')
          : text('Read Report to compare values, conditions and repeat checks.', 'В отчёте сравните значения, условия оценки и повторы.')}</p>
      <p className="asst-run-result-caution">{measurement.reliability}</p>
      {selected.confirmed && <p className="asst-run-result-caution">{uiText(resultSpreadText(selected.confirmed_std, language))}</p>}
      {selected.trust_advisory === true && <p className="asst-run-result-caution">{uiText(resultTrustAdvisoryText(language))}</p>}
      <p className="asst-run-result-caution">{text('First eligible is not necessarily the task baseline; detector coverage is not fully verified.',
        'Первый допустимый эксперимент не обязательно является базовым решением задачи; полнота проверок надёжности не подтверждена.')}</p>
      {caveats.length > 0 && <p className="asst-run-result-caution" title={uiText(bestMetricCaveatNotice(run))}>
        {text('Recorded caveats:', 'Ограничения:')} {caveats.map(code => resultCaveatText(code, language)).join(' · ')}. {text('Review Report and Trust.', 'Проверьте отчёт и раздел «Надёжность».')}
      </p>}
    </> : <p className="asst-run-result-caution">{((sourceIncomplete(run) ? uiText(sourceIntegrityNotice(run)) : text('No complete result summary is available. Open Report to inspect the recorded evidence.', 'Полного итога пока нет. Откройте отчёт и проверьте сохранённые данные.')))}</p>}
    <div className="asst-run-result-actions">
      {link(report, text('Read Report', 'Открыть отчёт'))}
      {selected && link(nodeHref(selected), text('Open selected code', 'Открыть выбранный код'))}
      {generation && link(hashWithRunRouteState(base, { generation, panel: 'artifacts' }), text('Find artifacts', 'Найти артефакты'))}
      {onAsk && <button className="btn sm ghost" disabled={askDisabled} onClick={onAsk}
        title={((askDisabled ? (askDisabledReason || uiText('Wait for the current action before preparing a question')) : text('Prepare a question in this chat; review it before Send', 'Подготовить вопрос в чате; проверьте его перед отправкой')))}>{text('Ask about this result', 'Спросить об этом результате')}</button>}
    </div>
    {onAsk && <div className="asst-run-result-note">{text('Ask prepares a message. Sending it may use a paid model.', 'Кнопка готовит текст вопроса. Отправка может вызвать платную модель.')}</div>}
  </section>
}
