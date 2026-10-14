import { useUILanguage } from './uiLanguage.js'
import React from 'react'
import { useAssistantUILanguage } from './useAssistantLanguage.js'
import { agentActivityChip, validAgentActivity } from './agentActivityModel.js'

export { validAgentActivity }

// Activity is an optional observation. A malformed/failed read must neither show
// a stale "recent" signal nor hide the measured evidence and checkpoint obligations.
// Wording and the warning rule live in `agentActivityModel.js`.
export default function AgentActivity({ activity, fresh }) {
  useUILanguage()
  const language = useAssistantUILanguage()
  if (activity == null) return null // older server
  const chip = agentActivityChip(activity, { fresh, ru: language === 'ru' })
  if (chip.unavailable) return <span role="status" className="muted">{chip.text}</span>
  return <span className={'chip' + (chip.warn ? ' warn' : '')} role="status" title={chip.title}>
    {chip.text}
  </span>
}
