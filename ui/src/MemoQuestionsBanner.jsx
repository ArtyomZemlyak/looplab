import { uiText, uiMessage, uiPlural, useUILanguage } from './uiLanguage.js'
// The Research view's banner for memo questions that are NOT on the question board (review
// 2026-10-14 §4.2). An external agent's memos carried 212 open questions and the board held none,
// because only LoopLab's own Researcher turns memo questions into cards. The server projects them
// (`serve/memo_questions.py`, a derived read model: no recorded run's board moves); this banner
// SAYS so and offers the one explicit action that changes the log — filing the question, the same
// `hypothesis_added` command the agent should have sent, with the memo's own concepts.
import React, { useState } from 'react'

import { memoQuestionCommand, memoQuestionSummary } from './memoQuestionsModel.js'
import './memo-questions.css'

export default function MemoQuestionsBanner({ projection, onFile = null, locked = false }) {
  useUILanguage()
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(() => new Set())
  const summary = memoQuestionSummary(projection)
  if (!summary.missing) return null
  const file = async (rows) => {
    if (!onFile || locked) return
    setBusy(prev => new Set([...prev, ...rows.map(row => row.id)]))
    try {
      for (const row of rows) await onFile(memoQuestionCommand(row))
    } finally {
      setBusy(prev => {
        const next = new Set(prev)
        for (const row of rows) next.delete(row.id)
        return next
      })
    }
  }
  const disabled = !onFile || locked
  return <section className="memo-questions-banner" role="status"
    aria-label={uiText("Research memo questions not on the board")}>
    <div className="memo-questions-head">
      <b>{uiPlural(summary.missing,
        '{0} open question from research memos is not on the question board',
        '{0} open questions from research memos are not on the question board')}</b>
      <span className="muted">{uiText("Only filed questions are on the board, and only a filed question can have experiments attached. Filing adds the question with the concepts its memo gave it; nothing else in the run changes.")}</span>
      <div className="memo-questions-actions">
        <button type="button" className="btn sm" aria-expanded={open}
          onClick={() => setOpen(value => !value)}>
          {open ? uiText("Hide questions") : uiText("Show questions")}</button>
        {onFile && <button type="button" className="btn sm" disabled={disabled || busy.size > 0}
          onClick={() => file(summary.rows)}>
          {uiPlural(summary.rows.length, 'File {0} question on the board', 'File {0} questions on the board')}</button>}
      </div>
    </div>
    {open && <ol className="memo-questions-list">
      {summary.rows.map(row => <li key={row.id}>
        <span className="memo-question-statement">{row.statement}</span>
        {row.concepts.map(tag => <span key={tag} className="chip chip-concept">{tag}</span>)}
        <span className="muted memo-question-meta">
          {uiMessage("asked in {0} memo(s), {1} time(s)", [row.memos || 1, row.timesAsked])}
          {row.parentStatement && <> · {uiMessage("narrows: {0}", [row.parentStatement])}</>}
        </span>
        {onFile && <button type="button" className="btn sm ghost"
          disabled={disabled || busy.has(row.id)} onClick={() => file([row])}>
          {busy.has(row.id) ? uiText("Filing…") : uiText("File on the board")}</button>}
      </li>)}
    </ol>}
    {open && summary.truncated && <p className="muted">
      {uiMessage("Only the {0} most recently asked are listed.", [summary.rows.length])}</p>}
  </section>
}
