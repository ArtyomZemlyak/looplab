const GENESIS_COMMAND = /^\/(?:new|genesis|run)(?:\s|$)/i
const visibleContent = message => {
  const value = String(message?.content || '')
  return message?.role === 'user'
    ? value.replace(/\n*\[UI context:[^\]]*\]\s*$/, '').trimEnd()
    : value
}

// A proposal belongs to the Assistant message that renders it.  Its launch provenance begins at the
// latest explicit Genesis command before that message; unrelated earlier session turns fail closed
// instead of silently becoming part of the run's durable chat.
export function proposalLaunchChat(messages, assistantIndex) {
  const rows = Array.isArray(messages) ? messages : []
  const end = Math.min(Math.max(Number(assistantIndex) || 0, 0), Math.max(rows.length - 1, 0))
  let start = -1
  for (let index = end; index >= 0; index -= 1) {
    const message = rows[index]
    if (message?.role === 'user' && GENESIS_COMMAND.test(visibleContent(message).trim())) {
      start = index
      break
    }
  }
  if (start < 0) return []
  return rows.slice(start, end + 1)
    .filter(message => (message?.role === 'user' || message?.role === 'assistant')
      && !message.streaming && visibleContent(message).trim())
    .map(message => ({ role: message.role, content: visibleContent(message) }))
}
