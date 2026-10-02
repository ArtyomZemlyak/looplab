import React from 'react'
import './launch-guidance.css'

const guidance = {
  review: [
    'Next: check the proposal',
    'Review the goal, metric direction and code/data paths, then choose Validate — free. It resolves the settings on the server without starting experiments or calling a model.',
    'Дальше: проверить план',
    'Проверьте цель, направление метрики и пути к коду/данным, затем нажмите Validate — free. Проверка уточнит настройки на сервере, без запуска экспериментов и вызова модели.',
  ],
  validated: [
    'Next: review the checked plan and start',
    'Check the effective limits above and cost warning below. Start run launches this exact plan. Editing it requires Validate again; validation alone does not start a run.',
    'Дальше: проверить условия и запустить',
    'Проверьте итоговые лимиты выше и предупреждение о затратах ниже. Start run запустит именно этот план. После правки нужна повторная Validate; сама проверка запуск не начинает.',
  ],
  validating: ['Checking the plan…', 'Wait for the server preview. No experiment has started.',
    'Проверяем план…', 'Дождитесь итоговых настроек сервера. Эксперимент ещё не запущен.'],
  pending: ['Confirming startup…', 'Wait for this startup to be confirmed. Keep this card to recover its status if the reply is lost.',
    'Подтверждаем запуск…', 'Дождитесь подтверждения этого запуска. Если ответ потеряется, его статус можно восстановить в этой карточке.'],
  recovery: ['Next: check the previous startup', 'Choose Check startup to read the status of the launch already sent. An unknown reply does not prove failure; do not submit another launch.',
    'Дальше: проверить прежний запуск', 'Нажмите Check startup: он прочитает статус уже отправленного запуска. Потерянный ответ не доказывает сбой; повторный запуск пока заблокирован.'],
  damaged: ['Next: inspect startup recovery', 'The saved startup record is damaged. Inspect runs and provider activity before Release after inspection.',
    'Дальше: проверить историю запуска', 'Сохранённая запись запуска повреждена. Перед Release after inspection проверьте список запусков и активность провайдера.'],
  settings: ['Next: resolve Settings', 'Read the Settings warning above and open Go to Settings. Return after the saved configuration is confirmed.',
    'Дальше: разобраться с настройками', 'Прочитайте предупреждение выше и откройте Go to Settings. Вернитесь после подтверждения сохранённых настроек.'],
  storage: ['Next: restore browser storage', 'Startup was not sent. Restore session storage, then Reset proposal and validate again.',
    'Дальше: восстановить хранилище браузера', 'Запуск не отправлен. Восстановите session storage, затем нажмите Reset proposal и проверьте план снова.'],
  errors: ['Next: fix the highlighted problem', 'Review the errors above; proposal details open for editable fields. Your edits are kept. Validate the corrected plan before starting.',
    'Дальше: исправить отмеченную проблему', 'Посмотрите ошибки выше: поля с ошибками открываются для правки. Изменения сохранены. Исправленный план нужно проверить перед запуском.'],
  started: ['Next: open the run', 'Open started run shows progress and measured results when available. Confirmed startup does not mean experiments have finished.',
    'Дальше: открыть запуск', 'Open started run покажет прогресс и измеренные результаты по мере готовности. Подтверждение старта не означает завершение экспериментов.'],
  loading: ['Reading startup state…', 'Wait while this card reads its saved recovery state.',
    'Читаем состояние запуска…', 'Карточка читает сохранённую запись восстановления.'],
}

// Display only. All admission, validation and startup decisions remain in LaunchCard.
export default function LaunchGuidance({ phase, language = 'auto', notice }) {
  const ru = language === 'ru'
  const row = guidance[phase] || guidance.review
  return <div className="asst-launch-guide">
    <strong>{row[ru ? 2 : 0]}</strong>
    <p>{row[ru ? 3 : 1]}</p>
    {notice && <details><summary>{ru ? 'Подробный статус' : 'Detailed status'}</summary>
      <p>{notice}</p></details>}
  </div>
}
