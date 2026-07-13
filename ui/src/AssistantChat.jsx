import React, { useState } from 'react'
import Markdown from './markdown.jsx'
import { fmt, startRun } from './util.js'
import { assistantErrorInfo } from './assistantErrors.js'

const RUN_MENTION = /@run:([^\s.,;:!?)]+)/g
const runMentions = (s) => [...new Set([...(s || '').matchAll(RUN_MENTION)].map(m => m[1]))]

// A live inline card for a run referenced with @run:<id> — so a running run shows up right in the chat
// (a direct ask). Links to the run view; the dot pulses while its engine is live.
function RunChip({ id, run }) {
  const phase = run ? (run.phase || (run.finished ? 'finished' : 'running')) : '—'
  return <a className="asst-runchip" href={`#/run/${encodeURIComponent(id)}`}
    title={run ? (run.goal || id) : id}>
    <span className={'asst-run-dot' + (run && run.engine_running ? ' live' : '')} />
    <b>{id}</b>
    {run && <span className="muted"> {phase}{run.best_metric != null ? ' · ' + fmt(run.best_metric) : ''}</span>}
  </a>
}


// One assistant/user turn in the feed. Tool steps (what the agent read) render as a compact sub-line;
// any @run:<id> mention gets a live inline run card.
// Strip the invisible "[UI context: …]" preamble the bar appends to a user turn for the model, so it
// never shows in the bubble — including messages persisted before the server started storing the clean
// display text. The injected context is still surfaced separately as the faint caption plaque.
const stripCtx = (s) => typeof s === 'string'
  ? s.replace(/\n*\[UI context:[^\]]*\]\s*$/, '').trimEnd() : s

function AssistantErrorCard({ error, onRetry, onOpenSettings }) {
  return <div className={`assistant-error-card ${error.kind}`} role="alert">
    <div className="assistant-error-card__head">
      <span className="assistant-error-card__icon" aria-hidden="true">!</span>
      <div>
        <strong>{error.title}</strong>
        <p>{error.message}</p>
      </div>
    </div>
    {error.technical && <code className="assistant-error-card__technical">{error.technical}</code>}
    <div className="assistant-error-card__actions">
      {error.retryable && onRetry && <button className="btn xs primary" onClick={onRetry}>Retry</button>}
      {onOpenSettings && <button className="btn xs ghost" onClick={onOpenSettings}>Open Settings</button>}
    </div>
  </div>
}

export function Turn({ m, runsById, onRevert, onRetry, onOpenSettings, readOnly = false }) {
  const who = m.role === 'user' ? 'you' : 'assistant'
  const content = m.role === 'user' ? stripCtx(m.content) : m.content
  // Provider payloads can contain URLs, model routing, account ids and other implementation details.
  // Classify the raw persisted text, but render only the fixed, allow-listed copy returned here.
  const assistantError = m.role === 'assistant' && !m.streaming ? assistantErrorInfo(content, m.error_kind) : null
  const mentions = assistantError ? [] : runMentions(content)
  return <div className={'feed-msg chat ' + m.role}>
    <div className="fm-body">
      <div className="chat-who">{who}</div>
      {/* Live, interleaved activity (Claude-Desktop style): prose the agent writes BETWEEN tool rounds
          renders as its own line; a run of consecutive tool calls collapses into one status line. */}
      {m.role === 'assistant' && Array.isArray(m.activity) && m.activity.length > 0 &&
        <div className="asst-activity">{m.activity.map((seg, i) => seg.type === 'text'
          ? <Markdown key={i} text={seg.content} className="asst-inter" />
          : <div key={i} className="asst-status"><span className="asst-status-ic">⚙</span>
              {seg.labels.length > 3
                ? <span> {seg.labels.length} steps · {seg.labels[seg.labels.length - 1]}</span>
                : <span> {seg.labels.join(' · ')}</span>}</div>)}</div>}
      {/* Legacy compact step line — only when there's no richer activity timeline to show. */}
      {m.role === 'assistant' && !(m.activity && m.activity.length) && Array.isArray(m.steps) && m.steps.length > 0 &&
        <div className="asst-steps">{m.steps.map((s, i) =>
          <span key={i} className="asst-step">{s.label || s.tool}</span>)}</div>}
      {m.role === 'assistant' && m.streaming && !m.content && !(m.activity && m.activity.length) &&
        <div className="asst-status thinking"><span className="asst-status-ic">…</span><span> thinking</span></div>}
      {m.role === 'assistant' && Array.isArray(m.applied) && m.applied.length > 0 &&
        <div className="asst-steps">{m.applied.map((a, i) =>
          <span key={i} className="asst-step done">✓ {a.label || a.tool}
            {onRevert && a.abs_path && <button className="asst-undo" title="undo this change"
              onClick={() => onRevert(a.abs_path)}>undo</button>}</span>)}</div>}
      {m.role === 'assistant' && <Todos items={m.todos} />}
      {(m.content || !m.streaming) && <div
        className={'chat-bubble' + (assistantError ? ' assistant-error-bubble' : '')
          + (m.recoveryBlocked ? ' assistant-recovery-blocked' : '')}
        role={m.recoveryBlocked ? 'alert' : undefined}>
        {m.role === 'assistant'
          ? assistantError
            ? <AssistantErrorCard error={assistantError} onRetry={onRetry} onOpenSettings={onOpenSettings} />
            : <><Markdown text={m.content || ''} className="chat-text" />{m.streaming && <span className="asst-cursor">▍</span>}</>
          : <div className="chat-text">{content}</div>}
      </div>}
      {mentions.length > 0 && <div className="asst-runchips">
        {mentions.map(id => <RunChip key={id} id={id} run={runsById && runsById[id]} />)}
      </div>}
      {!readOnly && Array.isArray(m.proposals) && m.proposals.map((sp, i) => <LaunchCard key={i} spec={sp} />)}
    </div>
  </div>
}

