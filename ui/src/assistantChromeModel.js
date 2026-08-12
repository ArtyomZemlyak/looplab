// Doc 25 UI-05. The Assistant is one conversation rendered in three layouts (bar / side / full), and
// the controls the layouts SHARE were written out once per layout: the same gate, the same explanation
// ladder, two or three times over. Measured on 2026-08-05: the new-chat gate appears twice byte for
// byte, and the fold-to-bar control three times, each carrying its own copy of the run-command
// confirmation branch.
//
// The rule these decisions exist to hold is one rule: **a control's disabled state, the reason it
// shows, and the action it performs all come from the SAME branch.** A gate that drifts from its
// tooltip is not a cosmetic bug — it is a control that refuses without saying why, or (worse) a fold
// button labelled "Cancel command" that collapses the view instead of cancelling the command. Three
// copies of a four-arm ladder is how that happens; one function that returns all three fields
// together is why it cannot.
//
// The per-layout copy is NOT shared, and that is deliberate: "new chat", "collapse to the bar",
// "fold back to the bar" and "fold to the bar" are four different sentences for four different
// positions on screen, and only the operator's view can say which one is true. The idle title is
// therefore an argument, and every arm ABOVE idle is the shared part.

export function newChatGate({
  turnStarting = false, retryChecking = false, directConfirm = false,
} = {}, idleTitle = undefined) {
  // An unbound first turn, an in-flight saved-turn check and an unresolved run-command confirmation
  // each own state a new chat would orphan. Ordered by how close the cause is to the operator.
  if (turnStarting) {
    return { disabled: true, title: 'Wait for the new Assistant chat to finish starting' }
  }
  if (retryChecking) return { disabled: true, title: 'Wait for the saved turn check to finish' }
  if (directConfirm) return { disabled: true, title: 'Resolve the run-command confirmation first' }
  return { disabled: false, title: idleTitle }
}

// A pending run-command confirmation REPURPOSES this button: the same pixel stops folding the view and
// starts cancelling the command. Label, tooltip and action have to move together or the button lies.
export function foldControl(directConfirm, idleTitle = undefined) {
  return directConfirm
    ? {
        action: 'cancel', label: 'Cancel command',
        title: 'Cancel the run-command confirmation and keep the draft',
      }
    : { action: 'collapse', label: '▾ bar', title: idleTitle }
}


// THE CONTEXT CHIP, and why its number moved. The operator watched it read 20k, then 50k, then 25k,
// then 18k inside ONE conversation and reasonably concluded the accounting was broken. It was not —
// but the label was: the chip said "tokens in the assistant's context (grows with the conversation)",
// a promise the number cannot keep, for two honest reasons.
//
//   * It shows the LAST TURN's peak single prompt. A turn with forty tool calls carries a big peak;
//     the one-line turn after it carries a small one. Neither is the window "shrinking".
//   * `drive_tool_loop` COMPACTS the history in place when it grows past the budget, so the context
//     really does get smaller — that is the loop working, and the chip made it look like a fault.
//
// So the chip now reports both: what the last turn actually carried, and the high-water mark for the
// chat, which is the monotonic number the old label was describing.
export function contextUsage(messages = []) {
  let last = 0
  let peak = 0
  let spent = 0
  for (const message of messages) {
    const tokens = message?.tokens || {}
    // `context` is the peak SINGLE prompt of that turn; `prompt` SUMS the same context re-sent by
    // every call in the turn (billed, O(calls²)), so it is a cost number and never a window number.
    const value = Number(tokens.context || 0)
    if (Number.isFinite(value) && value > 0) {
      last = value
      if (value > peak) peak = value
    }
    const total = Number(tokens.total || 0)
    if (Number.isFinite(total) && total > 0) spent += total
  }
  return { last, peak, spent, compacted: peak > 0 && last > 0 && last < peak }
}

export function contextChipTitle(usage) {
  if (!usage || !usage.last) return ''
  const parts = [`≈${usage.last.toLocaleString()} tokens carried by the last turn`]
  if (usage.compacted) {
    parts.push(`peak this chat ≈${usage.peak.toLocaleString()} — it fell because the agent loop `
      + 'compacted the history, which is the loop working, not a fault')
  } else if (usage.peak > usage.last) {
    parts.push(`peak this chat ≈${usage.peak.toLocaleString()}`)
  }
  parts.push(`≈${usage.spent.toLocaleString()} tokens billed across this chat`)
  return parts.join(' · ')
}
