import React from 'react'
import { OpIcon } from './icons.jsx'

export function PanelResourceNotice({ resource, label, onRetry }) {
  if (resource.status === 'ready') return null
  if (resource.status === 'loading') return <div className="muted" role="status">Loading {label}…</div>
  const stale = resource.status === 'stale'
  return <div className={'report-inline-state' + (stale ? '' : ' error')} role={stale ? 'status' : 'alert'}>
    <OpIcon name="alert" size={14} />
    <span>{label}: {resource.pending
      ? `${resource.pending === 'retry' ? 'Retrying' : 'Refreshing'}…${stale ? ' Last loaded data remains visible.' : ''}`
      : stale ? 'Last loaded data; refresh failed.' : 'Unavailable.'}</span>
    <button className="btn sm" disabled={!!resource.pending} onClick={onRetry}>
      {resource.pending === 'retry' ? 'Retrying…' : resource.pending ? 'Refreshing…' : 'Retry'}</button>
  </div>
}
