// Preserve conversational order; insert receipts only where recorded time supports it.
const time = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null
export function resultConversation(messages, rows) {
  const pending = [...rows]
  const timeline = []
  messages.forEach((message, index) => {
    const stamp = time(message.ts)
    if (stamp !== null) {
      for (let i = 0; i < pending.length;) {
        const completed = time(pending[i].completed_at)
        if (completed !== null && completed <= stamp) {
          timeline.push({ result: pending.splice(i, 1)[0] })
        } else i++
      }
    }
    timeline.push({ message, index })
  })
  return [...timeline, ...pending.map(result => ({ result }))]
}
