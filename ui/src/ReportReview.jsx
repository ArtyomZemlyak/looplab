import { uiMessage } from './uiLanguage.js'
import React, { useEffect, useRef, useState } from 'react'
import { uiText, useUILanguage } from './uiLanguage.js'
import { clearReportReviewIntent, getReportReview, loadReportReviewIntent,
  saveReportReviewIntent, submitReportReview } from './api.js'

export default function ReportReview({ runId, generation, readOnly = false, modelUnavailable = '' }) {
  useUILanguage()
  const [view, setView] = useState(null)
  const [intent, setIntent] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [storageError, setStorageError] = useState(false)
  const controller = useRef(null)
  const scope = `${runId}:${generation}:${readOnly}`
  const currentScope = useRef(scope)
  currentScope.current = scope
  useEffect(() => {
    setView(null); setError(''); setIntent(null); setStorageError(false); setBusy(false)
    if (!readOnly) {
      try { setIntent(loadReportReviewIntent(runId, generation)) }
      catch { setStorageError(true) }
    }
    return () => controller.current?.abort()
  }, [runId, generation, readOnly])

  const read = async () => {
    setBusy(true); setError('')
    const active = new AbortController(); controller.current = active
    try {
      const result = await getReportReview(runId, { signal: active.signal })
      if (result?.generation !== generation || !result?.input?.input_digest) throw new Error()
      if (currentScope.current === scope && !active.signal.aborted) setView(result)
    } catch { if (currentScope.current === scope && !active.signal.aborted) setError(uiText('Review evidence is unavailable. Reload the current run.')) }
    finally { if (currentScope.current === scope && !active.signal.aborted) setBusy(false) }
  }

  const review = async () => {
    setBusy(true); setError('')
    const active = new AbortController(); controller.current = active
    try {
      const saved = saveReportReviewIntent(runId, generation, view?.input)
      setIntent(saved)
      const result = await submitReportReview(runId, generation, { signal: active.signal })
      clearReportReviewIntent(runId, generation, saved.key)
      if (currentScope.current !== scope || active.signal.aborted) return
      setIntent(null)
      if (!result.ok) { setError(uiText('The optional review failed. The original report is unchanged.')); return }
      setView({ latest: { seq: result.seq, content: result.content, is_current: false }, input: null })
      try {
        const refreshed = await getReportReview(runId, { signal: active.signal })
        if (currentScope.current === scope && !active.signal.aborted) setView(refreshed)
      }
      catch { if (currentScope.current === scope && !active.signal.aborted) setError(uiText('Review saved. Refresh its evidence before nominating.')) }
    } catch { if (currentScope.current === scope && !active.signal.aborted) setError(uiText('Review outcome is unknown. Resume the saved request; do not start another paid request.')) }
    finally { if (currentScope.current === scope && !active.signal.aborted) setBusy(false) }
  }

  if (readOnly) return null
  const latest = view?.latest
  const receipt = latest?.content
  const ask = comment => {
    const text = `Present the separate operator approval card for nominate_report_review with run_id=${JSON.stringify(runId)}, review_seq=${latest.seq}, nomination_id=${JSON.stringify(comment.nomination_id)}. Read the original review/current native evidence first. This records only a research nomination and must not launch an experiment, repeat, ablation or grant.`
    window.dispatchEvent(new CustomEvent('ll:focus-assistant', { detail: { text } }))
  }
  return <section className="report-section">
    <h3>{uiText('Independent report review')}</h3>
    <p>{uiText('Optional separate model review of the published report and recorded outcomes. It does not run experiments or replace the report.')}</p>
    <div className="toolbar">
      <button type="button" className="btn sm" disabled={busy} onClick={read}>{uiText('Read report review')}</button>
      {(view || intent) && <button type="button" className="btn sm" onClick={review}
        disabled={busy || storageError || (!intent && (!view?.input || !!modelUnavailable || view?.unresolved || view?.conflicted))}>
        {uiText(intent ? 'Resume saved review request' : 'Review with model · optional · paid')}</button>}
    </div>
    {storageError && <p role="alert">{uiText('Paid review requires working session storage. Existing requests are preserved.')}</p>}
    {!!modelUnavailable && !intent && <p>{modelUnavailable}</p>}
    {intent && <p role="status">{uiText('A paid review request is saved. Resume rechecks that same request.')}</p>}
    {busy && <p role="status">{uiText('Reading or reviewing the report…')}</p>}
    {error && <p role="alert">{uiText(error)}</p>}
    {latest?.unavailable && <p role="alert">{uiText('Saved review receipt is unavailable. It cannot support a nomination.')}</p>}
    {receipt && <article>
      <p><strong>{uiText(latest.is_current ? 'Review of current evidence' : 'Historical review · evidence changed or freshness unverified')}</strong></p>
      <p>{receipt.summary}</p>
      <p>{uiText('Reviewer opinion, not a measured result or independent provider attestation.')}</p>
      {(receipt.comments || []).map((comment, index) => <div key={index}>
        <blockquote>{comment.quote}</blockquote><p>{comment.comment}</p>
        <ul>{(comment.outcomes || []).map(outcome => <li key={outcome.node_id}>
          {uiText('Recorded outcome')}: #{outcome.node_id} · {outcome.metric ?? uiText('unknown')} · {uiText('attempt')} {outcome.attempt}
        </li>)}</ul>
        {comment.nomination_id && <>
          <p>{comment.rationale}</p>
          <button type="button" className="btn sm" disabled={!latest.is_current || busy}
            onClick={() => ask(comment)}>{uiText('Discuss nomination with Assistant')}</button>
        </>}
      </div>)}
      <p>{uiText('The Assistant presents a separate approval card. Approval records research consideration only; scientific scope and execution gates still apply.')}</p>
    </article>}
  </section>
}
