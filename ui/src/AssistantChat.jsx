import React, { useEffect, useId, useRef } from 'react'
import Markdown from './markdown.jsx'
import { fmt } from './util.js'
import { assistantErrorInfo } from './assistantErrors.js'
import { permissionPresentation } from './assistantPermission.js'
import LaunchCard from './LaunchCard.jsx'
import { launchDraftKey } from './launchDraftStore.js'
import { OpIcon } from './icons.jsx'
import { toolActivityProjection } from './assistantToolActivity.js'
import './assistant-tool-activity.css'

const RUN_MENTION_MAX = 32
const runMentions = value => {
  const text = String(value || '')
  const re = /@run:([^\s.,;:!?)\]]{1,255})(?=$|[\s.,;:!?)\]])/g
  const seen = new Set()
  const mentions = []
  // Stop as soon as the chip budget is full. Besides bounding DOM, this avoids scanning the rest of
  // a provider-controlled answer after it can no longer affect the UI.
  let match
  while (mentions.length < RUN_MENTION_MAX && (match = re.exec(text)) !== null) {
    if (seen.has(match[1])) continue
    seen.add(match[1])
    mentions.push(match[1])
  }
  return mentions
}

// A live inline card for a run referenced with @run:<id> — so a running run shows up right in the chat
// (a direct ask). Links to the run view; the dot pulses while its engine is live.
function RunChip({ id, run, interactive = true, onOpen }) {
  const phase = run ? (run.phase || (run.finished ? 'finished' : 'running')) : '—'
  const href = `#/run/${encodeURIComponent(id)}`
  const content = <>
    <span className={'asst-run-dot' + (run && run.engine_running ? ' live' : '')} />
    <b>{id}</b>
    {run && <span className="muted"> {phase}{run.best_metric != null ? ' · ' + fmt(run.best_metric) : ''}</span>}
  </>
  return interactive
    ? <a className="asst-runchip" href={href}
        onClick={onOpen ? event => onOpen(event, href) : undefined}
        title={run ? (run.goal || id) : id}>{content}</a>
    : <span className="asst-runchip inert" title="Run references are not links in a public transcript">
        {content}
      </span>
}


// One assistant/user turn in the feed. Tool steps (what the agent read) render as a compact sub-line;
// any @run:<id> mention gets a live inline run card.
// Strip the invisible "[UI context: …]" preamble the bar appends to a user turn for the model, so it
// never shows in the bubble — including messages persisted before the server started storing the clean
// display text. The injected context is still surfaced separately as the faint caption plaque.
const stripCtx = (s) => typeof s === 'string'
  ? s.replace(/\n*\[UI context:[^\]]*\]\s*$/, '').trimEnd() : s

const exactRecoveryAvailable = action => !!action
  && typeof action.abs_path === 'string' && action.abs_path.length > 0
  && typeof action.recovery_id === 'string' && action.recovery_id.length > 0
  && typeof action.recovery_postimage_exists === 'boolean'
  && (action.recovery_postimage_exists
    ? typeof action.recovery_postimage_digest === 'string'
      && /^[0-9a-f]{64}$/.test(action.recovery_postimage_digest)
      && Number.isInteger(action.recovery_postimage_mode)
      && action.recovery_postimage_mode >= 0 && action.recovery_postimage_mode <= 0o7777
    : action.recovery_postimage_digest == null && action.recovery_postimage_mode == null)

