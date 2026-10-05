import { uiText, useUILanguage, effectiveUILanguage } from './uiLanguage.js'
import React from 'react'
import './new-run-starter.css'

// Examples are drafts, never commands. The composer owns editing and explicit submission.
export default function NewRunStarter({ language = 'auto', disabled = false, onDraft }) {
  useUILanguage()

  const ru = effectiveUILanguage(language) === 'ru'
  const examples = ru ? [
    ['Есть код', 'Цель: [что улучшить]. Код находится на сервере LoopLab: [путь к репозиторию]. Помоги выбрать команду оценки и метрику. Начни с плана на три эксперимента; покажи изменяемые файлы и ограничения перед запуском.'],
    ['Есть данные', 'Цель: [что предсказывать или исследовать]. Данные находятся на сервере LoopLab: [путь к данным]. Помоги подготовить код и честную оценку результата. Предложи план на три эксперимента и покажи условия перед запуском.'],
  ] : [
    ['I have code', 'Goal: [what to improve]. Code on the LoopLab server: [repository path]. Help choose an evaluation command and metric. Prepare a plan for three experiments; show editable files and limits before launch.'],
    ['I have data', 'Goal: [what to predict or investigate]. Data on the LoopLab server: [data path]. Help prepare code and a fair evaluation. Propose a plan for three experiments and show the conditions before launch.'],
  ]
  return <section className="asst-run-starter" aria-label={((ru ? 'Подготовка задачи' : uiText('Prepare a task')))}>
    <h3>{((ru ? 'Для начала достаточно трёх вещей' : uiText('Start with three things')))}</h3>
    <ol>
      <li>{((ru ? 'Цель: что улучшить. Если метрики ещё нет, Assistant поможет её выбрать.' : uiText('Goal: what to improve. Assistant can help choose a metric.')))}</li>
      <li>{((ru ? 'Путь к коду или данным на сервере LoopLab, где будут идти эксперименты.' : uiText('Code or data path on the LoopLab server, where experiments will run.')))}</li>
      <li>{((ru ? 'Лимит экспериментов или времени. Денежный бюджет задаётся отдельно в Settings.' : uiText('Experiment or time limit. Set a monetary budget separately in Settings.')))}</li>
    </ol>
    <div className="asst-run-starter-examples">{examples.map(([label, draft]) => <button
      type="button" className="btn" key={label} disabled={disabled}
      onClick={() => onDraft(draft)}>{uiText(label)}</button>)}</div>
    <p>{((ru ? 'Кнопки заполняют поле сообщения. Замените текст в [скобках]. Для другого примера очистите поле. Ответ модели может оплачиваться.' : uiText('Buttons fill the composer. Replace text in [brackets]. Clear the field to choose another example. Model replies may incur cost.')))}</p>
  </section>
}
