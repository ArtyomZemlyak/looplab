import React, { useMemo, useRef, useState } from 'react'
import { createInspectorDraftStore, useInspectorDraftField } from './inspectorDraftStore.js'

function highlighted(text, query) {
  if (!query) return text || ' '
  const lower = text.toLowerCase(), needle = query.toLowerCase()
  const parts = []; let from = 0, index
  while ((index = lower.indexOf(needle, from)) >= 0) {
    if (index > from) parts.push(text.slice(from, index))
    parts.push(<mark key={`${index}:${parts.length}`}>{text.slice(index, index + query.length)}</mark>)
    from = index + query.length
  }
  if (from < text.length) parts.push(text.slice(from))
  return parts.length ? parts : (text || ' ')
}

export default function CodeViewer({
  code = '', diff = null, label = 'Code', maxHeight = 420, copyText = null,
  draftStore: sharedDraftStore = null, draftScope = null,
}) {
  const fallbackDraftStoreRef = useRef(null)
  if (!fallbackDraftStoreRef.current) fallbackDraftStoreRef.current = createInspectorDraftStore()
  const draftStore = sharedDraftStore || fallbackDraftStoreRef.current
  const scope = draftScope || `code-viewer:${label}`
  const [query, setQuery] = useInspectorDraftField(
    draftStore, scope, 'query', '', { disposable: true })
  const [wrap, setWrap] = useInspectorDraftField(
    draftStore, scope, 'wrap', false, { disposable: true })
  const [copied, setCopied] = useState(false)
  const rows = useMemo(() => diff || String(code || '').split('\n').map((line, index) => ({
    line, l: line, kind: 'same', cls: '', oldNo: null, newNo: index + 1,
  })), [code, diff])
  const matches = query ? rows.filter(row => String(row.line ?? row.l ?? '').toLowerCase().includes(query.toLowerCase())).length : 0
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(copyText ?? code)
      setCopied(true); setTimeout(() => setCopied(false), 1400)
    } catch { setCopied(false) }
  }
  return <div className={'code-viewer' + (wrap ? ' wrap' : '') + (diff ? ' has-diff' : '')} style={{ '--code-max-h': `${maxHeight}px` }}>
    <div className="code-tools">
      <label className="code-search"><span className="sr-only">Search {label}</span>
        <input value={query} onChange={event => setQuery(event.target.value)} placeholder={`Search ${label.toLowerCase()}…`} />
      </label>
      {query && <span className="muted">{matches} line{matches === 1 ? '' : 's'}</span>}
      <span className="spacer" />
      <button className={'btn sm ghost' + (wrap ? ' on' : '')} onClick={() => setWrap(value => !value)}
              aria-pressed={wrap}>Wrap</button>
      <button className="btn sm ghost" onClick={copy}>{copied ? 'Copied' : 'Copy'}</button>
    </div>
    <div className="code-lines" role="region" aria-label={label} tabIndex={0}>
      {rows.map((row, index) => <div key={index} className={'code-line ' + (row.cls || '')}>
        {diff && <span className="code-old-no">{row.oldNo ?? ''}</span>}
        <span className="code-new-no">{row.newNo ?? ''}</span>
        <span className="code-sign" aria-hidden="true">{row.kind === 'add' ? '+' : row.kind === 'del' ? '−' : ' '}</span>
        <code>{highlighted(String(row.line ?? row.l ?? ''), query)}</code>
      </div>)}
    </div>
  </div>
}