function ToolActivity({ items, live = false }) {
  // The window, the truncation and the numbering live in `assistantToolActivity.js` — pure
  // decisions with a `node --test` sibling, because nothing in the suite mounts this component.
  const { steps, total, limited } = toolActivityProjection(items)
  if (steps.length === 0) return null
  const liveProps = live ? { 'aria-live': 'polite', 'aria-atomic': 'true' } : {}
  if (total <= 3 && !limited) {
    return <div className="asst-tool-line">
      <OpIcon name="gear" size={12} className="asst-tool-icon" />
      <span className="asst-tool-label" {...liveProps}>
        {steps.map(step => step.label).join(' · ')}</span>
    </div>
  }
  const summary = `${total} tool steps · ${steps[steps.length - 1].label}`
  return <details className="asst-tool-disclosure">
    <summary className="asst-tool-toggle">
      <OpIcon name="gear" size={12} className="asst-tool-icon" />
      <span className="asst-tool-label" {...liveProps}>{summary}</span>
      <OpIcon name="chevron-down" size={13} className="asst-tool-chevron" />
    </summary>
    <div className="asst-tool-body">
      {limited && <p className="asst-tool-limit">Showing the latest {steps.length} of {total} steps.</p>}
      {/* An explicit `value` per item, never a computed `start`: an unlabelled payload inside the
          window is skipped but still counted in `total`, so `total - shown + 1` was the right first
          ordinal only when every windowed item happened to carry a label. */}
      <ol className="asst-tool-list">
        {steps.map(step => <li key={`${step.position}:${step.label}`} value={step.position}>
          {step.label}</li>)}
      </ol>
    </div>
  </details>
}

