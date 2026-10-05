import { uiText, uiMessage, useUILanguage } from './uiLanguage.js'
import React from 'react'
import './baseRevision.css'
import { baseChoices } from './baseRevision.js'
import { upstreamCheckSummary, upstreamHistoryRows } from './upstreamCheckModel.js'
import { useAssistantUILanguage } from './useAssistantLanguage.js'
import { upstreamProposalSummary, upstreamRecoveryDraft } from './upstreamProposalModel.js'
import UpstreamRecovery from './UpstreamRecovery.jsx'

export default function UpstreamPanel({ state, onClose }) {
  useUILanguage()

  const language = useAssistantUILanguage()
  const ru = language === 'ru'
  const enabled = state.upstream_enabled === true
  const bases = baseChoices(state.nodes)
  const history = upstreamHistoryRows(state.upstream_history)
  const advances = (history || []).filter(row => row.type === 'base_advanced')
  const proposal = upstreamProposalSummary(state.upstream_history)
  // Once a proposal is visible, its own check is the only relevant verdict.
  // A new/failed authoring claim must not display a preceding proposal's pass.
  const check = history ? (proposal ? proposal.check : upstreamCheckSummary(history)) : { status: 'unknown' }
  if (!enabled && !bases.some(row => row.digest !== 'unknown') && history && !history.length) return null
  const discuss = () => {
    const text = enabled && proposal ? upstreamRecoveryDraft(proposal, language) : enabled
      ? (ru
        ? 'Помоги выбрать изменение кода для следующих экспериментов. Прочитай upstream_status и инструкции Maintainer. Объясни пользу, прежнее поведение, проверки и стоимость. Предложи план: не запускай проверки, не меняй базу и не возобновляй запуск.'
        : 'Help choose a code change for future experiments. Read upstream_status and Maintainer instructions. Explain the benefit, original behavior, checks and cost. Propose a plan; do not execute checks, advance the base or resume.')
      : (ru
        ? 'Помоги подготовить новый запуск с переносом полезных изменений кода. Проверь задачу, записанный архив, защищённую оценку и тесты. Объясни, чего не хватает, и предложи план. Не меняй этот запуск, не запускай новый и не выполняй проверки.'
        : 'Help prepare a new run with reusable code changes. Check the task, recorded archive, protected evaluation and required tests. Explain missing prerequisites and propose a plan. Do not change this run, start a new run or execute checks.')
    onClose?.()
    requestAnimationFrame(() => window.dispatchEvent(new CustomEvent('ll:focus-assistant', { detail: {
      text } })))
  }
  return <section className="ov-section upstream-panel" aria-label={((ru ? 'Код для следующих экспериментов' : uiText('Code for future experiments')))}>
    <div className="ov-section-head"><h3>{((ru ? 'Код для следующих экспериментов' : uiText('Code for future experiments')))}</h3><span>{((history ? ru ? `Обновлений базы: ${advances.length}` : uiMessage("{0} recorded base updates", [advances.length]) : (ru ? 'История обновлений недоступна' : uiText('Base update history unavailable'))))}</span></div>
    <p>{((ru ? 'Полезное изменение из одного эксперимента можно проверить и добавить в общий исходный код следующих экспериментов.' : uiText('A useful change from one experiment can be checked and added to the shared starting code for future experiments.')))}</p>
    <p className="muted">{((enabled && proposal ? (ru ? 'Разберите записанный перенос с Assistant перед выбором следующего действия.' : uiText('Inspect recorded code reuse with Assistant before choosing the next action.')) : (enabled ? (ru ? 'Начните с Assistant: выберите изменение и обсудите проверки. Если результатов ещё нет, сначала оцените эксперимент.' : uiText('Start with Assistant: choose a change and discuss checks. If there are no results yet, evaluate an experiment first.')) : (ru ? 'В этом запуске перенос кода не включён. Assistant поможет подготовить новый запуск с записанной исходной базой и нужными проверками.' : uiText('Code reuse is not enabled for this run. Assistant can help prepare a new run with a recorded starting base and the required checks.')))))}</p>
    <UpstreamRecovery proposal={proposal} ru={ru} />
    <button type="button" className="btn" onClick={discuss}>{((enabled && proposal ? (ru ? 'Разобрать перенос с Assistant' : uiText('Inspect code reuse with Assistant')) : (enabled ? (ru ? 'Выбрать изменение с Assistant' : uiText('Choose a change with Assistant')) : (ru ? 'Подготовить с Assistant' : uiText('Prepare with Assistant')))))}</button>
    <p className="muted">{((ru ? 'Кнопка подготовит сообщение. Проверьте его и нажмите «Отправить» для обращения к модели; возможна оплата провайдеру.' : uiText('The button prepares a message. Review it and press Send to contact the model; provider charges may apply.')))}</p>
    {state.upstream_base && <p>{((ru ? 'Новая исходная база' : uiText('Updated starting base')))} <code title={state.upstream_base.selector?.digest}>{state.upstream_base.selector?.digest?.slice(0, 12)}</code> · {((ru ? 'из эксперимента' : uiText('from experiment')))} #{state.upstream_base.source_node_id}</p>}
    <p className="muted">{((ru ? 'Уже измеренные результаты сохраняют свой код. Новая база влияет на будущие эксперименты; для сравнения оценок нужны сопоставимые условия.' : uiText('Measured results keep their original code. A new base affects future experiments; comparing scores requires comparable conditions.')))}</p>
    <div className="upstream-bases">{bases.map(row => <span className="pill" key={row.digest} title={row.digest}>{((row.digest === 'unknown' ? (ru ? 'База неизвестна' : uiText('Base unknown')) : row.digest.slice(0, 12)))} · {(ru ? `Экспериментов: ${row.count}` : uiMessage("{0} experiment{1}", [row.count, row.count === 1 ? '' : 's']))}</span>)}</div>
    {check && <p>{check.status === 'unfinished' ? (((ru ? 'Проверка не завершена — прочитайте прогресс; прерванную проверку нужно восстановить явно.' : uiText('Unfinished check — read progress; recover interrupted checks explicitly.'))))
      : check.status === 'abandoned' ? (((ru ? 'Проверка отменена — поздние результаты не разрешают обновить базу.' : uiText('Abandoned check — late results cannot authorize advancement.'))))
      : check.status === 'unknown' ? (((ru ? 'Доказательства проверки недоступны — прочитайте полную историю переноса.' : uiText('Check evidence unavailable — read complete upstream history.'))))
      : <>{((ru ? 'Записанная завершённая проверка' : uiText('Recorded completed check')))}: <strong>{((ru ? check.status === 'passed' ? 'пройдена' : 'не пройдена' : uiText(check.status)))}</strong> · {((check.executions === null ? (ru ? 'число выполнений неизвестно' : uiText('execution count unavailable')) : ru ? `Выполнений: ${check.executions}` : uiMessage("{0} explicit executions", [check.executions])))} · {((check.seconds === null ? (ru ? 'затраты времени неизвестны' : uiText('cost unavailable')) : `${check.seconds.toFixed(1)} ${ru ? 'с' : 's'}`))}</>}</p>}
    {check && <p className="muted">{((ru ? 'Перед обновлением базы прочитайте актуальные доказательства; записанный результат сам по себе не разрешает перенос.' : uiText('Read current upstream evidence before advancement; recorded results do not approve it.')))}</p>}
    {advances.length > 0 && <details><summary>{((ru ? 'Откуда взяты изменения' : uiText('Where the changes came from')))}</summary><ol>{advances.map(row => <li key={row.seq}>{((ru ? 'Эксперимент' : uiText('Experiment')))} #{row.source_node_id} → <code>{row.selector?.digest?.slice(0, 12)}</code> · {row.summary} · {((ru ? 'флаг' : uiText('flag')))} {row.flag?.name}, {((ru ? 'прежнее значение' : uiText('old default')))} {row.flag?.default}</li>)}</ol></details>}
    {enabled && <details><summary>{((ru ? 'Как переносится код' : uiText('How code reuse works')))}</summary><p className="muted">{((ru ? 'Пауза → завершение движка → предложение Maintainer → измеренные проверки → явное обновление исходной базы → отдельное возобновление. Прерванные проверки восстанавливает оператор.' : uiText('Pause → wait for engine exit → Maintainer proposal → measured checks → explicit base advance → resume. Interrupted checks require operator recovery.')))}</p></details>}
  </section>
}
