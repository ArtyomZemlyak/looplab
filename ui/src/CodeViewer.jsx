import { uiText, useUILanguage } from './uiLanguage.js'
import React, { useEffect, useMemo, useRef, useState } from 'react'
import { createInspectorDraftStore, useInspectorDraftField } from './inspectorDraftStore.js'
import { codeSearchRows } from './codeSearch.js'

function highlighted(text, ranges) {
  if (!ranges.length) return text || ' '
  const parts = []; let from = 0
  for (const range of ranges) {
    if (range.from > from) parts.push(text.slice(from, range.from))
    parts.push(<mark key={range.from}>{text.slice(range.from, range.to)}</mark>)
    from = range.to
  }
  if (from < text.length) parts.push(text.slice(from))
  return parts.length ? parts : (text || ' ')
}

const sameCopySource = (left, right) => left?.scope === right.scope
  && left?.label === right.label && left?.text === right.text

export default function CodeViewer({
  code = '', diff = null, label = 'Code', maxHeight = 420, copyText = null,
  draftStore: sharedDraftStore = null, draftScope = null, language = 'en', allowCopy = true,
}) {
  useUILanguage()

  const ru = language === 'ru'
  const fallbackDraftStoreRef = useRef(null)
  if (!fallbackDraftStoreRef.current) fallbackDraftStoreRef.current = createInspectorDraftStore()
  const draftStore = sharedDraftStore || fallbackDraftStoreRef.current
  const scope = draftScope || `code-viewer:${label}`
  const [query, setQuery] = useInspectorDraftField(
    draftStore, scope, 'query', '', { disposable: true })
  const [wrap, setWrap] = useInspectorDraftField(
    draftStore, scope, 'wrap', false, { disposable: true })
  const [copied, setCopied] = useState(null)
  const copyTextValue = copyText ?? code
  const copySource = { scope, label: label, text: copyTextValue }
  const currentCopySource = useRef(copySource)
  currentCopySource.current = copySource
  const copySerial = useRef(0)
  const copyTimer = useRef(null)
  useEffect(() => {
    setCopied(null)
    return () => {
      // Clipboard writes cannot be cancelled. Only a current source/request may acknowledge one.
      copySerial.current += 1
      clearTimeout(copyTimer.current)
    }
  }, [scope, label, copyTextValue, allowCopy])
  const rows = useMemo(() => diff || String(code || '').split('\n').map((line, index) => ({
    line, l: line, kind: 'same', cls: '', oldNo: null, newNo: index + 1,
  })), [code, diff])
  const { rows: searchedRows, matches } = useMemo(() => codeSearchRows(rows, query), [rows, query])
  const copy = async () => {
    const ticket = ++copySerial.current
    const source = currentCopySource.current
    clearTimeout(copyTimer.current)
    setCopied(null)
    const current = () => ticket === copySerial.current
      && sameCopySource(source, currentCopySource.current)
    try {
      await navigator.clipboard.writeText(source.text)
      if (!current()) return
      setCopied(source)
      copyTimer.current = setTimeout(() => {
        if (current()) setCopied(null)
      }, 1400)
    } catch { if (current()) setCopied(null) }
  }
  return <div className={'code-viewer' + (wrap ? ' wrap' : '') + (diff ? ' has-diff' : '')} style={{ '--code-max-h': `${maxHeight}px` }}>
    <div className="code-tools">
      <label className="code-search"><span className="sr-only">{((ru ? 'Поиск' : uiText('Search')))} {uiText(label)}</span>
        <input value={query} onChange={event => setQuery(event.target.value)} placeholder={`${ru ? 'Поиск' : 'Search'} ${label.toLowerCase()}…`} />
      </label>
      {query && <span className="muted">{((ru ? 'Строк с совпадением:' : uiText('Matching lines:')))} {matches}</span>}
      <span className="spacer" />
      <button className={'btn sm ghost' + (wrap ? ' on' : '')} onClick={() => setWrap(value => !value)}
              aria-pressed={wrap}>{((ru ? 'Перенос строк' : uiText('Wrap')))}</button>
      {allowCopy && <button className="btn sm ghost" onClick={copy}>{((sameCopySource(copied, copySource) ? (ru ? 'Скопировано' : uiText('Copied')) : (ru ? 'Копировать' : uiText('Copy'))))}</button>}
    </div>
    <div className="code-lines" role="region" aria-label={uiText(label)} tabIndex={0}>
      {searchedRows.map(({ row, text, ranges }, index) => <div key={index} className={'code-line ' + (row.cls || '')}>
        {diff && <span className="code-old-no">{row.oldNo ?? ''}</span>}
        <span className="code-new-no">{row.newNo ?? ''}</span>
        <span className="code-sign" aria-hidden="true">{row.kind === 'add' ? '+' : row.kind === 'del' ? '−' : ' '}</span>
        <code>{highlighted(text, ranges)}</code>
      </div>)}
    </div>
  </div>
}
