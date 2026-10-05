import { uiText, useUILanguage } from './uiLanguage.js'
import React from 'react'
import { useAssistantLanguage } from './useAssistantLanguage.js'

export function validAgentActivity(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || value.scope !== 'this_ui_process' || value.source !== 'authenticated_harness_progress_read'
      || !Number.isSafeInteger(value.quiet_after_s) || value.quiet_after_s < 1) return false
  if (value.status === 'not_observed') return value.last_seen_at === null && value.age_seconds === null
  return ['recent_request', 'quiet'].includes(value.status)
    && typeof value.last_seen_at === 'string' && Number.isFinite(Date.parse(value.last_seen_at))
    && Number.isSafeInteger(value.age_seconds) && value.age_seconds >= 0
    && (value.status === 'quiet') === (value.age_seconds >= value.quiet_after_s)
}

// Activity is an optional observation. A malformed/failed read must neither show
// a stale "recent" signal nor hide the measured evidence and checkpoint obligations.
export default function AgentActivity({ activity, fresh }) {
  useUILanguage()

  const [language] = useAssistantLanguage()
  const ru = language === 'ru'
  if (activity == null) return null // older server
  if (!fresh || !validAgentActivity(activity)) return <span role="status" className="muted">
    {((((ru ? 'Активность агента недоступна' : uiText('Agent activity unavailable')))))}
  </span>
  const ago = activity.age_seconds == null ? '' : activity.age_seconds < 60
    ? `${activity.age_seconds}${ru ? ' с' : 's'}`
    : `${Math.floor(activity.age_seconds / 60)}${ru ? ' мин' : 'm'} ${activity.age_seconds % 60}${ru ? ' с' : 's'}`
  const label = activity.status === 'not_observed'
    ? (ru ? 'Обращений агента ещё не наблюдалось' : 'No agent reads observed')
    : activity.status === 'quiet'
      ? (ru ? `Нет новых обращений агента · ${ago}` : `No recent agent reads · ${ago}`)
      : (ru ? `Агент обращался ${ago} назад` : `Agent read ${ago} ago`)
  const explanation = ru
    ? 'Только успешные чтения Agent cycle с harness-токеном в этом процессе UI. Опрос браузера не учитывается. Отсутствие обращений не доказывает гибель агента; проверьте клиент, вопросы и квитанции. Перезапуск UI очищает наблюдение.'
    : 'Only successful Agent cycle reads with the harness credential in this UI process. Browser polling does not count. Silence does not prove agent death; inspect the client, questions and receipts. UI restart clears this observation.'
  return <span className={'chip' + (activity.status === 'quiet' ? ' warn' : '')}
    role="status" title={explanation + (activity.last_seen_at
      ? ` ${ru ? 'Последнее чтение' : 'Last read'}: ${activity.last_seen_at}` : '')}>
    {uiText(label)}
  </span>
}
