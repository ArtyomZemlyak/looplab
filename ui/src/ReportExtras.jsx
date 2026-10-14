import { uiText, uiMessage, uiPlural, useUILanguage } from './uiLanguage.js'
// The Report's figures and its version history (review 2026-10-14 section 6, section 6.3).
//
// Figures are images the publisher ATTACHED from the run's artifacts: the server copied each one
// through the `/artifact/raw` fences into a content-addressed store and serves it with nosniff and a
// sandboxing CSP; the page fetches the bytes (an <img src> cannot carry the owner token) and shows
// an object URL. Versions are every `report_generated` row, read through the fold's own sanitizer;
// "Compare" diffs two recorded versions field by field (`reportHistoryModel.js::reportDiff`).
import React, { useEffect, useState } from 'react'

import { get, getBlob, runApiPath } from './api.js'
import Markdown from './markdown.jsx'
import { normalizeReportFigures, normalizeReportVersions, reportDiff } from './reportHistoryModel.js'
import './report-extras.css'

function Figure({ runId, figure }) {
  useUILanguage()
  const [url, setUrl] = useState(null), [failed, setFailed] = useState(false)
  useEffect(() => {
    let alive = true, objectUrl = null
    const controller = new AbortController()
    getBlob(runApiPath(runId, `/report-figures/${figure.figureId}`), { signal: controller.signal, cache: 'no-store' })
      .then(blob => { if (alive) { objectUrl = URL.createObjectURL(blob); setUrl(objectUrl) } })
      .catch(() => { if (alive) setFailed(true) })
    return () => { alive = false; controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl) }
  }, [runId, figure.figureId])
  const source = figure.source
    ? `${figure.source.nodeId != null ? `#${figure.source.nodeId} · ` : ''}${figure.source.path}` : ''
  return <figure className="report-figure">
    {failed ? <div className="notice">{uiText('This figure is no longer available.')}</div>
      : url ? <img src={url} alt={figure.caption || uiText('Report figure')} />
        : <div className="muted">{uiText('Loading…')}</div>}
    {(figure.caption || source) && <figcaption>{figure.caption}
      {source && <span className="muted"> · {uiMessage('from {0}', [source])}</span>}</figcaption>}
  </figure>
}

export function ReportFigures({ runId, figures, available = true }) {
  useUILanguage()
  const rows = normalizeReportFigures(figures)
  if (!rows.length) return null
  if (!available || typeof runId !== 'string' || !runId) {
    return <p className="muted">{uiPlural(rows.length, '{0} attached figure is not shown in this view.',
      '{0} attached figures are not shown in this view.')}</p>
  }
  return <div className="report-figures">{rows.map(figure => <Figure key={figure.figureId}
    runId={runId} figure={figure} />)}</div>
}

const FIELD_LABELS = {
  headline: 'Headline', verdict: 'Verdict', summary: 'Summary', champion_summary: 'Champion note',
  caveats: 'Caveats', what_worked: 'What worked', what_didnt: "What didn't work",
  learnings: 'Learnings', next_directions: 'Next directions', body_markdown: 'Report body',
  figures: 'Figures', key_metrics: 'Key metrics',
}
const STATUS_LABELS = { same: 'unchanged', changed: 'changed', added: 'added', removed: 'removed' }

function DiffView({ diff }) {
  useUILanguage()
  if (!diff.changed) return <p className="muted">{uiText('The two versions say the same thing.')}</p>
  return <dl className="report-diff">{diff.fields.map(row => <React.Fragment key={row.field}>
    <dt>{uiText(FIELD_LABELS[row.field] || row.field)} · <span className={`report-diff-${row.status}`}>{uiText(STATUS_LABELS[row.status] || row.status)}</span></dt>
    <dd>
      {row.kind === 'text' && row.status !== 'same' && <>
        {row.before && <div className="report-diff-del"><Markdown text={row.before} externalOnly /></div>}
        {row.after && <div className="report-diff-add"><Markdown text={row.after} externalOnly /></div>}
      </>}
      {row.kind === 'list' && row.status !== 'same' && <ul className="bul">
        {row.removed.map(item => <li key={`-${item}`} className="report-diff-del">{item}</li>)}
        {row.added.map(item => <li key={`+${item}`} className="report-diff-add">{item}</li>)}
      </ul>}
      {row.kind === 'lines' && row.status !== 'same' && (row.ops
        ? <pre className="report-diff-lines">{row.ops.filter(op => op.op !== 'same').map((op, i) =>
          <div key={i} className={op.op === 'add' ? 'report-diff-add' : 'report-diff-del'}>{op.op === 'add' ? '+ ' : '− '}{op.text}</div>)}</pre>
        : <span className="muted">{uiText('Too long to compare line by line.')}</span>)}
      {row.kind === 'count' && row.status !== 'same' && <span>{uiMessage('{0} added, {1} removed', [row.added, row.removed])}</span>}
    </dd>
  </React.Fragment>)}</dl>
}

