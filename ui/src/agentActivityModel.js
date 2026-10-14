import { uiMessage, uiPlural, uiText } from './uiLanguage.js'

// The agent-activity chip as a pure projection of the server's `agent_activity` observation
// (`looplab/serve/agent_activity.py`). Review 2026-10-14 section 2: "No recent agent reads · 6m 40s" read
// as an outage although nothing was asked of the agent. Silence is now neutral; the chip turns to a
// warning only when the server says LoopLab is WAITING for the agent (`attention ===
// 'waiting_on_agent'`: an unanswered evaluation question, or an idle running engine waiting for the
// next-experiment decision) and the agent has been quiet. A server that predates `attention` keeps
// the historical rule (quiet = warn) so a mixed deployment never hides a real wait.

export const ACTIVITY_SOURCES = ['authenticated_harness_progress_read', 'authenticated_harness_read']
const ATTENTION = ['waiting_on_agent', 'agent_has_work', 'nothing_required']

export function validAgentActivity(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || value.scope !== 'this_ui_process' || !ACTIVITY_SOURCES.includes(value.source)
      || !Number.isSafeInteger(value.quiet_after_s) || value.quiet_after_s < 1) return false
  if (value.attention !== undefined && !ATTENTION.includes(value.attention)) return false
  if (value.status === 'not_observed') return value.last_seen_at === null && value.age_seconds === null
  return ['recent_request', 'quiet'].includes(value.status)
    && typeof value.last_seen_at === 'string' && Number.isFinite(Date.parse(value.last_seen_at))
    && Number.isSafeInteger(value.age_seconds) && value.age_seconds >= 0
    && (value.status === 'quiet') === (value.age_seconds >= value.quiet_after_s)
}

// "6m 40s" / "6 мин 40 с"; the five freshest seconds read "just now" rather than "0s ago".
export function activityAge(seconds, ru = false) {
  if (!Number.isSafeInteger(seconds) || seconds < 0) return ''
  if (seconds < 5) return uiText('just now')
  const h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60
  const span = h ? (ru ? `${h} ч ${m} мин` : `${h}h ${m}m`)
    : m ? (ru ? `${m} мин ${s} с` : `${m}m ${s}s`) : (ru ? `${s} с` : `${s}s`)
  return uiMessage('{0} ago', [span])
}

function waitingPhrase(waiting) {
  if (waiting?.reason === 'evaluation_question') {
    const n = Number.isSafeInteger(waiting.count) && waiting.count > 0 ? waiting.count : 1
    return uiPlural(n, '{0} evaluation question', '{0} evaluation questions')
  }
  if (waiting?.reason === 'next_experiment') return uiText('the next experiment decision')
  return uiText('a pending decision')
}

// { text, warn, title } — or the "unavailable" chip for a failed/malformed read.
export function agentActivityChip(activity, { fresh = true, ru = false } = {}) {
  if (!fresh || !validAgentActivity(activity))
    return { text: uiText('Agent activity unavailable'), warn: false, unavailable: true, title: '' }
  const observed = activity.status !== 'not_observed'
  const ago = observed ? activityAge(activity.age_seconds, ru) : ''
  const legacy = activity.attention === undefined
  const waiting = activity.attention === 'waiting_on_agent'
  const idle = legacy ? false : activity.attention === 'nothing_required'
  let text
  if (waiting)
    text = observed
      ? uiMessage('Waiting for the agent: {0} · last contact {1}', [waitingPhrase(activity.waiting_for), ago])
      : uiMessage('Waiting for the agent: {0} · no contact observed', [waitingPhrase(activity.waiting_for)])
  else if (!observed)
    text = idle ? uiText('No agent contact observed yet · nothing required') : uiText('No agent contact observed yet')
  else if (idle && activity.status === 'quiet')
    text = uiMessage('Agent last contacted the API {0} · nothing required', [ago])
  else text = uiMessage('Agent last contacted the API {0}', [ago])
  const title = uiText('Counts successful requests (reads and writes) made with the harness credential in this UI process. Browser polling does not count. Silence does not prove agent death; inspect the client, questions and receipts. UI restart clears this observation.')
    + (activity.last_seen_at ? ` ${uiText('Last contact')}: ${activity.last_seen_at}` : '')
  return { text, warn: waiting || (legacy && activity.status === 'quiet'), unavailable: false, title }
}
