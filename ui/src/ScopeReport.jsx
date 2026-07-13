import React, { useEffect, useRef, useState } from 'react'
import { getScopeReport, genScopeReport, fmt, fmtAgo } from './util.js'
import { OpIcon } from './icons.jsx'
import { useDialogFocus } from './useDialogFocus.js'

function Section({ title, items }) {
  if (!items || !items.length) return null
  return <div className="sr-sec">
    <div className="sr-h">{title}</div>
    <ul className="sr-list">{items.map((x, i) => <li key={i}>{typeof x === 'string' ? x : JSON.stringify(x)}</li>)}</ul>
  </div>
}

// Cross-run aggregate report for a scope (project folder | task | super-task). On open it fetches the
// stored report (if any); you can Generate when there's none, or Regenerate when it's gone stale. The
// report is authored by an agent with access to every run in the scope.
export default function ScopeReport({ scope, onOpen, onClose }) {
  const dialogRef = useRef(null)
  const [data, setData] = useState(null)        // GET/POST response: {exists, content, generated_at, …}
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)

  useEffect(() => {
    setData(null); setErr(null)
    getScopeReport(scope.type, scope.id).then(setData).catch(e => setErr(e.message))
  }, [scope.type, scope.id])

  const generate = async () => {
    setBusy(true); setErr(null)
    try { setData({ ...(await genScopeReport(scope.type, scope.id)), exists: true }) }
    catch (e) { setErr(/400/.test(e.message) ? 'No runs in this scope yet.' : 'Generation failed: ' + e.message) }
    finally { setBusy(false) }
  }

  const c = data?.content
  const label = data?.label || data?.scope?.label || scope.label || `${scope.type} ${scope.id}`
  const added = data?.added || []
  useDialogFocus(dialogRef, onClose)

  return <div className="overlay" onMouseDown={event => { if (event.target === event.currentTarget) onClose?.() }}>
    <div ref={dialogRef} className="panel sr-panel" role="dialog" aria-modal="true"
      aria-label={`Cross-run report for ${label}`} tabIndex={-1}>
      <div className="panel-h">
        <span className="ttl"><OpIcon name="doc" className="t-ic" /> Cross-run report</span>
        <span className="pill">{label}</span>
        <span className="right" style={{ flex: 1 }} />
        {data?.exists && <button className="btn sm" disabled={busy} onClick={generate}>{busy ? '… generating' : '↻ Regenerate'}</button>}
        <button className="btn sm ghost" onClick={onClose} aria-label="Close cross-run report">✕</button>
      </div>
      <div className="panel-b">
        {err && <div className="notice" role="alert" style={{ borderColor: 'var(--fail)', color: 'var(--fg)' }}>{err}</div>}
        {data == null && !err && <div className="notice" role="status">Loading…</div>}

        {data && !data.exists && <div className="sr-empty">
          <div className="muted" style={{ marginBottom: 12 }}>
            No report yet for this {scope.type} — <b>{data.run_count || 0}</b> run(s) in scope.
            Generate one and an agent reads every run (their reports, configs, metrics) and synthesizes.
          </div>
          <button className="btn primary" disabled={busy || !data.run_count} onClick={generate}>
            {busy ? '… generating' : '✦ Generate report'}</button>
          {!data.run_count && <div className="sr-help">Add runs to this {scope.type} first.</div>}
        </div>}

        {data?.exists && c && <div className="sr-body">
          <div className="sr-meta">
            {data.generated_at && <span>generated {fmtAgo(data.generated_at / 1000)}</span>}
            <span>· over {data.run_ids?.length ?? '?'} runs</span>
            {data.model && <span>· {data.model}</span>}
            {data.stale && <span className="sr-stale">· ⟳ stale: {added.length ? `${added.length} new run(s)` : 'a run changed'} — regenerate</span>}
          </div>
          {c.headline && <div className="sr-headline">{c.headline}</div>}
          {c.verdict && <div className="sr-verdict">{c.verdict}</div>}
          {c.best_runs?.length > 0 && <div className="sr-sec">
            <div className="sr-h">Best runs</div>
            <div className="sr-bests">{c.best_runs.map((b, i) => <button type="button" key={i}
              className="sr-best" disabled={!b.run_id}
              title={b.run_id ? 'open ' + b.run_id : ''} onClick={() => onOpen?.(b.run_id)}>
              <b className="sr-m">{fmt(b.metric)}</b>
              <span className="sr-rid">{b.run_id}</span>
              {b.why && <span className="muted"> · {b.why}</span>}
            </button>)}</div>
          </div>}
          <Section title="What worked" items={c.what_worked} />
          <Section title="What didn’t" items={c.what_didnt} />
          <Section title="Learnings" items={c.learnings} />
          <Section title="Next directions" items={c.next_directions} />
          <Section title="Caveats" items={c.caveats} />
        </div>}
      </div>
    </div>
  </div>
}
