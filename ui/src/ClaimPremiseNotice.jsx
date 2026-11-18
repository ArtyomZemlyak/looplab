import React from 'react'
import { uiText, uiMessage } from './uiLanguage.js'

export default function ClaimPremiseNotice({ review, compact = false }) {
  if (!review || typeof review !== 'object' || !['current', 'stale', 'unavailable'].includes(review.state)
      || review.authority !== 'authored_interpretation' || review.verdict_changed !== false) return null
  const undercut = Array.isArray(review.undercut) ? review.undercut : []
  const statuses = Array.isArray(review.premise_status) ? review.premise_status : []
  const unknown = review.state !== 'current' || statuses.some(row => ['stale', 'unavailable'].includes(row?.state))
  return <aside role="note" className="card-control-feedback">
    <strong>{undercut.length ? uiText('Logical premise challenged') : unknown
      ? uiText('Premise review is stale or unavailable') : uiText('Authored premise review')}</strong>
    {!compact && <>
      <p>{uiText('This is an authored interpretation. Child measurements and research verdict stay unchanged; no approval or execution gate is added.')}</p>
      {statuses.map((row, index) => <div key={index}>
        <code>{typeof row?.premise_id === 'string' ? row.premise_id : ''}</code>
        {' · '}{uiMessage('Premise review state: {0}', [typeof row?.state === 'string' ? row.state : 'unavailable'])}
      </div>)}
      {undercut.map((row, index) => <p key={index}>
        <code>{typeof row?.premise_id === 'string' ? row.premise_id : ''}</code>
        {' · '}{typeof row?.reason === 'string' ? row.reason : ''}
        {' · '}{uiMessage('Native review event: {0}', [Number.isSafeInteger(row?.review_seq) ? row.review_seq : '?'])}
      </p>)}
    </>}
  </aside>
}