const _when = ts => (ts ? new Date(ts * 1000).toLocaleString() : '')
const PUBLISHER_LABELS = { external_agent: 'external agent', looplab_model: 'LoopLab model', operator: 'operator' }

export function ReportVersions({ runId, available = true }) {
  useUILanguage()
  const [open, setOpen] = useState(false)
  const [list, setList] = useState(null), [unreadable, setUnreadable] = useState(false)
  const [pick, setPick] = useState([])
  const [diff, setDiff] = useState(null), [diffState, setDiffState] = useState('idle')
  useEffect(() => {
    if (!open || !available || typeof runId !== 'string' || !runId) return undefined
    let alive = true
    const controller = new AbortController()
    get(runApiPath(runId, '/report/versions'), { signal: controller.signal, cache: 'no-store' })
      .then(value => { if (alive) setList(normalizeReportVersions(value)) })
      .catch(() => { if (alive) setUnreadable(true) })
    return () => { alive = false; controller.abort() }
  }, [open, runId, available])
  useEffect(() => {
    if (pick.length !== 2) { setDiff(null); setDiffState('idle'); return undefined }
    let alive = true
    const controller = new AbortController()
    const [older, newer] = [...pick].sort((x, y) => x - y)
    setDiffState('loading')
    Promise.all([older, newer].map(seq => get(runApiPath(runId, `/report/versions/${seq}`),
      { signal: controller.signal, cache: 'no-store' })))
      .then(([a, b]) => { if (alive) { setDiff(reportDiff(a?.content, b?.content)); setDiffState('ready') } })
      .catch(() => { if (alive) setDiffState('error') })
    return () => { alive = false; controller.abort() }
  }, [pick, runId])
  if (!available) return null
  const toggle = seq => setPick(prev => prev.includes(seq) ? prev.filter(s => s !== seq)
    : [...prev, seq].slice(-2))
  return <details className="report-versions" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>{uiText('Version history')}{list ? ` (${list.total})` : ''}</summary>
    {unreadable && <p className="muted" role="status">{uiText('The report history could not be read.')}</p>}
    {!list && !unreadable && open && <p className="muted">{uiText('Loading…')}</p>}
    {list && <>
      <p className="muted">{uiText('Every published version of this report, newest first. Pick two to compare them.')}
        {list.distinct < list.total && <> {uiPlural(list.total - list.distinct,
          '{0} version repeats an earlier one word for word.', '{0} versions repeat an earlier one word for word.')}</>}</p>
      <ol className="report-version-list">{list.versions.map(version => <li key={version.seq}
        className={version.current ? 'current' : ''}>
        <label>
          <input type="checkbox" checked={pick.includes(version.seq)} onChange={() => toggle(version.seq)}
            aria-label={uiMessage('Compare version #{0}', [version.seq])} />
          <b>#{version.seq}</b>
          {version.current && <span className="pill">{uiText('current')}</span>}
          {version.duplicateOf != null && <span className="pill warn">{uiMessage('same as #{0}', [version.duplicateOf])}</span>}
          <span className="muted"> {_when(version.ts)}{version.atNode != null ? ` · ${uiMessage('{0} experiments', [version.atNode])}` : ''}
            {PUBLISHER_LABELS[version.publisher] ? ` · ${uiText(PUBLISHER_LABELS[version.publisher])}` : ''}{version.authorClaim ? ` · ${version.authorClaim}` : ''}</span>
          <span className="report-version-headline">{version.headline}</span>
        </label>
      </li>)}</ol>
      {list.truncated && <p className="muted">{uiText('Only the most recent versions are listed.')}</p>}
      {pick.length === 2 && diffState === 'loading' && <p className="muted">{uiText('Loading…')}</p>}
      {pick.length === 2 && diffState === 'error' && <p className="muted">{uiText('The two versions could not be read.')}</p>}
      {pick.length === 2 && diff && <DiffView diff={diff} />}
    </>}
  </details>
}