function AssistantErrorCard({ error, onRetry, retryLabel = 'Retry', retryBusy = false, onOpenSettings }) {
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
      {error.retryable && onRetry && <button className="btn xs primary"
        aria-disabled={retryBusy || undefined}
        onClick={event => { if (!retryBusy) onRetry(event) }}>{retryLabel}</button>}
      {onOpenSettings && <button className="btn xs ghost" onClick={onOpenSettings}>Open Settings</button>}
    </div>
  </div>
}
export function Turn({
  m, runsById, onRevert, onRetry, retryLabel, retryBusy, onOpenSettings, onRunOpen, launchChat,
  readOnly = false,
  audience = 'owner',
  launchSessionId, launchMessageId, launchMessageIndex, launchDrafts, launchDisclosures,
  onLaunchDraft, onLaunchDisclosure, onLaunchStarted,
  revertState,
}) {
  const publicAudience = audience === 'public'
  const who = m.role === 'user' ? (publicAudience ? 'chat owner' : 'you') : 'assistant'
  const content = m.role === 'user' ? stripCtx(m.content) : m.content
  // Provider payloads can contain URLs, model routing, account ids and other implementation details.
  // Classify the raw persisted text, but render only the fixed, allow-listed copy returned here.
  const assistantError = m.role === 'assistant' && !m.streaming ? assistantErrorInfo(content, m.error_kind) : null
  const mentions = assistantError ? [] : runMentions(content)
  const hasLaunch = !readOnly && Array.isArray(m.proposals) && m.proposals.length > 0
  return <div className={'feed-msg chat ' + m.role + (hasLaunch ? ' has-launch' : '')
    + (publicAudience ? ' audience-public' : '')}>
    <div className="fm-body">
      <div className="chat-who">{who}</div>
      {/* Live, interleaved activity: prose the agent writes between tool rounds renders as its own
          line; longer tool groups use the same bounded disclosure as persisted legacy steps. */}
      {m.role === 'assistant' && Array.isArray(m.activity) && m.activity.length > 0 &&
        <div className="asst-activity">{m.activity.map((seg, i) => seg.type === 'text'
          ? <Markdown key={i} text={seg.content} className="asst-inter" externalOnly={publicAudience} />
          : seg.type === 'tools' ? <ToolActivity key={i} items={seg.labels} live={m.streaming} />
            : null)}</div>}
      {/* Persisted legacy steps share the live activity presentation when no timeline is available. */}
      {m.role === 'assistant' && !(m.activity && m.activity.length) && Array.isArray(m.steps) && m.steps.length > 0 &&
        <div className="asst-tool-legacy"><ToolActivity items={m.steps} /></div>}
      {m.role === 'assistant' && m.streaming && !m.content && !(m.activity && m.activity.length) &&
        <div className="asst-status thinking"><span className="asst-status-ic">…</span><span> thinking</span></div>}
      {m.role === 'assistant' && Array.isArray(m.applied) && m.applied.length > 0 &&
        <div className="asst-steps">{m.applied.map((a, i) => {
          const recovery = revertState?.(a) || {}
          const canRevert = onRevert && exactRecoveryAvailable(a)
          return <span key={i} className="asst-step done">✓ {a.label || a.tool}
            {canRevert && <button className="asst-undo" title={recovery.done
              ? 'this exact file change was reverted'
              : recovery.busy ? 'reverting this exact file change' : 'undo this exact file change'}
              aria-label={`${recovery.done ? 'Reverted' : recovery.busy ? 'Reverting' : 'Undo'} ${a.label || a.tool || 'file change'}`}
              disabled={recovery.busy || recovery.done}
              onClick={() => onRevert(a)}>{recovery.done ? 'reverted'
                : recovery.busy ? 'reverting…' : 'undo'}</button>}</span>
        })}</div>}
      {m.role === 'assistant' && <Todos items={m.todos} />}
      {(m.content || !m.streaming) && <div
        className={'chat-bubble' + (assistantError ? ' assistant-error-bubble' : '')
          + (m.recoveryBlocked ? ' assistant-recovery-blocked' : '')}
        role={m.recoveryBlocked ? 'alert' : undefined}>
        {m.role === 'assistant'
          ? assistantError
            ? <AssistantErrorCard error={assistantError} onRetry={onRetry} retryLabel={retryLabel}
                retryBusy={retryBusy}
                onOpenSettings={onOpenSettings} />
            : <><Markdown text={m.content || ''} className="chat-text" externalOnly={publicAudience} />{m.streaming && <span className="asst-cursor">▍</span>}</>
          : <div className="chat-text">{content}</div>}
      </div>}
      {mentions.length > 0 && <div className="asst-runchips">
        {mentions.map(id => <RunChip key={id} id={id} run={runsById && runsById[id]}
          interactive={!publicAudience} onOpen={onRunOpen} />)}
      </div>}
      {!readOnly && Array.isArray(m.proposals) && m.proposals.map((sp, i) => {
        const draftKey = launchDraftKey({ sessionId: launchSessionId, messageId: launchMessageId,
          messageIndex: launchMessageIndex, proposalId: sp.proposal_id, proposalIndex: i })
        return <LaunchCard key={draftKey} spec={sp} chat={launchChat} launchIdentity={draftKey}
          retainedDraft={launchDrafts?.[draftKey]}
          retainedConfigOpen={launchDisclosures?.[draftKey] === true}
          onOpenSettings={onOpenSettings}
          onDraftChange={draft => onLaunchDraft?.(draftKey, draft)}
          onConfigOpenChange={open => onLaunchDisclosure?.(draftKey, open)}
          onStarted={() => onLaunchStarted?.(draftKey)} />
      })}
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
export function PermCard({
  req, onResolve, busy = false, autoFocus = false, suppressAutoFocus = false,
  focusRequest = 0, onFocused = null, onFocusFailed = null,
}) {
  const titleId = useId()
  const detailsId = useId()
  const cardRef = useRef(null)
  const rejectRef = useRef(null)
  const skipSuppressionReleaseRef = useRef(false)
  const permission = permissionPresentation(req)
  const a = permission.action
  const isDiff = a.tool === 'write_file' || a.tool === 'edit_file' || a.tool === 'apply_patch'
  const requiresCaution = permission.risk === 'HIGH' || permission.risk === 'UNKNOWN'
  useEffect(() => {
    if (focusRequest) {
      skipSuppressionReleaseRef.current = true
      let cancelled = false
      let retryFrame = null
      const focusExact = remaining => {
        if (cancelled) return
        const card = cardRef.current
        const target = busy ? card : rejectRef.current
        target?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' })
        const rect = target?.getBoundingClientRect()
        const feedRect = card?.closest('[role="log"]')?.getBoundingClientRect()
        const visible = !!rect && !!feedRect && rect.bottom > feedRect.top
          && rect.top < feedRect.bottom && rect.right > feedRect.left && rect.left < feedRect.right
        if (visible) target?.focus({ preventScroll: true })
        if (visible && document.activeElement === target) {
          onFocused?.(req.id, focusRequest)
          return
        }
        if (remaining > 0) {
          retryFrame = requestAnimationFrame(() => focusExact(remaining - 1))
          return
        }
        onFocusFailed?.(req.id, focusRequest)
      }
      focusExact(2)
      return () => {
        cancelled = true
        if (retryFrame != null) cancelAnimationFrame(retryFrame)
      }
    }
    if (suppressAutoFocus) {
      // Existing cards must not steal focus when a failed exact handoff releases its suppression.
      // Skip only that release render; later busy -> ready and newly-last transitions stay usable.
      skipSuppressionReleaseRef.current = true
      return
    }
    if (skipSuppressionReleaseRef.current) {
      skipSuppressionReleaseRef.current = false
      return
    }
    if (!autoFocus || busy) return
    rejectRef.current?.focus({ preventScroll: true })
  }, [autoFocus, busy, focusRequest, onFocusFailed, onFocused, req.id, suppressAutoFocus])
  return <div ref={cardRef} className={'asst-perm risk-' + permission.risk.toLowerCase()}
    data-assistant-permission-id={req.id}
    tabIndex={-1}
    role="alertdialog" aria-modal="false" aria-labelledby={titleId} aria-describedby={detailsId}
    aria-busy={busy ? 'true' : 'false'}>
    <div className="asst-perm-h" id={titleId}>
      <span className="asst-perm-badge">approval required</span>
      <b>{a.label || a.tool || 'Unknown mutation'}</b>
      <span className={'asst-perm-risk ' + permission.risk.toLowerCase()}>
        {permission.risk} risk</span>
    </div>
    <dl className="asst-perm-details" id={detailsId}>
      <div><dt>Scope</dt><dd>{permission.scope}{permission.scopeDigest
        ? <span className="asst-perm-digest" title={permission.scopeDigest}>
            {' · ID '}{permission.scopeDigest.slice(0, 12)}…</span> : null}</dd></div>
      <div><dt>Consequence</dt><dd>{permission.consequence}</dd></div>
      <div><dt>Mode</dt><dd>{permission.modeLabel}</dd></div>
      <div><dt>Expiry</dt><dd>{permission.expiresIso
        ? <time dateTime={permission.expiresIso}>{permission.expiryLabel}</time>
        : permission.expiryLabel}</dd></div>
      {permission.canAlways && <div><dt>Remembered grant</dt><dd>
        This exact action and scope · current turn · {permission.modeLabel} mode · {permission.grantDurationLabel}
      </dd></div>}
    </dl>
    {a.preview && <pre className={'asst-perm-pre' + (isDiff ? ' diff' : '')}
      aria-label="Proposed action preview">{a.preview}</pre>}
    {permission.expired && <div className="asst-perm-state" role="status">
      This approval has expired; waiting for the server to close it.</div>}
    {requiresCaution &&
      <div className="asst-perm-state warning">
        Persistent approval is unavailable for {permission.risk === 'HIGH'
          ? 'high-risk actions' : 'actions without a verified risk classification'}.</div>}
    {busy && <div className="asst-perm-state" role="status">Submitting your decision…</div>}
    <div className="asst-perm-actions">
      <button ref={rejectRef} data-permission-reject
        className={'btn xs ' + (requiresCaution ? 'primary' : 'ghost')} disabled={busy}
        onClick={() => onResolve(req.id, 'deny')}>Reject</button>
      {permission.canAlways && <button className="btn xs" disabled={busy || permission.expired}
        onClick={() => onResolve(req.id, 'allow_always')}
        title={`Remember only this exact action and security scope for the current turn and ${permission.modeLabel} mode (${permission.grantDurationLabel})`}>
        Allow exact scope · {permission.grantDurationLabel}</button>}
      <button className={'btn xs ' + (requiresCaution ? 'danger' : 'primary')}
        disabled={busy || permission.expired}
        onClick={() => onResolve(req.id, 'allow_once')}>Approve once</button>
    </div>
  </div>
}

export { LaunchCard }