// The assistant's live TODO list for a multi-step task.
function Todos({ items }) {
  if (!items || !items.length) return null
  const mark = (s) => s === 'completed' ? '✓' : (s === 'in_progress' ? '▸' : '○')
  return <div className="asst-todos">{items.map((t, i) =>
    <div key={i} className={'asst-todo ' + (t.status || 'pending')}>
      <span className="asst-todo-box">{mark(t.status)}</span><span>{t.content}</span>
    </div>)}</div>
}

// A human-in-the-loop confirm card: the turn paused to ask before a mutating action. Approve/Reject
// resolves it server-side and the turn resumes.
export function PermCard({ req, onResolve }) {
  const a = req.action || {}
  const isDiff = a.tool === 'write_file' || a.tool === 'edit_file' || a.tool === 'apply_patch'
  return <div className="asst-perm">
    <div className="asst-perm-h"><span className="asst-perm-badge">approve?</span>
      <b>{a.label || a.tool}</b>{a.cwd && <span className="muted"> · {a.cwd}</span>}</div>
    {a.preview && <pre className={'asst-perm-pre' + (isDiff ? ' diff' : '')}>{a.preview}</pre>}
    <div className="asst-perm-actions">
      <button className="btn xs ghost" onClick={() => onResolve(req.id, 'deny')}>Reject</button>
      <button className="btn xs" onClick={() => onResolve(req.id, 'allow_always')} title="allow this kind for the session">Always</button>
      <button className="btn xs primary" onClick={() => onResolve(req.id, 'allow_once')}>Approve</button>
    </div>
  </div>
}

// A launch card for a run the assistant proposed (propose_run). The user reviews and starts it via
// the existing /api/start; the New-run flow becomes one assistant capability.
function LaunchCard({ spec }) {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  const [done, setDone] = useState(false)
  const task = spec.task || {}
  const what = spec.task_file ? spec.task_file.split(/[\\/]/).pop() : (task.kind || 'task')
  const launch = async () => {
    setBusy(true); setErr(null)
    try {
      const body = { run_id: spec.run_id, settings: spec.settings || {} }
      if (spec.task_file) body.task_file = spec.task_file; else body.task = task
      await startRun(body)
      setDone(true); location.hash = `#/run/${encodeURIComponent(spec.run_id)}`
    } catch (e) { setErr(e.status === 409 ? `"${spec.run_id}" already exists — rename it` : e.message); setBusy(false) }
  }
  return <div className="asst-launch">
    <div className="asst-launch-h"><span className="asst-perm-badge">new run</span><b>{spec.run_id}</b>
      <span className="muted"> · {what}</span></div>
    {spec.rationale && <div className="muted" style={{ fontSize: 12, margin: '4px 0' }}>{spec.rationale}</div>}
    {spec.settings && Object.keys(spec.settings).length > 0 &&
      <div className="asst-steps">{Object.entries(spec.settings).map(([k, v]) =>
        <span key={k} className="asst-step">{k}={String(v)}</span>)}</div>}
    {err && <div className="notice" style={{ borderColor: 'var(--fail)', color: '#ffd3d3', marginTop: 6 }}>{err}</div>}
    <div className="asst-perm-actions">
      <button className="btn xs primary" disabled={busy || done} onClick={launch}>{done ? 'started ✓' : (busy ? '…' : '▶ Start run')}</button>
    </div>
  </div>
}
