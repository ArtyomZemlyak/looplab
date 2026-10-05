import React from 'react'
import './baseRevision.css'
import { baseChoices } from './baseRevision.js'
import { upstreamCheckSummary } from './upstreamCheckModel.js'
import { useAssistantLanguage } from './useAssistantLanguage.js'
import { upstreamProposalSummary, upstreamRecoveryDraft } from './upstreamProposalModel.js'
import UpstreamRecovery from './UpstreamRecovery.jsx'

export default function UpstreamPanel({ state, onClose }) {
  const [language] = useAssistantLanguage()
  const ru = language === 'ru'
  const enabled = state.upstream_enabled === true
  const bases = baseChoices(state.nodes)
  const history = Array.isArray(state.upstream_history) ? state.upstream_history : []
  const advances = history.filter(row => row?.type === 'base_advanced')
  const proposal = upstreamProposalSummary(history)
  // Once a proposal is visible, its own check is the only relevant verdict.
  // A new/failed authoring claim must not display a preceding proposal's pass.
  const check = proposal ? proposal.check : upstreamCheckSummary(history)
  if (!enabled && !bases.some(row => row.digest !== 'unknown') && !history.length) return null
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
  return <section className="ov-section upstream-panel" aria-label={ru ? 'Код для следующих экспериментов' : 'Code for future experiments'}>
    <div className="ov-section-head"><h3>{ru ? 'Код для следующих экспериментов' : 'Code for future experiments'}</h3><span>{ru ? `Обновлений базы: ${advances.length}` : `${advances.length} recorded base updates`}</span></div>
    <p>{ru ? 'Полезное изменение из одного эксперимента можно проверить и добавить в общий исходный код следующих экспериментов.' : 'A useful change from one experiment can be checked and added to the shared starting code for future experiments.'}</p>
    <p className="muted">{enabled && proposal
      ? (ru ? 'Разберите записанный перенос с Assistant перед выбором следующего действия.' : 'Inspect recorded code reuse with Assistant before choosing the next action.') : enabled
      ? (ru ? 'Начните с Assistant: выберите изменение и обсудите проверки. Если результатов ещё нет, сначала оцените эксперимент.' : 'Start with Assistant: choose a change and discuss checks. If there are no results yet, evaluate an experiment first.')
      : (ru ? 'В этом запуске перенос кода не включён. Assistant поможет подготовить новый запуск с записанной исходной базой и нужными проверками.' : 'Code reuse is not enabled for this run. Assistant can help prepare a new run with a recorded starting base and the required checks.')}</p>
    <UpstreamRecovery proposal={proposal} ru={ru} />
    <button type="button" className="btn" onClick={discuss}>{enabled && proposal
      ? (ru ? 'Разобрать перенос с Assistant' : 'Inspect code reuse with Assistant') : enabled
      ? (ru ? 'Выбрать изменение с Assistant' : 'Choose a change with Assistant')
      : (ru ? 'Подготовить с Assistant' : 'Prepare with Assistant')}</button>
    <p className="muted">{ru ? 'Кнопка подготовит сообщение. Проверьте его и нажмите «Отправить» для обращения к модели; возможна оплата провайдеру.' : 'The button prepares a message. Review it and press Send to contact the model; provider charges may apply.'}</p>
    {state.upstream_base && <p>{ru ? 'Новая исходная база' : 'Updated starting base'} <code title={state.upstream_base.selector?.digest}>{state.upstream_base.selector?.digest?.slice(0, 12)}</code> · {ru ? 'из эксперимента' : 'from experiment'} #{state.upstream_base.source_node_id}</p>}
    <p className="muted">{ru ? 'Уже измеренные результаты сохраняют свой код. Новая база влияет на будущие эксперименты; для сравнения оценок нужны сопоставимые условия.' : 'Measured results keep their original code. A new base affects future experiments; comparing scores requires comparable conditions.'}</p>
    <div className="upstream-bases">{bases.map(row => <span className="pill" key={row.digest} title={row.digest}>{row.digest === 'unknown' ? (ru ? 'База неизвестна' : 'Base unknown') : row.digest.slice(0, 12)} · {ru ? `Экспериментов: ${row.count}` : `${row.count} experiment${row.count === 1 ? '' : 's'}`}</span>)}</div>
    {check && <p>{check.status === 'unfinished' ? (ru ? 'Проверка не завершена — прочитайте прогресс; прерванную проверку нужно восстановить явно.' : 'Unfinished check — read progress; recover interrupted checks explicitly.')
      : check.status === 'abandoned' ? (ru ? 'Проверка отменена — поздние результаты не разрешают обновить базу.' : 'Abandoned check — late results cannot authorize advancement.')
      : check.status === 'unknown' ? (ru ? 'Доказательства проверки недоступны — прочитайте полную историю переноса.' : 'Check evidence unavailable — read complete upstream history.')
      : <>{ru ? 'Записанная завершённая проверка' : 'Recorded completed check'}: <strong>{ru ? (check.status === 'passed' ? 'пройдена' : 'не пройдена') : check.status}</strong> · {check.executions === null ? (ru ? 'число выполнений неизвестно' : 'execution count unavailable') : (ru ? `Выполнений: ${check.executions}` : `${check.executions} explicit executions`)} · {check.seconds === null ? (ru ? 'затраты времени неизвестны' : 'cost unavailable') : `${check.seconds.toFixed(1)} ${ru ? 'с' : 's'}`}</>}</p>}
    {check && <p className="muted">{ru ? 'Перед обновлением базы прочитайте актуальные доказательства; записанный результат сам по себе не разрешает перенос.' : 'Read current upstream evidence before advancement; recorded results do not approve it.'}</p>}
    {advances.length > 0 && <details><summary>{ru ? 'Откуда взяты изменения' : 'Where the changes came from'}</summary><ol>{advances.map(row => <li key={row.seq}>{ru ? 'Эксперимент' : 'Experiment'} #{row.source_node_id} → <code>{row.selector?.digest?.slice(0, 12)}</code> · {row.summary} · {ru ? 'флаг' : 'flag'} {row.flag?.name}, {ru ? 'прежнее значение' : 'old default'} {row.flag?.default}</li>)}</ol></details>}
    {enabled && <details><summary>{ru ? 'Как переносится код' : 'How code reuse works'}</summary><p className="muted">{ru
      ? 'Пауза → завершение движка → предложение Maintainer → измеренные проверки → явное обновление исходной базы → отдельное возобновление. Прерванные проверки восстанавливает оператор.'
      : 'Pause → wait for engine exit → Maintainer proposal → measured checks → explicit base advance → resume. Interrupted checks require operator recovery.'}</p></details>}
  </section>
}
