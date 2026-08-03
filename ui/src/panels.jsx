import React, { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { deadlineGet, get, post, fmt, fmtInt, fmtBytes, fmtElapsedSeconds, CONTROL,
  saveRunConfig, operatorMeta, commandFeedback, runApiPath, runNodeApiPath,
  createIdempotencyKey, getRunCommand, isTransientCommandReadError, retryRunCommand, runCommand,
  getAuthoringOperation, putAuthoringOperation, validAuthoringName, validAuthoringTargetRootId,
  getRunArtifactContent, getRunArtifactInventory,
} from './util.js'
import { usePoll } from './hooks.js'
import { Bars, ParallelCoords, Scatter } from './charts.jsx'
import { hyperImportance } from './report.js'
import Markdown, { stripMd } from './markdown.jsx'
import { OpIcon } from './icons.jsx'
import CodeViewer from './CodeViewer.jsx'
import { diffLines } from './lineDiff.js'
import SettingsForm from './SettingsForm.jsx'
import { toForm, fromForm, settingsValidationErrors, loadSettingsSchema } from './settingsSchema.js'
import {
  reconcileAcceptedRecord, reconcileUnknownRecord, runConfigWriteDisposition, splitRunConfigPayload,
  validateRunConfigSaveAck,
} from './settingsModel.js'
import { driftStatus, leakageStatus, rewardHackStatus } from './trustSemantics.js'
import { metricComparable, sortRuns } from './runIndex.js'
import VirtualTimeline from './VirtualTimeline.jsx'
import { timelineEventKey } from './timelineModel.js'
import { queuedGenerationControls } from './queue.js'
import Panel from './PanelShell.jsx'
import { DataTable } from './accessibility.jsx'
import { safeExternalHref } from './urlSafety.js'
import { normalizeResearchMemos } from './researchMemoModel.js'
import { deadlineRequest } from './requestDeadline.js'
import { installNavigationLossGuard } from './navigationLossGuard.js'
import { cardControlSubmission, cardEditReflected } from './cardControlModel.js'
import { createInspectorDraftStore, useInspectorDraftField } from './inspectorDraftStore.js'
import {
  AUTHORING_OPERATION_STORAGE_PREFIX, authoringRecoveryStorageKey,
} from './authoringRecoveryStorage.js'

export { default as Panel } from './PanelShell.jsx'

const Stat = ({ n, l }) => <div className="stat"><div className="n">{n}</div><div className="l">{l}</div></div>

const MetricGauge = ({ value, max = 100, hot = false, label, valueText }) => {
  const numericValue = typeof value === 'number' && Number.isFinite(value) ? value : null
  const numericMax = typeof max === 'number' && Number.isFinite(max) && max > 0 ? max : null
  if (numericValue == null || numericMax == null) {
    return <span className="muted" aria-label={`${label} unavailable`}>unavailable</span>
  }
  const safeValue = Math.max(0, Math.min(numericMax, numericValue))
  return <div className="gauge">
    <div className="bar" role="progressbar" aria-label={label} aria-valuemin={0}
      aria-valuemax={numericMax} aria-valuenow={safeValue} aria-valuetext={valueText}>
      <div className={'fill' + (hot ? ' hot' : '')}
        style={{ width: `${safeValue / numericMax * 100}%` }} />
    </div>
  </div>
}

const invalidPanelPayload = () => { throw new Error('Invalid panel payload') }
const isRecord = value => !!value && typeof value === 'object' && !Array.isArray(value)
const nullableText = value => value === null || typeof value === 'string'
const nullableNumber = value => value === null || (typeof value === 'number' && Number.isFinite(value))

// HTTP 200 proves transport success, not resource truth. Each panel validates its exact envelope
// before replacing last-good data or presenting an authoritative empty state.
const PANEL_REQUEST_TIMEOUT_MS = 15_000
const AUTHORING_SAVE_TIMEOUT_MS = 12_000
const AUTHORING_MAX_BYTES = 256 * 1024
const AUTHORING_OPERATION_SCHEMA = 'looplab.authoring-operation-intent/v1'
const AUTHORING_PANEL_DRAFT_SCOPE = 'panel:authoring'
const AUTHORING_KINDS = new Set(['prompts', 'skills', 'knowledge'])
const AUTHORING_REVISION_RE = /^(?:missing|sha256:[0-9a-f]{64})$/
const AUTHORING_OPERATION_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const AUTHORING_OPERATION_KEYS = new Set([
  'schema', 'operationId', 'kind', 'name', 'submittedText', 'expectedRevision',
  'expectedTargetRootId', 'desiredRevision', 'updatedAt',
])
const authoringDigestQuarantine = new Map()
const publicConfigForm = (form, settingsSchema = null) => {
  const sanitized = { ...(form || {}), llm_api_key: '' }
  for (const [key, field] of Object.entries(settingsSchema?.fieldByKey || {})) {
    if (field?.type === 'secret') sanitized[key] = ''
  }
  return sanitized
}
// Exported for test: the guarantee is "no secret field leaves the browser in a config draft", and
// that is a property of the SCHEMA WALK, not of any one field name — a source regex can only see
// that llm_api_key is mentioned.
export const __testPublicConfigForm = publicConfigForm

const publicConfigMeta = meta => ({
  configRevision: typeof meta?.configRevision === 'string' ? meta.configRevision : '',
  pinnedFields: new Set(meta?.pinnedFields instanceof Set ? meta.pinnedFields : []),
  readOnlyFields: new Set(meta?.readOnlyFields instanceof Set ? meta.readOnlyFields : []),
  mismatchFields: Array.isArray(meta?.mismatchFields) ? [...meta.mismatchFields] : [],
})
const RUN_GENERATION_RE = /^[0-9a-f]{64}$/
const CONFIG_DRAFT_SCHEMA = 'looplab.config-draft/v1'
const configDraftScope = runId => `panel:config:${String(runId)}`

function validConfigDraftEnvelope(value, runId) {
  if (!isRecord(value) || value.schema !== CONFIG_DRAFT_SCHEMA || value.unsafe !== true
      || value.runId !== String(runId) || !RUN_GENERATION_RE.test(value.expectedGeneration)
      || !isRecord(value.settingsSchema) || !isRecord(value.form) || !isRecord(value.saved)
      || !isRecord(value.agentControl) || !isRecord(value.savedAC) || !isRecord(value.configMeta)
      || typeof value.saveInFlight !== 'boolean' || !Array.isArray(value.dirtyKeys)
      || !Array.isArray(value.dirtyControlKeys)
      || (value.reconcileGeneration != null
        && !RUN_GENERATION_RE.test(value.reconcileGeneration))
      || value.dirtyKeys.some(key => typeof key !== 'string')
      || value.dirtyControlKeys.some(key => typeof key !== 'string')
      || (value.configMutationUnknown != null && !isRecord(value.configMutationUnknown))) return null
  return value
}

function authoringPayload(value) {
  if (!isRecord(value) || !nullableText(value.dir) || !Array.isArray(value.files)) invalidPanelPayload()
  const targetRootId = value.target_root_id == null ? null : value.target_root_id
  const truncatedFiles = value.truncated_files == null ? 0 : value.truncated_files
  if (!Number.isSafeInteger(truncatedFiles) || truncatedFiles < 0) invalidPanelPayload()
  if ((value.dir == null && (targetRootId !== null || value.files.length > 0 || truncatedFiles > 0))
      || (value.dir != null && !validAuthoringTargetRootId(targetRootId))) invalidPanelPayload()
  const files = value.files.map(file => {
    if (!isRecord(file) || !validAuthoringName(file.name) || typeof file.text !== 'string'
        || typeof file.truncated !== 'boolean') invalidPanelPayload()
    const truncated = file.truncated
    const revision = typeof file.revision === 'string' && AUTHORING_REVISION_RE.test(file.revision)
      ? file.revision : null
    if ((truncated && file.revision !== null) || (!truncated && revision == null)) invalidPanelPayload()
    return { name: file.name, text: file.text, revision, truncated }
  })
  return { dir: value.dir, targetRootId, files, truncatedFiles }
}

const authoringScope = (kind, name) => `${String(kind || '')}\u0000${String(name || '')}`
const authoringStorageKey = authoringRecoveryStorageKey
const authoringStorage = () => {
  try { return typeof sessionStorage === 'undefined' ? null : sessionStorage } catch { return null }
}
const authoringUtf8Bytes = text => {
  try { return new TextEncoder().encode(String(text)).byteLength } catch { return Infinity }
}
const authoringTextWellFormed = text => {
  if (typeof text !== 'string') return false
  if (typeof text.isWellFormed === 'function') return text.isWellFormed()
  for (let index = 0; index < text.length; index++) {
    const unit = text.charCodeAt(index)
    if (unit >= 0xd800 && unit <= 0xdbff) {
      const next = text.charCodeAt(index + 1)
      if (!(next >= 0xdc00 && next <= 0xdfff)) return false
      index++
    } else if (unit >= 0xdc00 && unit <= 0xdfff) return false
  }
  return true
}
const validAuthoringOperation = value => isRecord(value)
  && Object.keys(value).every(key => AUTHORING_OPERATION_KEYS.has(key))
  && Object.keys(value).length === AUTHORING_OPERATION_KEYS.size
  && value.schema === AUTHORING_OPERATION_SCHEMA
  && AUTHORING_OPERATION_RE.test(value.operationId)
  && AUTHORING_KINDS.has(value.kind) && validAuthoringName(value.name)
  && authoringTextWellFormed(value.submittedText)
  && authoringUtf8Bytes(value.submittedText) <= AUTHORING_MAX_BYTES
  && AUTHORING_REVISION_RE.test(value.expectedRevision)
  && validAuthoringTargetRootId(value.expectedTargetRootId)
  && /^sha256:[0-9a-f]{64}$/.test(value.desiredRevision)
  && Number.isSafeInteger(value.updatedAt) && value.updatedAt >= 0

function parseAuthoringStorageIdentity(key) {
  if (typeof key !== 'string' || !key.startsWith(AUTHORING_OPERATION_STORAGE_PREFIX)) return null
  try {
    const decoded = decodeURIComponent(key.slice(AUTHORING_OPERATION_STORAGE_PREFIX.length))
    const split = decoded.indexOf('\u0000')
    if (split <= 0 || decoded.indexOf('\u0000', split + 1) !== -1) return null
    const kind = decoded.slice(0, split), name = decoded.slice(split + 1)
    return AUTHORING_KINDS.has(kind) && validAuthoringName(name)
      ? { kind, name, scope: authoringScope(kind, name) } : null
  } catch { return null }
}

function inspectAuthoringOperations() {
  const storage = authoringStorage()
  if (!storage) return { available: false, valid: {}, damaged: {} }
  const valid = {}, damaged = {}
  try {
    const keys = []
    for (let index = 0; index < storage.length; index++) {
      const key = storage.key(index)
      if (key?.startsWith(AUTHORING_OPERATION_STORAGE_PREFIX)) keys.push(key)
    }
    for (const key of keys) {
      const raw = storage.getItem(key)
      const identity = parseAuthoringStorageIdentity(key)
      const quarantined = authoringDigestQuarantine.get(key)
      if (quarantined && quarantined.raw !== raw) authoringDigestQuarantine.delete(key)
      const exactQuarantine = quarantined?.raw === raw ? quarantined : null
      let parsed = null
      try { parsed = JSON.parse(raw || 'null') } catch { /* damaged below */ }
      if (identity && key === authoringStorageKey(identity.kind, identity.name)
          && !exactQuarantine
          && !Object.hasOwn(valid, identity.scope) && validAuthoringOperation(parsed)
          && parsed.kind === identity.kind && parsed.name === identity.name) {
        valid[identity.scope] = { ...parsed, scope: identity.scope, storageKey: key, storageRaw: raw }
      } else {
        const scope = `damaged:${key}`
        damaged[scope] = {
          scope, key, raw: raw ?? '', identity, inspected: false,
          ...(exactQuarantine ? { reason: exactQuarantine.reason } : {}),
        }
      }
    }
    return { available: true, valid, damaged }
  } catch { return { available: false, valid: {}, damaged: {} } }
}

function saveAuthoringOperationIntent(intent) {
  const storage = authoringStorage()
  if (!storage || !validAuthoringOperation(intent)) return null
  const key = authoringStorageKey(intent.kind, intent.name)
  try {
    const raw = storage.getItem(key)
    if (raw != null) {
      let existing = null
      try { existing = JSON.parse(raw) } catch { return null }
      if (!validAuthoringOperation(existing)
          || existing.kind !== intent.kind || existing.name !== intent.name
          || existing.operationId !== intent.operationId
          || existing.submittedText !== intent.submittedText
          || existing.expectedRevision !== intent.expectedRevision
          || existing.expectedTargetRootId !== intent.expectedTargetRootId
          || existing.desiredRevision !== intent.desiredRevision) return null
    }
    const serialized = JSON.stringify(intent)
    storage.setItem(key, serialized)
    if (storage.getItem(key) !== serialized) return null
    return { ...intent, scope: authoringScope(intent.kind, intent.name), storageKey: key, storageRaw: serialized }
  } catch { return null }
}

function clearAuthoringOperationIntent(intent) {
  const storage = authoringStorage()
  if (!storage || !intent?.storageKey || typeof intent.storageRaw !== 'string') return false
  try {
    if (storage.getItem(intent.storageKey) !== intent.storageRaw) return false
    storage.removeItem(intent.storageKey)
    return storage.getItem(intent.storageKey) == null
  } catch { return false }
}

function clearDamagedAuthoringOperation(recovery) {
  const storage = authoringStorage()
  if (!storage || !recovery?.key || typeof recovery.raw !== 'string') return false
  try {
    if (storage.getItem(recovery.key) !== recovery.raw) return false
    storage.removeItem(recovery.key)
    return storage.getItem(recovery.key) == null
  } catch { return false }
}

async function authoringTextRevision(text) {
  if (!authoringTextWellFormed(text)) {
    const error = new Error('File text contains an unpaired Unicode surrogate.')
    error.code = 'AUTHORING_TEXT_NOT_WELL_FORMED'
    throw error
  }
  const bytes = new TextEncoder().encode(text)
  if (bytes.byteLength > AUTHORING_MAX_BYTES) {
    const error = new Error(`File is larger than ${AUTHORING_MAX_BYTES} UTF-8 bytes.`)
    error.code = 'AUTHORING_TEXT_TOO_LARGE'
    throw error
  }
  if (!globalThis.crypto?.subtle) {
    const error = new Error('Secure browser hashing is unavailable.')
    error.code = 'AUTHORING_HASH_UNAVAILABLE'
    throw error
  }
  const digest = await globalThis.crypto.subtle.digest('SHA-256', bytes)
  return 'sha256:' + [...new Uint8Array(digest)]
    .map(value => value.toString(16).padStart(2, '0')).join('')
}

function memoryPayload(value) {
  if (!isRecord(value) || !nullableText(value.dir)
      || !['cases', 'lessons', 'notes'].every(key => Array.isArray(value[key]))) invalidPanelPayload()
  const cases = value.cases.map(row => {
    if (!isRecord(row) || !row.task_id || typeof row.task_id !== 'string' || typeof row.goal !== 'string'
        || !nullableNumber(row.metric) || !Object.hasOwn(row, 'params')
        || (row.params_truncated != null && typeof row.params_truncated !== 'boolean')) invalidPanelPayload()
    return { ...row, params_truncated: row.params_truncated === true }
  })
  const lessonText = ['role', 'kind', 'outcome', 'task_id']
  for (const row of value.lessons) {
    if (!isRecord(row) || !row.statement || typeof row.statement !== 'string'
        || lessonText.some(key => row[key] != null && typeof row[key] !== 'string')
        || ['delta', 'confidence'].some(key => row[key] != null && !nullableNumber(row[key]))
        || (row.evidence_count != null && (!Number.isSafeInteger(row.evidence_count) || row.evidence_count < 0))) invalidPanelPayload()
  }
  const notes = value.notes.map(row => {
    const note = isRecord(row) && (row.note || row.statement)
    if (typeof note !== 'string' || !note || (row.task_id != null && typeof row.task_id !== 'string')) invalidPanelPayload()
    return { ...row, note }
  })
  if (value.dir == null) {
    if (cases.length || value.lessons.length || notes.length || value.projection != null || value.page != null) {
      invalidPanelPayload()
    }
    return { dir: null, cases, lessons: value.lessons, notes, projection: null, page: null }
  }
  if (value.projection !== 'bounded_recent_tail' || !isRecord(value.page)
      || !isRecord(value.page.tiers)) invalidPanelPayload()
  const tiers = {}
  for (const key of ['cases', 'lessons', 'notes']) {
    const receipt = value.page.tiers[key]
    if (!isRecord(receipt)
        || !Number.isSafeInteger(receipt.limit) || receipt.limit <= 0
        || !Number.isSafeInteger(receipt.returned) || receipt.returned < 0 || receipt.returned > receipt.limit
        || receipt.returned !== value[key].length
        || !Number.isSafeInteger(receipt.skipped) || receipt.skipped < 0
        || typeof receipt.source_window_truncated !== 'boolean'
        || typeof receipt.unavailable !== 'boolean'
        || (receipt.unavailable && receipt.returned !== 0)) invalidPanelPayload()
    tiers[key] = {
      limit: receipt.limit,
      returned: receipt.returned,
      skipped: receipt.skipped,
      sourceWindowTruncated: receipt.source_window_truncated,
      unavailable: receipt.unavailable,
    }
  }
  const truncated = Object.values(tiers).some(receipt => receipt.sourceWindowTruncated)
  const unavailable = Object.values(tiers).some(receipt => receipt.unavailable)
  const partial = Object.values(tiers).some(receipt => receipt.sourceWindowTruncated
    || receipt.skipped > 0 || receipt.unavailable)
  if (value.page.truncated !== truncated || value.page.unavailable !== unavailable
      || value.page.partial !== partial) invalidPanelPayload()
  return {
    dir: value.dir,
    cases,
    lessons: value.lessons,
    notes,
    projection: value.projection,
    page: { tiers, truncated, unavailable, partial },
  }
}

const memoryTierIncomplete = receipt => !!receipt && (receipt.sourceWindowTruncated
  || receipt.skipped > 0 || receipt.unavailable)

function runsPayload(value) {
  if (!Array.isArray(value)) invalidPanelPayload()
  for (const row of value) {
    if (!isRecord(row) || !row.run_id || typeof row.run_id !== 'string' || typeof row.task_id !== 'string'
        || !['min', 'max'].includes(row.direction) || typeof row.finished !== 'boolean'
        || typeof row.phase !== 'string' || !Number.isSafeInteger(row.nodes) || row.nodes < 0
        || !nullableNumber(row.best_metric) || !nullableNumber(row.best_confirmed)
        || !nullableText(row.label)) invalidPanelPayload()
  }
  return value
}

function gpuPayload(value) {
  if (!isRecord(value) || typeof value.available !== 'boolean'
      || (value.gpus != null && !Array.isArray(value.gpus))) invalidPanelPayload()
  const gpus = value.gpus || []
  for (const gpu of gpus) {
    if (!isRecord(gpu) || !gpu.name || typeof gpu.name !== 'string'
        || ['util', 'mem_used', 'mem_total', 'temp', 'power'].some(key => !nullableNumber(gpu[key]))) invalidPanelPayload()
  }
  if (value.available && !Array.isArray(value.gpus)) invalidPanelPayload()
  return { available: value.available, gpus }
}

// A poll and a manual retry share one synchronous lock: interval ticks skip an active request, while
// Retry starts immediately when idle. A failed refresh retains last-good data and marks it stale.
function usePanelResource(loader, normalize = value => value, key = '', pollMs = null) {
  const [value, setValue] = useState({ key, state: 'loading', data: null, pending: null })
  const flight = useRef(null)
  const startRef = useRef(null)
  useEffect(() => {
    let alive = true
    const owner = {}
    const start = (intent = 'refresh') => {
      if (flight.current) return false
      const timed = deadlineRequest(signal => Promise.resolve().then(() => loader(signal)).then(normalize),
        PANEL_REQUEST_TIMEOUT_MS)
      const request = { owner, controller: timed.controller }
      flight.current = request
      setValue(previous => previous.key !== key
        ? { key, state: 'loading', data: null, pending: null }
        : (intent === 'retry' || ['error', 'stale'].includes(previous.state))
          ? { ...previous, pending: intent } : previous)
      const finish = (ok, data = null) => {
        if (flight.current !== request) return
        flight.current = null
        if (!alive) return
        setValue(previous => {
          const lastGood = previous.key === key ? previous.data : null
          return ok ? { key, state: 'ready', data, pending: null }
            : { key, state: lastGood == null ? 'error' : 'stale', data: lastGood, pending: null }
        })
      }
      timed.promise.then(data => finish(true, data), () => finish(false))
      return true
    }
    startRef.current = start
    start('load')
    const timer = pollMs == null ? null : setInterval(start, pollMs)
    return () => {
      alive = false
      if (timer != null) clearInterval(timer)
      if (startRef.current === start) startRef.current = null
      if (flight.current?.owner === owner) {
        flight.current.controller.abort()
        flight.current = null
      }
    }
  }, [key, pollMs])
  const resource = value.key === key ? value : { key, state: 'loading', data: null, pending: null }
  return [resource, () => startRef.current?.('retry') || false]
}

function PanelResourceNotice({ resource, label, onRetry }) {
  if (resource.state === 'ready') return null
  if (resource.state === 'loading') return <div className="muted" role="status">Loading {label}…</div>
  const stale = resource.state === 'stale'
  return <div className={'report-inline-state' + (stale ? '' : ' error')} role={stale ? 'status' : 'alert'}>
    <OpIcon name="alert" size={14} />
    <span>{label}: {resource.pending
      ? `${resource.pending === 'retry' ? 'Retrying' : 'Refreshing'}…${stale ? ' Last loaded data remains visible.' : ''}`
      : stale ? 'Last loaded data; refresh failed.' : 'Unavailable.'}</span>
    <button className="btn sm" disabled={!!resource.pending} onClick={onRetry}>
      {resource.pending === 'retry' ? 'Retrying…' : resource.pending ? 'Refreshing…' : 'Retry'}</button>
  </div>
}

// Overall-info tab (round-8): the run's at-a-glance metrics, lifted out of the cramped top bar so the
// header stays a single line. Everything derives from the folded state (+ maxEval from config).
export function OverviewPanel({ state, maxEval, onClose, onOpenPanel }) {
  const nodes = Object.values(state.nodes || {})
  const evaluated = nodes.filter(n => n.metric != null).length
  const failed = nodes.filter(n => n.status === 'failed').length
  const best = state.best_node_id != null ? (state.nodes || {})[state.best_node_id] : null
  const evalSec = state.total_eval_seconds || 0
  const cost = state.llm_cost
  const strat = state.active_strategy
  const hints = state.pending_hints || []
  return (
    <Panel title="Overview" sub={state.task_id || ''} onClose={onClose}>
      {state.goal && <div className="ov-goal">{state.goal}</div>}
      <div className="stat-grid">
        <Stat n={best ? fmt(best.confirmed_mean ?? best.metric) : '—'} l="best metric" />
        <Stat n={state.direction || '—'} l="direction" />
        <Stat n={nodes.length} l="nodes" />
        <Stat n={evaluated} l="evaluated" />
        <Stat n={failed} l="failed" />
        <Stat n={fmtElapsedSeconds(evalSec) + (maxEval != null ? ' / ' + fmtElapsedSeconds(maxEval) : '')} l="eval time" />
        {cost && <Stat n={fmtInt(cost.total_tokens)} l="tokens" />}
        {state.paused ? <Stat n="paused" l="status" /> : null}
      </div>
      {strat && <div className="ov-row"><span className="k"><OpIcon name="compass" className="t-ic" /> strategy</span>{' '}
        {(strat.policy || 'greedy') + (strat.fidelity ? '/' + strat.fidelity : '')}
        {strat.rationale && <div className="muted ov-why">{strat.rationale}</div>}</div>}
      {hints.length > 0 && <div className="ov-row"><span className="k"><OpIcon name="bulb" className="t-ic" /> hints ({hints.length})</span>
        <ul className="ov-hints">{hints.map((h, i) => <li key={(h.text || '') + i}>{h.text || JSON.stringify(h)}</li>)}</ul></div>}
      {(state.novelty_events?.length > 0 || state.reward_hacks?.length > 0) && <div className="ov-row ov-alerts">
        {state.novelty_events?.length > 0 && <span className="chip" title="near-duplicate proposals nudged to diversify (E1)"><OpIcon name="replay" className="t-ic" /> dedup {state.novelty_events.length}</span>}
        {state.reward_hacks?.length > 0 && <button type="button" className="chip alarm run-metric-chip"
          title="suspicious wins flagged (B5)" onClick={() => onOpenPanel?.('trust')}>
          <OpIcon name="alert" size={11} /> hack? {state.reward_hacks.length}</button>}
      </div>}
    </Panel>
  )
}

// Deep-research drawer: every memo in one place (instead of scrolling the timeline feed), with
// ACTIONABLE directions — "steer →" posts a hint the Researcher folds into the next proposal. Deep
// research is no longer a DAG node; this drawer + the Dock timeline marker are its home.
export function ResearchPanel({ state, runId, onToast, onClose }) {
  const memoProjection = useMemo(() => normalizeResearchMemos(state.research), [state.research])
  const memos = [...memoProjection.memos].reverse()   // newest retained first
  const steer = async (text) => {
    try {
      const feedback = commandFeedback(await CONTROL.hint(runId, 'try this research direction: ' + text), {
        success: 'Steered the next proposal', noop: 'That direction was already queued',
        executing: 'Steer request accepted — waiting for the run', failure: 'Could not steer',
      }); onToast?.(feedback.message)
    } catch (error) {
      onToast?.(`Could not steer: ${error.message || error}`)
    }
  }
  return (
    <Panel title="Deep research" sub={memos.length ? `${memos.length} memo${memos.length === 1 ? '' : 's'}` : 'none yet'} onClose={onClose} wide>
      {!memos.length && <div className="muted">No deep-research memos yet. Trigger one with <code>/deep-research</code> in the chat, or set a cadence in Config.</div>}
      {memoProjection.omitted > 0 && <div className="muted">
        Showing {memos.length} of {memoProjection.total} newest valid memos; older, malformed, or over-budget entries are omitted.
      </div>}
      {memos.map(m => (
        // Key by the STABLE original index (research is append-only), not the reversed position:
        // keyed by `i`, a new memo landing at index 0 reuses the prior memo's DOM node and its open
        // <details> state bleeds onto the new one.
        <div className="rsch-memo" key={m.sourceIndex}>
          <div className="rsch-h">
            <span className="rsch-ic"><OpIcon name="search" /></span>
            <b>{m.summary || '(no summary)'}</b>
            <span className="right" />
            {m.trigger && <span className="pill">{m.trigger}</span>}
            {m.at_node != null && <span className="pill">@#{m.at_node}</span>}
          </div>
          {(m.findings || []).length > 0 && <><div className="section-h">Findings</div>
            <ul className="bul">{m.findings.map((f, j) => <li key={j}>{f}</li>)}</ul></>}
          {/* D8: decoupled Verifier verdicts over the memo's claims (synthesis is the weak link) */}
          {m.verification && ((m.verification.verdicts || []).length > 0
            || m.verification.omittedVerdicts > 0) && <>
            <div className="section-h">Verification
              {m.verification.unsupported > 0 &&
                <span className="chip warn" title="claims whose cited evidence does not support them">
                  {m.verification.unsupported} unsupported</span>}
              {m.verification.omittedVerdicts > 0 &&
                <span className="chip warn">verification incomplete</span>}
              <span className="muted"> ({m.verification.method})</span></div>
            {m.verification.omittedVerdicts > 0 && <p className="memo-verification-incomplete" role="note">
              Showing {m.verification.verdicts.length} of {m.verification.totalVerdicts} verifier verdicts;
              omitted verdicts make this check incomplete.
            </p>}
            <ul className="bul">{m.verification.verdicts.map((v, j) => (
              <li key={j} className={v.verdict === 'supported' ? 'ok'
                : (v.verdict === 'unclear' || v.verdict === 'cited') ? '' : 'bad'}>
                <span className="pill">{v.verdict}</span> {v.statement}
                {v.note && <span className="muted"> — {v.note}</span>}</li>))}</ul></>}
          {(m.recommended_directions || []).length > 0 && <><div className="section-h">Recommended directions</div>
            <ul className="rsch-dirs">{m.recommended_directions.map((d, j) => (
              <li key={j}><span>{d}</span>
                <button className="btn sm ghost" title="steer the next proposal toward this direction (posts a hint)"
                        onClick={() => steer(d)}>
                  steer →
                </button>
              </li>))}</ul></>}
          {(m.sources || []).length > 0 && <><div className="section-h">Sources</div>
            <ul className="bul">{m.sources.map((source, index) => {
              // Deep-research sources are untrusted provider output. Only credential-free HTTP(S)
              // URLs become links; unsafe, oversized or malformed values remain bounded inert text.
              const href = safeExternalHref(source?.url)
              const label = String(source?.title ?? source?.url ?? 'source').slice(0, 300)
              const snippet = source?.snippet == null ? '' : String(source.snippet).slice(0, 160)
              return <li key={index}>{href
                ? <a href={href} target="_blank" rel="noreferrer noopener">{label}</a> : label}
                {snippet && <div className="muted">{snippet}</div>}</li>
            })}</ul></>}
          {m.reasoning && <details className="rsch-reasoning"><summary>reasoning (debug)</summary>
            <Markdown className="think-body" text={m.reasoning} /></details>}
        </div>
      ))}
    </Panel>
  )
}

function TrustState({ value, action = null }) {
  const icon = value.tone === 'alarm' ? 'alert' : value.tone === 'ok' ? 'check' : 'dot'
  return <div className={`trust-state ${value.tone}`} role={value.tone === 'alarm' ? 'alert' : 'status'}>
    <OpIcon name={icon} size={14} />
    <strong>{value.label}</strong>
    <span>{value.detail}</span>
    {action && <div className="trust-state-actions">{action}</div>}
  </div>
}

export function TrustPanel({ state, runId, onClose, onSelect, onToast, readOnly = false }) {
  const [configResource, setConfigResource] = useState({ status: 'loading', data: null, error: null })
  const [configNonce, setConfigNonce] = useState(0)
  useEffect(() => {
    let alive = true
    setConfigResource({ status: 'loading', data: null, error: null })
    get(`/api/runs/${encodeURIComponent(runId)}/config`)
      .then(data => { if (alive) setConfigResource({ status: 'ready', data, error: null }) })
      .catch(error => { if (alive) setConfigResource({ status: 'error', data: null, error: error.message || 'Request failed' }) })
    return () => { alive = false }
  }, [runId, configNonce])
  const cfg = configResource.data
  const quarantine = async (id) => {   // U6: act on a flagged node — remove it from the search
    try {
      const feedback = commandFeedback(await CONTROL.nodeAbort(runId, id, state.nodes?.[id]?.attempt), {
        success: `Quarantined #${id}`, noop: `#${id} was already settled`,
        executing: `Quarantine of #${id} requested — waiting for the run`, failure: `Could not quarantine #${id}`,
      }); onToast?.(feedback.message)
    } catch (error) { onToast?.(`Could not quarantine #${id}: ${error.message || error}`) }
  }
  const nodes = Object.values(state.nodes)
  const evald = nodes.filter(n => n.metric != null && n.feasible !== false)
  const chooser = state.direction === 'min' ? (a, b) => a < b : (a, b) => a > b
  const naive = evald.slice().sort((a, b) => chooser(a.metric, b.metric) ? -1 : 1)[0]
  const robust = state.best_node_id != null ? state.nodes[state.best_node_id] : null
  const leak = state.leakage
  const leakState = leakageStatus(leak)
  const driftState = driftStatus(state.drifts, cfg, evald.length)
  const hackState = rewardHackStatus(state.reward_hacks, cfg, evald.length)
  return (
    <Panel title="Trust & rigor" sub="evidence and coverage" onClose={onClose} wide>
      <div className="trust-panel-body">
      {configResource.status === 'loading' && <TrustState value={{ tone: 'loading', label: 'Loading detector configuration', detail: 'Checking which trust controls were actually enabled for this run.' }} />}
      {configResource.status === 'error' && <TrustState
        value={{ tone: 'unknown', label: 'Detector configuration unavailable', detail: `Coverage cannot be verified: ${configResource.error}` }}
        action={<button className="btn sm" onClick={() => setConfigNonce(n => n + 1)}>Retry</button>} />}

      <div className="cardgrid">
        <Stat n={cfg?.trust_mode || (configResource.status === 'loading' ? 'Loading…' : 'Unknown')} l="sandbox tier" />
        <Stat n={cfg?.eval_trust_mode || (configResource.status === 'loading' ? 'Loading…' : 'Unknown')} l="eval trust mode" />
        <Stat n={state.host_grading ? 'host-side' : 'self-reported'} l="metric scoring" />
        <Stat n={state.workspace_changed ? 'changed' : 'no change flag'} l="workspace drift" />
      </div>
      {state.host_grading
        ? <TrustState value={{ tone: 'ok', label: 'Host-side grading recorded', detail: `The candidate writes predictions only; ${state.host_grading.scorer || 'the host scorer'} evaluates ${state.host_grading.n_labels ?? 'held-out'} labels outside the candidate process.` }} />
        : <TrustState value={{ tone: 'warn', label: 'Metric is not host-graded', detail: 'This run does not record an out-of-process grader, so the displayed metric may be self-reported by the candidate process.' }} />}

      <div className="section-h">Seed-luck and robustness</div>
      {robust && naive
        ? <>
          <TrustState value={robust.confirmed_mean != null
            ? { tone: 'ok', label: 'Winner is multi-seed confirmed', detail: `${robust.confirmed_seeds || 'Multiple'} successful seeds produced ${fmt(robust.confirmed_mean)} ±${fmt(robust.confirmed_std)}.` }
            : { tone: 'warn', label: 'Winner is single-evaluation', detail: 'Seed luck has not been ruled out; the selected winner is not a robust result yet.' }} />
          <div className="kv">
            <div className="k">single-eval leader</div><div className="v">#{naive.id} · {fmt(naive.metric)}</div>
            <div className="k">selected winner</div><div className="v">#{robust.id} · {fmt(robust.confirmed_mean ?? robust.metric)}{robust.confirmed_mean != null ? ` ±${fmt(robust.confirmed_std)}` : ' (unconfirmed)'}</div>
            {robust.confirmed_mean != null && naive.id !== robust.id && <><div className="k flag">demotion</div><div className="v">Single-eval leader #{naive.id} was not selected — multi-seed confirmation corrected a seed-lucky result.</div></>}
          </div>
        </>
        : <TrustState value={{ tone: 'unknown', label: 'No result to confirm', detail: 'There are no feasible evaluated nodes yet.' }} />}

      <div className="section-h">Leakage scan {leak && leak.leak && <span className="chip alarm">LEAK — run refused</span>}</div>
      <TrustState value={leakState} />
      {(leak?.verdicts || []).length > 0 &&
        <DataTable caption="Leakage detector verdicts" card={false}><table className="tbl"><thead><tr><th>detector</th><th>result</th><th>detail</th></tr></thead><tbody>
          {(leak.verdicts || []).map((v, i) => <tr key={i}>
            <td>{v.detector || 'unnamed detector'}</td><td className={`trust-result ${v.leak ? 'fail' : 'pass'}`}>{v.leak ? 'Flagged' : 'Passed'}</td>
            <td className="muted">{Object.entries(v).filter(([k]) => !['detector', 'leak'].includes(k)).map(([k, val]) => `${k}=${typeof val === 'object' ? JSON.stringify(val) : val}`).join('  ') || '—'}</td>
          </tr>)}</tbody></table></DataTable>}

      <div className="section-h">Drift cross-check</div>
      <TrustState value={driftState} />
      {(state.drifts || []).length
        ? <DataTable caption="Run metric drift comparisons" card={false}><table className="tbl"><thead><tr><th>node</th><th>primary</th><th>cross</th><th>tolerance</th></tr></thead><tbody>
          {state.drifts.map((d, i) => <tr key={i}><td className="flag">#{d.node_id}</td><td>{fmt(d.primary)}</td><td>{fmt(d.cross)}</td><td>{fmt(d.tolerance)}</td></tr>)}</tbody></table></DataTable>
        : null}

      <div className="section-h">Reward-hacking monitor (B5) {(state.reward_hacks || []).length > 0 && <span className="chip alarm">{state.reward_hacks.length} flagged</span>}</div>
      <TrustState value={hackState} />
      {/* Folded state is the enforcement truth (it applies trust_gate_changed events);
          the config snapshot alone can claim a gate the fold never engages. */}
      {(state.trust_gate || cfg) && <div className="muted" style={{ marginBottom: 6 }}>
        enforcement: <b>{state.trust_gate || cfg?.trust_gate || 'audit'}</b>{(state.trust_gate || cfg?.trust_gate || 'audit') === 'audit'
          ? ' — signals are logged only; set trust_gate=gate/block (or the thorough profile) to keep a high-precision flag from winning.'
          : ' — a high-precision flag is excluded from best-selection and breeding/confirmation.'}
        {' '}Only high-precision signals gate; broad critic/perfect-score warnings stay advisory, except
        <code> critic:hardcoded_metric</code>, which is classified as high precision.</div>}
      {(state.reward_hacks || []).length
        ? <DataTable caption="Reward-hacking signals" card={false}><table className="tbl"><thead><tr><th>node</th><th>signal</th><th>detail</th><th>action</th></tr></thead><tbody>
          {state.reward_hacks.map((h, i) => <tr key={i}>
            <td className="flag"><button className="btn xs ghost" onClick={() => { onSelect && onSelect(h.node_id); onClose() }}>#{h.node_id}</button></td>
            <td>{(h.signals || []).map(s => s.signal).join(', ')}</td>
            <td className="muted">{(h.signals || []).map(s => s.detail).filter(Boolean).join(' · ')}</td>
            <td>{!readOnly && <button className="btn xs ghost" title="quarantine: abort this node so it can't be selected"
              onClick={() => quarantine(h.node_id)}>quarantine</button>}</td>
          </tr>)}</tbody></table></DataTable>
        : null}
      </div>
    </Panel>
  )
}

export function SensitivityPanel({ state, onClose, onSelect }) {
  // Aggregate ablation impacts across all ablate events (latest wins per param).
  const impacts = {}
  ;(state.ablations || []).forEach(a => Object.entries(a.impacts || {}).forEach(([k, v]) => { impacts[k] = Math.abs(v) }))
  const bars = Object.entries(impacts).map(([label, value]) => ({ label, value })).sort((a, b) => b.value - a.value)
  return (
    <Panel title="Parameter sensitivity" onClose={onClose} wide>
      <div className="section-h">Ablation impact (|Δmetric| when param zeroed)</div>
      {bars.length ? <Bars data={bars} color="#9a6bff" /> : <div className="muted">No ablation events yet (enable ablate_every or use Force-ablate on a node).</div>}
      <div className="section-h">Parallel coordinates — params → metric</div>
      <ParallelCoords nodes={Object.values(state.nodes)} direction={state.direction}
        onPick={onSelect ? (id) => { onSelect(id); onClose && onClose() } : undefined} />
    </Panel>
  )
}

export function FailuresPanel({ state, onClose, onSelect }) {
  const failed = Object.values(state.nodes).filter(n => n.status === 'failed')
  const byReason = {}
  failed.forEach(n => { (byReason[n.error_reason || 'unknown'] ||= []).push(n) })
  return (
    <Panel title="Failures" sub={`${failed.length} failed`} onClose={onClose}>
      <div className="cardgrid" style={{ marginBottom: 12 }}>
        {Object.entries(byReason).map(([r, ns]) => <Stat key={r} n={ns.length} l={r} />)}
        {!failed.length && <Stat n={0} l="no failures" />}
      </div>
      <DataTable caption="Failed nodes and errors" card={false}><table className="tbl"><thead><tr><th>node</th><th>reason</th><th>error</th></tr></thead><tbody>
        {failed.map(n => <tr key={n.id}>
          <td><button type="button" className="btn xs ghost" onClick={() => { onSelect?.(n.id); onClose?.() }}>#{n.id}</button></td>
          <td className="flag">{n.error_reason}</td><td className="muted">{(n.error || '').slice(0, 80)}</td></tr>)}
      </tbody></table></DataTable>
    </Panel>
  )
}

// U1 · experiment queue: the search's planned/in-flight work, made VISIBLE and cancelable. Pending
// nodes (created, not yet evaluated) are the concrete queue — each cancelable via node_abort; queued
// control requests (injects/forks/confirm/ablate not yet materialized) show as read-only chips so
// the operator can see what's coming. Order is policy-driven (the engine picks next), so this is
// "see + cancel + add", not manual reordering (which no engine event supports).
export function QueuePanel({ state, runId, onSelect, onClose, onToast }) {
  const nodes = Object.values(state.nodes || {})
  const pending = nodes.filter(n => n.status === 'pending').sort((a, b) => a.id - b.id)
  const working = nodes.filter(n => n.status === 'running')   // (if the fold ever exposes it)
  const injects = (state.inject_requests || []).slice(state.injects_done || 0)
  const forks = (state.fork_requests || []).slice(state.forks_done || 0)
  const { confirms: confirmReq, ablates: ablateReq } = queuedGenerationControls(state)
  const cancel = async (id) => {
    try {
      const feedback = commandFeedback(await CONTROL.nodeAbort(runId, id, state.nodes?.[id]?.attempt), {
        success: `Cancelled #${id}`, noop: `#${id} was already settled`,
        executing: `Cancellation of #${id} requested — waiting for the run`, failure: `Could not cancel #${id}`,
      }); onToast?.(feedback.message)
    } catch (error) { onToast?.(`Could not cancel #${id}: ${error.message || error}`) }
  }
  const queuedCount = pending.length + injects.length + forks.length + confirmReq.length + ablateReq.length
  return (
    <Panel title="Queue" sub={`${queuedCount} planned / in-flight`} onClose={onClose}>
      <div className="muted" style={{ marginBottom: 10 }}>
        The next experiment is chosen by the search policy; this is the live work-list — cancel a
        pending experiment, or add one from the chat (<code>/experiment</code>) or a node’s “explore”.
      </div>
      <div className="section-h">Pending experiments {pending.length > 0 && <span className="pill">{pending.length}</span>}</div>
      {pending.length
        ? <DataTable caption="Pending experiment queue" card={false}><table className="tbl"><thead><tr><th>node</th><th>op</th><th>parents</th><th>hypothesis / rationale</th><th>action</th></tr></thead><tbody>
          {pending.map(n => <tr key={n.id}>
            <td><button className="btn xs ghost" onClick={() => { onSelect && onSelect(n.id); onClose() }}>#{n.id}</button></td>
            <td><span className="op-icon"><OpIcon name={operatorMeta(n.operator).icon} size={12} /></span> {n.operator}</td>
            <td className="muted">{(n.parent_ids || []).map(p => '#' + p).join(', ') || '—'}</td>
            <td className="muted">{stripMd(n.idea?.hypothesis || n.idea?.rationale || '').slice(0, 70)}</td>
            <td><button className="btn xs ghost" aria-label={`Cancel experiment ${n.id}`}
              title="cancel this experiment (node_abort)" onClick={() => cancel(n.id)}><OpIcon name="cross" size={11} /></button></td>
          </tr>)}
        </tbody></table></DataTable>
        : <div className="muted">No experiment is queued right now — the loop is idle or between picks.</div>}
      {(injects.length + forks.length + confirmReq.length + ablateReq.length) > 0 && <>
        <div className="section-h">Queued control requests</div>
        <div className="chips">
          {injects.map((q, i) => <span key={'i' + i} className="chip sm" title="operator-injected experiment awaiting materialization">inject: {(q.idea?.operator || 'experiment')}</span>)}
          {forks.map((q, i) => <span key={'f' + i} className="chip sm" title="fork awaiting materialization">fork #{q.from_node_id ?? q.parent_id ?? '?'}</span>)}
          {confirmReq.map(r => <span key={`c:${r.node_id}:${r.generation ?? 'legacy'}`} className="chip sm" title={r.generation == null ? undefined : `node generation ${r.generation}`}>confirm #{r.node_id}</span>)}
          {ablateReq.map(r => <span key={`a:${r.node_id}:${r.generation ?? 'legacy'}`} className="chip sm" title={r.generation == null ? undefined : `node generation ${r.generation}`}>ablate #{r.node_id}</span>)}
        </div>
      </>}
    </Panel>
  )
}

// I5 · non-dominated (Pareto-optimal) set over the primary metric (direction-aware) + every
// extra_metric (treated as cost-like / minimize). A node is Pareto-optimal if no other node is
// at-least-as-good on all objectives and strictly better on one.
function paretoFront(nodes, direction) {
  const keys = [...new Set(nodes.flatMap(n => Object.keys(n.extra_metrics || {})))]
  const vec = (n) => [direction === 'min' ? n.metric : -n.metric,
    ...keys.map(k => { const v = n.extra_metrics?.[k]; return v == null ? Infinity : v })]
  const dominates = (a, b) => { let strict = false; for (let i = 0; i < a.length; i++) { if (a[i] > b[i]) return false; if (a[i] < b[i]) strict = true } return strict }
  const pts = nodes.map(n => ({ n, v: vec(n) }))
  return { keys, front: pts.filter(p => !pts.some(q => q !== p && dominates(q.v, p.v))).map(p => p.n) }
}
export function ParetoPanel({ state, onClose, onSelect }) {
  const nodes = Object.values(state.nodes).filter(n => n.metric != null && n.feasible !== false)
  // first constraint dimension, if any
  const withV = nodes.filter(n => (n.violations || []).length || Object.keys(n.extra_metrics || {}).length)
  let scatter = null
  const cName = withV.length ? (withV[0].violations?.[0]?.name || Object.keys(withV[0].extra_metrics || {})[0]) : null
  if (cName) {
    const data = nodes.map(n => {
      const cv = (n.violations || []).find(v => v.name === cName)?.value ?? n.extra_metrics?.[cName]
      return cv == null ? null : { x: cv, y: n.confirmed_mean ?? n.metric, feasible: n.feasible !== false, id: n.id }
    }).filter(Boolean)
    scatter = <Scatter data={data} xlab={cName} ylab="metric"
      onPick={onSelect ? (id) => { onSelect(id); onClose && onClose() } : undefined} />
  }
  const archive = state.archive
  const ops = {}
  Object.values(state.nodes).forEach(n => { const o = (ops[n.operator] ||= { n: 0, ev: 0 }); o.n++; if (n.status === 'evaluated') o.ev++ })
  return (
    <Panel title="Pareto · Diversity · Operators" onClose={onClose} wide>
      {(() => {
        const { keys, front } = paretoFront(nodes, state.direction)
        return <>
          <div className="section-h">Pareto-optimal set (I5) {keys.length ? <span className="pill">{keys.length + 1} objectives</span> : <span className="pill">metric only</span>}</div>
          {keys.length
            ? <DataTable caption="Pareto-optimal node metrics" card={false}><table className="tbl"><thead><tr><th>node</th><th>metric</th>{keys.map(k => <th key={k}>{k}</th>)}</tr></thead><tbody>
                {front.sort((a, b) => (state.direction === 'min' ? a.metric - b.metric : b.metric - a.metric)).map(n =>
                  <tr key={n.id}><td>#{n.id}{n.id === state.best_node_id ? <OpIcon name="crown" size={10} /> : ''}</td><td>{fmt(n.confirmed_mean ?? n.metric)}</td>
                    {keys.map(k => <td key={k} className="muted">{fmt(n.extra_metrics?.[k])}</td>)}</tr>)}
              </tbody></table></DataTable>
            : <div className="muted">Single-objective task — the Pareto front is just the best node (#{state.best_node_id ?? '—'}). Add extra_metrics (e.g. latency, size) to trade off.</div>}
        </>
      })()}
      <div className="section-h">Pareto (metric vs constraint)</div>
      {scatter || <div className="muted">No constraints/aux metrics in this task.</div>}
      <div className="section-h">Diversity archive {archive && <span className="pill">{archive.niches} niches</span>}</div>
      {archive?.elites?.length
        ? <DataTable caption="Diversity archive elite nodes" card={false}><table className="tbl"><thead><tr><th>node</th><th>metric</th><th>params</th></tr></thead><tbody>
          {archive.elites.map((e, i) => <tr key={i}><td>#{e.node_id}</td><td>{fmt(e.metric)}</td><td className="muted">{JSON.stringify(e.params)}</td></tr>)}</tbody></table></DataTable>
        : <div className="muted">No archive (run not finished).</div>}
      <div className="section-h">Operator productivity</div>
      <DataTable caption="Operator productivity summary" card={false}><table className="tbl"><thead><tr><th>operator</th><th>nodes</th><th>evaluated</th></tr></thead><tbody>
        {Object.entries(ops).map(([o, s]) => <tr key={o}><td>{o}</td><td>{s.n}</td><td>{s.ev}</td></tr>)}</tbody></table></DataTable>
    </Panel>
  )
}

export function DataQualityPanel({ state, onClose }) {
  const prof = state.data_profile
  if (!prof) return <Panel title="Data quality" onClose={onClose}><div className="muted">No data profile (task exposes no dataset).</div></Panel>
  const cols = Object.entries(prof)
  return (
    <Panel title="Data quality" sub={`${cols.length} columns`} onClose={onClose} wide>
      <DataTable caption="Dataset column quality profile" card={false}><table className="tbl"><thead><tr><th>column</th><th>dtype</th><th>missing%</th><th>unique</th><th>min</th><th>max</th><th>mean</th><th>flags</th></tr></thead><tbody>
        {cols.map(([c, s]) => <tr key={c}>
          <td>{c}</td><td>{s.dtype}</td><td>{fmt((s.missing_frac || 0) * 100, 3)}</td><td>{fmtInt(s.n_unique)}</td>
          <td>{fmt(s.min)}</td><td>{fmt(s.max)}</td><td>{fmt(s.mean)}</td>
          <td>{s.constant && <span className="flag">constant </span>}{s.high_missing && <span className="flag">high-missing</span>}</td></tr>)}
      </tbody></table></DataTable>
    </Panel>
  )
}

// Per-run config: shows the run's config.snapshot.json and lets you EDIT it. Edits are saved back to
// the snapshot, which a later RESUME re-reads (resume does NOT pick up the UI's global new-run
// defaults), so this is how you change a specific run's settings (e.g. raise `timeout`, enable timeout
// repair). Works for live runs too: saving the snapshot is safe mid-run (the engine never re-reads it),
// and a "Pause & resume" applies it now by restarting the engine (pause → wait for it to stop → resume).
export function ConfigPanel({
  runId, expectedGeneration, state, live, onClose: closePanel, onToast, draftStore = null,
  navigationGuardOwner = 'panel', publishNavigationGuard = null,
}) {
  const [cfg, setCfg] = useState(null)
  const [settingsSchema, setSettingsSchema] = useState(null)
  const [form, setForm] = useState(null)
  const [loadError, setLoadError] = useState('')
  const [loadNonce, setLoadNonce] = useState(0)
  const [saved, setSaved] = useState(null)   // last-persisted form (to detect unsaved edits)
  const [agentControl, setAgentControl] = useState({})   // per-run governance matrix (agent_control)
  const [savedAC, setSavedAC] = useState({})
  const [configMeta, setConfigMeta] = useState({
    configRevision: '', pinnedFields: new Set(), readOnlyFields: new Set(), mismatchFields: [],
  })
  const [sec, setSec] = useState('')
  const [busy, setBusy] = useState(false)
  const [raw, setRaw] = useState(false)
  const [configMutationUnknown, setConfigMutationUnknown] = useState(null)
  const [invalidFocus, setInvalidFocus] = useState({ key: '', request: 0 })
  const budgetHelpId = useId()
  const budgetInputId = `${budgetHelpId}-input`
  const loadGenerationRef = useRef(0)
  const loadedIdentityRef = useRef({ runId: '', expectedGeneration: '' })
  const mutationRef = useRef(null)
  const configSaveInFlightRef = useRef(false)
  const allowConfigNavigationRef = useRef(false)
  useLayoutEffect(() => {
    allowConfigNavigationRef.current = false
    return () => { allowConfigNavigationRef.current = true }
  }, [])
  const draftScope = configDraftScope(runId)
  const retainedDraftRef = useRef({ scope: '', value: null })
  if (retainedDraftRef.current.scope !== draftScope) {
    retainedDraftRef.current = {
      scope: draftScope,
      value: validConfigDraftEnvelope(
        draftStore?.readField(draftScope, 'draft', null), runId),
    }
  }
  useEffect(() => setSec(''), [runId, expectedGeneration])
  useEffect(() => {
    const requestedGeneration = typeof expectedGeneration === 'string' ? expectedGeneration : ''
    const previousIdentity = loadedIdentityRef.current
    const generation = ++loadGenerationRef.current
    const retainedDraft = retainedDraftRef.current.scope === draftScope
      ? retainedDraftRef.current.value : null
    if (retainedDraft) {
      retainedDraftRef.current = { scope: draftScope, value: null }
      mutationRef.current = null
      configSaveInFlightRef.current = false
      loadedIdentityRef.current = {
        runId, expectedGeneration: retainedDraft.expectedGeneration,
      }
      setBusy(false); setCfg(null); setLoadError('')
      setSettingsSchema(retainedDraft.settingsSchema)
      setForm(publicConfigForm(retainedDraft.form, retainedDraft.settingsSchema))
      setSaved(publicConfigForm(retainedDraft.saved, retainedDraft.settingsSchema))
      setAgentControl(retainedDraft.agentControl); setSavedAC(retainedDraft.savedAC)
      setConfigMeta(publicConfigMeta(retainedDraft.configMeta)); setSec('')
      const generationChanged = retainedDraft.expectedGeneration !== requestedGeneration
      const requiresReconcile = generationChanged
        || retainedDraft.reconcileGeneration === requestedGeneration
      const retainedKeys = [...new Set([
        ...retainedDraft.dirtyKeys,
        ...(retainedDraft.configMutationUnknown?.uncertainKeys || []),
      ])]
      const retainedControlKeys = [...new Set([
        ...retainedDraft.dirtyControlKeys,
        ...(retainedDraft.configMutationUnknown?.uncertainControlKeys || []),
      ])]
      const retainedRecovery = requiresReconcile || retainedDraft.saveInFlight
        ? {
          stage: requiresReconcile ? 'conflict' : 'unknown', runId, generation,
          expectedGeneration: requestedGeneration,
            submittedForm: publicConfigForm(retainedDraft.form, retainedDraft.settingsSchema),
            submittedControl: retainedDraft.agentControl,
            uncertainKeys: retainedKeys,
            uncertainControlKeys: retainedControlKeys,
          }
        : retainedDraft.configMutationUnknown
      setConfigMutationUnknown(retainedRecovery)
      if (requiresReconcile) {
        onToast('The run changed. Your settings draft is retained in this tab; load the current version to review it.')
      } else if (retainedDraft.saveInFlight && !retainedDraft.configMutationUnknown) {
        onToast('A settings save was interrupted. Refresh server state before making another change.')
      }
      return undefined
    }
    const generationChanged = previousIdentity.runId === runId
      && previousIdentity.expectedGeneration
      && previousIdentity.expectedGeneration !== requestedGeneration
    const uncertainKeys = settingsSchema && form && saved
      ? Object.keys(settingsSchema.fieldByKey).filter(
        key => JSON.stringify(form[key]) !== JSON.stringify(saved[key]))
      : []
    const uncertainControlKeys = JSON.stringify(agentControl) !== JSON.stringify(savedAC)
      ? [...new Set([...Object.keys(agentControl), ...Object.keys(savedAC)])] : []
    const retainedKeys = [...new Set([
      ...uncertainKeys, ...(configMutationUnknown?.uncertainKeys || []),
    ])]
    const retainedControlKeys = [...new Set([
      ...uncertainControlKeys, ...(configMutationUnknown?.uncertainControlKeys || []),
    ])]
    // A reset may replace the run while this panel has edits or an uncertain write. Keep that form
    // visibly fenced to its old identity until an explicit authoritative reload rebases the draft.
    if (generationChanged && form && saved
        && (retainedKeys.length || retainedControlKeys.length || busy || configMutationUnknown)) {
      mutationRef.current = null
      setBusy(false); setLoadError('')
      setConfigMutationUnknown({
        stage: 'conflict', runId, generation, expectedGeneration: requestedGeneration,
        submittedForm: publicConfigForm(form, settingsSchema), submittedControl: agentControl,
        uncertainKeys: retainedKeys, uncertainControlKeys: retainedControlKeys,
      })
      onToast('The run changed. Load its current settings and review your retained draft.')
      return undefined
    }
    loadedIdentityRef.current = { runId: '', expectedGeneration: '' }
    // A reused panel must never display or reconcile the previous run while the next config loads.
    mutationRef.current = null
    configSaveInFlightRef.current = false
    setBusy(false); setCfg(null); setSettingsSchema(null); setForm(null); setSaved(null); setLoadError('')
    setConfigMutationUnknown(null)
    setAgentControl({}); setSavedAC({})
    setConfigMeta({ configRevision: '', pinnedFields: new Set(), readOnlyFields: new Set(), mismatchFields: [] })
    if (!RUN_GENERATION_RE.test(requestedGeneration)) {
      setLoadError('Run identity is not available yet. Wait for the current run state and retry.')
      return undefined
    }
    const configRequest = deadlineGet(runApiPath(runId, '/config'), PANEL_REQUEST_TIMEOUT_MS)
    Promise.all([
      configRequest.promise,
      loadSettingsSchema({ reload: loadNonce > 0 }),
    ]).then(([c, nextSchema]) => {
      if (configRequest.controller.signal.aborted || generation !== loadGenerationRef.current) return
      const parsed = splitRunConfigPayload(c, nextSchema)
      loadedIdentityRef.current = { runId, expectedGeneration: requestedGeneration }
      setCfg(parsed.config); setConfigMeta(parsed)
      setSettingsSchema(nextSchema)
      const f = toForm(parsed.config, nextSchema); setForm(f); setSaved(f)
      const ac = parsed.config.agent_control || {}; setAgentControl(ac); setSavedAC(ac)
    }).catch(error => {
      if (error?.name !== 'AbortError' && generation === loadGenerationRef.current) {
        setCfg(null)
        setLoadError('Run settings could not be loaded. Check the connection and retry.')
      }
    })
    return () => configRequest.controller.abort()
  }, [runId, expectedGeneration, loadNonce, draftScope])

  // A live engine keeps its in-memory settings until it restarts; gate on `live` (not the possibly
  // historical `state`) so time-travel doesn't misreport liveness.
  const engineLive = live?.engine_running === true
  const engineStopped = live?.engine_running === false
  const loadedIdentity = loadedIdentityRef.current
  const configIdentityReady = loadedIdentity.runId === runId
    && loadedIdentity.expectedGeneration === expectedGeneration
    && RUN_GENERATION_RE.test(loadedIdentity.expectedGeneration)
  const controlBusy = busy || !!configMutationUnknown || !configIdentityReady
  const liveEvalSeconds = Number(live?.total_eval_seconds ?? state?.total_eval_seconds)
  const runtimeEvalCeiling = Number(
    live?.budget_overrides?.max_eval_seconds ?? state?.budget_overrides?.max_eval_seconds)
  const configuredEvalCeiling = Number(cfg?.max_eval_seconds)
  const hasRuntimeEvalCeiling = Number.isFinite(runtimeEvalCeiling) && runtimeEvalCeiling > 0
  const snapshotEvalCeilingKnown = engineStopped && cfg !== null
  // The snapshot can be edited while an engine is live, but that engine keeps the settings it
  // launched with until restart. Without a folded runtime override the active ceiling is therefore
  // unknown here; presenting the mutable snapshot as current would make lowering warnings dishonest.
  const currentEvalCeiling = hasRuntimeEvalCeiling
    ? runtimeEvalCeiling
    : snapshotEvalCeilingKnown && Number.isFinite(configuredEvalCeiling) && configuredEvalCeiling > 0
      ? configuredEvalCeiling : null
  const currentEvalCeilingUnknown = !snapshotEvalCeilingKnown && !hasRuntimeEvalCeiling
  const requestedEvalCeiling = Number(sec)
  const knownEvalSeconds = Number.isFinite(liveEvalSeconds) && liveEvalSeconds >= 0
    ? liveEvalSeconds : null
  const hasCeilingInput = sec.trim() !== ''
  const validEvalCeiling = hasCeilingInput
    && Number.isFinite(requestedEvalCeiling)
    && requestedEvalCeiling > 0
    && requestedEvalCeiling <= 1_000_000_000_000
  const unchangedEvalCeiling = validEvalCeiling
    && currentEvalCeiling != null && requestedEvalCeiling === currentEvalCeiling
  const exhaustedEvalCeiling = validEvalCeiling
    && knownEvalSeconds != null && requestedEvalCeiling <= knownEvalSeconds
  const loweringEvalCeiling = validEvalCeiling
    && currentEvalCeiling != null && requestedEvalCeiling < currentEvalCeiling
  const replacingUnknownEvalCeiling = validEvalCeiling && currentEvalCeilingUnknown
  let budgetHelp = currentEvalCeilingUnknown
    ? 'The applied engine ceiling is not available in the latest state. '
      + 'Setting a cumulative total replaces it immediately.'
    : currentEvalCeiling == null
      ? 'Current ceiling is unbounded. Enter a cumulative total to create a finite limit.'
    : `Current ceiling ${fmtElapsedSeconds(currentEvalCeiling)}. Setting a value replaces this limit.`
  if (knownEvalSeconds != null) {
    budgetHelp += ` ${fmtElapsedSeconds(knownEvalSeconds)} spent in the latest state.`
  }
  if (hasCeilingInput && !validEvalCeiling) {
    budgetHelp = 'Enter a finite positive ceiling no greater than 1,000,000,000,000 seconds.'
  } else if (unchangedEvalCeiling) {
    budgetHelp = `The eval ceiling is already ${fmtElapsedSeconds(requestedEvalCeiling)}.`
  } else if (exhaustedEvalCeiling) {
    budgetHelp = `The latest state has already spent ${fmtElapsedSeconds(knownEvalSeconds)}; `
      + 'this ceiling will stop new evaluations at the next budget check.'
  } else if (loweringEvalCeiling) {
    budgetHelp = `Based on the latest loaded state, this lowers the ceiling by `
      + `${fmtElapsedSeconds(currentEvalCeiling - requestedEvalCeiling)}.`
  }
  const budgetHelpTone = hasCeilingInput && !validEvalCeiling
    ? ' error'
    : loweringEvalCeiling || exhaustedEvalCeiling || replacingUnknownEvalCeiling ? ' warning' : ''
  const resumeLabels = { success: 'Resumed with the saved settings', noop: 'Run was already running',
    executing: 'Resume requested — waiting for the engine to load the saved settings', failure: 'Resume failed' }
  const restartLabels = { success: 'Restarted with the saved settings', noop: 'Restart was already satisfied',
    executing: 'Restart requested — the current experiment will stop before a replacement engine loads the saved settings',
    failure: 'Restart failed' }
  const acceptResume = async (expectedGeneration = loadGenerationRef.current, requestedRunId = runId) => {
    const record = await CONTROL.resume(requestedRunId)
    if (expectedGeneration !== loadGenerationRef.current) return null
    const feedback = commandFeedback(record, resumeLabels)
    onToast(feedback.message)
    return feedback
  }
  const dirty = useMemo(() => {
    if (!form || !saved || !settingsSchema) return new Set()
    const cur = fromForm(form, settingsSchema, { allowClear: false })
    const base = fromForm(saved, settingsSchema, { allowClear: false }), s = new Set()
    for (const k of Object.keys(settingsSchema.fieldByKey)) {
      if (JSON.stringify(cur[k]) !== JSON.stringify(base[k])) s.add(k)
    }
    return s
  }, [form, saved, settingsSchema])
  const acDirty = useMemo(() => JSON.stringify(agentControl) !== JSON.stringify(savedAC), [agentControl, savedAC])
  const validationErrors = useMemo(() => form && settingsSchema
    ? settingsValidationErrors(form, settingsSchema, { allowClear: false }) : {}, [form, settingsSchema])
  const invalidCount = Object.keys(validationErrors).length
  const hasChanges = dirty.size > 0 || acDirty
  const canSave = hasChanges && invalidCount === 0
  const configNavigationUnsafe = hasChanges || busy || !!configMutationUnknown
  const configNavigationSummary = [
    hasChanges ? 'This Run settings panel has unsaved changes.' : '',
    configMutationUnknown?.stage === 'conflict'
      ? 'The server version changed while this draft was open.'
      : configMutationUnknown
        ? 'The last Run settings save may or may not have reached the server.' : '',
    busy ? 'A Run settings operation is still in progress; its server-side outcome may arrive after this view closes.' : '',
  ].filter(Boolean).join(' ')
  const configCloseMessage = `${configNavigationSummary} Closing it discards this panel's client-only state. Close the Run settings panel anyway?`
  const configLeaveSummary = `${configNavigationSummary} Leaving this run discards this panel's client-only state.`
  const writeConfigDraft = (overrides = {}) => {
    if (!draftStore || allowConfigNavigationRef.current) return
    const nextForm = Object.hasOwn(overrides, 'form') ? overrides.form : form
    const nextSaved = Object.hasOwn(overrides, 'saved') ? overrides.saved : saved
    const nextAgentControl = Object.hasOwn(overrides, 'agentControl')
      ? overrides.agentControl : agentControl
    const nextSavedAC = Object.hasOwn(overrides, 'savedAC') ? overrides.savedAC : savedAC
    const nextRecovery = Object.hasOwn(overrides, 'configMutationUnknown')
      ? overrides.configMutationUnknown : configMutationUnknown
    const saveInFlight = Object.hasOwn(overrides, 'saveInFlight')
      ? overrides.saveInFlight : configSaveInFlightRef.current
    if (!nextForm || !nextSaved || !settingsSchema) return
    const currentRecord = fromForm(nextForm, settingsSchema, { allowClear: false })
    const savedRecord = fromForm(nextSaved, settingsSchema, { allowClear: false })
    const dirtyKeys = Object.keys(settingsSchema.fieldByKey).filter(
      key => JSON.stringify(currentRecord[key]) !== JSON.stringify(savedRecord[key]))
    const nextAcDirty = JSON.stringify(nextAgentControl) !== JSON.stringify(nextSavedAC)
    const dirtyControlKeys = nextAcDirty
      ? [...new Set([...Object.keys(nextAgentControl), ...Object.keys(nextSavedAC)])] : []
    if (!dirtyKeys.length && !nextAcDirty && !nextRecovery && !saveInFlight) {
      if (configIdentityReady) draftStore.clear(draftScope)
      return
    }
    const identityGeneration = loadedIdentityRef.current.expectedGeneration || expectedGeneration
    if (!RUN_GENERATION_RE.test(identityGeneration || '')) return
    const storedRecovery = nextRecovery ? {
      ...nextRecovery,
      submittedForm: publicConfigForm(nextRecovery.submittedForm, settingsSchema),
    } : null
    draftStore.updateField(draftScope, 'draft', {
      schema: CONFIG_DRAFT_SCHEMA,
      unsafe: true,
      runId: String(runId),
      expectedGeneration: identityGeneration,
      settingsSchema,
      form: publicConfigForm(nextForm, settingsSchema),
      saved: publicConfigForm(nextSaved, settingsSchema),
      agentControl: nextAgentControl,
      savedAC: nextSavedAC,
      configMeta: publicConfigMeta(configMeta),
      saveInFlight,
      dirtyKeys,
      dirtyControlKeys,
      configMutationUnknown: storedRecovery,
    }, null)
  }
  useEffect(() => {
    writeConfigDraft()
  }, [agentControl, busy, configIdentityReady, configMeta, configMutationUnknown, form, saved,
    savedAC, settingsSchema])
  useLayoutEffect(() => {
    if (navigationGuardOwner !== 'run' || typeof publishNavigationGuard !== 'function') {
      return undefined
    }
    return publishNavigationGuard({
      route: 'config', unsafe: configNavigationUnsafe,
      closeMessage: configCloseMessage, leaveSummary: configLeaveSummary,
      dispose: () => {
        allowConfigNavigationRef.current = true
        draftStore?.clear(draftScope)
      },
    })
  }, [navigationGuardOwner, publishNavigationGuard, configNavigationUnsafe,
    configCloseMessage, configLeaveSummary, draftStore, draftScope])
  useEffect(() => {
    if (navigationGuardOwner === 'run' || !configNavigationUnsafe) {
      return undefined
    }
    const guardedHash = location.hash
    return installNavigationLossGuard({
      allowRef: allowConfigNavigationRef,
      guardedHash,
      message: () => {
        const warning = configMutationUnknown?.stage === 'conflict'
          ? 'The server version changed while this draft was open.'
          : configMutationUnknown
            ? 'The last run-settings save may or may not have reached the server.'
          : busy ? 'A run-settings operation is still in progress.'
            : 'This run-settings panel has unsaved changes.'
        return `${warning} Leave this run anyway?`
      },
      onAllow: () => draftStore?.clear(draftScope),
    })
  }, [navigationGuardOwner, configNavigationUnsafe, busy, configMutationUnknown, draftScope,
    draftStore])
  const onChange = (k, v) => {
    const next = { ...form, [k]: v }
    writeConfigDraft({ form: next })
    setForm(next)
  }
  const onToggleAgent = (key, role) => {
    const cur = new Set(agentControl[key] || [])
    cur.has(role) ? cur.delete(role) : cur.add(role)
    const next = { ...agentControl, [key]: [...cur] }
    writeConfigDraft({ agentControl: next })
    setAgentControl(next)
  }
  const beginMutation = (kind = 'control', reconcileGeneration = '') => {
    if (mutationRef.current || (configMutationUnknown && kind !== 'reconciling')) return null
    const identity = loadedIdentityRef.current
    const mutationGeneration = kind === 'reconciling'
      ? reconcileGeneration : identity.expectedGeneration
    if (identity.runId !== runId || !RUN_GENERATION_RE.test(mutationGeneration)
        || mutationGeneration !== expectedGeneration
        || (kind !== 'reconciling' && identity.expectedGeneration !== expectedGeneration)) return null
    const token = {
      generation: loadGenerationRef.current, runId,
      expectedGeneration: mutationGeneration, kind,
    }
    mutationRef.current = token
    setBusy(true)
    return token
  }
  const finishMutation = token => {
    if (mutationRef.current !== token) return
    mutationRef.current = null
    if (token.generation === loadGenerationRef.current) setBusy(false)
  }
  const focusFirstInvalid = () => {
    const key = Object.keys(validationErrors)[0]
    if (!key) return
    setRaw(false)
    setInvalidFocus(previous => ({ key, request: previous.request + 1 }))
  }
  const rememberUnknownSave = (stage, submittedForm, submittedControl, submittedRunId, mutation,
    uncertainKeys, uncertainControlKeys) => {
    if (allowConfigNavigationRef.current || mutation.generation !== loadGenerationRef.current
        || submittedRunId !== runId
        || mutation.expectedGeneration !== expectedGeneration) return
    const recovery = {
      stage,
      runId: submittedRunId,
      generation: mutation.generation,
      expectedGeneration: mutation.expectedGeneration,
      submittedForm: publicConfigForm(toForm(
        fromForm(submittedForm, settingsSchema, { allowClear: false }), settingsSchema), settingsSchema),
      submittedControl,
      uncertainKeys,
      uncertainControlKeys,
    }
    setConfigMutationUnknown(recovery)
    writeConfigDraft({ configMutationUnknown: recovery, saveInFlight: true })
    onToast(stage === 'conflict'
      ? 'Run settings changed elsewhere. Load the current server version and review your retained draft.'
      : 'Save outcome unknown. Refresh the server state before making another change.')
  }
  const reconcileUnknownSave = async () => {
    const recovery = configMutationUnknown
    if (!recovery) return
    const mutation = beginMutation('reconciling', recovery.expectedGeneration)
    if (!mutation) return
    const request = deadlineGet(
      runApiPath(recovery.runId, '/config'), PANEL_REQUEST_TIMEOUT_MS)
    try {
      const response = await request.promise
      if (mutation.generation !== loadGenerationRef.current || recovery.runId !== runId
          || mutation.expectedGeneration !== expectedGeneration) return
      const parsed = splitRunConfigPayload(response, settingsSchema)
      const acceptedForm = toForm(parsed.config, settingsSchema)
      const acceptedControl = parsed.config.agent_control || {}
      loadedIdentityRef.current = {
        runId: recovery.runId, expectedGeneration: mutation.expectedGeneration,
      }
      setCfg(parsed.config); setConfigMeta(parsed); setSaved(acceptedForm); setSavedAC(acceptedControl)
      setForm(current => reconcileUnknownRecord(
        current, recovery.submittedForm, acceptedForm, recovery.uncertainKeys,
      ))
      setAgentControl(current => reconcileUnknownRecord(
        current, recovery.submittedControl, acceptedControl, recovery.uncertainControlKeys,
      ))
      setConfigMutationUnknown(null)
      onToast(recovery.stage === 'conflict'
        ? 'Current server settings loaded. Review the retained draft before saving again.'
        : 'Server state refreshed. The uncertain save was not replayed.')
    } catch (error) {
      if (mutation.generation === loadGenerationRef.current && recovery.runId === runId) {
        onToast('Server state is still unavailable: ' + (error.message || error))
      }
    } finally {
      finishMutation(mutation)
    }
  }
  const onSave = async () => {
    if (configMutationUnknown || !configIdentityReady) {
      onToast('Load the current server settings before saving this retained draft.')
      return
    }
    if (invalidCount) {
      focusFirstInvalid()
      onToast('Fix invalid settings before saving')
      return
    }
    const submittedForm = form
    const submittedControl = agentControl
    const submittedRevision = configMeta.configRevision
    const cur = fromForm(submittedForm, settingsSchema, { allowClear: false }), changed = {}
    for (const k of dirty) changed[k] = cur[k]    // send ONLY edited fields (minimal snapshot diff)
    if (acDirty) changed.agent_control = submittedControl
    if (!Object.keys(changed).length) return
    const mutation = beginMutation('save')
    if (!mutation) return
    configSaveInFlightRef.current = true
    writeConfigDraft({
      form: submittedForm, agentControl: submittedControl, saveInFlight: true,
    })
    const submittedRunId = mutation.runId
    try {
      const write = deadlineRequest(
        signal => saveRunConfig(submittedRunId, changed, {
          signal, expectedRevision: submittedRevision,
          expectedGeneration: mutation.expectedGeneration,
        }), PANEL_REQUEST_TIMEOUT_MS,
      )
      const r = validateRunConfigSaveAck(await write.promise, settingsSchema)
      if (mutation.generation !== loadGenerationRef.current
          || loadedIdentityRef.current.expectedGeneration !== mutation.expectedGeneration) return
      const parsed = splitRunConfigPayload(r.config, settingsSchema)
      const acceptedForm = toForm(parsed.config, settingsSchema)
      const acceptedControl = parsed.config.agent_control || {}
      setCfg(parsed.config); setConfigMeta(parsed); setSaved(acceptedForm); setSavedAC(acceptedControl)
      setForm(current => reconcileAcceptedRecord(current, submittedForm, acceptedForm))
      setAgentControl(current => reconcileAcceptedRecord(current, submittedControl, acceptedControl))
      const repaired = r.normalized_pinned?.length
        ? `; repaired legacy snapshot drift in ${r.normalized_pinned.join(', ')}` : ''
      const what = (r.changed?.length ? `saved ${r.changed.join(', ')}` : 'saved') + repaired
      onToast(what + (r.engine_running ? ' — applies when the live run restarts' : ' — applies on next resume'))
    } catch (e) {
      const disposition = e?.status === 409 && e?.code === 'run_generation_changed'
        ? 'conflict' : runConfigWriteDisposition(e)
      if (disposition === 'conflict') {
        rememberUnknownSave(
          'conflict', submittedForm, submittedControl, submittedRunId, mutation,
          Object.keys(changed).filter(key => key !== 'agent_control'),
          acDirty ? [...new Set([...Object.keys(submittedControl), ...Object.keys(savedAC)])] : [],
        )
      } else if (disposition === 'unknown') {
        rememberUnknownSave(
          'unknown', submittedForm, submittedControl, submittedRunId, mutation,
          Object.keys(changed).filter(key => key !== 'agent_control'),
          acDirty ? [...new Set([...Object.keys(submittedControl), ...Object.keys(savedAC)])] : [],
        )
      } else if (mutation.generation === loadGenerationRef.current) {
        onToast('save failed: ' + e.message)
      }
    } finally {
      configSaveInFlightRef.current = false
      finishMutation(mutation)
    }
  }
  const onResume = async () => {           // stalled/finished: just spawn the engine (re-reads the snapshot)
    const mutation = beginMutation()
    if (!mutation) return
    try {
      await acceptResume(mutation.generation, runId)
    } catch (e) {
      if (mutation.generation === loadGenerationRef.current) onToast('Resume failed: ' + e.message)
    }
    finally { finishMutation(mutation) }
  }
  const onPauseResume = async () => {
    const mutation = beginMutation()
    if (!mutation) return
    const submittedRunId = runId
    try {
      // This is one durable command/postcondition. Never restore a client-side
      // pause-then-resume saga here: unmounting between commands would strand the accepted intent.
      const record = await CONTROL.restart(submittedRunId)
      if (mutation.generation !== loadGenerationRef.current) return
      const feedback = commandFeedback(record, restartLabels)
      onToast(feedback.message)
    } catch (e) {
      if (mutation.generation === loadGenerationRef.current) onToast('Pause/resume failed: ' + e.message)
    }
    finally { finishMutation(mutation) }
  }
  const setEvalCeiling = async () => {
    if (!validEvalCeiling || unchangedEvalCeiling || controlBusy) return
    const mutation = beginMutation()
    if (!mutation) return
    const submittedRunId = runId
    const submittedInput = sec
    const submittedCeiling = requestedEvalCeiling
    try {
      const record = await CONTROL.setEvalCeiling(submittedRunId, submittedCeiling)
      if (mutation.generation !== loadGenerationRef.current) return
      const feedback = commandFeedback(record, {
        success: `Eval ceiling set to ${submittedCeiling}s`,
        noop: `Eval ceiling is already ${submittedCeiling}s`,
        executing: `Eval ceiling change to ${submittedCeiling}s requested`,
        failure: 'Eval ceiling change failed',
      })
      if (feedback.kind === 'success') {
        setSec(current => current === submittedInput ? '' : current)
      }
      onToast(feedback.message)
    } catch (error) {
      if (mutation.generation === loadGenerationRef.current) onToast(`Eval ceiling change failed: ${error.message || error}`)
    }
    finally { finishMutation(mutation) }
  }
  const revertConfigDraft = () => {
    setForm(saved)
    setAgentControl(savedAC)
    writeConfigDraft({
      form: saved, agentControl: savedAC, configMutationUnknown: null, saveInFlight: false,
    })
  }
  const requestClose = () => {
    if (navigationGuardOwner === 'run') {
      if (closePanel?.() !== false) allowConfigNavigationRef.current = true
      return
    }
    if (!hasChanges && !busy && !configMutationUnknown) {
      draftStore?.clear(draftScope)
      closePanel()
      return
    }
    const warning = configMutationUnknown?.stage === 'conflict'
      ? 'The server version changed while this draft was open.'
      : configMutationUnknown
        ? 'The last save may or may not have reached the server.'
      : busy ? 'A settings operation is still in progress.' : 'This panel has unsaved changes.'
    if (window.confirm(`${warning} Close the run settings panel anyway?`)) {
      allowConfigNavigationRef.current = true
      draftStore?.clear(draftScope)
      closePanel()
    }
  }
  // PanelShell routes Escape, backdrop clicks, and its close button through this single guard.
  const onClose = requestClose

  const rawTable = <DataTable caption="Raw run configuration" card={false}><table className="tbl"><tbody>{cfg && Object.entries(cfg).map(([k, v]) =>
    <tr key={k}><th scope="row" className="muted">{k}</th><td>{typeof v === 'object' ? JSON.stringify(v) : String(v)}</td></tr>)}</tbody></table></DataTable>

  return (
    <Panel title="Run settings" sub={engineLive ? 'live · applies on restart'
      : engineStopped ? 'edit · applies on resume' : 'engine status unknown'} onClose={onClose} wide>
      <form className="toolbar" style={{ marginBottom: 12 }}
        onSubmit={event => { event.preventDefault(); setEvalCeiling() }}>
        <label className="muted" htmlFor={budgetInputId}>set eval ceiling:</label>
        <input id={budgetInputId} className="text" style={{ width: 140 }} type="number"
          max="1000000000000" step="any" inputMode="decimal"
          aria-label="Cumulative evaluation budget ceiling in seconds"
          aria-describedby={budgetHelpId}
          aria-invalid={hasCeilingInput && !validEvalCeiling ? 'true' : undefined}
          placeholder="total seconds" value={sec} disabled={controlBusy}
          onChange={e => setSec(e.target.value)} />
        <button className={'btn sm primary'
          + (loweringEvalCeiling || exhaustedEvalCeiling || replacingUnknownEvalCeiling ? ' warn' : '')}
          type="submit"
          disabled={!validEvalCeiling || unchangedEvalCeiling || controlBusy}
        >set ceiling</button>
        <span id={budgetHelpId} className={'budget-ceiling-help' + budgetHelpTone}
          role={hasCeilingInput ? 'status' : undefined}>
          {budgetHelp}
        </span>
      </form>
      {configMutationUnknown && <div className="report-inline-state error" role="alert" style={{ marginBottom: 12 }}>
        <OpIcon name="alert" size={14} />
        {configMutationUnknown.stage === 'conflict'
          ? <span><b>Run settings changed elsewhere.</b> Load the current server version, then review
              your retained draft before saving it against the new version.</span>
          : <span><b>Save outcome unknown.</b> The request timed out or lost its response. Refresh the
              authoritative server state; this client will not replay the save automatically.</span>}
        <button className="btn sm" disabled={busy} onClick={reconcileUnknownSave}>
          {configMutationUnknown.stage === 'conflict' ? 'Load current version' : 'Refresh server state'}
        </button>
      </div>}
      {!form || !settingsSchema ? (loadError
        ? <div className="report-inline-state error" role="alert">
            <OpIcon name="alert" size={14} /><span>{loadError}</span>
            <button className="btn sm" onClick={() => setLoadNonce(value => value + 1)}>Retry</button>
          </div>
        : <div className="muted" role="status">Loading run settings…</div>) : <>
        <div className="notice" style={{ marginBottom: 10 }}>
          {engineLive
            ? <>This run is <b>live</b>. Saving updates its <code>config.snapshot.json</code>, but the running engine keeps its current settings until it restarts — use <b>Pause &amp; resume</b> to stop it (the current experiment finishes first) and continue with the new settings.</>
            : <>Edits are saved to this run's <code>config.snapshot.json</code> and applied on the next <b>resume</b>.</>}
          {' '}<span className="sf-dot unsaved">●</span> = changed.
        </div>
        {configMeta.pinnedFields.size > 0 && <div className="notice" role="note" style={{ marginBottom: 10 }}>
          Fields marked <b>launch-pinned</b> show the values recorded in this run's event log and cannot
          be changed on resume. Start a new run to change holdout or verifier semantics.
          {configMeta.mismatchFields.length > 0 && <>
            {' '}A legacy snapshot disagrees for {configMeta.mismatchFields.join(', ')}; the effective
            launch values are shown and will be repaired when another editable setting is saved.
          </>}
        </div>}
        <div className="toolbar" style={{ marginBottom: 10 }}>
          <span className="spacer" style={{ flex: 1 }} />
          <button className="btn sm ghost" disabled={!cfg}
            title={!cfg ? 'Load the current server version before viewing raw settings' : undefined}
            onClick={() => setRaw(r => !r)}>{raw ? 'form' : 'raw'}</button>
          {invalidCount > 0 && <button type="button"
            className="settings-summary-link settings-save-state is-invalid"
            onClick={focusFirstInvalid}>
            {invalidCount} invalid setting{invalidCount === 1 ? '' : 's'} — review
          </button>}
          <button className="btn sm ghost" disabled={controlBusy || !hasChanges}
            onClick={revertConfigDraft}>↺ revert</button>
          <button className="btn sm primary" disabled={controlBusy || !canSave} onClick={onSave}>Save</button>
          {engineLive
            ? <button className="btn sm" disabled={controlBusy || hasChanges} onClick={onPauseResume} title="pause the run, then resume it with the saved settings">Pause &amp; resume ▸</button>
            : <button className="btn sm" disabled={controlBusy || hasChanges} onClick={onResume} title="continue this run with the saved settings">Resume ▸</button>}
        </div>
        {/* This panel's `dirty` is changed-vs-saved (unsaved), so feed it as `unsaved` → the amber dot that clears on Save. */}
        {raw ? rawTable : <SettingsForm form={form} onChange={onChange} unsaved={dirty}
          errors={validationErrors} agentControl={agentControl} onToggleAgent={onToggleAgent}
          readOnlyKeys={configMeta.readOnlyFields} hideSecret schema={settingsSchema}
          focusKey={invalidFocus.key} focusRequest={invalidFocus.request} />}
      </>}
    </Panel>
  )
}

export function AuthoringPanel({
  onClose, onToast, draftStore: sharedDraftStore = null, navigationGuardOwner = 'panel',
  publishNavigationGuard = null,
}) {
  const [kind, setKind] = useState('prompts')
  const [selectedScope, setSelectedScope] = useState(null)
  const fallbackDraftStoreRef = useRef(null)
  if (!fallbackDraftStoreRef.current) fallbackDraftStoreRef.current = createInspectorDraftStore()
  const draftStore = sharedDraftStore || fallbackDraftStoreRef.current
  const allowNavigationRef = useRef(false)
  const [documents, setStoredDocuments] = useInspectorDraftField(
    draftStore, AUTHORING_PANEL_DRAFT_SCOPE, 'documents', {},
  )
  const documentsRef = useRef(documents)
  documentsRef.current = documents
  const setDocuments = update => {
    if (allowNavigationRef.current) return documentsRef.current
    return setStoredDocuments(update)
  }
  const [saveState, setSaveState] = useState(null)
  const [reconciledSource, setReconciledSource] = useState(null)
  const initialRecoveryRef = useRef(null)
  if (initialRecoveryRef.current == null) initialRecoveryRef.current = inspectAuthoringOperations()
  const initialUncertainSavesRef = useRef(null)
  if (initialUncertainSavesRef.current == null) {
    initialUncertainSavesRef.current = Object.fromEntries(
      Object.entries(initialRecoveryRef.current.valid).map(([scope, recovery]) => [scope, {
        ...recovery, phase: 'unknown', inspectedMissing: false,
        releaseAllowed: false, releaseInspected: false,
        message: `A saved operation for ${recovery.name} may still be pending. Check its exact durable receipt.`,
      }]),
    )
  }
  const [uncertainSaves, setStoredUncertainSaves] = useInspectorDraftField(
    draftStore, AUTHORING_PANEL_DRAFT_SCOPE, 'uncertainSaves', initialUncertainSavesRef.current,
  )
  const [damagedRecoveries, setStoredDamagedRecoveries] = useInspectorDraftField(
    draftStore, AUTHORING_PANEL_DRAFT_SCOPE, 'damagedRecoveries',
    initialRecoveryRef.current.damaged,
  )
  const [storageAvailable, setStorageAvailable] = useState(initialRecoveryRef.current.available)
  const uncertainSavesRef = useRef(uncertainSaves)
  uncertainSavesRef.current = uncertainSaves
  const damagedRecoveriesRef = useRef(damagedRecoveries)
  damagedRecoveriesRef.current = damagedRecoveries
  const setUncertainSaves = update => {
    if (allowNavigationRef.current) return uncertainSavesRef.current
    return setStoredUncertainSaves(update)
  }
  const setDamagedRecoveries = update => {
    if (allowNavigationRef.current) return damagedRecoveriesRef.current
    return setStoredDamagedRecoveries(update)
  }
  const saveRef = useRef(null)
  const activeRef = useRef(true)
  const [source, retry] = usePanelResource(signal => get(`/api/${kind}`, { signal }), authoringPayload, kind)
  const data = source.data || { dir: null, targetRootId: null, files: [], truncatedFiles: 0 }
  const scopeFor = authoringScope
  useEffect(() => {
    // Materialize the initial lazy-panel fallback in RunView's shared store. A direct storage scan
    // covers an even earlier generation replacement; subsequent recovery changes are synchronous.
    setStoredUncertainSaves(current => current)
    setStoredDamagedRecoveries(current => current)
  }, [setStoredDamagedRecoveries, setStoredUncertainSaves])
  useLayoutEffect(() => {
    activeRef.current = true
    allowNavigationRef.current = false
    return () => {
      activeRef.current = false
      allowNavigationRef.current = true
    }
  }, [])
  useEffect(() => {
    if (source.state !== 'ready') {
      setReconciledSource(null)
      return
    }
    setDocuments(current => {
      let changed = false
      const next = { ...current }
      for (const file of data.files || []) {
        const scope = scopeFor(kind, file.name)
        const previous = current[scope]
        if (!previous) {
          next[scope] = {
            kind, name: file.name, savedText: file.text, draftText: file.text,
            savedRevision: file.revision, observedText: file.text, observedRevision: file.revision,
            savedTargetRootId: data.targetRootId, observedTargetRootId: data.targetRootId,
            truncated: file.truncated, conflict: false, rootConflict: false,
            observationIncomplete: false, error: '',
            recoveryOperationId: null, recoveryStorageRaw: null,
          }
          changed = true
        } else if (previous.draftText === previous.savedText && !uncertainSaves[scope]) {
          if (previous.savedText !== file.text || previous.observedText !== file.text
              || previous.savedRevision !== file.revision || previous.truncated !== file.truncated
              || previous.savedTargetRootId !== data.targetRootId
              || previous.observedTargetRootId !== data.targetRootId
              || previous.conflict || previous.rootConflict
              || previous.observationIncomplete || previous.error) {
            next[scope] = { ...previous, savedText: file.text, draftText: file.text,
              savedRevision: file.revision, observedText: file.text, observedRevision: file.revision,
              savedTargetRootId: data.targetRootId, observedTargetRootId: data.targetRootId,
              truncated: file.truncated, conflict: false, rootConflict: false,
              observationIncomplete: false, error: '' }
            changed = true
          }
        } else if (previous.observedText !== file.text
            || previous.observedRevision !== file.revision
            || previous.observedTargetRootId !== data.targetRootId
            || previous.observationIncomplete
            || previous.conflict !== (previous.savedRevision !== file.revision
              || previous.savedTargetRootId !== data.targetRootId)) {
          // A refresh must never replace local text. Remember the newly observed server version and
          // make the conflict explicit while preserving both the draft and its original baseline.
          const rootConflict = previous.savedTargetRootId !== data.targetRootId
          next[scope] = { ...previous,
            observedText: file.text, observedRevision: file.revision,
            observedTargetRootId: data.targetRootId, truncated: file.truncated,
            rootConflict, observationIncomplete: false,
            conflict: rootConflict || previous.savedRevision !== file.revision }
          changed = true
        }
      }
      const visibleNames = new Set((data.files || []).map(file => file.name))
      const listComplete = data.truncatedFiles === 0
      for (const [scope, previous] of Object.entries(current)) {
        if (previous.kind !== kind || visibleNames.has(previous.name)
            || uncertainSaves[scope]) continue
        // A retained document that disappeared from the response must be rebound even while clean.
        // Otherwise an edit made after this refresh would still submit the old root/revision. Only a
        // complete list proves absence; a capped list leaves the observation unknown and Save disabled.
        const observedRevision = validAuthoringTargetRootId(data.targetRootId) && listComplete
          ? 'missing' : null
        const rootConflict = previous.savedTargetRootId !== data.targetRootId
        const observationIncomplete = validAuthoringTargetRootId(data.targetRootId) && !listComplete
        const conflict = rootConflict || observationIncomplete
          || previous.savedRevision !== observedRevision
        if (previous.observedText !== null || previous.observedRevision !== observedRevision
            || previous.observedTargetRootId !== data.targetRootId
            || previous.rootConflict !== rootConflict || previous.conflict !== conflict
            || previous.observationIncomplete !== observationIncomplete) {
          next[scope] = {
            ...previous, observedText: null, observedRevision,
            observedTargetRootId: data.targetRootId, rootConflict, conflict,
            observationIncomplete,
          }
          changed = true
        }
      }
      for (const recovery of Object.values(uncertainSaves)) {
        if (recovery.kind !== kind) continue
        const scope = recovery.scope
        const previous = next[scope]
        const sameRoot = data.targetRootId === recovery.expectedTargetRootId
        const file = sameRoot
          ? (data.files || []).find(candidate => candidate.name === recovery.name) : null
        const observationIncomplete = sameRoot && !file && !listComplete
        const observedRevision = sameRoot
          ? (file ? file.revision : (listComplete ? 'missing' : null)) : null
        const sameRecovery = previous?.recoveryOperationId === recovery.operationId
          && previous?.recoveryStorageRaw === recovery.storageRaw
        const hydrated = {
          ...(sameRecovery ? previous : {}),
          kind: recovery.kind, name: recovery.name,
          savedText: sameRecovery ? previous.savedText : (file?.text ?? ''),
          draftText: sameRecovery ? previous.draftText : recovery.submittedText,
          savedRevision: recovery.expectedRevision,
          observedText: file?.text ?? null, observedRevision,
          savedTargetRootId: recovery.expectedTargetRootId,
          observedTargetRootId: data.targetRootId,
          truncated: file?.truncated === true, rootConflict: !sameRoot,
          observationIncomplete,
          conflict: !sameRoot || observationIncomplete
            || (observedRevision !== recovery.expectedRevision
            && observedRevision !== recovery.desiredRevision),
          error: recovery.message, recoveryOperationId: recovery.operationId,
          recoveryStorageRaw: recovery.storageRaw,
        }
        if (!previous || Object.keys(hydrated).some(key => hydrated[key] !== previous[key])) {
          next[scope] = hydrated
          changed = true
        }
      }
      return changed ? next : current
    })
    if (!allowNavigationRef.current) {
      setReconciledSource(current => current?.kind === kind && current?.data === source.data
        ? current : { kind, data: source.data })
    }
  }, [kind, source.state, source.data, uncertainSaves])
  const selected = selectedScope ? documents[selectedScope] || null : null
  const sourceReconciled = source.state === 'ready'
    && reconciledSource?.kind === kind && reconciledSource?.data === source.data
  const selectedSourceReconciled = sourceReconciled && selected?.kind === kind
  const selectedUncertainSave = selectedScope ? uncertainSaves[selectedScope] || null : null
  const damagedRows = Object.values(damagedRecoveries)
  const selectedDamagedRecovery = damagedRows.find(recovery => !recovery.identity
    || recovery.identity.scope === selectedScope) || null
  const uncertainSaveCount = Object.keys(uncertainSaves).length
  const damagedRecoveryCount = damagedRows.length
  const dirtyCount = Object.values(documents)
    .filter(document => document.draftText !== document.savedText).length
  const mutationBusy = !!saveState
  const navigationUnsafe = dirtyCount > 0 || mutationBusy
    || uncertainSaveCount > 0 || damagedRecoveryCount > 0
  const authoringNavigationSummary = [
    dirtyCount > 0
      ? `${dirtyCount} unsaved Authoring draft${dirtyCount === 1 ? '' : 's'} will be discarded.` : '',
    mutationBusy ? 'A file save is still in progress; its immediate outcome may no longer be visible here.' : '',
    uncertainSaveCount > 0
      ? `${uncertainSaveCount} file save outcome${uncertainSaveCount === 1 ? '' : 's'} may still be unknown; retained recovery must be reviewed before any retry.` : '',
    damagedRecoveryCount > 0
      ? `${damagedRecoveryCount} damaged Authoring recovery record${damagedRecoveryCount === 1 ? ' remains' : 's remain'} quarantined in this browser tab.` : '',
  ].filter(Boolean).join(' ')
  const authoringCloseMessage = `${authoringNavigationSummary} Close Authoring anyway?`
  const navigationUnsafeRef = useRef(navigationUnsafe)
  navigationUnsafeRef.current = navigationUnsafe
  useLayoutEffect(() => {
    if (navigationGuardOwner !== 'run' || typeof publishNavigationGuard !== 'function') {
      return undefined
    }
    return publishNavigationGuard({
      route: 'authoring', unsafe: navigationUnsafe,
      closeMessage: authoringCloseMessage, leaveSummary: authoringNavigationSummary,
      dispose: () => {
        allowNavigationRef.current = true
        activeRef.current = false
        draftStore.clear(AUTHORING_PANEL_DRAFT_SCOPE)
      },
    })
  }, [navigationGuardOwner, publishNavigationGuard, navigationUnsafe,
    authoringCloseMessage, authoringNavigationSummary, draftStore])
  useEffect(() => {
    if (navigationGuardOwner === 'run' || !navigationUnsafe) {
      return undefined
    }
    return installNavigationLossGuard({
      allowRef: allowNavigationRef,
      guardedHash: location.hash,
      message: () => uncertainSaveCount > 0
        ? `${uncertainSaveCount} file save outcome${uncertainSaveCount === 1 ? '' : 's'} may still be unknown. Leave Authoring anyway?`
        : damagedRecoveryCount > 0
          ? `${damagedRecoveryCount} damaged Authoring recovery record${damagedRecoveryCount === 1 ? '' : 's'} remain quarantined. Leave Authoring anyway?`
        : mutationBusy
          ? 'A file save is still in progress. Leave Authoring anyway?'
          : `${dirtyCount} unsaved Authoring draft${dirtyCount === 1 ? '' : 's'} will be lost. Leave anyway?`,
      onAllow: () => draftStore.clear(AUTHORING_PANEL_DRAFT_SCOPE),
    })
  }, [navigationGuardOwner, draftStore, navigationUnsafe, mutationBusy, uncertainSaveCount,
    damagedRecoveryCount, dirtyCount])
  useEffect(() => () => {
    const retained = draftStore.readField(AUTHORING_PANEL_DRAFT_SCOPE, 'documents', {})
    const hasUnsafeStoredDocument = retained && typeof retained === 'object'
      && !Array.isArray(retained) && Object.values(retained).some(document => document
        && typeof document === 'object' && !Array.isArray(document)
        && (document.draftText !== document.savedText
          || document.recoveryOperationId || document.recoveryStorageRaw))
    const hasUnsafeStoredRecovery = ['uncertainSaves', 'damagedRecoveries'].some(field => {
      const records = draftStore.readField(AUTHORING_PANEL_DRAFT_SCOPE, field, {})
      return records && typeof records === 'object' && !Array.isArray(records)
        && Object.keys(records).length > 0
    })
    if (allowNavigationRef.current
        || (!navigationUnsafeRef.current
          && !hasUnsafeStoredDocument && !hasUnsafeStoredRecovery)) {
      draftStore.clear(AUTHORING_PANEL_DRAFT_SCOPE)
    }
  }, [draftStore])
  const retainNotice = destination => {
    if (!selected || selected.draftText === selected.savedText) return
    onToast?.(`Unsaved draft for ${selected.name} is preserved while you ${destination}.`)
  }
  const chooseKind = nextKind => {
    if (nextKind === kind) return
    retainNotice('switch sections')
    setKind(nextKind)
    setSelectedScope(null)
  }
  const chooseFile = file => {
    const scope = scopeFor(kind, file.name)
    if (scope === selectedScope) return
    retainNotice('switch files')
    setDocuments(current => current[scope] ? current : {
      ...current,
      [scope]: { kind, name: file.name, savedText: file.text, draftText: file.text,
        savedRevision: file.revision, observedText: file.text, observedRevision: file.revision,
        savedTargetRootId: data.targetRootId, observedTargetRootId: data.targetRootId,
        truncated: file.truncated, conflict: false, rootConflict: false,
        observationIncomplete: false, error: '',
        recoveryOperationId: null, recoveryStorageRaw: null },
    })
    setSelectedScope(scope)
  }
  const editSelected = value => {
    if (!selectedScope) return
    setDocuments(current => current[selectedScope] ? {
      ...current, [selectedScope]: { ...current[selectedScope], draftText: value, error: '' },
    } : current)
  }
  const updateRecovery = (token, patch) => setUncertainSaves(current => {
    const exact = current[token.scope]
    if (!exact || exact.operationId !== token.operationId
        || exact.storageRaw !== token.storageRaw) return current
    return { ...current, [token.scope]: { ...exact, ...patch } }
  })
  const removeRecovery = token => setUncertainSaves(current => {
    const exact = current[token.scope]
    if (!exact || exact.operationId !== token.operationId
        || exact.storageRaw !== token.storageRaw) return current
    const next = { ...current }; delete next[token.scope]; return next
  })
  const quarantineAuthoringRecovery = (recovery, message) => {
    const exact = uncertainSavesRef.current[recovery?.scope]
    if (!exact || exact.operationId !== recovery.operationId
        || exact.storageRaw !== recovery.storageRaw) return false
    const next = { ...uncertainSavesRef.current }
    delete next[recovery.scope]
    uncertainSavesRef.current = next
    setUncertainSaves(next)
    authoringDigestQuarantine.set(recovery.storageKey, { raw: recovery.storageRaw, reason: message })
    setDamagedRecoveries(current => {
      let damagedScope = `damaged:${recovery.storageKey}`
      while (Object.hasOwn(current, damagedScope)
          && (current[damagedScope].key !== recovery.storageKey
            || current[damagedScope].raw !== recovery.storageRaw)) damagedScope += ':retained'
      return {
        ...current,
        [damagedScope]: {
          scope: damagedScope, key: recovery.storageKey, raw: recovery.storageRaw,
          identity: { kind: recovery.kind, name: recovery.name, scope: recovery.scope },
          inspected: false, reason: message,
        },
      }
    })
    setDocuments(current => {
      const retained = current[recovery.scope] || {
        kind: recovery.kind, name: recovery.name, savedText: null,
        draftText: recovery.submittedText, savedRevision: recovery.expectedRevision,
        observedText: null, observedRevision: recovery.expectedRevision,
        savedTargetRootId: recovery.expectedTargetRootId, observedTargetRootId: null,
        truncated: false, conflict: false, rootConflict: false,
        observationIncomplete: false,
      }
      return { ...current, [recovery.scope]: {
        ...retained, error: message,
        recoveryOperationId: null, recoveryStorageRaw: null,
      } }
    })
    onToast?.(message)
    return true
  }
  const verifyAuthoringRecovery = async recovery => {
    const exact = uncertainSavesRef.current[recovery?.scope]
    if (!exact || exact.operationId !== recovery.operationId
        || exact.storageRaw !== recovery.storageRaw) return null
    let actualRevision
    try {
      actualRevision = await authoringTextRevision(exact.submittedText)
    } catch (error) {
      const message = `Could not verify the retained contents for ${exact.name}: ${error?.message || error}. No request was sent.`
      updateRecovery(exact, {
        phase: 'unknown', releaseAllowed: false, releaseInspected: false, message,
      })
      onToast?.(message)
      return null
    }
    const storage = authoringStorage()
    const latest = uncertainSavesRef.current[exact.scope]
    let storedRaw = null
    try { storedRaw = storage?.getItem(exact.storageKey) ?? null } catch {
      setStorageAvailable(false)
      const message = `Browser recovery storage became unavailable while verifying ${exact.name}. No request was sent.`
      updateRecovery(exact, {
        phase: 'unknown', releaseAllowed: false, releaseInspected: false, message,
      })
      onToast?.(message)
      return null
    }
    if (!storage || storedRaw !== exact.storageRaw
        || !latest || latest.operationId !== exact.operationId
        || latest.storageRaw !== exact.storageRaw) {
      refreshRecoveryStore()
      onToast?.('The Authoring recovery record changed while it was being verified. Inspect the refreshed exact record.')
      return null
    }
    if (actualRevision !== exact.desiredRevision) {
      quarantineAuthoringRecovery(exact,
        `The retained operation for ${exact.name} has an invalid content digest. It was quarantined without contacting the server.`)
      return null
    }
    return latest
  }
  const refreshRecoveryStore = () => {
    const inspected = inspectAuthoringOperations()
    setStorageAvailable(inspected.available)
    if (!inspected.available) return
    setDamagedRecoveries(current => {
      const incoming = Object.values(inspected.damaged)
      const previous = Object.values(current)
      const retainedScopes = new Set()
      const next = {}
      for (const record of incoming) {
        const exact = previous.find(candidate => candidate.key === record.key
          && candidate.raw === record.raw)
        if (exact) retainedScopes.add(exact.scope)
        next[record.scope] = exact ? {
          ...record, inspected: exact.inspected, reason: record.reason || exact.reason,
          storageMissing: false,
        } : record
      }
      for (const record of previous) {
        if (retainedScopes.has(record.scope)) continue
        // A disappearing or replaced unreadable envelope is still evidence of an unknown write.
        // Keep that exact snapshot quarantined in this tab without touching any newer stored record.
        let scope = record.scope
        while (Object.hasOwn(next, scope)) scope += ':retained'
        next[scope] = {
          ...record, scope, storageMissing: true, inspected: false,
        }
      }
      return next
    })
    setUncertainSaves(current => {
      const next = Object.fromEntries(Object.entries(inspected.valid).map(([scope, record]) => {
        const previous = current[scope]
        return [scope, previous?.operationId === record.operationId
            && previous?.storageRaw === record.storageRaw
          ? { ...record, phase: previous.phase, inspectedMissing: previous.inspectedMissing,
            releaseAllowed: previous.releaseAllowed, releaseInspected: previous.releaseInspected,
            message: previous.message }
          : { ...record, phase: 'unknown', inspectedMissing: false,
            releaseAllowed: false, releaseInspected: false,
            message: `A saved operation for ${record.name} may still be pending. Check its exact durable receipt.` }]
      }))
      const damagedSnapshots = new Set(Object.values(inspected.damaged)
        .map(record => `${record.key}\u0000${record.raw}`))
      for (const [scope, previous] of Object.entries(current)) {
        if (next[scope] || damagedSnapshots.has(`${previous.storageKey}\u0000${previous.storageRaw}`)) continue
        next[scope] = {
          ...previous, phase: 'storage-missing', inspectedMissing: false,
          releaseAllowed: false, releaseInspected: false,
          message: `The browser recovery record for ${previous.name} disappeared or changed before a terminal receipt was proved. This tab keeps the exact draft quarantined; no new save will be sent.`,
        }
      }
      uncertainSavesRef.current = next
      return next
    })
  }
  const releaseTerminalRecovery = token => {
    if (clearAuthoringOperationIntent(token)) {
      removeRecovery(token)
      return true
    }
    try {
      const storage = authoringStorage()
      if (storage && storage.getItem(token.storageKey) == null) {
        removeRecovery(token)
        return true
      }
    } catch { /* retain fail-closed below */ }
    refreshRecoveryStore()
    updateRecovery(token, {
      phase: 'unknown', releaseAllowed: true, releaseInspected: false,
      message: `The server settled ${token.name}, but its browser recovery record changed or could not be released. Inspect the exact record before another save.`,
    })
    return false
  }
  const applyAuthoringReceipt = (token, receipt) => {
    if (receipt.desired_revision !== token.desiredRevision
        || receipt.target_root_id !== token.expectedTargetRootId) {
      const error = new Error('The durable receipt does not match the submitted file contents.')
      error.code = 'AUTHORING_PROTOCOL_ERROR'
      throw error
    }
    if (receipt.status === 'prepared') {
      const message = `The exact operation for ${token.name} is durably prepared but not complete. Resume the same save identity.`
      updateRecovery(token, {
        phase: 'prepared', inspectedMissing: false,
        releaseAllowed: false, releaseInspected: false, message,
      })
      setDocuments(current => current[token.scope] ? {
        ...current, [token.scope]: { ...current[token.scope], error: message },
      } : current)
      return 'prepared'
    }
    if (receipt.status === 'succeeded') {
      const released = releaseTerminalRecovery(token)
      setDocuments(current => {
        const exact = current[token.scope]
        if (!exact || exact.kind !== token.kind || exact.name !== token.name) return current
        return { ...current, [token.scope]: {
          ...exact, savedText: token.submittedText, savedRevision: token.desiredRevision,
          observedText: token.submittedText, observedRevision: token.desiredRevision,
          savedTargetRootId: token.expectedTargetRootId,
          observedTargetRootId: token.expectedTargetRootId,
          conflict: false, rootConflict: false, observationIncomplete: false, error: '',
          recoveryOperationId: released ? null : token.operationId,
          recoveryStorageRaw: released ? null : token.storageRaw,
        } }
      })
      onToast?.(`Saved ${token.name}`)
      return 'succeeded'
    }
    const message = receipt.code === 'authoring_intervening_write'
      ? `${token.name} changed after this exact save was prepared. Your draft is retained; inspect the current server copy before saving again.`
      : `${token.name} changed before this save began. Your retained draft was not written.`
    const released = releaseTerminalRecovery(token)
    setDocuments(current => {
      const exact = current[token.scope]
      const retained = exact || {
        kind: token.kind, name: token.name, savedText: null,
        draftText: token.submittedText, savedRevision: token.expectedRevision,
        savedTargetRootId: token.expectedTargetRootId,
        observedTargetRootId: token.expectedTargetRootId,
        truncated: false, rootConflict: false,
      }
      return { ...current, [token.scope]: {
        ...retained, observedText: null, observedRevision: receipt.result_revision,
        observedTargetRootId: token.expectedTargetRootId,
        conflict: true, rootConflict: false, observationIncomplete: false, error: message,
        recoveryOperationId: released ? null : token.operationId,
        recoveryStorageRaw: released ? null : token.storageRaw,
      } }
    })
    retry()
    onToast?.(message)
    return 'conflict'
  }
  const submitAuthoringSave = async token => {
    if (saveRef.current) return
    saveRef.current = token
    setSaveState(token)
    updateRecovery(token, {
      phase: 'submitting', inspectedMissing: false,
      releaseAllowed: false, releaseInspected: false,
      message: `Submitting the exact saved operation for ${token.name}…`,
    })
    setDocuments(current => current[token.scope] ? {
      ...current, [token.scope]: {
        ...current[token.scope], error: '', recoveryOperationId: token.operationId,
        recoveryStorageRaw: token.storageRaw,
      },
    } : current)
    const timed = deadlineRequest(
      signal => putAuthoringOperation(token.kind, token.name, token.operationId, {
        text: token.submittedText, expectedRevision: token.expectedRevision,
        expectedTargetRootId: token.expectedTargetRootId,
        desiredRevision: token.desiredRevision,
      }, { signal }),
      AUTHORING_SAVE_TIMEOUT_MS,
    )
    try {
      const receipt = await timed.promise
      if (saveRef.current !== token || !activeRef.current) return
      applyAuthoringReceipt(token, receipt)
    } catch (error) {
      if (saveRef.current !== token || !activeRef.current) return
      const message = `Save outcome for ${token.name} is not confirmed. Its exact operation and draft remain durable in this tab; check the receipt before retrying.`
      updateRecovery(token, {
        phase: 'unknown', inspectedMissing: false,
        // A failed client request cannot prove that the exact PUT is no longer waiting on the
        // server-side lock or fsync. Keep it quarantined until a validated terminal receipt exists.
        releaseAllowed: false, releaseInspected: false, message,
      })
      setDocuments(current => current[token.scope] ? {
        ...current, [token.scope]: { ...current[token.scope], error: message },
      } : current)
      onToast?.(message)
    } finally {
      if (saveRef.current === token) {
        saveRef.current = null
        if (activeRef.current) setSaveState(null)
      }
    }
  }
  const saveSelected = async () => {
    const document = selectedScope ? documents[selectedScope] : null
    if (!document || document.draftText === document.savedText || saveRef.current
        || selectedUncertainSave || selectedDamagedRecovery) return
    if (!selectedSourceReconciled) {
      onToast?.(`Load and reconcile the current ${kind} source before saving ${document.name}.`)
      return
    }
    if (document.rootConflict || !validAuthoringTargetRootId(document.observedTargetRootId)) {
      const message = `${document.name} belongs to a different or unavailable Authoring directory. Its retained draft was not written; refresh the configured directory before saving.`
      setDocuments(current => current[selectedScope] ? {
        ...current, [selectedScope]: { ...current[selectedScope], error: message },
      } : current)
      onToast?.(message)
      return
    }
    if (document.truncated || !AUTHORING_REVISION_RE.test(document.observedRevision || '')) {
      const message = `${document.name} has no complete writable revision. Refresh or edit the file outside this truncated view.`
      setDocuments(current => current[selectedScope] ? {
        ...current, [selectedScope]: { ...current[selectedScope], error: message },
      } : current)
      onToast?.(message)
      return
    }
    if (document.conflict && !window.confirm(
      `${document.name} changed on the server while this draft was open. Save this retained draft over the newer server copy?`,
    )) return
    try {
      const desiredRevision = await authoringTextRevision(document.draftText)
      if (!activeRef.current || saveRef.current) return
      const latestDocument = documentsRef.current[selectedScope]
      if (!latestDocument || latestDocument.draftText !== document.draftText
          || latestDocument.savedRevision !== document.savedRevision
          || latestDocument.observedRevision !== document.observedRevision
          || latestDocument.savedTargetRootId !== document.savedTargetRootId
          || latestDocument.observedTargetRootId !== document.observedTargetRootId
          || latestDocument.conflict !== document.conflict || latestDocument.rootConflict) {
        const message = `${document.name} changed while the save identity was being prepared. Review the retained draft and current server copy; no request was sent.`
        setDocuments(current => current[selectedScope] ? {
          ...current, [selectedScope]: { ...current[selectedScope], error: message },
        } : current)
        onToast?.(message)
        return
      }
      const intent = {
        schema: AUTHORING_OPERATION_SCHEMA,
        operationId: createIdempotencyKey(), kind: latestDocument.kind, name: latestDocument.name,
        submittedText: latestDocument.draftText,
        expectedRevision: latestDocument.conflict
          ? latestDocument.observedRevision : latestDocument.savedRevision,
        expectedTargetRootId: latestDocument.observedTargetRootId,
        desiredRevision, updatedAt: Date.now(),
      }
      const stored = saveAuthoringOperationIntent(intent)
      if (!stored) {
        const message = `Save was not sent because the exact operation for ${document.name} could not be retained in browser recovery storage.`
        setDocuments(current => current[selectedScope] ? {
          ...current, [selectedScope]: { ...current[selectedScope], error: message },
        } : current)
        refreshRecoveryStore()
        onToast?.(message)
        return
      }
      const recovery = {
        ...stored, phase: 'submitting', inspectedMissing: false,
        releaseAllowed: false, releaseInspected: false,
        message: `Submitting the exact saved operation for ${stored.name}…`,
      }
      setUncertainSaves(current => ({ ...current, [stored.scope]: recovery }))
      await submitAuthoringSave(recovery)
    } catch (error) {
      const message = `Could not prepare ${document.name} for saving: ${error?.message || error}`
      setDocuments(current => current[selectedScope] ? {
        ...current, [selectedScope]: { ...current[selectedScope], error: message },
      } : current)
      onToast?.(message)
    }
  }
  const reconcileSave = async recovery => {
    const exactRecovery = uncertainSaves[recovery?.scope]
    if (!recovery || saveRef.current || !exactRecovery
        || exactRecovery.operationId !== recovery.operationId
        || exactRecovery.storageRaw !== recovery.storageRaw) return
    const verifiedRecovery = await verifyAuthoringRecovery(exactRecovery)
    if (!verifiedRecovery || saveRef.current || !activeRef.current) return
    recovery = verifiedRecovery
    const token = { ...recovery, reconcile: true }
    saveRef.current = token
    setSaveState(token)
    const request = deadlineRequest(
      signal => getAuthoringOperation(recovery.kind, recovery.name, recovery.operationId, {
        signal, expectedRevision: recovery.expectedRevision,
        expectedTargetRootId: recovery.expectedTargetRootId,
        desiredRevision: recovery.desiredRevision,
      }),
      PANEL_REQUEST_TIMEOUT_MS,
    )
    try {
      const receipt = await request.promise
      if (saveRef.current !== token || !activeRef.current) return
      applyAuthoringReceipt(recovery, receipt)
    } catch (error) {
      if (saveRef.current === token && activeRef.current) {
        const missing = Number(error?.status) === 404
          && error?.code === 'authoring_operation_not_found'
        const message = missing
          ? `No durable receipt exists yet for ${recovery.name}. Resume the same operation identity; it cannot overwrite a newer revision.`
          : `Could not check ${recovery.name}: ${error?.message || error}. Its exact draft and operation identity remain retained.`
        updateRecovery(recovery, {
          phase: missing ? 'missing' : 'unknown', inspectedMissing: missing,
          // Even a 404 can race a still-running timed-out PUT before its prepared receipt is
          // published. Reuse/check the same identity; never release it from an unknown response.
          releaseAllowed: false,
          releaseInspected: false, message,
        })
        setDocuments(current => current[recovery.scope] ? {
          ...current, [recovery.scope]: { ...current[recovery.scope], error: message },
        } : current)
        onToast?.(message)
      }
    } finally {
      if (saveRef.current === token) {
        saveRef.current = null
        if (activeRef.current) setSaveState(null)
      }
    }
  }
  const retryExactSave = async recovery => {
    const exact = uncertainSaves[recovery?.scope]
    if (!exact || saveRef.current || exact.operationId !== recovery.operationId
        || exact.storageRaw !== recovery.storageRaw
        || !['prepared', 'missing'].includes(exact.phase)) return
    const verifiedRecovery = await verifyAuthoringRecovery(exact)
    if (!verifiedRecovery || saveRef.current || !activeRef.current
        || !['prepared', 'missing'].includes(verifiedRecovery.phase)) return
    if (!window.confirm(
      `Resume the exact retained save for ${recovery.name}?\n\nThis reuses the same durable operation identity, expected revision, and file contents. It cannot overwrite an intervening newer version.`,
    )) return
    submitAuthoringSave(verifiedRecovery)
  }
  const adoptObservedServerCopy = document => {
    if (!selectedSourceReconciled || !document?.conflict || document.truncated
        || document.observedText == null
        || !/^sha256:[0-9a-f]{64}$/.test(document.observedRevision || '') || saveRef.current) return
    if (!window.confirm(
      `Use the current server copy of ${document.name}?\n\nThis discards the retained local draft for that file.`,
    )) return
    const scope = scopeFor(document.kind, document.name)
    setDocuments(current => current[scope] ? {
      ...current,
      [scope]: {
        ...current[scope], savedText: document.observedText, draftText: document.observedText,
        savedRevision: document.observedRevision,
        savedTargetRootId: document.observedTargetRootId,
        observedTargetRootId: document.observedTargetRootId,
        conflict: false, rootConflict: false, observationIncomplete: false, error: '',
      },
    } : current)
    onToast?.(`Using the current server copy of ${document.name}.`)
  }
  const releaseAuthoringRecovery = recovery => {
    const exact = uncertainSaves[recovery?.scope]
    if (!exact || !exact.releaseAllowed || !exact.releaseInspected
        || exact.operationId !== recovery.operationId) return
    if (!window.confirm(
      `Release the exact saved operation for ${recovery.name}?\n\nThis sends no write. The retained draft stays open here, but closing the panel will discard it.`,
    )) return
    if (!clearAuthoringOperationIntent(exact)) {
      updateRecovery(exact, {
        releaseAllowed: false, releaseInspected: false,
        message: 'The browser recovery record changed or could not be released. It remains protected.',
      })
      refreshRecoveryStore()
      onToast?.('The exact Authoring recovery record changed or could not be released.')
      return
    }
    removeRecovery(exact)
    setDocuments(current => current[exact.scope] ? {
      ...current,
      [exact.scope]: {
        ...current[exact.scope], recoveryOperationId: null,
        recoveryStorageRaw: null,
        error: 'The old operation recovery was released. Review the retained draft and current server version before saving again.',
      },
    } : current)
    onToast?.('The exact Authoring recovery identity was released. No save was sent.')
  }
  const releaseDamagedRecovery = recovery => {
    if (!recovery?.inspected) return
    if (!window.confirm(
      'Release this exact unreadable Authoring recovery record?\n\nOnly continue after confirming that no retained save operation still needs recovery.',
    )) return
    const storedRecordReleased = clearDamagedAuthoringOperation(recovery)
    if (!storedRecordReleased) {
      let exactSnapshotIsGone = false
      if (recovery.storageMissing) {
        const storage = authoringStorage()
        try { exactSnapshotIsGone = !!storage && storage.getItem(recovery.key) !== recovery.raw } catch {
          setStorageAvailable(false)
        }
      }
      if (!exactSnapshotIsGone) {
        onToast?.('The recovery record changed or could not be released. It remains protected.')
        refreshRecoveryStore()
        return
      }
    }
    setDamagedRecoveries(current => {
      const exact = current[recovery.scope]
      if (!exact || exact.raw !== recovery.raw || exact.key !== recovery.key) return current
      const next = { ...current }; delete next[recovery.scope]; return next
    })
    const quarantined = authoringDigestQuarantine.get(recovery.key)
    if (quarantined?.raw === recovery.raw) authoringDigestQuarantine.delete(recovery.key)
    if (recovery.identity?.scope) {
      setDocuments(current => current[recovery.identity.scope] ? {
        ...current,
        [recovery.identity.scope]: {
          ...current[recovery.identity.scope],
          recoveryOperationId: null, recoveryStorageRaw: null,
          error: 'The old recovery record was released. Review the retained draft and current server version before saving again.',
        },
      } : current)
    }
    if (!storedRecordReleased) refreshRecoveryStore()
    onToast?.(storedRecordReleased
      ? 'The exact damaged Authoring recovery record was released. No save was sent.'
      : 'The retained snapshot of the missing recovery record was released. No stored record was changed.')
  }
  const requestClose = () => {
    if (navigationGuardOwner === 'run') {
      if (onClose?.() !== false) allowNavigationRef.current = true
      return
    }
    if (!navigationUnsafe) {
      draftStore.clear(AUTHORING_PANEL_DRAFT_SCOPE)
      onClose?.()
      return
    }
    const warning = uncertainSaveCount > 0
      ? `${uncertainSaveCount} file save outcome${uncertainSaveCount === 1 ? '' : 's'} may still be unknown, and the exact draft${uncertainSaveCount === 1 ? ' is' : 's are'} retained here.`
      : damagedRecoveryCount > 0
        ? `${damagedRecoveryCount} damaged Authoring recovery record${damagedRecoveryCount === 1 ? '' : 's'} remain quarantined in this tab.`
      : mutationBusy ? 'A file save is still in progress.'
        : `${dirtyCount} unsaved draft${dirtyCount === 1 ? '' : 's'} will be lost.`
    if (!window.confirm(`${warning} Close Authoring anyway?`)) return
    allowNavigationRef.current = true
    draftStore.clear(AUTHORING_PANEL_DRAFT_SCOPE)
    onClose?.()
  }
  const dirtyByKind = documentKind => Object.values(documents)
    .filter(document => document.kind === documentKind && document.draftText !== document.savedText).length
  const fileRows = [...(data.files || [])]
  for (const recovery of Object.values(uncertainSaves)) {
    if (recovery.kind === kind && !fileRows.some(file => file.name === recovery.name)) {
      fileRows.push({ name: recovery.name, text: recovery.submittedText,
        revision: null, truncated: false, recovered: true })
    }
  }
  for (const document of Object.values(documents)) {
    if (document.kind === kind
        && (document.draftText !== document.savedText || document.recoveryOperationId
          || scopeFor(document.kind, document.name) === selectedScope)
        && !fileRows.some(file => file.name === document.name)) {
      fileRows.push({
        name: document.name, text: document.draftText,
        revision: document.observedRevision || null,
        truncated: document.truncated === true, recovered: true,
      })
    }
  }
  return (
    <Panel title="Authoring — configure the scientist" sub="hot-reloaded next run" onClose={requestClose} wide>
      <div className="toolbar" style={{ marginBottom: 10 }}>
        {['prompts', 'skills', 'knowledge'].map(k => <button key={k} className={'btn sm' + (k === kind ? ' primary' : '')}
          onClick={() => chooseKind(k)}>{k}{dirtyByKind(k) ? ` (${dirtyByKind(k)} unsaved)` : ''}</button>)}
        {source.state === 'ready' && <span className="muted">{data.dir || `no ${kind} dir configured (set LOOPLAB_${kind.toUpperCase()}_DIR)`}</span>}
        {source.state === 'ready' && data.truncatedFiles > 0
          && <span className="muted">{data.truncatedFiles} more file{data.truncatedFiles === 1 ? '' : 's'} omitted</span>}
      </div>
      <PanelResourceNotice resource={source} label={`${kind} files`} onRetry={retry} />
      {dirtyCount > 0 && <div className="notice" role="status" style={{ marginBottom: 10 }}>
        {dirtyCount} unsaved draft{dirtyCount === 1 ? '' : 's'} retained in this panel. Switching files or sections is safe; closing is not.
      </div>}
      {selected && !selectedSourceReconciled && <div className="notice" role="status"
        style={{ marginBottom: 10 }}>
        Saving and server-copy actions stay disabled until the current {kind} source is loaded and
        reconciled with this retained draft.
      </div>}
      {!storageAvailable && <div className="report-inline-state error" role="alert" style={{ marginBottom: 10 }}>
        <OpIcon name="alert" size={14} /><span>Browser recovery storage is unavailable. Authoring saves stay disabled so an ambiguous write cannot lose its exact identity.</span>
      </div>}
      {damagedRows.map(recovery => <div key={recovery.scope}
        className="report-inline-state error" role="alert" style={{ marginBottom: 10 }}>
        <OpIcon name="alert" size={14} />
        <span>{recovery.storageMissing
          ? `A previously seen unreadable Authoring recovery record${recovery.identity
            ? ` for ${recovery.identity.name}` : ''} disappeared or was replaced in browser storage. Its exact snapshot remains quarantined in this tab.`
          : recovery.reason || `An unreadable Authoring recovery record exists${recovery.identity
          ? ` for ${recovery.identity.name}` : ''}. Saves for that scope remain locked.`}</span>
        {!recovery.inspected
          ? <button className="btn sm" onClick={() => setDamagedRecoveries(current => ({
            ...current, [recovery.scope]: { ...current[recovery.scope], inspected: true },
          }))}>Inspect recovery</button>
          : <>
            <span className="muted">Stored record {recovery.raw.length} bytes; {recovery.reason
              ? 'its retained contents fail the integrity check.'
              : 'its operation identity cannot be verified.'}</span>
            <button className="btn sm danger" onClick={() => releaseDamagedRecovery(recovery)}>Release exact record</button>
          </>}
      </div>)}
      {Object.values(uncertainSaves).map(recovery => <div key={recovery.scope}
        className="report-inline-state error" role="alert" style={{ marginBottom: 10 }}>
        <OpIcon name="alert" size={14} /><span>{recovery.message}</span>
        <button className="btn sm" disabled={mutationBusy} onClick={() => reconcileSave(recovery)}>
          {saveState?.reconcile && saveState.scope === recovery.scope ? 'Checking…' : 'Check exact operation'}</button>
        {['prepared', 'missing'].includes(recovery.phase) && <button className="btn sm" disabled={mutationBusy}
          onClick={() => retryExactSave(recovery)}>Resume exact save</button>}
        {recovery.releaseAllowed && !recovery.releaseInspected
          && <button className="btn sm" disabled={mutationBusy}
            onClick={() => updateRecovery(recovery, { releaseInspected: true })}>Inspect recovery</button>}
        {recovery.releaseAllowed && recovery.releaseInspected && <>
          <span className="muted">Operation {recovery.operationId}; expected revision {recovery.expectedRevision.slice(0, 18)}….</span>
          <button className="btn sm danger" disabled={mutationBusy}
            onClick={() => releaseAuthoringRecovery(recovery)}>Release exact recovery</button>
        </>}
      </div>)}
      <div className="authoring-layout">
        <div className="authoring-list">
          {fileRows.map(f => <button type="button" key={f.name}
            className={'run-card authoring-file' + (selectedScope === scopeFor(kind, f.name) ? ' sel' : '')}
            onClick={() => chooseFile(f)}>{f.name}
            {documents[scopeFor(kind, f.name)]?.draftText !== documents[scopeFor(kind, f.name)]?.savedText
              ? ' • unsaved' : ''}{uncertainSaves[scopeFor(kind, f.name)] ? ' • recovery' : ''}</button>)}
          {source.state === 'ready' && fileRows.length === 0 && <div className="muted">no files</div>}
        </div>
        <div className="authoring-editor">
          {selected ? <>
            <textarea className="text" aria-label={`Edit ${selected.name}`} value={selected.draftText}
              disabled={selected.truncated} onChange={e => editSelected(e.target.value)} />
            {selected.truncated && <div className="report-inline-state error" role="alert">
              <OpIcon name="alert" size={14} /><span>This file is larger than the safe editor limit. Only a prefix is shown, so saving is disabled.</span>
            </div>}
            {selected.conflict && <div className="report-inline-state error" role="alert">
              <OpIcon name="alert" size={14} /><span>{selected.rootConflict
                ? 'The configured Authoring directory changed while this draft was retained. The draft stays bound to its original directory and cannot be written into the new one.'
                : selected.observationIncomplete
                  ? 'The server returned an incomplete file list, so this file\'s current version could not be verified. Saving stays disabled until a complete refresh can prove its revision.'
                : 'The server copy changed while this draft was retained. Your text was not replaced.'}</span>
              {!selected.truncated && selected.observedText != null
                && /^sha256:[0-9a-f]{64}$/.test(selected.observedRevision || '')
                && !selectedUncertainSave && selectedSourceReconciled && <button className="btn sm"
                onClick={() => adoptObservedServerCopy(selected)}>Use server copy</button>}
            </div>}
            {selected.error && <div className="report-inline-state error" role="alert">
              <OpIcon name="alert" size={14} /><span>{selected.error}</span>
            </div>}
            <button className="btn sm primary" style={{ marginTop: 8 }} onClick={saveSelected}
              disabled={mutationBusy || !storageAvailable || !!selectedUncertainSave
                || !!selectedDamagedRecovery || selected.truncated
                || !selectedSourceReconciled
                || selected.rootConflict
                || !validAuthoringTargetRootId(selected.observedTargetRootId)
                || !AUTHORING_REVISION_RE.test(selected.observedRevision || '')
                || selected.draftText === selected.savedText}>
              {saveState && !saveState.reconcile ? `Saving ${saveState.name}…` : 'Save'}</button>
          </> : source.state === 'ready' && <div className="muted">select a file to edit</div>}
        </div>
      </div>
    </Panel>
  )
}

function MemoryCompletenessNotice({ resource, onRetry, error = false, children }) {
  const action = error ? 'Retry' : 'Refresh'
  return <div className={'report-inline-state' + (error ? ' error' : '')} role={error ? 'alert' : 'status'}>
    <OpIcon name="alert" size={14} />
    <span>{children}</span>
    <button type="button" className="btn sm" disabled={!!resource.pending} onClick={onRetry}>
      {resource.pending ? `${action}ing…` : action}</button>
  </div>
}

function KbNote({ note }) {
  const [open, setOpen] = useState(false)
  return <div className="mem-card">
    <button type="button" className="memory-note-toggle disclosure-button" aria-expanded={open}
      onClick={() => setOpen(o => !o)}>
      <span style={{ opacity: 0.6, fontSize: 10, marginRight: 4 }}>{open ? '▾' : '▸'}</span>{note.name}
      {note.truncated && <span className="muted" style={{ marginLeft: 6, fontSize: 10 }}>· prefix only</span>}</button>
    {open && <div style={{ marginTop: 6 }}>
      {note.truncated && <div className="report-inline-state" role="status" style={{ margin: '0 0 8px' }}>
        <OpIcon name="alert" size={14} /><span>Only the first {AUTHORING_MAX_BYTES / 1024} KiB of this note was returned; the rest is not shown.</span>
      </div>}
      <Markdown text={note.text || note.content || ''} />
    </div>}
  </div>
}

export function MemoryPanel({ onClose }) {
  // Everything the run has LEARNED, in one place: distilled lessons, solved-task cases, meta-notes, and
  // the agentic knowledge-base markdown notes (best configs / recipes the agents save + later retrieve).
  const [memory, retryMemory] = usePanelResource(signal => get('/api/memory', { signal }), memoryPayload)
  const [knowledge, retryKnowledge] = usePanelResource(signal => get('/api/knowledge', { signal }), authoringPayload)
  const mem = memory.data || { dir: null, cases: [], lessons: [], notes: [], projection: null, page: null }
  const kb = knowledge.data || { dir: null, files: [], truncatedFiles: 0 }   // /api/knowledge → {dir, files:[{name,text}]}
  const [tab, setTab] = useState('lessons')
  const [lessonRole, setLessonRole] = useState('all')      // §role-split: Researcher vs Developer lessons
  const kbFiles = kb.files || []
  const truncatedKnowledgePreviews = kbFiles.filter(file => file.truncated).length
  const knowledgeIncomplete = kb.truncatedFiles > 0 || truncatedKnowledgePreviews > 0
  const tabs = [
    ['lessons', 'Lessons', mem.lessons?.length, memoryTierIncomplete(mem.page?.tiers?.lessons)],
    ['cases', 'Cases', mem.cases?.length, memoryTierIncomplete(mem.page?.tiers?.cases)],
    ['notes', 'Notes', mem.notes?.length, memoryTierIncomplete(mem.page?.tiers?.notes)],
    ['knowledge', 'Knowledge', kbFiles.length, knowledgeIncomplete],
  ]
  const selectedResource = tab === 'knowledge' ? knowledge : memory
  const selectedReceipt = tab === 'knowledge' ? null : mem.page?.tiers?.[tab]
  const selectedReceiptIncomplete = memoryTierIncomplete(selectedReceipt)
  const selectedTierLabel = tab === 'lessons' ? 'Lessons' : tab === 'cases' ? 'Cases' : 'Meta-notes'
  const selectedReceiptIssues = []
  if (selectedReceipt?.unavailable) selectedReceiptIssues.push('This tier could not be read.')
  if (selectedReceipt?.sourceWindowTruncated) {
    selectedReceiptIssues.push(selectedReceipt.returned > 0
      ? `Showing the newest ${selectedReceipt.returned} ${selectedReceipt.returned === 1 ? 'item' : 'items'} from a bounded window; older entries were omitted.`
      : 'Only a bounded recent source window was checked; older entries were omitted.')
  }
  if (selectedReceipt?.skipped > 0) {
    selectedReceiptIssues.push(`${selectedReceipt.skipped} source ${selectedReceipt.skipped === 1 ? 'row was' : 'rows were'} not shown.`)
  }
  const memoryEmptyCopy = (receipt, noun, completeCopy) => {
    if (mem.dir == null) return 'No cross-run memory directory is configured.'
    if (receipt?.unavailable) return `No ${noun} can be shown because this memory tier is unavailable.`
    if (memoryTierIncomplete(receipt)) return `No ${noun} are visible in the loaded recent subset.`
    return completeCopy
  }
  return (
    <Panel title="Memory & knowledge — what the runs have learned" sub={memory.data ? (mem.dir || 'no memory dir') : ''} onClose={onClose} wide>
      <div className="conv-toggle memory-tabs" style={{ marginBottom: 12 }}>
        {tabs.map(([k, label, n, incomplete]) => <button key={k} aria-pressed={tab === k} className={'seg' + (tab === k ? ' on' : '')}
          onClick={() => setTab(k)}>{label} <span className="muted">{(k === 'knowledge' ? knowledge : memory).data
            ? `${n}${incomplete ? ' shown' : ''}` : '…'}</span></button>)}
      </div>
      <PanelResourceNotice resource={selectedResource} label={tab === 'knowledge' ? 'Knowledge notes' : 'Cross-run memory'}
        onRetry={tab === 'knowledge' ? retryKnowledge : retryMemory} />
      {tab !== 'knowledge' && memory.state === 'ready' && selectedReceiptIncomplete
        && <MemoryCompletenessNotice resource={memory} onRetry={retryMemory} error={selectedReceipt.unavailable}>
          <b>{selectedTierLabel} data is incomplete.</b>{' '}{selectedReceiptIssues.join(' ')}
        </MemoryCompletenessNotice>}
      {tab === 'knowledge' && knowledge.state === 'ready' && knowledgeIncomplete
        && <MemoryCompletenessNotice resource={knowledge} onRetry={retryKnowledge}>
          <b>Knowledge notes are incomplete.</b>{' '}
          {kb.truncatedFiles > 0 && `${kb.truncatedFiles} Markdown ${kb.truncatedFiles === 1 ? 'entry was' : 'entries were'} not returned. `}
          {truncatedKnowledgePreviews > 0 && `${truncatedKnowledgePreviews} loaded ${truncatedKnowledgePreviews === 1 ? 'preview contains' : 'previews contain'} only a prefix.`}
        </MemoryCompletenessNotice>}
      {/* General orientation shown on every tab; the role-split detail (§role-split) is lessons-only. */}
      <div className="muted" style={{ fontSize: 11, marginBottom: 10, lineHeight: 1.5 }}>
        Cross-run memory reused to guide future runs. Cases, notes and the knowledge base are shared;
        {' '}<b>lessons are split by role</b>.
      </div>
      {tab === 'lessons' && <div className="muted" style={{ fontSize: 11, marginBottom: 10, lineHeight: 1.5 }}>
        The <b>Researcher</b> gets R&D / “what technique to try” lessons; the <b>Developer</b> gets only
        its own “what code change fixed a crash” lessons (untagged/legacy lessons are shared).
      </div>}
      {tab === 'lessons' && <div className="conv-toggle memory-role-tabs" style={{ marginBottom: 8 }}>
        {[['all', 'All'], ['researcher', 'Researcher'], ['developer', 'Developer']].map(([r, label]) =>
          <button key={r} aria-pressed={lessonRole === r} className={'seg' + (lessonRole === r ? ' on' : '')}
            onClick={() => setLessonRole(r)}>{label}</button>)}
      </div>}
      {tab === 'lessons' && (() => {
        // Researcher/Developer filters ALSO include untagged (shared) lessons — mirrors the backend
        // routing where an untagged lesson reaches both roles.
        const shown = (mem.lessons || []).filter(l => lessonRole === 'all' || !l.role || l.role === lessonRole)
        return shown.length
          ? shown.map((l, i) => <div key={i} className="mem-card">
              <div>{l.statement}</div>
              <div className="mem-meta" style={{ marginTop: 4, display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
                <span className="chip xs">{l.role || 'shared'}</span>
                {l.kind && <span className="chip xs">{l.kind}</span>}
                {l.outcome && <span className="chip xs">{l.outcome}</span>}
                {l.delta != null && <span className={'chip xs' + (l.delta > 0 ? ' ok' : '')}>Δ{fmt(l.delta)}</span>}
                {l.confidence != null && <span className="muted" style={{ fontSize: 11 }}>conf {Math.round(l.confidence * 100)}%</span>}
                {l.evidence_count ? <span className="muted" style={{ fontSize: 11 }}>· {l.evidence_count} evidence</span> : null}
                {l.task_id && <span className="muted" style={{ fontSize: 11 }}>· {l.task_id}</span>}
              </div>
            </div>)
          : memory.state === 'ready' && <div className="muted">{memoryEmptyCopy(mem.page?.tiers?.lessons,
            `${lessonRole === 'all' ? '' : lessonRole + ' '}lessons`,
            lessonRole !== 'all' && mem.lessons.length > 0
              ? `No ${lessonRole} lessons match the loaded lessons.`
              : `No ${lessonRole === 'all' ? '' : lessonRole + ' '}lessons yet — they accrue as runs finish (reflection distils them into memory).`)}</div>
      })()}
      {tab === 'cases' && ((mem.cases || []).length
        ? <DataTable caption="Stored memory cases" card={false}><table className="tbl"><thead><tr><th>task</th><th>goal</th><th>metric</th><th>params</th></tr></thead><tbody>
          {mem.cases.map((c, i) => <tr key={i}><td>{c.task_id}</td><td className="muted">{c.goal}</td><td>{fmt(c.metric)}</td><td className="muted">{JSON.stringify(c.params)}
            {c.params_truncated && <span title="Only a bounded parameter projection was returned"
              aria-label="parameters truncated"> · partial</span>}</td></tr>)}</tbody></table></DataTable>
        : memory.state === 'ready' && <div className="muted">{memoryEmptyCopy(mem.page?.tiers?.cases,
          'cases', 'No cases stored.')}</div>)}
      {tab === 'notes' && ((mem.notes || []).length
        ? mem.notes.map((n, i) => <div key={i} className="mem-card">
            {n.task_id && <div className="muted" style={{ fontSize: 11, marginBottom: 2 }}>{n.task_id}</div>}
            <Markdown text={n.note || n.statement || JSON.stringify(n)} /></div>)
        : memory.state === 'ready' && <div className="muted">{memoryEmptyCopy(mem.page?.tiers?.notes,
          'meta-notes', 'No meta-notes yet.')}</div>)}
      {tab === 'knowledge' && (kbFiles.length
        ? <><div className="muted" style={{ fontSize: 11, marginBottom: 6 }}>{kb.dir} — agents save + retrieve these via kb_search</div>
          {kbFiles.map((n, i) => <KbNote key={i} note={n} />)}</>
        : knowledge.state === 'ready' && <div className="muted">{kb.dir == null
          ? 'No knowledge directory is configured.'
          : knowledgeIncomplete ? 'No knowledge notes are visible in the loaded subset.'
            : `No knowledge notes yet (${kb.dir}).`}</div>)}
    </Panel>
  )
}

export function RegistryPanel({ state, onClose }) {
  const [resource, retry] = usePanelResource(signal => get('/api/runs', { signal }), runsPayload)
  const runs = resource.data || []
  // Rank through the list view's own comparator instead of a raw descending sort. A raw sort put the
  // BEST run last on a `direction: 'min'` task, and ranked runs of different tasks / objectives /
  // metric units against each other on one unitless axis. `metricComparable` is where that judgement
  // already lives: one task, one direction, or no ranking at all.
  const rankable = metricComparable(runs)
  const rankedRuns = rankable ? sortRuns(runs, 'metric', 'asc') : runs   // 'asc' = best first
  const champ = state.champion != null ? state.nodes[state.champion] : (state.best_node_id != null ? state.nodes[state.best_node_id] : null)
  return (
    <Panel title="Solution registry & cross-run" onClose={onClose} wide>
      <div className="section-h">Champion (this run)</div>
      {champ ? <div className="kv"><div className="k">node</div><div className="v">#{champ.id} {state.champion != null ? '(promoted)' : '(auto-best)'}</div>
        <div className="k">metric</div><div className="v">{fmt(champ.confirmed_mean ?? champ.metric)}</div></div> : <div className="muted">no champion yet</div>}
      <div className="toolbar" style={{ marginTop: 6 }}>
        <button className="btn sm" onClick={async () => {
          const p = await get(runApiPath(state.run_id, '/prov'))
          const blob = new Blob([JSON.stringify(p, null, 2)], { type: 'application/json' })
          const a = document.createElement('a'); a.href = URL.createObjectURL(blob)
          a.download = `${state.run_id}_prov.json`; a.click(); URL.revokeObjectURL(a.href)
        }}><OpIcon name="download" size={12} /> W3C-PROV graph (JSON)</button>
      </div>
      <div className="section-h">Promotions</div>
      {(state.promotions || []).length
        ? <DataTable caption="Promoted solution nodes" card={false}><table className="tbl"><thead><tr><th>node</th><th>alias</th></tr></thead><tbody>{state.promotions.map((p, i) => <tr key={i}><td>#{p.node_id}</td><td>{p.alias || 'champion'}</td></tr>)}</tbody></table></DataTable>
        : <div className="muted">none — use Promote on a node</div>}
      <div className="section-h">{rankable ? 'Cross-run leaderboard' : 'Cross-run solutions'}</div>
      <PanelResourceNotice resource={resource} label="Cross-run leaderboard" onRetry={retry} />
      {runs.length > 0 && !rankable && <div className="muted" style={{ fontSize: 11, marginBottom: 4 }}>
        Mixed tasks or objectives — listed, not ranked.</div>}
      {/* The caption stays a static literal: dataTableMigration.test.js checks every table region's
          accessible name for uniqueness and meaning, which it can only do statically. So it says what
          the rows ARE, true whether or not they are ranked; the heading and the note above carry the
          ranked/unranked distinction. */}
      {runs.length > 0 && <DataTable caption="Cross-run best metric per run" card={false}><table className="tbl"><thead><tr><th>run</th><th>task</th><th>phase</th><th>best</th><th>nodes</th></tr></thead><tbody>
        {rankedRuns.map(r => <tr key={r.run_id}><td>{r.run_id}</td><td className="muted">{r.task_id}</td><td>{r.phase}</td><td>{fmt(r.best_confirmed ?? r.best_metric)}</td><td>{r.nodes}</td></tr>)}
      </tbody></table></DataTable>}
      {resource.state === 'ready' && !runs.length && <div className="muted">No runs in the registry yet.</div>}
    </Panel>
  )
}

// Live GPU telemetry (nvidia-smi via /api/gpu). Polls while open so an operator can watch
// utilization / VRAM / power during a real training run without leaving the browser.
export function GpuPanel({ onClose }) {
  const [resource, retry] = usePanelResource(signal => get('/api/gpu', { signal }), gpuPayload, '', 2000)
  const data = resource.data
  return (
    <Panel title="GPU monitor" sub="nvidia-smi · live" onClose={onClose} wide>
      <PanelResourceNotice resource={resource} label="GPU telemetry" onRetry={retry} />
      {data && !data.available
        ? <div className="notice">No GPU / nvidia-smi not available on the server host.</div>
        : data && !data.gpus.length
          ? <div className="notice">No GPU devices reported.</div>
        : data?.available && (data.gpus || []).map((g, i, gpus) => {
            const gpuLabel = gpus.length > 1
              ? `GPU ${i + 1} of ${gpus.length} · ${g.name}` : g.name
            const utilizationText = g.util == null ? '—' : `${fmt(g.util)}%`
            const memoryText = g.mem_used == null || g.mem_total == null || g.mem_total <= 0
              ? '—' : `${fmt(g.mem_used)} / ${fmt(g.mem_total)} MiB`
            const temperatureText = g.temp == null ? '—' : `${fmt(g.temp)}°C`
            const powerText = g.power == null ? '—' : `${fmt(g.power)} W`
            return <div key={i} style={{ marginBottom: 16 }}>
              <div className="section-h">{gpuLabel}</div>
              <div className="cardgrid" style={{ marginBottom: 10 }}>
                <Stat n={utilizationText} l="utilization" />
                <Stat n={memoryText} l="memory" />
                <Stat n={temperatureText} l="temperature" />
                <Stat n={powerText} l="power draw" />
              </div>
              <div className="kv">
                <div className="k">GPU util</div><div className="v"><MetricGauge
                  value={g.util} hot label={`${gpuLabel} utilization`}
                  valueText={utilizationText} /></div>
                <div className="k">VRAM</div><div className="v"><MetricGauge
                  value={g.mem_used} max={g.mem_total} label={`${gpuLabel} VRAM usage`}
                  valueText={`${fmt(g.mem_used)} of ${fmt(g.mem_total)} MiB`} /></div>
              </div>
            </div>
          })}
    </Panel>
  )
}

// F1 · Global hyperparameter importance — across ALL evaluated feasible nodes in the run, how
// strongly does each numeric param predict the metric (|Pearson r|)? The per-node Sensitivity panel
// is local (one ablation); this is the run-wide W&B-style "which knobs matter" view. Pure UI.
// The computation lives in report.js::hyperImportance (shared with the Report's Learnings section).
export function HyperImportancePanel({ state, onClose }) {
  const nodes = Object.values(state.nodes).filter(n => n.status === 'evaluated' && n.metric != null && n.feasible !== false)
  const rows = hyperImportance(state)
  const top = rows[0]?.imp || 1
  return (
    <Panel title="Hyperparameter importance" sub={`${nodes.length} evaluated`} onClose={onClose}>
      {rows.length
        ? <><div className="section-h">|correlation| of each param with the metric (run-wide)</div>
            <DataTable caption="Run-wide hyperparameter importance" card={false}><table className="tbl"><thead><tr><th>param</th><th>importance</th><th>r</th><th>n</th><th>relative importance</th></tr></thead><tbody>
              {rows.map(row => <tr key={row.k}>
                <td>{row.k}</td><td>{fmt(row.imp, 3)}</td>
                <td className="muted">{row.r >= 0 ? '+' : ''}{fmt(row.r, 3)}</td><td className="muted">{row.n}</td>
                <td style={{ width: 160 }}><MetricGauge value={row.imp} max={top}
                  label={`${row.k} relative importance`}
                  valueText={`${fmt(row.imp / top * 100, 1)}%`} /></td></tr>)}
            </tbody></table></DataTable>
            <div className="muted" style={{ marginTop: 8 }}>Sign of r shows direction: with a {state.direction === 'min' ? 'minimize' : 'maximize'} objective,
              a {state.direction === 'min' ? 'negative' : 'positive'} r means a larger value tends to help. Needs ≥3 evaluated nodes per param.</div></>
        : <div className="muted">Not enough numeric-param data yet — run more experiments (≥3 evaluated nodes that share a numeric param).</div>}
    </Panel>
  )
}

// Same-task IDs are an operational lookup key, not a ComparisonContract. Keep this
// legacy panel useful for navigation without ranking raw objectives whose metric unit, dataset/eval
// identity, and protocol are absent from /api/runs. The Research Atlas owns contract-bound comparison.
const CROSS_RUN_OBSERVATION_LIMIT = 100

export function CrossRunPanel({ state, onClose }) {
  const [resource, retry] = usePanelResource(signal => get('/api/runs', { signal }), runsPayload)
  const runs = resource.data || []
  const task = typeof state.task_id === 'string' && state.task_id.trim() ? state.task_id : ''
  // an absent task identity is not a shared identity. Never combine legacy rows merely
  // because they all encode the missing value as an empty string.
  const observations = (task ? runs.filter(r => r.task_id === task) : [])
    .map(r => ({ ...r, m: r.best_confirmed ?? r.best_metric }))
    .filter(r => r.m != null)
  const rows = observations.slice(0, CROSS_RUN_OBSERVATION_LIMIT)
  const hidden = observations.length - rows.length
  return (
    <Panel title="Same-task run observations"
      sub={resource.data ? `${observations.length} metric observation${observations.length === 1 ? '' : 's'}` : ''}
      onClose={onClose} wide>
      <PanelResourceNotice resource={resource} label="Cross-run results" onRetry={retry} />
      {resource.data && <div className="panel-resource-toolbar">
        <span className="muted">task ID:</span><code>{task || 'not recorded'}</code>
        <span className="muted">{rows.length} shown · comparison unavailable</span>
      </div>}
      {resource.data && (task
        ? <div className="notice resource-warning" role="status">
            <b>Cross-run ranking unavailable.</b>
            <span> A shared task ID does not bind metric name/unit, dataset and evaluation identity, or a
              comparison protocol. Values below remain per-run observations.</span>
          </div>
        : <div className="notice resource-warning" role="status">
            <b>Same-task observations unavailable.</b>
            <span> This run has no recorded task ID, so no portfolio row can be bound to it. Rows with
              missing identities are never grouped.</span>
          </div>)}
      {rows.length
        ? <DataTable caption="Same-task per-run metric observations" card={false}><table className="tbl"><thead><tr><th>run</th><th>recorded objective</th><th>direction</th><th>nodes</th><th>status</th></tr></thead><tbody>
            {rows.map(r => <tr key={r.run_id}>
              <td><a href={`#/run/${encodeURIComponent(r.run_id)}`}>{r.label || r.run_id}</a></td>
              <td>{fmt(r.m)}{r.best_confirmed != null ? ' (confirmed mean)' : ''}</td>
              <td className="muted">{r.direction}</td><td className="muted">{r.nodes}</td>
              <td className="muted">{r.phase || (r.finished ? 'finished' : '—')}</td></tr>)}
          </tbody></table></DataTable>
        : resource.state === 'ready' && task
          && <div className="muted">No per-run metric observations for this task ID yet.</div>}
      {hidden > 0 && <div className="muted" style={{ marginTop: 8 }}>
        {hidden} additional observation{hidden === 1 ? '' : 's'} omitted by the client render limit.
      </div>}
    </Panel>
  )
}

// Legacy direction board retained as a graceful fallback for pre-Card logs. Current runs use the
// bounded public Card DTO and four generation-fenced, server-stamped operator controls below.
const _HYP_COLUMNS = [
  ['open', 'Open', 'question posed, not yet tested'],
  ['testing', 'Testing', 'experiments running'],
  ['supported', 'Supported', 'an experiment improved'],
  ['tested', 'Tested', 'evaluated, no improvement'],
  ['abandoned', 'Abandoned', 'dropped'],
]
// Monochrome source glyphs (no emoji): who posed the hypothesis. Reuses the shared icon set.
const _HYP_ICON = { researcher: 'search', deep_research: 'bulb', human: 'user', strategist: 'compass' }

const _CARD_COLUMNS = [
  ['proposed', 'Proposed', 'work item is open and has not started'],
  // these speculative lanes are unreachable from the production Card projection:
  // requested/done receipts live only in recovery journals, while public status derives from
  // proposed/building/running/gated/evaluated/dropped. Paid in-flight or commit-buffered work therefore
  // appears Proposed; project a bounded speculative owner state before advertising these lanes.
  ['speculating', 'Speculating', 'speculative build requested'],
  ['building', 'Building', 'code is being produced'],
  ['built-awaiting-commit', 'Awaiting commit', 'build finished; durable node commit is pending'],
  ['coded', 'Coded', 'code exists and is waiting to run'],
  ['running', 'Running', 'evaluation is in flight'],
  ['evaluated', 'Evaluated', 'evidence has reached a verdict'],
  ['gated', 'Gated', 'trust or breeding gates exclude the available evidence'],
  ['dropped', 'Dropped', 'operator or engine removed the work item'],
]
const _CARD_FROZEN_STATUSES = new Set(['proposed', 'building', 'coded', 'running', 'evaluated', 'gated', 'dropped'])
const _CARD_OPTIONAL_STATUSES = new Set(['speculating', 'built-awaiting-commit'])
const _CARD_ICON = {
  researcher: 'search', deep_research: 'bulb', human: 'user', strategist: 'compass',
  operator: 'user', engine: 'bot', novelty: 'bulb',
}
const _CARD_RENDER_LIMIT = 256 // mirrors PUBLIC_CARD_MAX_COUNT at the wire boundary

const _cardText = value => typeof value === 'string' && value.trim() ? value.trim() : null
const _cardNumber = value => typeof value === 'number' && Number.isFinite(value) ? value : null
const _cardInt = value => Number.isSafeInteger(value) && value >= 0 ? value : null
const _cardRefs = value => Array.isArray(value)
  ? value.filter(item => typeof item === 'string' && item).slice(0, 32) : []
const _cardNodes = value => Array.isArray(value)
  ? value.filter(item => Number.isSafeInteger(item) && item >= 0).slice(0, 32) : []

function _cardStatus(card) {
  return _cardText(card.status) || 'unknown'
}

function _cardStatusLabel(status) {
  return status.split(/[-_]/).filter(Boolean)
    .map(word => word.charAt(0).toUpperCase() + word.slice(1)).join(' ') || 'Other'
}

function _cardRows(state) {
  if (!isRecord(state?.cards)) return []
  return Object.entries(state.cards)
    .filter(([id, card]) => typeof id === 'string' && id && isRecord(card))
    .slice(0, _CARD_RENDER_LIMIT)
    // The mapping key is authoritative. Never let a malformed/spoofed body id change joins or receipts.
    .map(([id, card]) => ({ ...card, id }))
}

function _cardLanes(cards) {
  const occupied = new Set(cards.map(_cardStatus))
  const configured = _CARD_COLUMNS.filter(([status]) => !_CARD_OPTIONAL_STATUSES.has(status) || occupied.has(status))
  const known = new Set(_CARD_COLUMNS.map(([status]) => status))
  const extra = [...occupied].filter(status => !known.has(status)).sort()
    .map(status => [status, _cardStatusLabel(status), 'new derived lifecycle status'])
  const dropped = configured.find(([status]) => status === 'dropped')
  return [...configured.filter(([status]) => status !== 'dropped'), ...extra, dropped].filter(Boolean)
}

function _cardOrder(a, b) {
  const ap = _cardNumber(a.priority) ?? _cardNumber(a.foresight_rank) ?? Infinity
  const bp = _cardNumber(b.priority) ?? _cardNumber(b.foresight_rank) ?? Infinity
  if (ap !== bp) return ap - bp
  const an = _cardInt(a.created_at_node) ?? Infinity
  const bn = _cardInt(b.created_at_node) ?? Infinity
  return an - bn || a.id.localeCompare(b.id)
}

const _CARD_CONTROL_KINDS = ['edit', 'priority', 'resources', 'drop', 'abandon']

function _cardResourceValues(value) {
  if (!isRecord(value)) return null
  const gpus = _cardInt(value.gpus)
  const gpuMem = _cardInt(value.gpu_mem_mib)
  return gpus == null && gpuMem == null ? null : {
    ...(gpus == null ? {} : { gpus }),
    ...(gpuMem == null ? {} : { gpu_mem_mib: gpuMem }),
  }
}

function _sameCardResourceValues(left, right) {
  const a = _cardResourceValues(left)
  const b = _cardResourceValues(right)
  if (a == null || b == null) return a === b
  return ['gpus', 'gpu_mem_mib'].every(key => (
    Object.hasOwn(a, key) === Object.hasOwn(b, key) && a[key] === b[key]
  ))
}

function cardControlReflected(card, kind, patch, baseline, expectedEventSeq) {
  if (!card || !isRecord(patch)) return false
  if (kind === 'edit') {
    // Modern folds publish the exact durable event that owns the display overlay. This remains
    // reliable when public-state secret redaction transforms the text into a non-prefix value.
    return cardEditReflected(card, patch, baseline, expectedEventSeq)
  }
  if (kind === 'priority') return card.priority === patch.priority && card.pinned === true
  if (kind === 'resources') return _sameCardResourceValues(card.resource_pin, patch.resource_pin)
  if (kind === 'drop') return card.status === 'dropped'
    && (!patch.dropped_reason || card.dropped_reason === patch.dropped_reason)
  if (kind === 'abandon') return card.verdict === 'abandoned'
  return false
}

function _cardWithOptimisticControls(card, controlState) {
  if (!isRecord(controlState?.updates)) return card
  const visible = { ...card }
  for (const kind of _CARD_CONTROL_KINDS) {
    if (isRecord(controlState.updates[kind])) Object.assign(visible, controlState.updates[kind])
  }
  return visible
}

function _cardResourceSummary(value, { unavailable = 'unspecified' } = {}) {
  const footprint = _cardResourceValues(value)
  if (!footprint) return unavailable
  const gpus = footprint.gpus
  const memory = footprint.gpu_mem_mib
  return [
    gpus == null ? 'GPU count unspecified'
      : gpus === 0 ? 'CPU only' : `${gpus} GPU${gpus === 1 ? '' : 's'}`,
    memory == null ? null : `${fmtInt(memory)} MiB/GPU`,
  ].filter(Boolean).join(' · ')
}

function _CardProjectionNotice({ projection, cards }) {
  if (!isRecord(projection)) return <div className="card-projection-note" role="status">
    Card coverage receipt unavailable; this older payload may be incomplete.
  </div>
  if (projection.complete === true) return null
  const total = _cardInt(projection.total)
  const returned = _cardInt(projection.returned) ?? cards.length
  const sourceInvalid = projection.source_valid === false
  return <div className="card-projection-note" role="status">
    <OpIcon name="alert" size={12} />
    <span>{sourceInvalid
      ? 'Card source was invalid; no complete board can be claimed.'
      : `Showing ${returned}${total == null ? '' : ` of ${total}`} Cards; clipped or redacted public fields are marked partial.`}</span>
  </div>
}

function _CardKanbanCard({
  card, receipt, onSelect, onClose, onControl, controlState, controlsLocked,
}) {
  const statement = _cardText(card.statement) || `Card ${card.id}`
  const source = _cardText(card.source)
  const operator = _cardText(card.operator)
  const evalProfile = _cardText(card.eval_profile)
  const params = isRecord(card.params) ? Object.entries(card.params)
    .filter(([, value]) => _cardNumber(value) != null).slice(0, 6) : []
  const spaceCount = isRecord(card.space) ? Object.keys(card.space).length : 0
  const footprintKnown = Object.hasOwn(card, 'footprint')
  const baseFootprint = isRecord(card.footprint) ? card.footprint : null
  const resourcePin = isRecord(card.resource_pin) ? card.resource_pin : null
  // This is the configured Card footprint after applying the operator override. Runtime scheduling may
  // still allocate less, so never label the client-side projection as an effective allocation.
  const configuredFootprint = baseFootprint || resourcePin
    ? { ...(_cardResourceValues(baseFootprint) || {}), ...(_cardResourceValues(resourcePin) || {}) }
    : null
  if (configuredFootprint?.gpus === 0) delete configuredFootprint.gpu_mem_mib
  const configuredGpus = configuredFootprint ? _cardInt(configuredFootprint.gpus) : null
  const pinValues = _cardResourceValues(resourcePin)
  const formGpus = pinValues?.gpus ?? configuredGpus
  const formGpuMem = pinValues && Object.hasOwn(pinValues, 'gpu_mem_mib')
    ? pinValues.gpu_mem_mib : null
  const evalTimeout = _cardNumber(card.eval_timeout)
  const identity = isRecord(card.identity) ? card.identity : null
  const selection = isRecord(card.selection_provenance) ? card.selection_provenance : null
  const blockersKnown = Object.hasOwn(card, 'selection_blockers')
  const blockers = _cardRefs(card.selection_blockers)
  const evidenceKnown = Object.hasOwn(card, 'evidence')
  const evidence = _cardNodes(card.evidence).slice(0, 8)
  const concepts = _cardRefs(card.concept_tags).slice(0, 5)
  const parents = _cardNodes(card.parent_ids)
  const parent = _cardInt(card.parent_id)
  if (parent != null && !parents.includes(parent)) parents.unshift(parent)
  const parentGenerations = isRecord(card.parent_generations) ? card.parent_generations : null
  const scoredAgainst = _cardInt(card.scored_against)
  const scoredAgainstGeneration = _cardInt(card.scored_against_generation)
  const parentLineage = parents.map(id => {
    const attempt = parentGenerations ? _cardInt(parentGenerations[String(id)]) : null
    return `#${id} · attempt ${attempt == null ? 'unknown' : attempt}`
  }).join(', ')
  const scoredLineage = scoredAgainst == null ? ''
    : `#${scoredAgainst} · attempt ${scoredAgainstGeneration == null ? 'unknown' : scoredAgainstGeneration}`
  const bestDelta = _cardNumber(card.best_delta)
  const priority = _cardNumber(card.priority)
  const novelty = isRecord(card.novelty_verdict) ? _cardText(card.novelty_verdict.grade) : null
  const omissionCount = isRecord(receipt?.omissions) ? Object.keys(receipt.omissions).length : 0
  const declaredResources = footprintKnown
    ? _cardResourceSummary(baseFootprint) : 'resource projection unavailable'
  const configuredResources = _cardResourceSummary(configuredFootprint)
  const pinResources = _cardResourceSummary(resourcePin)
  const provenanceBits = [
    source && `source ${source}`,
    identity && _cardText(identity.kind) && `identity ${identity.kind}`,
    _cardText(card.provenance_tier) && `tier ${card.provenance_tier}`,
    selection && _cardText(selection.action_source) && `action ${selection.action_source}`,
    baseFootprint && _cardText(baseFootprint.proposed_by) && `proposed ${baseFootprint.proposed_by}`,
    baseFootprint && _cardText(baseFootprint.finalized_by) && `finalized ${baseFootprint.finalized_by}`,
    resourcePin && _cardText(resourcePin.pinned_by) && `resource pin ${resourcePin.pinned_by}`,
    _cardText(card.research_origin) && `research ${card.research_origin}`,
  ].filter(Boolean)
  const [statementDraft, setStatementDraft] = useState(statement)
  const [priorityDraft, setPriorityDraft] = useState(
    _cardInt(card.priority) == null ? '' : String(card.priority + 1))
  const [gpuDraft, setGpuDraft] = useState(formGpus == null ? '' : String(formGpus))
  const [memoryDraft, setMemoryDraft] = useState(formGpuMem == null ? '' : String(formGpuMem))
  const [dropReason, setDropReason] = useState('operator dropped')
  const [controlError, setControlError] = useState('')
  const ownPending = isRecord(controlState?.pending) ? controlState.pending : null
  const busy = !!ownPending || controlsLocked === true
  // The research VERDICT (open/supported/testing/tested/abandoned — the only values `_evidence_verdict`
  // produces) is distinct from the work-lifecycle STATUS (peer review): replay can publish
  // status=proposed/evaluated with verdict=abandoned, so read the verdict separately — render it as its
  // own chip (a supported/tested outcome was otherwise invisible) and treat an abandoned belief as
  // terminal so the board stops offering edit/priority/drop controls.
  const verdict = _cardText(card.verdict)
  const terminal = _cardStatus(card) === 'dropped' || !!_cardText(card.merged_into)
    || verdict === 'abandoned'
  // Re-seed each draft ONLY when its own folded source (or the card identity) changes. A single effect
  // over every dep re-ran on ANY change, so an unrelated live fold (e.g. a card_ranked priority bump
  // arriving while the operator is typing a new statement) reset ALL four drafts and silently discarded
  // the in-progress edits in the other fields. Per-field effects keep each edit until its own source moves.
  useEffect(() => { setStatementDraft(statement) }, [card.id, statement])
  useEffect(() => {
    setPriorityDraft(_cardInt(card.priority) == null ? '' : String(card.priority + 1))
  }, [card.id, card.priority])
  useEffect(() => { setGpuDraft(formGpus == null ? '' : String(formGpus)) }, [card.id, formGpus])
  useEffect(() => {
    setMemoryDraft(formGpuMem == null ? '' : String(formGpuMem))
  }, [card.id, formGpuMem])

  const control = async (kind, data, patch) => {
    if (!onControl || busy) return
    setControlError('')
    try { await onControl(card, kind, data, patch) }
    catch (error) { setControlError(error?.message || String(error)) }
  }
  const saveStatement = () => {
    const next = statementDraft.trim()
    if (!next || next.length > 4000) { setControlError('Display statement must be 1–4000 characters.'); return }
    if (next !== statement) control('edit', { statement: next }, { statement: next })
  }
  const savePriority = () => {
    const visible = Number(priorityDraft)
    if (!Number.isSafeInteger(visible) || visible < 1 || visible > 256) {
      setControlError('Priority must be between 1 and 256.'); return
    }
    control('priority', { priority: visible - 1 }, { priority: visible - 1 })
  }
  const saveResources = () => {
    const nextGpus = Number(gpuDraft)
    const nextMemory = memoryDraft.trim() === '' ? null : Number(memoryDraft)
    if (!Number.isSafeInteger(nextGpus) || nextGpus < 0
      || (nextMemory != null && (!Number.isSafeInteger(nextMemory) || nextMemory < 0))) {
      setControlError('GPU count and memory must be non-negative integers.'); return
    }
    if (nextGpus === 0 && nextMemory != null) {
      setControlError('CPU-only Cards cannot request GPU memory.'); return
    }
    // This local patch contains quantitative display values only. Authority/provenance is stamped by
    // the server event and is never supplied by the browser, even optimistically.
    const pin = { gpus: nextGpus, ...(nextMemory == null ? {} : { gpu_mem_mib: nextMemory }) }
    control('resources', { gpus: nextGpus, gpu_mem_mib: nextMemory }, { resource_pin: pin })
  }
  const drop = () => {
    const reason = dropReason.trim() || 'operator dropped'
    control('drop', { reason }, { status: 'dropped', dropped_reason: reason })
  }
  // This is deliberately Card-scoped: the backend receives one Card id, so siblings that happen to
  // share a seed remain unchanged. The control changes the Card's research verdict, not its work lane.
  const abandonCard = () => control('abandon', {}, { verdict: 'abandoned' })
  return <article className="card-kanban-card" data-card-id={card.id} aria-label={statement}
    aria-busy={ownPending ? 'true' : undefined}>
    <div className="card-kanban-stmt">
      <span className="hyp-src" title={source ? `source: ${source}` : 'source unavailable'}>
        <OpIcon name={_CARD_ICON[source] || 'dot'} size={12} />
      </span>
      <span>{statement}</span>
    </div>
    <div className="card-kanban-meta">
      <span className="chip xs" title="durable Card identity">{card.id}</span>
      {verdict && verdict !== 'open' && <span
        className={'chip xs ' + (verdict === 'supported' ? 'ok' : verdict === 'abandoned' ? 'warn' : '')}
        title={`research verdict: ${verdict} (distinct from the work status)`}>{verdict}</span>}
      {priority != null && <span className="chip xs" title="derived priority; 1 is highest">#{priority + 1}</span>}
      {card.pinned === true && <span className="chip xs warn"><OpIcon name="flag" size={10} /> pinned</span>}
      {card.selection_ready === true
        ? <span className="chip xs ok" title="eligible for Card-driven selection">selection ready</span>
        : card.selection_ready === false
          ? <span className="chip xs warn" title="not eligible for Card-driven selection">not selection ready</span>
          : <span className="chip xs" title="selection readiness was not present in the public projection">readiness unknown</span>}
      {receipt && receipt.complete !== true && <span className="chip xs warn"
        title={`${omissionCount} public field omission${omissionCount === 1 ? '' : 's'}`}>
        partial details{omissionCount ? ` · ${omissionCount}` : ''}</span>}
    </div>
    {(operator || evalProfile || params.length || spaceCount) && <div className="card-kanban-fact">
      <span className="card-kanban-k">Action</span>
      <span>{operator || 'operator unspecified'}</span>
      {evalProfile && <span>profile {evalProfile}</span>}
      {params.map(([key, value]) => <span key={key} className="card-param">{key}={fmt(value)}</span>)}
      {spaceCount > 0 && <span>{spaceCount} search variable{spaceCount === 1 ? '' : 's'}</span>}
    </div>}
    <div className="card-kanban-fact">
      <span className="card-kanban-k">Declared</span>
      <span>{declaredResources}{evalTimeout == null ? '' : ` · ${fmt(evalTimeout)}s timeout`}</span>
    </div>
    {resourcePin && <div className="card-kanban-fact card-resource-pin">
      <span className="card-kanban-k">Configured pin</span>
      <span><span className="chip xs warn">{_cardText(resourcePin.pinned_by) === 'operator'
        ? 'operator override' : 'pending operator override'}</span> {configuredResources}
        <span className="card-resource-request">requested {pinResources}</span></span>
    </div>}
    <div className="card-kanban-fact">
      <span className="card-kanban-k">Provenance</span>
      <span>{provenanceBits.length ? provenanceBits.join(' · ') : 'unavailable'}</span>
    </div>
    <div className="card-kanban-fact">
      <span className="card-kanban-k">Gate</span>
      <span>{selection && _cardText(selection.freshness) ? `freshness ${selection.freshness}` : 'freshness unknown'}
        {selection && _cardText(selection.owner_state) ? ` · owner ${selection.owner_state}` : ''}
        {selection && typeof selection.action_complete === 'boolean'
          ? ` · action ${selection.action_complete ? 'complete' : 'incomplete'}` : ''}</span>
    </div>
    {blockers.length > 0 && <div className="card-kanban-blockers" aria-label="Selection blockers">
      {blockers.slice(0, 5).map(blocker => <span key={blocker} className="chip xs warn">
        {blocker.replaceAll('_', ' ')}</span>)}
      {blockers.length > 5 && <span className="muted">+{blockers.length - 5}</span>}
    </div>}
    {!blockersKnown && <div className="muted card-kanban-unknown">Selection blockers unavailable</div>}
    {(parents.length > 0 || scoredAgainst != null) && <div className="card-kanban-fact">
      <span className="card-kanban-k">Lineage</span>
      <span>{parents.length ? `parent ${parentLineage}` : ''}
        {scoredAgainst != null ? `${parents.length ? ' · ' : ''}scored vs ${scoredLineage}` : ''}</span>
    </div>}
    {(concepts.length > 0 || novelty) && <div className="card-kanban-tags">
      {novelty && <span className="chip xs">novelty {novelty}</span>}
      {concepts.map(concept => <span key={concept} className="chip xs">{concept}</span>)}
    </div>}
    {(_cardText(card.merged_into) || _cardText(card.dropped_reason)) && <div className="card-kanban-terminal">
      {_cardText(card.merged_into) ? `Merged into ${card.merged_into}` : card.dropped_reason}
      {_cardText(card.dropped_by) ? ` · by ${card.dropped_by}` : ''}
    </div>}
    <div className="card-kanban-evidence">
      {evidence.map(nid => <button key={nid} type="button" className="btn xs ghost"
        aria-label={`Open evidence node #${nid}`} title={`evidence node #${nid}`}
        onClick={() => { onSelect?.(nid); onClose?.() }}>#{nid}</button>)}
      {bestDelta != null && <span className={'chip xs ' + (bestDelta > 0 ? 'ok' : '')}
        title="best improvement over parent among the evidence">Δ{fmt(bestDelta)}</span>}
      {evidence.length === 0 && <span className="muted">
        {evidenceKnown ? 'No evidence nodes' : 'Evidence unavailable'}</span>}
    </div>
    {onControl && !terminal && <details className="card-kanban-controls">
      <summary aria-label={`Operator controls for ${card.id}`}>Operator controls</summary>
      <form className="card-control-form" onSubmit={event => { event.preventDefault(); saveStatement() }}>
        <label><span>Display statement</span><textarea className="text card-control-statement"
          aria-label={`Display statement for ${card.id}`} rows="3" value={statementDraft}
          maxLength={4000} disabled={busy} onChange={event => setStatementDraft(event.target.value)} /></label>
        <button type="submit" className="btn xs" disabled={busy || !statementDraft.trim()
          || statementDraft.trim() === statement}>Save text</button>
      </form>
      <form className="card-control-form" onSubmit={event => { event.preventDefault(); savePriority() }}>
        <label><span>Priority (1 is highest)</span><input className="text" type="number" min="1" max="256"
          aria-label={`Priority for ${card.id}`} value={priorityDraft} disabled={busy}
          onChange={event => setPriorityDraft(event.target.value)} /></label>
        <button type="submit" className="btn xs" disabled={busy || !priorityDraft}>Pin priority</button>
      </form>
      <form className="card-control-resource" onSubmit={event => { event.preventDefault(); saveResources() }}>
        <fieldset disabled={busy}>
          <legend>Configured resource override</legend>
          <div className="card-control-resource-fields">
            <label><span>GPUs</span><input className="text" type="number" min="0" step="1"
              aria-label={`GPU count for ${card.id}`} value={gpuDraft}
              onChange={event => {
                setGpuDraft(event.target.value)
                if (event.target.value === '0') setMemoryDraft('')
              }} /></label>
            <label><span>MiB / GPU</span><input className="text" type="number" min="0" step="1"
              aria-label={`GPU memory in MiB for ${card.id}`} placeholder="inherit declared"
              value={memoryDraft} disabled={busy || gpuDraft === '0'}
              onChange={event => setMemoryDraft(event.target.value)} /></label>
          </div>
          <div className="card-control-help">Validated against the current server GPU envelope;
            blank memory inherits the declared value. Execution may still wait for local GPU admission.</div>
          <button type="submit" className="btn xs" disabled={busy || gpuDraft === ''}>Pin resources</button>
        </fieldset>
      </form>
      <div className="card-control-form">
        <button type="button" className="btn xs" disabled={busy} onClick={abandonCard}
          title="Mark only this Card’s research verdict abandoned; sibling Cards stay unchanged and this Card remains visible">
          Abandon this Card
        </button>
      </div>
      <details className="card-control-danger">
        <summary>Drop Card…</summary>
        <form className="card-control-form" onSubmit={event => { event.preventDefault(); drop() }}>
          <label><span>Reason (optional)</span><input className="text" value={dropReason} maxLength={400}
            aria-label={`Drop reason for ${card.id}`} disabled={busy}
            onChange={event => setDropReason(event.target.value)} /></label>
          <button type="submit" className="btn xs danger" disabled={busy}>Confirm drop</button>
        </form>
      </details>
      {controlsLocked && !ownPending && <div className="card-control-feedback" role="status">
        Another Card command is still being submitted for this run.</div>}
    </details>}
    {isRecord(controlState?.notice) && <div
      className={'card-control-feedback ' + (controlState.notice.tone || '')}
      role={controlState.notice.tone === 'error' ? 'alert' : 'status'} aria-live="polite">
      {controlState.notice.text}</div>}
    {controlError && <div className="card-control-feedback error" role="alert">{controlError}</div>}
  </article>
}

function _CardKanban({ state, cards, runId, onSelect, onClose, onToast }) {
  const [optim, setOptim] = useState({})
  const [addDraft, setAddDraft] = useState('')
  const inFlight = useRef(new Set())
  const activeRef = useRef(true)
  useEffect(() => {
    activeRef.current = true
    return () => { activeRef.current = false }
  }, [])
  // Last edit statement SUBMITTED per card id. It outlives the optimistic override (which clears on a
  // success ack before the SSE fold arrives), so a chained extend edit can baseline against the prior
  // in-flight submission instead of a stale fold — see the editBaseline capture in cardControl.
  const sentEditRef = useRef({})
  const cardsById = new Map(cards.map(card => [card.id, card]))
  const cardsByIdRef = useRef(cardsById)
  cardsByIdRef.current = cardsById
  useEffect(() => {
    // Prune sentEditRef for cards no longer on the board: the ref outlives the optimistic override
    // (needed so a chained extend edit can baseline against the prior in-flight submission), so without
    // this it accumulates one entry per distinct id ever edited AND a recreated same id would inherit a
    // vanished card's last submission as a stale edit baseline. Bound it to live cards.
    for (const id of Object.keys(sentEditRef.current)) {
      if (!cardsByIdRef.current.has(id)) delete sentEditRef.current[id]
    }
    setOptim(current => {
      let changed = false
      const next = { ...current }
      for (const [id, entry] of Object.entries(current)) {
        const card = cardsByIdRef.current.get(id)
        if (!card) { delete next[id]; changed = true; continue }
        const updates = { ...(entry.updates || {}) }
        for (const kind of _CARD_CONTROL_KINDS) {
          if (updates[kind]
              && cardControlReflected(
                card, kind, updates[kind], entry.editBaseline, entry.editEventSeq)) {
            delete updates[kind]
            changed = true
          }
        }
        const pending = entry.pending && updates[entry.pending.kind] ? entry.pending : null
        if (pending !== entry.pending) changed = true
        if (Object.keys(updates).length === 0 && !pending) {
          delete next[id]
          changed = true
        } else if (changed || pending !== entry.pending) {
          next[id] = { ...entry, updates, pending }
        }
      }
      return changed ? next : current
    })
  }, [state.cards])
  const visibleCards = cards.map(card => _cardWithOptimisticControls(card, optim[card.id]))
  // A 'confirmation-unknown' pending (a lost/uncertain submission) MAY never self-clear: if the intent
  // never actually landed, the fold never reflects it and the reconcile effect above never drops it (if
  // it DID land, that effect clears it normally). Because it can hang indefinitely, it must not count
  // toward the board-wide lock, or one uncertain command would freeze the controls on EVERY Card until
  // reload. The real concurrency guard is `inFlight` (released in the finally), and the stuck Card still
  // shows its own 'waiting for the live fold' notice via its own pending. Only genuinely-progressing
  // pendings gate the rest of the board.
  const globalPending = Object.values(optim).some(
    entry => isRecord(entry?.pending) && entry.pending.phase !== 'confirmation-unknown')
  const cardControl = async (card, kind, data, patch) => {
    const labels = {
      edit: { saving: 'Saving Card display text…', success: 'Card display text updated', failure: 'Could not edit Card' },
      priority: { saving: 'Pinning Card priority…', success: 'Card priority pinned', failure: 'Could not pin Card priority' },
      resources: { saving: 'Pinning Card resources…', success: 'Card resources pinned', failure: 'Could not pin Card resources' },
      drop: { saving: 'Dropping Card…', success: 'Card dropped', failure: 'Could not drop Card' },
      abandon: { saving: 'Abandoning this Card…', success: 'Card abandoned', failure: 'Could not abandon Card' },
    }[kind]
    if (!labels || inFlight.current.size > 0) {
      const message = 'Another Card command is still being submitted for this run.'
      onToast?.(message)
      return { kind: 'pending', message }
    }
    inFlight.current.add(card.id)
    // Baseline for edit-reflection = the value the card shows JUST BEFORE this edit. Normally that is the
    // current fold, but for a CHAINED edit the prior edit may not have folded yet, so the visible fold is
    // stale (one step behind). Use the prior SUBMITTED statement when the current card is a proper prefix
    // of it (we are still catching up to that earlier edit); otherwise the card has already moved on, so
    // the current statement is right. This self-cleans: once the fold reaches the prior submission the
    // prefix test fails and we fall back to the fold. (See `cardControlReflected`.)
    let editBaseline
    if (kind === 'edit' && typeof card.statement === 'string') {
      const prior = sentEditRef.current[card.id]
      editBaseline = (typeof prior === 'string' && prior !== card.statement
        && prior.startsWith(card.statement)) ? prior : card.statement
      if (typeof patch.statement === 'string') sentEditRef.current[card.id] = patch.statement
    }
    // Only this submission's receipt may satisfy the edit fence; a chained edit resets the previous seq.
    setOptim(current => cardControlSubmission(
      current, card.id, kind, patch, editBaseline, labels.saving))
    try {
      const record = kind === 'edit'
        ? await CONTROL.editCard(runId, card.id, data.statement)
        : kind === 'priority'
          ? await CONTROL.reprioritizeCard(runId, card.id, data.priority)
          : kind === 'resources'
            ? await CONTROL.pinCardResources(runId, card.id, data.gpus, data.gpu_mem_mib)
            : kind === 'abandon'
              ? await CONTROL.abandonHypothesis(runId, card.id)
              : await CONTROL.dropCard(runId, card.id, data.reason)
      if (!activeRef.current) return { kind: 'stale', message: 'Card board scope changed' }
      const feedback = commandFeedback(record, {
        success: labels.success, noop: `${labels.success} (already current)`,
        executing: `${labels.success} — waiting for the live fold`, failure: labels.failure,
      })
      const recordEditSeq = kind === 'edit' ? _cardInt(record?.event_seq) : null
      onToast?.(feedback.message)
      setOptim(current => {
        const entry = current[card.id]
        if (!entry) return current
        const updates = { ...(entry.updates || {}) }
        const rawCard = cardsByIdRef.current.get(card.id)
        const editEventSeq = recordEditSeq ?? entry.editEventSeq
        // Clear the optimistic override once the command DEFINITIVELY settles (success/noop/error) — not
        // only on an exact fold reflection: the server may clip/redact the value, so waiting for a
        // byte-equal fold would leave the card stuck showing operator text with its controls disabled.
        // Only a 'pending' (accepted, engine will apply later) settle keeps the override until the fold.
        const settled = ['error', 'success', 'noop'].includes(feedback.kind)
        if (settled || cardControlReflected(
          rawCard, kind, patch, entry.editBaseline, editEventSeq)) {
          delete updates[kind]
        }
        const pending = feedback.kind === 'pending' && updates[kind]
          ? { kind, phase: 'waiting-for-fold' } : null
        const notice = feedback.kind === 'error'
          ? { tone: 'error', text: feedback.message }
          : { tone: feedback.kind === 'pending' ? 'pending' : 'success', text: feedback.message }
        return { ...current, [card.id]: {
          ...entry, updates, pending, notice,
          ...(editEventSeq == null ? {} : { editEventSeq }),
        } }
      })
      return feedback
    } catch (error) {
      if (!activeRef.current) return { kind: 'stale', message: 'Card board scope changed' }
      const uncertain = error?.submissionMayHaveSucceeded === true || error?.commandUnknown === true
        || ['accepted', 'executing'].includes(error?.commandRecord?.status)
      const commandEditSeq = kind === 'edit' ? _cardInt(error?.commandRecord?.event_seq) : null
      const message = uncertain
        ? `${labels.success} may still complete — waiting for the live fold`
        : `${labels.failure}: ${error?.message || error}`
      onToast?.(message)
      setOptim(current => {
        const entry = current[card.id]
        if (!entry) return current
        const updates = { ...(entry.updates || {}) }
        if (!uncertain) delete updates[kind]
        return { ...current, [card.id]: {
          ...entry, updates,
          ...(commandEditSeq == null ? {} : { editEventSeq: commandEditSeq }),
          pending: uncertain ? { kind, phase: 'confirmation-unknown' } : null,
          notice: { tone: uncertain ? 'pending' : 'error', text: message },
        } }
      })
      return { kind: uncertain ? 'pending' : 'error', message }
    } finally {
      inFlight.current.delete(card.id)
    }
  }
  const projection = isRecord(state.cards_projection) ? state.cards_projection : null
  const receipts = isRecord(projection?.items) ? projection.items : {}
  const lanes = _cardLanes(visibleCards)
  const total = _cardInt(projection?.total)
  const sub = total != null && total !== cards.length
    ? `${visibleCards.length} of ${total} public work items` : `${visibleCards.length} work item${visibleCards.length === 1 ? '' : 's'}`
  // A card is born as a hypothesis (peer review): keep the "+ Add" belief affordance on the
  // authoritative Card board, not only the empty-Card fallback — otherwise the operator loses the
  // documented control the moment the first card exists. Wired to the same addHypothesis control.
  const canAdd = typeof runId === 'string' && !!runId
  const addCard = async () => {
    const s = addDraft.trim()
    if (!s) return
    try {
      const feedback = commandFeedback(await CONTROL.addHypothesis(runId, s), {
        success: 'Card added', noop: 'That hypothesis was already tracked',
        executing: 'Card requested — waiting for the run', failure: 'Could not add Card',
      })
      if (feedback.kind === 'success') setAddDraft('')
      onToast?.(feedback.message)
    } catch (error) { onToast?.(`Could not add Card: ${error.message || error}`) }
  }
  return <Panel title="Cards" sub={sub} onClose={onClose} wide>
    <_CardProjectionNotice projection={projection} cards={visibleCards} />
    {canAdd && <div className="toolbar" style={{ marginBottom: 10, gap: 6 }}>
      <input className="text" style={{ flex: 1 }} aria-label="New hypothesis"
        placeholder="Pose a hypothesis to test (e.g. “target is right-skewed; a log transform helps”)"
        value={addDraft} onChange={e => setAddDraft(e.target.value)}
        onKeyDown={e => { if (e.key === 'Enter') addCard() }} />
      <button className="btn sm primary" onClick={addCard} disabled={!addDraft.trim()}>+ Add</button>
    </div>}
    <div className="card-board" role="region" aria-label="Card lifecycle kanban">
      {lanes.map(([key, label, hint]) => {
        const rows = visibleCards.filter(card => _cardStatus(card) === key).sort(_cardOrder)
        const tone = _CARD_FROZEN_STATUSES.has(key) ? ` card-${key}` : ''
        const laneId = `card-lane-${encodeURIComponent(key)}`
        return <section key={key} className={'card-col' + tone} aria-labelledby={laneId}>
          <h3 id={laneId} className="card-col-h" title={hint}>
            {label} <span className="muted">{rows.length}</span>
          </h3>
          {rows.map(card => <_CardKanbanCard key={card.id} card={card}
            receipt={isRecord(receipts[card.id]) ? receipts[card.id] : null}
            controlState={optim[card.id]} controlsLocked={globalPending && !optim[card.id]?.pending}
            onSelect={onSelect} onClose={onClose}
            onControl={typeof runId === 'string' && runId ? cardControl : null} />)}
          {rows.length === 0 && <div className="muted card-empty">—</div>}
        </section>
      })}
    </div>
  </Panel>
}

const HYPOTHESIS_DELETE_STORAGE_PREFIX = 'll.hypothesis-delete.'
const HYPOTHESIS_DELETE_COMMAND_RE = /^cmd_[0-9a-f]{32}$/
const HYPOTHESIS_DELETE_PENDING = new Set(['submitting', 'accepted', 'executing'])
const HYPOTHESIS_DELETE_TERMINAL = new Set(['succeeded', 'noop', 'failed', 'timed_out', 'rejected'])
const HYPOTHESIS_DELETE_STORED = new Set([
  ...HYPOTHESIS_DELETE_PENDING, 'failed', 'timed_out', 'rejected',
])
const HYPOTHESIS_DELETE_KEYS = new Set([
  'runId', 'expectedGeneration', 'hypothesisId', 'idempotencyKey', 'commandId', 'status', 'updatedAt',
])
const canonicalHypothesisId = value => typeof value === 'string' && value === value.trim()
  && value.length > 0 && [...value].length <= 256 && !/\p{C}/u.test(value)
const ownHypothesisEntry = (collection, id) => collection
  && Object.hasOwn(collection, String(id)) ? collection[String(id)] : null
const exactHypothesisDeleteRecord = (intent, record) => !!intent && isRecord(record)
  && typeof record.id === 'string' && HYPOTHESIS_DELETE_COMMAND_RE.test(record.id)
  && (!intent.commandId || record.id === intent.commandId)
  && record.event_type === 'hypothesis_updated'
  && record.run_generation === intent.expectedGeneration
  && isRecord(record.subject) && record.subject.kind === 'hypothesis'
  && record.subject.id === intent.hypothesisId && record.subject.status === 'deleted'

const hypothesisDeleteStorage = () => {
  try { return typeof sessionStorage === 'undefined' ? null : sessionStorage } catch { return null }
}
const hypothesisDeleteStorageKey = (runId, generation) => HYPOTHESIS_DELETE_STORAGE_PREFIX
  + encodeURIComponent(`${String(runId || '')}\u0000${String(generation || '')}`)
const validHypothesisDeleteIntent = (value, runId, generation) => !!value && isRecord(value)
  && Object.keys(value).every(key => HYPOTHESIS_DELETE_KEYS.has(key))
  && value.runId === String(runId) && value.expectedGeneration === String(generation)
  && RUN_GENERATION_RE.test(value.expectedGeneration)
  && canonicalHypothesisId(value.hypothesisId)
  && typeof value.idempotencyKey === 'string' && value.idempotencyKey.length > 0
  && value.idempotencyKey.length <= 200 && !/[\u0000-\u001f\u007f]/.test(value.idempotencyKey)
  && typeof value.commandId === 'string'
  && (!value.commandId || HYPOTHESIS_DELETE_COMMAND_RE.test(value.commandId))
  && HYPOTHESIS_DELETE_STORED.has(value.status) && Number.isFinite(value.updatedAt)

function loadHypothesisDeleteIntent(runId, generation) {
  const storage = hypothesisDeleteStorage()
  if (!storage || !runId || !RUN_GENERATION_RE.test(String(generation || ''))) return null
  try {
    const parsed = JSON.parse(storage.getItem(hypothesisDeleteStorageKey(runId, generation)) || 'null')
    return validHypothesisDeleteIntent(parsed, runId, generation) ? parsed : null
  } catch { return null }
}

function inspectHypothesisDeleteRecovery(runId, generation) {
  const storage = hypothesisDeleteStorage()
  if (!storage || !runId || !RUN_GENERATION_RE.test(String(generation || ''))) {
    return { state: 'unavailable', raw: null, key: null, intent: null }
  }
  const key = hypothesisDeleteStorageKey(runId, generation)
  try {
    const raw = storage.getItem(key)
    if (raw == null) return { state: 'empty', raw: null, key, intent: null }
    const intent = loadHypothesisDeleteIntent(runId, generation)
    return intent ? { state: 'valid', raw, key, intent }
      : { state: 'damaged', raw, key, intent: null }
  } catch { return { state: 'unavailable', raw: null, key, intent: null } }
}

function clearDamagedHypothesisDeleteRecovery(recovery) {
  const storage = hypothesisDeleteStorage()
  if (!storage || recovery?.state !== 'damaged' || !recovery.key || typeof recovery.raw !== 'string') return false
  try {
    // Compare-and-clear only the unreadable envelope the operator inspected. A different tab or a
    // late command receipt wins the race and remains protected.
    if (storage.getItem(recovery.key) !== recovery.raw) return false
    storage.removeItem(recovery.key)
    return storage.getItem(recovery.key) == null
  } catch { return false }
}

function saveHypothesisDeleteIntent(intent, expectedRaw) {
  const storage = hypothesisDeleteStorage()
  if (!storage || !validHypothesisDeleteIntent(intent, intent?.runId, intent?.expectedGeneration)) return null
  try {
    const key = hypothesisDeleteStorageKey(intent.runId, intent.expectedGeneration)
    const raw = storage.getItem(key)
    if (expectedRaw !== undefined && raw !== expectedRaw) return null
    const existing = loadHypothesisDeleteIntent(intent.runId, intent.expectedGeneration)
    // A corrupt/unknown recovery envelope may still describe an accepted destructive command. Never
    // overwrite it with a fresh identity; keep the surface fail-closed until storage is repaired.
    if (raw != null && !existing) return null
    if (existing && (existing.idempotencyKey !== intent.idempotencyKey
        || existing.hypothesisId !== intent.hypothesisId
        || (existing.commandId && existing.commandId !== intent.commandId))) return null
    const serialized = JSON.stringify(intent)
    storage.setItem(key, serialized)
    const stored = loadHypothesisDeleteIntent(intent.runId, intent.expectedGeneration)
    return stored && stored.idempotencyKey === intent.idempotencyKey
      && stored.hypothesisId === intent.hypothesisId && stored.commandId === intent.commandId
      && storage.getItem(key) === serialized
      ? { storageKey: key, storageRaw: serialized } : null
  } catch { return null }
}

function clearHypothesisDeleteIntent(intent) {
  const storage = hypothesisDeleteStorage()
  if (!storage || !intent?.storageKey || typeof intent.storageRaw !== 'string') return false
  try {
    const key = hypothesisDeleteStorageKey(intent.runId, intent.expectedGeneration)
    if (intent.storageKey !== key || storage.getItem(key) !== intent.storageRaw) return false
    storage.removeItem(key)
    return storage.getItem(key) == null
  } catch { return false }
}

function _HypothesisFallback({ state, runId, runGeneration, onSelect, onClose, onToast,
  onRecoveryReleased }) {
  const [draft, setDraft] = useState('')
  // Optimistic status overrides {id: 'abandoned'|'deleted'}: the run-state round-trip that reflects a
  // control event can lag (its SSE is buffered by a proxy), so apply the click to the board AT ONCE
  // instead of leaving it looking dead for up to a minute. The real fold catches up idempotently.
  const [optim, setOptim] = useState({})
  const [deleteIntents, setDeleteIntents] = useState(() => {
    const recovery = inspectHypothesisDeleteRecovery(runId, runGeneration)
    const restored = recovery.state === 'valid' ? recovery.intent : null
    return restored ? { [restored.hypothesisId]: {
      ...restored, storageKey: recovery.key, storageRaw: recovery.raw,
      phase: 'unknown',
      releaseAllowed: false, releaseInspected: false,
      message: restored.commandId
        ? 'A saved permanent deletion needs recovery. Check this exact command before another action.'
        : 'A prior permanent deletion has an unknown outcome. Resume the exact saved request to recover it safely.',
    } } : {}
  })
  const [deleteNotices, setDeleteNotices] = useState({})
  const [damagedRecovery, setDamagedRecovery] = useState(() => {
    const inspected = inspectHypothesisDeleteRecovery(runId, runGeneration)
    return inspected.state === 'damaged' ? inspected : null
  })
  const [damagedInspected, setDamagedInspected] = useState(false)
  const deleteIntentsRef = useRef(deleteIntents)
  deleteIntentsRef.current = deleteIntents
  const deleteFlights = useRef(new Set())
  const activeRef = useRef(true)
  useEffect(() => {
    activeRef.current = true
    return () => { activeRef.current = false }
  }, [])
  const refreshDeleteRecovery = fallback => {
    const inspected = inspectHypothesisDeleteRecovery(runId, runGeneration)
    if (inspected.state === 'valid') {
      const restored = {
        ...inspected.intent, storageKey: inspected.key, storageRaw: inspected.raw,
        phase: 'unknown', releaseAllowed: false, releaseInspected: false,
        message: inspected.intent.commandId
          ? 'The saved recovery changed. Check its exact command before another action.'
          : 'The saved recovery changed. Resume its exact retained request before another action.',
      }
      const collection = { [restored.hypothesisId]: restored }
      deleteIntentsRef.current = collection
      if (activeRef.current) setDeleteIntents(collection)
      setDamagedRecovery(null)
      setDamagedInspected(false)
      return
    }
    if (inspected.state === 'damaged') {
      deleteIntentsRef.current = {}
      if (activeRef.current) setDeleteIntents({})
      setDamagedRecovery(inspected)
      setDamagedInspected(false)
      return
    }
    const current = ownHypothesisEntry(deleteIntentsRef.current, fallback.hypothesisId)
    if (!current || current.idempotencyKey !== fallback.idempotencyKey) return
    const retained = { ...current, phase: 'unknown', releaseAllowed: false,
      releaseInspected: false,
      message: 'Recovery storage changed or became unavailable. No command was sent; keep this tab open and inspect recovery again.' }
    const collection = { ...deleteIntentsRef.current, [fallback.hypothesisId]: retained }
    deleteIntentsRef.current = collection
    if (activeRef.current) setDeleteIntents(collection)
  }
  const updateDeleteIntent = (intent, patch, persist = true) => {
    const current = ownHypothesisEntry(deleteIntentsRef.current, intent.hypothesisId)
    if (!current || current.idempotencyKey !== intent.idempotencyKey
        || current.expectedGeneration !== intent.expectedGeneration) return null
    const next = { ...current, ...patch, updatedAt: Date.now() }
    const storedSnapshot = persist ? saveHypothesisDeleteIntent({
      runId: next.runId, expectedGeneration: next.expectedGeneration,
      hypothesisId: next.hypothesisId, idempotencyKey: next.idempotencyKey,
      commandId: next.commandId || '', status: HYPOTHESIS_DELETE_STORED.has(next.status)
        ? next.status : 'submitting', updatedAt: next.updatedAt,
    }, current.storageRaw) : null
    const durable = !persist || !!storedSnapshot
    const presented = durable ? {
      ...next,
      ...(storedSnapshot || { storageKey: current.storageKey, storageRaw: current.storageRaw }),
    } : {
      ...next,
      phase: 'unknown',
      message: 'The command was observed, but its updated recovery receipt could not be saved. Keep this tab open; recovery will reuse the original exact request identity.',
    }
    if (!durable) {
      refreshDeleteRecovery(current)
      return null
    }
    const collection = { ...deleteIntentsRef.current, [intent.hypothesisId]: presented }
    deleteIntentsRef.current = collection
    if (activeRef.current) setDeleteIntents(collection)
    return presented
  }
  const dropDeleteIntent = intent => {
    const current = ownHypothesisEntry(deleteIntentsRef.current, intent.hypothesisId)
    if (!current || current.idempotencyKey !== intent.idempotencyKey) return false
    if (!clearHypothesisDeleteIntent({ ...current, commandId: current.commandId || '' })) {
      updateDeleteIntent(current, {
        phase: 'unknown',
        message: 'The command settled, but its saved recovery identity could not be released. No new deletion will be sent.',
      }, false)
      return false
    }
    const collection = { ...deleteIntentsRef.current }
    delete collection[intent.hypothesisId]
    deleteIntentsRef.current = collection
    if (activeRef.current) setDeleteIntents(collection)
    onRecoveryReleased?.()
    return true
  }
  // Drop an optimistic override once the real fold REFLECTS it (deleted card gone from state; abandoned
  // card now status='abandoned'), so a stale override can't keep masking a LATER server-side reopen of
  // the same hypothesis while the board stays mounted.
  useEffect(() => {
    setOptim(o => {
      const next = Object.create(null)
      for (const [id, v] of Object.entries(o)) {
        const h = ownHypothesisEntry(state.hypotheses, id)
        if (v === 'deleted' && h) next[id] = v                          // not yet dropped by the fold
        else if (v === 'abandoned' && h && h.status !== 'abandoned') next[id] = v   // not yet reflected
      }
      return next
    })
  }, [state.hypotheses])
  const hyps = Object.values(state.hypotheses || {})
    .filter(h => ownHypothesisEntry(optim, h.id) !== 'deleted')
    .map(h => {
      const status = ownHypothesisEntry(optim, h.id)
      return status ? { ...h, status } : h
    })
  // FOREAGENT board prioritization: order cards by predicted payoff (`priority`, 0 = best;
  // unranked cards last), so the kanban shows the sort the world model chose. `ranking` carries the
  // analysis trace (reason + confidence) surfaced as a header note and per-card tooltip.
  const ranking = state.hypothesis_ranking || null
  const rankConf = ranking && typeof ranking.confidence === 'number' ? Math.round(ranking.confidence * 100) : null
  const byStatus = (s) => hyps.filter(h => (h.status || 'open') === s)
    .sort((a, b) => (a.priority ?? Infinity) - (b.priority ?? Infinity))
  const pendingDelete = Object.values(deleteIntents)[0] || null
  const deleteLocked = !!pendingDelete || !!damagedRecovery
  const add = async () => {
    const s = draft.trim()
    if (!s || deleteLocked) return
    try {
      const feedback = commandFeedback(await CONTROL.addHypothesis(runId, s), {
        success: 'Hypothesis added', noop: 'That hypothesis was already tracked',
        executing: 'Hypothesis requested — waiting for the run', failure: 'Could not add hypothesis',
      })
      if (feedback.kind === 'success') setDraft('')
      onToast?.(feedback.message)
    } catch (error) { onToast?.(`Could not add hypothesis: ${error.message || error}`) }
  }
  const _revert = (id) => setOptim(o => { const n = { ...o }; delete n[id]; return n })
  const abandon = async (h) => {
    if (deleteLocked) {
      onToast?.('Finish checking the pending permanent deletion before another hypothesis command.')
      return
    }
    setOptim(o => ({ ...o, [h.id]: 'abandoned' }))          // reflect immediately (SSE lag)
    try {
      const feedback = commandFeedback(await CONTROL.abandonHypothesis(runId, h.id), {
        success: 'Hypothesis abandoned', noop: 'Hypothesis was already abandoned',
        executing: 'Abandon requested — waiting for the run', failure: 'Could not update hypothesis',
      })
      if (feedback.kind !== 'success') _revert(h.id)
      onToast?.(feedback.message)
    } catch (error) { _revert(h.id); onToast?.(`Could not update hypothesis: ${error.message || error}`) }
  }
  const deleteFailure = (intent, message) => {
    dropDeleteIntent(intent)
    if (!activeRef.current) return
    setDeleteNotices(current => ({ ...current, [intent.hypothesisId]: message }))
    onToast?.(message)
  }
  const retainDeleteFailure = (intent, record, message) => {
    const retryable = ['failed', 'timed_out'].includes(record?.status)
      && record?.error?.retryable === true
    updateDeleteIntent(intent, {
      commandId: record.id, status: record.status,
      phase: retryable ? 'retryable' : 'terminal', message,
      releaseAllowed: !retryable, releaseInspected: false,
    })
    if (activeRef.current) onToast?.(message)
  }
  const deleteSuccess = (intent, message) => {
    dropDeleteIntent(intent)
    if (!activeRef.current) return
    setOptim(current => ({ ...current, [intent.hypothesisId]: 'deleted' }))
    setDeleteNotices(current => {
      const next = { ...current }; delete next[intent.hypothesisId]; return next
    })
    onToast?.(message)
  }
  const observeDeleteRecord = (intent, record) => {
    if (!record || typeof record.id !== 'string' || !HYPOTHESIS_DELETE_COMMAND_RE.test(record.id)
        || !exactHypothesisDeleteRecord(intent, record)) return null
    if (!HYPOTHESIS_DELETE_PENDING.has(record.status)) return record
    const message = record.status === 'executing'
      ? 'Permanent deletion is executing — waiting for the run.'
      : 'Permanent deletion was accepted — waiting for the run.'
    return updateDeleteIntent(intent, {
      commandId: record.id, status: record.status, phase: 'pending', message,
      releaseAllowed: false, releaseInspected: false,
    }) ? record : null
  }
  const submitDelete = async intent => {
    const hypothesisId = intent.hypothesisId
    const current = ownHypothesisEntry(deleteIntentsRef.current, hypothesisId)
    if (deleteFlights.current.has(hypothesisId) || !current
        || current.idempotencyKey !== intent.idempotencyKey
        || current.expectedGeneration !== intent.expectedGeneration) return
    const durableSubmission = updateDeleteIntent(current, {
      commandId: current.commandId || '', status: current.status || 'submitting', phase: 'submitting',
      message: 'Submitting the exact saved permanent deletion…',
      releaseAllowed: false, releaseInspected: false,
    })
    if (!durableSubmission) return
    deleteFlights.current.add(hypothesisId)
    try {
      const record = await runCommand(intent.runId, 'hypothesis_updated', {
        id: intent.hypothesisId, status: 'deleted',
      }, {
        expectedGeneration: intent.expectedGeneration,
        idempotencyKey: intent.idempotencyKey,
        onRecord: next => {
          const latest = ownHypothesisEntry(deleteIntentsRef.current, hypothesisId)
          if (!latest || latest.idempotencyKey !== intent.idempotencyKey) return
          observeDeleteRecord(intent, next)
        },
      })
      const currentIntent = ownHypothesisEntry(deleteIntentsRef.current, hypothesisId)
      if (!currentIntent || currentIntent.idempotencyKey !== intent.idempotencyKey) return
      if (!record || !exactHypothesisDeleteRecord(currentIntent, record)) {
        const current = ownHypothesisEntry(deleteIntentsRef.current, hypothesisId)
        const observedCommandId = typeof record?.id === 'string'
          && HYPOTHESIS_DELETE_COMMAND_RE.test(record.id) ? record.id : ''
        const terminalMismatch = !!record && HYPOTHESIS_DELETE_TERMINAL.has(record.status)
          && !!observedCommandId && (!current?.commandId || current.commandId === observedCommandId)
        updateDeleteIntent(intent, {
          commandId: current?.commandId || observedCommandId,
          phase: 'unknown', releaseAllowed: terminalMismatch, releaseInspected: false,
          message: 'The delete command receipt did not prove this exact run generation and hypothesis. It remains quarantined.',
        })
        if (activeRef.current) onToast?.('Permanent deletion outcome is unknown. The receipt did not prove the exact target.')
        return
      }
      const feedback = commandFeedback(record, {
        success: 'Hypothesis deleted', noop: 'Hypothesis was already deleted',
        executing: 'Delete requested — waiting for the run', failure: 'Could not delete hypothesis',
      })
      if (feedback.kind === 'success') deleteSuccess(intent, feedback.message)
      else if (feedback.kind === 'pending') {
        observeDeleteRecord(intent, record)
        if (activeRef.current) onToast?.(feedback.message)
      } else if (record.status === 'rejected') deleteFailure(intent, feedback.message)
      else retainDeleteFailure(intent, record, feedback.message)
    } catch (error) {
      const latestIntent = ownHypothesisEntry(deleteIntentsRef.current, hypothesisId)
      if (!latestIntent || latestIntent.idempotencyKey !== intent.idempotencyKey) return
      const record = error?.commandRecord
      const recoveryIntent = ownHypothesisEntry(deleteIntentsRef.current, hypothesisId)
      if (record && !exactHypothesisDeleteRecord(recoveryIntent, record)) {
        const savedCommandId = ownHypothesisEntry(deleteIntentsRef.current, hypothesisId)?.commandId || ''
        const commandId = savedCommandId || (typeof record.id === 'string'
          && HYPOTHESIS_DELETE_COMMAND_RE.test(record.id) ? record.id : '')
        const transportUnknown = error?.submissionMayHaveSucceeded === true
          || error?.commandUnknown === true || isTransientCommandReadError(error)
        const terminalMismatch = HYPOTHESIS_DELETE_TERMINAL.has(record.status)
          && (!savedCommandId || savedCommandId === record.id)
        const message = transportUnknown
          ? 'The exact command is temporarily unavailable. Its identity remains quarantined; check it again.'
          : 'A command receipt was returned, but it did not prove this exact run generation and hypothesis. The deletion remains quarantined.'
        updateDeleteIntent(intent, {
          commandId, phase: 'unknown', message,
          releaseAllowed: !transportUnknown && terminalMismatch, releaseInspected: false,
        })
        if (activeRef.current) onToast?.(message)
        return
      }
      if (record && ['failed', 'timed_out'].includes(record.status)) {
        const feedback = commandFeedback(record, { failure: 'Could not delete hypothesis' })
        retainDeleteFailure(intent, record, feedback.message)
        return
      }
      if (record?.status === 'rejected') {
        const feedback = commandFeedback(record, { failure: 'Could not delete hypothesis' })
        deleteFailure(intent, feedback.message)
        return
      }
      const pendingRecord = HYPOTHESIS_DELETE_PENDING.has(record?.status)
        && typeof record?.id === 'string' && HYPOTHESIS_DELETE_COMMAND_RE.test(record.id)
        && exactHypothesisDeleteRecord(recoveryIntent, record)
      const errorCommandId = [error?.commandId]
        .map(value => String(value || '')).find(value => HYPOTHESIS_DELETE_COMMAND_RE.test(value)) || ''
      const ambiguous = pendingRecord || error?.submissionMayHaveSucceeded === true
        || error?.commandUnknown === true || isTransientCommandReadError(error)
      if (ambiguous) {
        const commandId = pendingRecord ? record.id
          : (ownHypothesisEntry(deleteIntentsRef.current, hypothesisId)?.commandId || errorCommandId)
        const status = pendingRecord ? record.status
          : (ownHypothesisEntry(deleteIntentsRef.current, hypothesisId)?.status || 'submitting')
        const message = commandId
          ? 'Permanent deletion may still complete. Check this exact saved command; it was not replayed.'
          : 'Permanent deletion outcome is unknown. Resume this exact saved request; it reuses the same identity and cannot create a second logical deletion.'
        updateDeleteIntent(intent, {
          commandId, status, phase: 'unknown', message,
          releaseAllowed: false, releaseInspected: false,
        })
        if (activeRef.current) onToast?.(message)
      } else if (errorCommandId) {
        const message = 'The exact command identity was returned, but its target outcome could not be proved. Inspect it before releasing recovery.'
        updateDeleteIntent(intent, {
          commandId: ownHypothesisEntry(deleteIntentsRef.current, hypothesisId)?.commandId || errorCommandId,
          phase: 'unknown', message, releaseAllowed: false, releaseInspected: false,
        })
        if (activeRef.current) onToast?.(message)
      } else {
        const feedback = record ? commandFeedback(record, {
          failure: 'Could not delete hypothesis',
        }) : null
        deleteFailure(intent, feedback?.message || `Could not delete hypothesis: ${error?.message || error}`)
      }
    } finally {
      deleteFlights.current.delete(hypothesisId)
    }
  }
  const del = async h => {
    const hypothesisId = String(h.id)
    if (!canonicalHypothesisId(hypothesisId)) {
      const message = 'This hypothesis has a non-canonical id and cannot be targeted safely. Refresh or repair the run record before deleting it.'
      setDeleteNotices(current => ({ ...current, [hypothesisId]: message }))
      onToast?.(message)
      return
    }
    if (deleteFlights.current.has(hypothesisId)
        || ownHypothesisEntry(deleteIntentsRef.current, hypothesisId)
        || Object.keys(deleteIntentsRef.current).length > 0) {
      onToast?.('A permanent hypothesis deletion is already pending. Recover that exact command first.')
      return
    }
    if (!runId || !RUN_GENERATION_RE.test(String(runGeneration || ''))) {
      const message = 'The displayed run generation is unavailable. Refresh the run before deleting a hypothesis.'
      setDeleteNotices(current => ({ ...current, [hypothesisId]: message }))
      onToast?.(message)
      return
    }
    const statement = String(h.statement || '').trim()
    if (!window.confirm(`Delete this hypothesis permanently?\n\n${statement.slice(0, 500)}\n\nThis removes it from the board and cannot be undone.`)) return
    const intent = {
      runId: String(runId), expectedGeneration: String(runGeneration), hypothesisId,
      idempotencyKey: createIdempotencyKey(), commandId: '', status: 'submitting',
      updatedAt: Date.now(), phase: 'submitting',
      message: 'Submitting one permanent deletion…',
    }
    const stored = {
      runId: intent.runId, expectedGeneration: intent.expectedGeneration,
      hypothesisId: intent.hypothesisId, idempotencyKey: intent.idempotencyKey,
      commandId: '', status: 'submitting', updatedAt: intent.updatedAt,
    }
    // Commit the exact operation identity before POST. If tab-scoped storage is unavailable, fail
    // closed: an unremembered destructive request could be replayed after close/reload.
    const storedSnapshot = saveHypothesisDeleteIntent(stored, null)
    if (!storedSnapshot) {
      const message = 'Permanent deletion was not sent because this browser could not retain its recovery identity.'
      setDeleteNotices(current => ({ ...current, [hypothesisId]: message }))
      onToast?.(message)
      return
    }
    const retainedIntent = { ...intent, ...storedSnapshot }
    deleteIntentsRef.current = { [hypothesisId]: retainedIntent }
    setDeleteIntents(deleteIntentsRef.current)
    setDeleteNotices(current => { const next = { ...current }; delete next[hypothesisId]; return next })
    await submitDelete(retainedIntent)
  }
  const resumeDelete = async intent => {
    const current = ownHypothesisEntry(deleteIntentsRef.current, intent?.hypothesisId)
    if (!current || current.commandId || deleteFlights.current.has(current.hypothesisId)
        || current.idempotencyKey !== intent.idempotencyKey) return
    if (!window.confirm(
      'Resume the exact saved permanent deletion?\n\nThis reuses the original idempotency identity and payload. It cannot create a second logical deletion.',
    )) return
    await submitDelete(current)
  }
  const checkDelete = async intent => {
    if (!intent?.commandId || deleteFlights.current.has(intent.hypothesisId)) return
    const durableCheck = updateDeleteIntent(intent, {
      phase: 'checking', message: 'Checking the exact delete command…',
      releaseAllowed: false, releaseInspected: false,
    })
    if (!durableCheck) return
    deleteFlights.current.add(intent.hypothesisId)
    try {
      const record = await getRunCommand(intent.runId, intent.commandId, {
        requestTimeoutMs: PANEL_REQUEST_TIMEOUT_MS,
      })
      const current = ownHypothesisEntry(deleteIntentsRef.current, intent.hypothesisId)
      if (!activeRef.current || !current || current.idempotencyKey !== intent.idempotencyKey
          || current.commandId !== intent.commandId) return
      if (!exactHypothesisDeleteRecord(intent, record)) {
        const terminalMismatch = HYPOTHESIS_DELETE_TERMINAL.has(record?.status)
          && record?.id === intent.commandId
        updateDeleteIntent(intent, {
          phase: 'unknown', releaseAllowed: terminalMismatch, releaseInspected: false,
          message: 'The saved command did not prove this exact run generation and hypothesis. It remains quarantined.',
        })
        return
      }
      const feedback = commandFeedback(record, {
        success: 'Hypothesis deleted', noop: 'Hypothesis was already deleted',
        executing: 'Delete requested — waiting for the run', failure: 'Could not delete hypothesis',
      })
      if (feedback.kind === 'success') deleteSuccess(intent, feedback.message)
      else if (feedback.kind === 'pending') {
        observeDeleteRecord(intent, record)
        onToast?.(feedback.message)
      } else if (record.status === 'rejected') deleteFailure(intent, feedback.message)
      else retainDeleteFailure(intent, record, feedback.message)
    } catch (error) {
      const current = ownHypothesisEntry(deleteIntentsRef.current, intent.hypothesisId)
      if (!activeRef.current || !current || current.idempotencyKey !== intent.idempotencyKey
          || current.commandId !== intent.commandId) return
      const message = isTransientCommandReadError(error)
        ? 'The exact delete command is temporarily unavailable. Its identity is retained; try checking again.'
        : 'The exact delete command could not be verified. Its identity remains quarantined; no delete was replayed.'
      updateDeleteIntent(intent, {
        phase: 'unknown', message,
        releaseAllowed: !isTransientCommandReadError(error)
          && ([403, 404].includes(Number(error?.status))
            || error?.code === 'COMMAND_PROTOCOL_ERROR'),
        releaseInspected: false,
      })
      onToast?.(message)
    } finally {
      deleteFlights.current.delete(intent.hypothesisId)
    }
  }
  const retryDelete = async intent => {
    const current = ownHypothesisEntry(deleteIntentsRef.current, intent?.hypothesisId)
    if (!current || current.phase !== 'retryable' || !current.commandId
        || deleteFlights.current.has(current.hypothesisId)
        || current.idempotencyKey !== intent.idempotencyKey) return
    if (!window.confirm(
      'Retry this exact failed permanent-deletion command?\n\nThis reuses the same durable command id; it does not submit a new delete intent.',
    )) return
    const durableRetry = updateDeleteIntent(current, {
      phase: 'retrying', releaseAllowed: false, releaseInspected: false,
      message: 'Retrying the exact durable delete command…',
    })
    if (!durableRetry) return
    deleteFlights.current.add(current.hypothesisId)
    try {
      const record = await retryRunCommand(current.runId, current.commandId, {
        requestTimeoutMs: PANEL_REQUEST_TIMEOUT_MS,
        onRecord: next => {
          const latest = ownHypothesisEntry(deleteIntentsRef.current, current.hypothesisId)
          if (!latest || latest.idempotencyKey !== current.idempotencyKey) return
          observeDeleteRecord(latest, next)
        },
      })
      const latest = ownHypothesisEntry(deleteIntentsRef.current, current.hypothesisId)
      if (!activeRef.current || !latest || latest.idempotencyKey !== current.idempotencyKey) return
      if (!exactHypothesisDeleteRecord(latest, record)) {
        const message = 'The retry receipt did not prove this exact run generation and hypothesis. Recovery remains quarantined.'
        updateDeleteIntent(latest, {
          phase: 'unknown', message,
          releaseAllowed: HYPOTHESIS_DELETE_TERMINAL.has(record?.status)
            && record?.id === latest.commandId,
          releaseInspected: false,
        })
        onToast?.(message)
        return
      }
      const feedback = commandFeedback(record, {
        success: 'Hypothesis deleted', noop: 'Hypothesis was already deleted',
        executing: 'Delete retry is still executing', failure: 'Could not delete hypothesis',
      })
      if (feedback.kind === 'success') deleteSuccess(latest, feedback.message)
      else if (feedback.kind === 'pending') {
        observeDeleteRecord(latest, record)
        onToast?.(feedback.message)
      } else if (record.status === 'rejected') deleteFailure(latest, feedback.message)
      else retainDeleteFailure(latest, record, feedback.message)
    } catch (error) {
      const latest = ownHypothesisEntry(deleteIntentsRef.current, current.hypothesisId)
      if (!activeRef.current || !latest || latest.idempotencyKey !== current.idempotencyKey) return
      const message = isTransientCommandReadError(error) || error?.submissionMayHaveSucceeded === true
        ? 'The exact retry outcome is temporarily unavailable. Its command identity remains retained.'
        : 'The exact retry could not be verified. Inspect the saved command before releasing recovery.'
      updateDeleteIntent(latest, {
        phase: 'unknown', message,
        releaseAllowed: false,
        releaseInspected: false,
      })
      onToast?.(message)
    } finally {
      deleteFlights.current.delete(current.hypothesisId)
    }
  }
  const releaseValidRecovery = intent => {
    const current = ownHypothesisEntry(deleteIntentsRef.current, intent?.hypothesisId)
    if (!current || !current.releaseAllowed || !current.releaseInspected
        || current.idempotencyKey !== intent.idempotencyKey) return
    if (!window.confirm(
      'Release this exact permanent-deletion recovery identity?\n\nThis sends no command. Only continue after inspecting the run and accepting that this old outcome cannot be proved.',
    )) return
    if (!clearHypothesisDeleteIntent({ ...current, commandId: current.commandId || '' })) {
      updateDeleteIntent(current, {
        phase: 'unknown', releaseAllowed: false, releaseInspected: false,
        message: 'The saved recovery identity changed or could not be released. It remains protected.',
      }, false)
      onToast?.('The exact recovery identity changed or could not be released.')
      return
    }
    const collection = { ...deleteIntentsRef.current }
    delete collection[current.hypothesisId]
    deleteIntentsRef.current = collection
    setDeleteIntents(collection)
    onRecoveryReleased?.()
    onToast?.('The exact recovery identity was released. No deletion was sent.')
  }
  const releaseDamagedRecovery = () => {
    if (!damagedRecovery || !damagedInspected) return
    if (!window.confirm(
      'Release this exact unreadable recovery record?\n\nOnly continue after inspecting the current run state and confirming that no permanent deletion still needs recovery.',
    )) return
    if (!clearDamagedHypothesisDeleteRecovery(damagedRecovery)) {
      onToast?.('The recovery record changed or could not be released. It remains protected; inspect it again.')
      const refreshed = inspectHypothesisDeleteRecovery(runId, runGeneration)
      if (refreshed.state === 'valid') {
        const restored = {
          ...refreshed.intent, phase: 'unknown',
          storageKey: refreshed.key, storageRaw: refreshed.raw,
          releaseAllowed: false, releaseInspected: false,
          message: refreshed.intent.commandId
            ? 'The recovery record changed to a valid permanent deletion. Check its exact command.'
            : 'The recovery record changed to a valid id-less deletion. Resume its exact saved request.',
        }
        deleteIntentsRef.current = { [restored.hypothesisId]: restored }
        setDeleteIntents(deleteIntentsRef.current)
        setDamagedRecovery(null)
      } else if (refreshed.state === 'damaged') setDamagedRecovery(refreshed)
      setDamagedInspected(false)
      return
    }
    setDamagedRecovery(null)
    setDamagedInspected(false)
    onToast?.('The exact damaged recovery record was released. No deletion was sent.')
    onRecoveryReleased?.()
  }
  return (
    <Panel title="Hypotheses" sub={`${hyps.length} tracked — what the run is trying to learn`} onClose={onClose} wide>
      <div className="toolbar" style={{ marginBottom: 10, gap: 6 }}>
        <input className="text" style={{ flex: 1 }} aria-label="New hypothesis"
          placeholder="Pose a hypothesis to test (e.g. “target is right-skewed; a log transform helps”)"
          value={draft} disabled={deleteLocked} onChange={e => setDraft(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') add() }} />
        <button className="btn sm primary" onClick={add} disabled={!draft.trim() || deleteLocked}>+ Add</button>
      </div>
      {pendingDelete && <div className="report-inline-state" role="status" style={{ marginBottom: 10 }}>
        <OpIcon name="alert" size={14} /><span>{pendingDelete.message}</span>
        {pendingDelete.commandId && <button className="btn sm" onClick={() => checkDelete(pendingDelete)}
          disabled={['checking', 'retrying', 'submitting'].includes(pendingDelete.phase)}>
          {pendingDelete.phase === 'checking' ? 'Checking…' : 'Check exact command'}</button>}
        {pendingDelete.commandId && pendingDelete.phase === 'retryable'
          && <button className="btn sm" onClick={() => retryDelete(pendingDelete)}>Retry exact command</button>}
        {!pendingDelete.commandId && <button className="btn sm" onClick={() => resumeDelete(pendingDelete)}
          disabled={pendingDelete.phase === 'submitting'}>
          {pendingDelete.phase === 'submitting' ? 'Submitting…' : 'Resume exact request'}</button>}
        {pendingDelete.releaseAllowed && !pendingDelete.releaseInspected
          && <button className="btn sm" onClick={() => updateDeleteIntent(pendingDelete, {
            releaseInspected: true,
          }, false)}>Inspect recovery</button>}
        {pendingDelete.releaseAllowed && pendingDelete.releaseInspected && <>
          <span className="muted">Run {pendingDelete.runId}; generation {pendingDelete.expectedGeneration.slice(0, 12)}…;
            hypothesis {pendingDelete.hypothesisId}; command {pendingDelete.commandId || 'not recorded'}.</span>
          <button className="btn sm danger" onClick={() => releaseValidRecovery(pendingDelete)}>
            Release exact recovery</button>
        </>}
      </div>}
      {damagedRecovery && <div className="report-inline-state error" role="alert" style={{ marginBottom: 10 }}>
        <OpIcon name="alert" size={14} />
        <span>An unreadable permanent-deletion recovery record exists for this exact run generation.
          Destructive controls stay locked until it is inspected and explicitly released.</span>
        {!damagedInspected
          ? <button className="btn sm" onClick={() => setDamagedInspected(true)}>Inspect recovery</button>
          : <>
            <span className="muted">Run {runId}; generation {String(runGeneration).slice(0, 12)}…;
              stored record {damagedRecovery.raw.length} bytes. Its command identity cannot be verified.</span>
            <button className="btn sm danger" onClick={releaseDamagedRecovery}>Release exact record</button>
          </>}
      </div>}
      {ranking && <div className="muted" style={{ marginBottom: 8, fontSize: 12, display: 'flex', gap: 6, alignItems: 'baseline' }}
        title={ranking.reason || 'predicted before execution'}>
        <OpIcon name="bulb" size={11} />
        <span>Predicted priority order (FOREAGENT{rankConf != null ? `, ${rankConf}% confidence` : ''})
          {ranking.reason ? `: ${ranking.reason}` : ''}</span>
      </div>}
      {hyps.length === 0
        ? <div className="muted">No hypotheses yet. The Researcher states one per experiment (its
          <code> hypothesis</code> field); deep-research directions and your “+ Add” questions land here too,
          then get tracked to a verdict as experiments run.</div>
        : <div className="hyp-board">
          {_HYP_COLUMNS.map(([key, label, hint]) => {
            const col = byStatus(key)
            return <div key={key} className={'hyp-col hyp-' + key}>
              <div className="hyp-col-h" title={hint}>{label} <span className="muted">{col.length}</span></div>
              {col.map(h => {
                const deletion = ownHypothesisEntry(deleteIntents, h.id)
                const deleteNotice = ownHypothesisEntry(deleteNotices, h.id) || ''
                return <div key={h.id} className="hyp-card">
                <div className="hyp-stmt">
                  <span className="hyp-src" title={`source: ${h.source}`}>
                    <OpIcon name={_HYP_ICON[h.source] || 'dot'} size={12} /></span> {h.statement}
                </div>
                <div className="hyp-meta">
                  {h.priority != null && <span className="chip xs" title={'predicted priority '
                    + (h.priority + 1) + (rankConf != null ? ` · ${rankConf}% confidence` : '')
                    + (ranking && ranking.reason ? ` · ${ranking.reason}` : '')}>#{h.priority + 1}</span>}
                  {(h.evidence || []).slice(0, 8).map(nid => <button key={nid} className="btn xs ghost"
                    title={`experiment #${nid}`} onClick={() => { onSelect && onSelect(nid); onClose() }}>#{nid}</button>)}
                  {h.best_delta != null && <span className={'chip xs ' + (h.best_delta > 0 ? 'ok' : '')}
                    title="best improvement over parent among the evidence">Δ{fmt(h.best_delta)}</span>}
                  {key !== 'abandoned' && <button className="btn xs ghost" title="abandon — move to the Abandoned column (keeps the record)"
                    disabled={deleteLocked} onClick={() => abandon(h)}><OpIcon name="cross" size={11} /></button>}
                  <button className="btn xs ghost danger" title="delete this hypothesis permanently (remove from the board)"
                    disabled={deleteLocked} aria-label={`Delete hypothesis ${h.id} permanently`}
                    onClick={() => del(h)}>{deletion ? 'Deleting…' : 'Delete'}</button>
                </div>
                {deleteNotice && <div className="report-inline-state error" role="alert">
                  <OpIcon name="alert" size={14} /><span>{deleteNotice}</span>
                </div>}
              </div>})}
              {col.length === 0 && <div className="muted hyp-empty">—</div>}
            </div>
          })}
        </div>}
    </Panel>
  )
}

export function HypothesisBoard({ state, runId, runGeneration, onSelect, onClose, onToast }) {
  const [, setRecoveryEpoch] = useState(0)
  const cards = _cardRows(state)
  const projection = isRecord(state?.cards_projection) ? state.cards_projection : null
  // A non-empty/omitted/invalid Card projection is authoritative. With no Cards at all, preserve the
  // hypothesis add/abandon workflow for older logs and for a run before its first Card is minted.
  // Both operator affordances now live on the authoritative Card board too: `+ Add` (below) and
  // `Abandon this Card` (the per-card `abandon` control emitting hypothesis_updated(status=abandoned)),
  // so an ordinary cards-only run — where this fallback is unmounted after the first Card — still
  // exposes them; this fallback remains only for the pre-first-Card / legacy-hypotheses shape.
  const hasAuthoritativeCards = cards.length > 0 || (_cardInt(projection?.total) ?? 0) > 0
    || projection?.source_valid === false
  // Card ids can repeat across runs and after an in-place reset. Remount every optimistic/ref tracker
  // at that exact scope boundary; the child also ignores completions after unmount.
  const scopeKey = `${runId || ''}:${runGeneration || ''}`
  const recovery = inspectHypothesisDeleteRecovery(runId, runGeneration)
  const recoveryVisible = recovery.state === 'valid' || recovery.state === 'damaged'
  return hasAuthoritativeCards && !recoveryVisible
    ? <_CardKanban key={`cards:${scopeKey}`} state={state} cards={cards} runId={runId} onSelect={onSelect}
      onClose={onClose} onToast={onToast} />
    : <_HypothesisFallback key={`hypotheses:${scopeKey}`} state={state} runId={runId}
      runGeneration={runGeneration}
      onSelect={onSelect} onClose={onClose} onToast={onToast}
      onRecoveryReleased={() => setRecoveryEpoch(value => value + 1)} />
}

// Module scope so their identity is stable across SSE frames (ComparePanel re-renders on every live
// fold); defined inline they remounted the <select> each frame, closing an open dropdown mid-pick.
function CmpSel({ label, v, set, ids, nodes, currentAttempt = null, selectRef = null }) {
  return <label className="cmp-select"><span>{label}</span>
    <select ref={selectRef} className="text" value={v ?? ''} aria-label={label}
            onChange={e => set(Number(e.target.value))}>{ids.map(i => {
              const attempt = i === v && Number.isSafeInteger(currentAttempt)
                ? currentAttempt : nodes?.[i]?.attempt
              return <option key={i} value={i}>
                #{i} · attempt {Number.isSafeInteger(attempt) ? attempt : 'unknown'}
              </option>
            })}</select>
  </label>
}

function useNodeResource({ runId, stateRunId, nodeId, expectedGeneration, expectedAttempt, reviewMode }) {
  const accessMode = reviewMode ? 'review' : 'owner'
  const identityReady = String(stateRunId || '') === String(runId || '')
    && RUN_GENERATION_RE.test(expectedGeneration || '')
    && Number.isSafeInteger(nodeId) && nodeId >= 0
    && Number.isSafeInteger(expectedAttempt) && expectedAttempt >= 0
  const scope = JSON.stringify([
    String(runId || ''), String(stateRunId || ''), expectedGeneration || '', nodeId,
    expectedAttempt, accessMode,
  ])
  const [resource, setResource] = useState({
    scope: null, status: 'idle', data: null, error: null, retryable: false,
  })
  const [nonce, setNonce] = useState(0)
  useEffect(() => {
    if (nodeId == null) {
      setResource({ scope, status: 'idle', data: null, error: null, retryable: false })
      return undefined
    }
    if (!identityReady) {
      setResource({ scope, status: 'waiting', data: null, error: null, retryable: false })
      return undefined
    }
    let alive = true
    const requested = {
      runId: String(runId), nodeId, generation: expectedGeneration,
      attempt: expectedAttempt, accessMode,
    }
    setResource({ scope, status: 'loading', data: null, error: null, retryable: false })
    const suffix = `?expected_generation=${encodeURIComponent(requested.generation)}`
    const request = deadlineGet(runNodeApiPath(requested.runId, requested.nodeId, suffix),
      PANEL_REQUEST_TIMEOUT_MS)
    request.promise.then(data => {
      if (!alive) return
      const responseAttemptValid = Number.isSafeInteger(data?.attempt) && data.attempt >= 0
      const attemptMatches = responseAttemptValid && (requested.accessMode === 'review'
        ? data.attempt === requested.attempt : data.attempt >= requested.attempt)
      const valid = isRecord(data) && String(data.id) === String(requested.nodeId)
        && typeof data.status === 'string' && data.run_generation === requested.generation
        && attemptMatches
      setResource(valid
        ? { scope, status: 'ready', data, error: null, retryable: false }
        : { scope, status: 'error', data: null,
            error: 'The run or experiment attempt changed while details were loading.',
            retryable: false })
    }, error => {
      if (!alive || error?.name === 'AbortError') return
      const identityChanged = ['invalid_run_generation', 'run_generation_changed',
        'run_generation_unavailable'].includes(error?.code)
      const httpStatus = Number(error?.status)
      const retryable = !identityChanged && (error?.name === 'TimeoutError'
        || error?.status == null || httpStatus === 0 || httpStatus === 408
        || httpStatus === 429 || httpStatus >= 500)
      setResource({
        scope, status: 'error', data: null,
        error: identityChanged
          ? 'The run changed while details were loading.'
          : error?.name === 'TimeoutError'
            ? 'Detail loading timed out.' : 'Full details could not be loaded.',
        retryable,
      })
    })
    return () => { alive = false; request.controller.abort() }
  }, [runId, stateRunId, nodeId, expectedGeneration, expectedAttempt, accessMode,
    identityReady, scope, nonce])
  const current = resource.scope === scope ? resource : {
    scope, status: nodeId == null ? 'idle' : identityReady ? 'loading' : 'waiting',
    data: null, error: null, retryable: false,
  }
  const retry = () => {
    setResource(previous => previous.scope === scope
      ? { ...previous, status: 'loading', data: null, error: null, retryable: false }
      : previous)
    setNonce(n => n + 1)
  }
  return { ...current, retry }
}

function CmpCol({ resource, label, surfaceRef = null, onFocusCapture = null, onRetry = null }) {
  const d = resource.data
  const failed = resource.status === 'error'
  return <div ref={surfaceRef} tabIndex={-1} onFocusCapture={onFocusCapture}
    className={`cmp-col${failed ? ' notice resource-error' : d ? '' : ' muted'}`}
    role={failed ? 'alert' : d ? 'group' : 'status'}
    aria-label={failed ? undefined : d ? `${label} details` : undefined}
    aria-live={failed || d ? undefined : 'polite'}>
    {failed
      ? <>
          <span>{label}: {resource.error || 'full details could not be loaded.'}</span>
          {resource.retryable && <button className="btn sm" onClick={onRetry || resource.retry}
            aria-label={`Retry ${label} details`}>Retry</button>}
        </>
      : d
        ? <>
            <div className="kv">
              <div className="k">operator</div><div className="v">{d.operator}</div>
              <div className="k">metric</div><div className="v">{fmt(d.confirmed_mean ?? d.metric)}</div>
              <div className="k">status</div><div className="v">{d.status}</div>
              <div className="k">params</div><div className="v">{JSON.stringify(d.idea?.params)}</div>
            </div>
            <CodeViewer code={d.code || '(no code)'} label={`${label} code`} maxHeight={280} />
          </>
        : <>Loading {label} details…</>}
  </div>
}

export function ComparePanel({ state, runId, expectedGeneration, reviewMode = false, onClose, initialPair = null }) {
  const ids = Object.keys(state.nodes).map(Number).sort((a, b) => a - b)
  const [a, setA] = useState(null), [b, setB] = useState(null)
  const [diff, setDiff] = useState(false)   // U4: line diff between ANY two nodes (not just vs parent)
  const leftSelectRef = useRef(null), rightSelectRef = useRef(null)
  const leftSurfaceRef = useRef(null), rightSurfaceRef = useRef(null), diffSurfaceRef = useRef(null)
  const emptyStateRef = useRef(null)
  const detailFocusOwnerRef = useRef(null)
  const previousNodeCountRef = useRef(ids.length)
  const comparisonScope = JSON.stringify([
    String(runId || ''), String(state?.run_id || ''), expectedGeneration || '', reviewMode ? 'review' : 'owner',
  ])
  const previousComparisonScopeRef = useRef(comparisonScope)
  useLayoutEffect(() => {
    if (previousComparisonScopeRef.current === comparisonScope) return
    previousComparisonScopeRef.current = comparisonScope
    const active = typeof document === 'undefined' ? null : document.activeElement
    const diffFocusWouldBeLost = detailFocusOwnerRef.current === 'diff'
      && (!active || active === document.body || !active.isConnected
        || diffSurfaceRef.current?.contains(active))
    if (diffFocusWouldBeLost) {
      detailFocusOwnerRef.current = null
      const resetFocusTarget = leftSelectRef.current || emptyStateRef.current
      resetFocusTarget?.focus({ preventScroll: true })
    }
    setA(null)
    setB(null)
    setDiff(false)
  }, [comparisonScope])
  // Seed from an explicit pair (e.g. canvas "diff vs champion"), else best vs latest.
  useEffect(() => {
    if (initialPair && initialPair.length === 2) {
      if (initialPair[0] != null) setA(initialPair[0])
      if (initialPair[1] != null) setB(initialPair[1])
      setDiff(true)
    }
  }, [initialPair && initialPair.join(',')])
  // Seed/repair the selectors once nodes exist (the panel may open before any node arrives).
  // Functional updates: on mount this runs in the same commit as the initialPair seeding above
  // and would otherwise read a=null from the stale closure and overwrite the explicit pair.
  useEffect(() => {
    if (!ids.length) return
    setA(cur => (cur == null || !ids.includes(cur)) ? (state.best_node_id ?? ids[0]) : cur)
    setB(cur => (cur == null || !ids.includes(cur)) ? ids[ids.length - 1] : cur)
  }, [comparisonScope, ids.join(','), state.best_node_id])
  const attemptA = a == null ? null : state.nodes?.[a]?.attempt
  const attemptB = b == null ? null : state.nodes?.[b]?.attempt
  const resourceA = useNodeResource({ runId, stateRunId: state?.run_id, nodeId: a,
    expectedGeneration, expectedAttempt: attemptA, reviewMode })
  const resourceB = useNodeResource({ runId, stateRunId: state?.run_id, nodeId: b,
    expectedGeneration, expectedAttempt: attemptB, reviewMode })
  const da = resourceA.data, db = resourceB.data
  const displayedAttemptA = Number.isSafeInteger(da?.attempt) ? da.attempt : attemptA
  const displayedAttemptB = Number.isSafeInteger(db?.attempt) ? db.attempt : attemptB
  const selectionReady = Number.isSafeInteger(a) && ids.includes(a)
    && Number.isSafeInteger(b) && ids.includes(b)
  const codeDiff = useMemo(
    () => diff && da?.code != null && db?.code != null ? diffLines(da.code, db.code) : null,
    [diff, da?.code, db?.code])
  const diffError = resourceA.status === 'error' || resourceB.status === 'error'
  const diffErrorText = [
    resourceA.status === 'error' ? `Left node #${a}: ${resourceA.error}` : '',
    resourceB.status === 'error' ? `Right node #${b}: ${resourceB.error}` : '',
  ].filter(Boolean).join(' ')
  const previousDetailScopesRef = useRef([resourceA.scope, resourceB.scope])
  useLayoutEffect(() => {
    const previous = previousDetailScopesRef.current
    const leftChanged = previous[0] !== resourceA.scope
    const rightChanged = previous[1] !== resourceB.scope
    previousDetailScopesRef.current = [resourceA.scope, resourceB.scope]
    if ((!leftChanged && !rightChanged) || typeof document === 'undefined') return
    const active = document.activeElement
    if (active && active !== document.body && active.isConnected) return
    const owner = detailFocusOwnerRef.current
    const target = owner === 'left' && leftChanged
      ? (leftSurfaceRef.current || leftSelectRef.current || emptyStateRef.current)
      : owner === 'right' && rightChanged
        ? (rightSurfaceRef.current || rightSelectRef.current || emptyStateRef.current)
        : owner === 'diff' && (leftChanged || rightChanged)
          ? (diffSurfaceRef.current || leftSelectRef.current || emptyStateRef.current)
          : null
    target?.focus({ preventScroll: true })
  }, [resourceA.scope, resourceB.scope])
  useLayoutEffect(() => {
    const previousCount = previousNodeCountRef.current
    previousNodeCountRef.current = ids.length
    if (previousCount !== 0 || ids.length === 0 || detailFocusOwnerRef.current !== 'empty') return
    const active = typeof document === 'undefined' ? null : document.activeElement
    detailFocusOwnerRef.current = null
    if (!active || active === document.body || !active.isConnected) {
      leftSelectRef.current?.focus({ preventScroll: true })
    }
  }, [ids.length])
  const retrySide = (owner, resource, surfaceRef) => {
    if (!resource.retryable) return
    detailFocusOwnerRef.current = owner
    resource.retry()
    requestAnimationFrame(() => surfaceRef.current?.focus({ preventScroll: true }))
  }
  const retryDiff = () => {
    detailFocusOwnerRef.current = 'diff'
    if (resourceA.retryable) resourceA.retry()
    if (resourceB.retryable) resourceB.retry()
    requestAnimationFrame(() => diffSurfaceRef.current?.focus({ preventScroll: true }))
  }
  if (!ids.length) return <Panel title="Compare nodes" onClose={onClose}>
    <div ref={emptyStateRef} tabIndex={-1} className="muted" role="status"
      onFocus={() => { detailFocusOwnerRef.current = 'empty' }}>No nodes yet.</div>
  </Panel>
  return (
    <Panel title="Compare nodes" onClose={onClose} wide>
      <div className="toolbar" style={{ marginBottom: 10 }}>
        <CmpSel label="Left node" v={a} set={setA} ids={ids} nodes={state.nodes}
          currentAttempt={displayedAttemptA} selectRef={leftSelectRef} />
        <span className="muted">vs</span>
        <CmpSel label="Right node" v={b} set={setB} ids={ids} nodes={state.nodes}
          currentAttempt={displayedAttemptB} selectRef={rightSelectRef} />
        <span className="spacer" style={{ flex: 1 }} />
        <button className={'btn sm' + (diff ? ' primary' : '')} aria-pressed={diff}
          disabled={!selectionReady}
          onClick={() => setDiff(d => !d)}
          title="ordered line diff of the two nodes' code">Code diff</button>
      </div>
      {!selectionReady
        ? <div className="muted" role="status" aria-live="polite">Preparing node comparison…</div>
        : diff
        ? <div ref={diffSurfaceRef} tabIndex={-1} role="group" aria-label="Code comparison details"
            onFocusCapture={() => { detailFocusOwnerRef.current = 'diff' }}>
            {diffError
            ? <div className="notice resource-error" role="alert">
                <span>{diffErrorText || 'Could not load both node details for the diff.'}</span>
                {(resourceA.retryable || resourceB.retryable) && <button className="btn sm"
                  onClick={retryDiff}>Retry failed details</button>}
              </div>
            : codeDiff
            ? <CodeViewer diff={codeDiff} copyText={db.code || ''}
                label={`Code diff #${a} attempt ${displayedAttemptA} to #${b} attempt ${displayedAttemptB}`} maxHeight={460} />
            : <div className="muted" role="status" aria-live="polite">
                Loading code for #{a} attempt {displayedAttemptA ?? 'unknown'} and #{b} attempt {displayedAttemptB ?? 'unknown'}…
              </div>}
          </div>
        : <div className="cmp-cols">
            <CmpCol resource={resourceA} label={`Node #${a} · attempt ${displayedAttemptA ?? 'unknown'}`}
              surfaceRef={leftSurfaceRef} onFocusCapture={() => { detailFocusOwnerRef.current = 'left' }}
              onRetry={() => retrySide('left', resourceA, leftSurfaceRef)} />
            <CmpCol resource={resourceB} label={`Node #${b} · attempt ${displayedAttemptB ?? 'unknown'}`}
              surfaceRef={rightSurfaceRef} onFocusCapture={() => { detailFocusOwnerRef.current = 'right' }}
              onRetry={() => retrySide('right', resourceB, rightSurfaceRef)} />
          </div>}
    </Panel>
  )
}


const explorerEvent = event => {
  const omitted = event?._log_page?.truncated === true
  const bytes = omitted ? Number(event._log_page.raw_bytes || 0) : 0
  if (omitted) {
    const preview = `details omitted · ${bytes.toLocaleString()} source bytes exceed page limit`
    return {
      event, preview, searchType: String(event.type || '').toLowerCase(),
      searchData: preview.toLowerCase(), omitted: true, serialized: null,
    }
  }
  let serialized = '{}'
  try { serialized = JSON.stringify(event.data || {}) } catch { serialized = '[unserializable event data]' }
  const preview = serialized.length > 500 ? serialized.slice(0, 500) + '…' : serialized
  return {
    event, preview, serialized, searchType: String(event.type || '').toLowerCase(),
    searchData: serialized.slice(0, 4_000).toLowerCase(), omitted: false,
  }
}
const explorerEventKey = item => timelineEventKey(item.event)

const explorerPreviewForQuery = (item, query) => {
  if (!query || item.omitted || !item.serialized) return item.preview
  const match = item.searchData.indexOf(query)
  if (match < 0) return item.preview
  // Put the hit near the beginning so it remains visible in the single-line desktop preview. The
  // rest of the 500-character window supplies useful context after the match.
  const start = Math.max(0, match - 24)
  const end = Math.min(item.serialized.length, start + Math.max(500, query.length + 24))
  return `${start ? '…' : ''}${item.serialized.slice(start, end)}${end < item.serialized.length ? '…' : ''}`
}

const highlightedExplorerText = (value, query) => {
  if (!query) return value
  const lower = value.toLowerCase()
  const parts = []
  let from = 0
  let match = lower.indexOf(query)
  while (match >= 0) {
    if (match > from) parts.push(value.slice(from, match))
    parts.push(<mark className="event-explorer-hit" key={`${match}:${parts.length}`}>
      {value.slice(match, match + query.length)}
    </mark>)
    from = match + query.length
    match = lower.indexOf(query, from)
  }
  if (from < value.length) parts.push(value.slice(from))
  return parts.length ? parts : value
}

export function EventExplorer({ runId, timeline, historyActive = false, onReturnToLive = null, onClose }) {
  const [f, setF] = useState('')
  const [expandedPayload, setExpandedPayload] = useState(null)
  const [copyStatus, setCopyStatus] = useState(null)
  const copyRequestRef = useRef(0)
  const payloadRef = useRef(null)
  const detailIdPrefix = useId()
  const query = f.trim().toLowerCase()
  const indexed = useMemo(() => timeline.rows.map(explorerEvent), [timeline.rows])
  const rows = useMemo(() => indexed.filter(item => !query
    || item.searchType.includes(query) || item.searchData.includes(query)), [indexed, query])
  const explorerIdentity = `${runId}:${timeline.generation || 'pending'}`
  const expandedKey = expandedPayload?.identity === explorerIdentity ? expandedPayload.key : null
  useEffect(() => {
    copyRequestRef.current += 1
    setExpandedPayload(null)
    setCopyStatus(null)
  }, [explorerIdentity])
  useEffect(() => {
    if (expandedKey == null || rows.some(item => (
      explorerEventKey(item) === expandedKey && !item.omitted && item.serialized !== '{}'
    ))) return
    copyRequestRef.current += 1
    setExpandedPayload(null)
    setCopyStatus(null)
  }, [expandedKey, rows])
  const togglePayload = item => {
    const key = explorerEventKey(item)
    const opening = expandedKey !== key
    if (opening && !historyActive && timeline.followingTail) timeline.setFollowingTail(false)
    copyRequestRef.current += 1
    setCopyStatus(null)
    setExpandedPayload(opening ? { identity: explorerIdentity, key } : null)
  }
  const copyPayload = async item => {
    const key = explorerEventKey(item)
    const request = copyRequestRef.current + 1
    copyRequestRef.current = request
    setCopyStatus({ identity: explorerIdentity, key, state: 'copying' })
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable')
      await navigator.clipboard.writeText(item.serialized)
      if (copyRequestRef.current === request) {
        setCopyStatus({ identity: explorerIdentity, key, state: 'copied' })
      }
    } catch {
      if (copyRequestRef.current === request) {
        setCopyStatus({ identity: explorerIdentity, key, state: 'error' })
      }
    }
  }
  const selectPayload = item => {
    const key = explorerEventKey(item)
    const element = payloadRef.current
    const selection = window.getSelection?.()
    if (!element || expandedKey !== key || !selection) {
      setCopyStatus({ identity: explorerIdentity, key, state: 'selection-error' })
      return
    }
    copyRequestRef.current += 1
    try {
      element.focus({ preventScroll: true })
      const range = document.createRange()
      range.selectNodeContents(element)
      selection.removeAllRanges()
      selection.addRange(range)
      setCopyStatus({ identity: explorerIdentity, key, state: 'selected' })
    } catch {
      setCopyStatus({ identity: explorerIdentity, key, state: 'selection-error' })
    }
  }
  const totalLabel = timeline.totalEvents == null
    ? `${timeline.rows.length} loaded events`
    : `${timeline.rows.length} loaded of ${timeline.totalEvents} events`
  return (
    <Panel title="Raw event explorer" sub={totalLabel} onClose={onClose} wide>
      <div className="event-explorer-tools">
        <input className="text" aria-label="Filter loaded events by type or first 4,000 data characters"
          placeholder="filter loaded type or first 4k data chars…"
          value={f} onChange={event => setF(event.target.value)} />
        <button type="button" className="btn sm" disabled={!timeline.hasMore.older || timeline.loading.older}
          onClick={timeline.loadOlder}>{timeline.loading.older ? 'Loading…' : 'Load older'}</button>
        {timeline.hasMore.newer && <button type="button" className="btn sm" disabled={timeline.loading.newer}
          onClick={timeline.loadNewer}>{timeline.loading.newer ? 'Loading…' : 'Load newer'}</button>}
        <button type="button" className="btn sm ghost" disabled={timeline.loading.tail || (!historyActive && timeline.followingTail && timeline.windowAtTail)}
          onClick={onReturnToLive || timeline.jumpToLive}>{timeline.loading.tail ? 'Refreshing…' : 'Latest'}</button>
      </div>
      {timeline.totalEvents != null && timeline.totalEvents > timeline.rows.length && <div className="timeline-window-note" role="note">
        Search covers the loaded window only. Page through the log to inspect other source events.
      </div>}
      {timeline.errors.tail && <div className="notice resource-error compact" role="alert">
        Newest events could not be refreshed. <button type="button" className="btn sm" onClick={() => timeline.retry('tail')}>Retry</button></div>}
      {timeline.errors.older && <div className="notice resource-error compact" role="alert">
        Could not load older events; current rows are unchanged. <button type="button" className="btn sm" onClick={timeline.loadOlder}>Retry</button></div>}
      {timeline.errors.newer && <div className="notice resource-error compact" role="alert">
        Newer-page refresh failed; loaded rows may be behind. <button type="button" className="btn sm" onClick={timeline.loadNewer}>Retry</button></div>}
      {timeline.errors.around && <div className="notice resource-error compact" role="alert">
        Replay window could not be loaded. <button type="button" className="btn sm" onClick={() => timeline.retry('around')}>Retry</button></div>}
      {timeline.tornTail && <div className="timeline-window-note warning" role="status">
        {timeline.sourceTailLimited ? 'Raw source tail exceeded the safety limit.' : 'Only the verified canonical event prefix is shown.'}
      </div>}
      {timeline.status === 'loading' && !timeline.rows.length
        ? <div className="timeline-resource muted" role="status">Loading events…</div>
        : timeline.status !== 'error' && rows.length === 0
          ? <div className="timeline-resource muted">{query ? 'No matches in the loaded window.' : 'No verified events.'}</div>
          : <VirtualTimeline rows={rows} getKey={explorerEventKey}
              identity={`${runId}:${timeline.generation || 'pending'}:explorer`}
              className="event-explorer-timeline" ariaLabel="Loaded raw events"
              followingTail={!historyActive && timeline.followingTail} windowAtTail={!historyActive && timeline.windowAtTail}
              unread={timeline.unread} unreadUnknown={timeline.unreadUnknown}
              busy={Object.values(timeline.loading).some(Boolean)}
              onFollowingTailChange={value => { if (!historyActive) timeline.setFollowingTail(value) }}
              onJumpToLive={onReturnToLive || timeline.jumpToLive}
              estimateSize={42}
              renderRow={item => {
                const key = explorerEventKey(item)
                const open = expandedKey === key
                const expandable = !item.omitted && item.serialized !== '{}'
                const detailsId = `${detailIdPrefix}-${item.event.seq}`
                const status = copyStatus?.identity === explorerIdentity && copyStatus.key === key
                  ? copyStatus.state : null
                const preview = explorerPreviewForQuery(item, query)
                return <div className={'event-explorer-row' + (item.omitted ? ' omitted' : '')}>
                  {expandable
                    ? <button type="button" className="event-explorer-toggle" aria-expanded={open}
                        aria-controls={detailsId}
                        aria-label={`${open ? 'Hide' : 'Show'} full payload for event ${item.event.seq}`}
                        onClick={() => togglePayload(item)}>
                        <span aria-hidden="true">{open ? '▾' : '▸'}</span>
                      </button>
                    : <span className="event-explorer-toggle-placeholder" aria-hidden="true" />}
                  <span className="event-explorer-seq">{item.event.seq}</span>
                  <span className="event-explorer-type">
                    {highlightedExplorerText(String(item.event.type || ''), query)}
                  </span>
                  <span className="event-explorer-data">{highlightedExplorerText(preview, query)}</span>
                  {open && <div id={detailsId} className="event-explorer-detail">
                    <div className="event-explorer-detail-tools">
                      <span>Full payload · {item.serialized.length.toLocaleString()} characters</span>
                      <div className="event-explorer-detail-actions">
                        <button type="button" className="btn sm ghost" onClick={() => selectPayload(item)}>
                          Select payload
                        </button>
                        <button type="button" className="btn sm ghost" disabled={status === 'copying'}
                          onClick={() => copyPayload(item)}>
                          {status === 'copying' ? 'Copying…' : status === 'copied' ? 'Copied' : 'Copy payload'}
                        </button>
                      </div>
                    </div>
                    {status === 'copied' && <div className="event-explorer-copy-status" role="status">Payload copied.</div>}
                    {status === 'selected' && <div className="event-explorer-copy-status" role="status">
                      Payload selected. Press Ctrl+C to copy it.
                    </div>}
                    {status === 'error' && <div className="event-explorer-copy-status error" role="status">
                      Copy failed. Use Select payload, then press Ctrl+C.
                    </div>}
                    {status === 'selection-error' && <div className="event-explorer-copy-status error" role="status">
                      Payload selection is unavailable in this browser.
                    </div>}
                    <pre ref={payloadRef} className="event-explorer-json" role="region" tabIndex={0}
                      aria-label={`Full payload for event ${item.event.seq}`}>{item.serialized}</pre>
                  </div>}
                </div>
              }} />}
    </Panel>
  )
}

const _MAX_VIEW = 2_000_000   // mirrors server _ART_MAX_BYTES (inline text view cap)

const ARTIFACT_SCOPE_ERRORS = new Set([
  'artifact_attempt_protocol_error',
  'artifact_generation_protocol_error',
  'artifact_path_protocol_error',
  'artifact_run_protocol_error',
  'invalid_run_generation',
  'node_attempt_changed',
  'node_attempt_required',
  'run_generation_changed',
  'run_generation_unavailable',
])

const artifactScopeChanged = error => error?.status === 409 || ARTIFACT_SCOPE_ERRORS.has(error?.code)

// Workspace file browser: lists the run directory and live host repo / reference / data paths declared
// by a RepoTask. Those task paths can contain inputs, later edits and outputs; without a start-time
// manifest the UI deliberately makes no file-level production claim. Text files open inline; binary /
// oversize ones are flagged. Backed by GET /api/runs/{id}/artifacts + /artifact.
export function ArtifactsPanel({ runId, expectedGeneration, onToast, onClose }) {
  const [roots, setRoots] = useState(null)
  const [err, setErr] = useState(null)
  const [open, setOpen] = useState({})        // root id -> expanded?
  const [sel, setSel] = useState(null)        // { root, path }
  const [content, setContent] = useState(null)
  const [busy, setBusy] = useState(false)
  const [filter, setFilter] = useState('')
  const [reload, setReload] = useState(0)
  const inventoryReqRef = useRef(0)
  const contentAbortRef = useRef(null)
  const scope = `${String(runId)}@${String(expectedGeneration || '')}`
  const scopeRef = useRef(scope)
  scopeRef.current = scope
  const generationReady = RUN_GENERATION_RE.test(expectedGeneration || '')
  const reqRef = useRef(0)                     // request token — drops out-of-order view() responses

  useEffect(() => {
    const requestScope = scope
    const token = ++inventoryReqRef.current
    ++reqRef.current
    contentAbortRef.current?.abort()
    contentAbortRef.current = null
    setRoots(null)
    setErr(null)
    setOpen({})
    setSel(null)
    setContent(null)
    setBusy(false)
    setFilter('')

    if (!generationReady) return () => { inventoryReqRef.current += 1 }

    const controller = new AbortController()
    getRunArtifactInventory(runId, expectedGeneration, { signal: controller.signal }).then(d => {
      if (token !== inventoryReqRef.current || requestScope !== scopeRef.current) return
      if (!Array.isArray(d.roots)) throw new Error('Invalid file inventory')
      const rs = d.roots
      setRoots(rs)
      const o = {}; rs.forEach(r => { o[r.id] = !!r.is_run_dir })   // expand the run dir, collapse repos
      setOpen(o)
    }).catch(e => {
      if (e?.name === 'AbortError' || token !== inventoryReqRef.current || requestScope !== scopeRef.current) return
      setErr(artifactScopeChanged(e)
        ? 'The run or experiment attempt changed while files were loading. Reopen Files from the current run.'
        : e.message)
    })
    return () => {
      controller.abort()
      ++reqRef.current
      contentAbortRef.current?.abort()
      contentAbortRef.current = null
      if (token === inventoryReqRef.current) inventoryReqRef.current += 1
    }
  }, [expectedGeneration, generationReady, reload, runId, scope])

  const view = (rootId, f) => {
    if (!generationReady) return
    const token = ++reqRef.current
    const requestScope = scope
    contentAbortRef.current?.abort()
    const controller = new AbortController()
    contentAbortRef.current = controller
    const hasNodeIdentity = f?.node_id != null || f?.attempt != null
    setSel({
      root: rootId,
      path: f.path,
      ...(hasNodeIdentity ? { nodeId: f.node_id, attempt: f.attempt } : {}),
    })
    setContent(null); setBusy(true)
    // Always fetch — don't trust the extension-based is_text GUESS (a text .bin/.pb would otherwise be
    // unviewable); the SERVER does the authoritative binary sniff. The token ignores a stale response
    // (fast-clicking A then B must not let A's slower reply render under B).
    getRunArtifactContent(runId, {
      root: rootId,
      path: f.path,
      expectedGeneration,
      ...(hasNodeIdentity ? { nodeId: f.node_id, attempt: f.attempt } : {}),
      signal: controller.signal,
    }).then(c => {
      if (token === reqRef.current && requestScope === scopeRef.current) setContent(c)
    }).catch(e => {
      if (e?.name === 'AbortError' || token !== reqRef.current || requestScope !== scopeRef.current) return
      setContent(null)
      if (artifactScopeChanged(e)) {
        setRoots(null)
        setOpen({})
        setSel(null)
        setErr(e?.code === 'node_attempt_changed'
          ? 'This experiment attempt changed. Reopen Files to load its current inventory.'
          : 'The run changed while this file was loading. Reopen Files from the current run.')
      } else onToast?.('view failed: ' + e.message)
    }).finally(() => {
      if (token !== reqRef.current || requestScope !== scopeRef.current) return
      if (contentAbortRef.current === controller) contentAbortRef.current = null
      setBusy(false)
    })
  }

  const ql = filter.trim().toLowerCase()
  // Search every server-bounded root, including collapsed ones. Keeping disclosure state separate
  // avoids rendering thousands of hidden-root rows while still making each root's match count honest.
  const matchesByRoot = ql && roots
    ? new Map(roots.map(r => [r.id, r.files.filter(f => f.path.toLowerCase().includes(ql))]))
    : null
  const totalMatches = matchesByRoot
    ? [...matchesByRoot.values()].reduce((total, files) => total + files.length, 0)
    : 0
  const cappedSearch = !!(ql && roots?.some(r => r.truncated))
  const binary = content && content.is_text === false   // the server's verdict, not the extension guess
  return (
    <Panel title="Files" sub={runId} wide onClose={onClose}>
      {!generationReady ? <div className="muted">Waiting for the current run identity…</div>
        : err ? <div className="notice">Could not load files: {err}{' '}
            <button type="button" className="btn sm" onClick={() => { setErr(null); setReload(n => n + 1) }}>Retry</button>
          </div>
        : !roots ? <div className="muted">Loading…</div> :
        <div className="art-wrap">
          <div className="art-list">
            <input className="text art-filter" aria-label="Filter loaded files" placeholder="filter loaded files…" value={filter}
                   onChange={e => setFilter(e.target.value)} />
            {ql && <div className="muted art-filter-status" role="status" aria-live="polite" aria-atomic="true">
              {totalMatches === 0
                ? 'No matches in the loaded file inventory.'
                : `${totalMatches} ${totalMatches === 1 ? 'match' : 'matches'} in the loaded file inventory.`}
              {cappedSearch && ' Some roots reached the listing limit, so other matches may exist.'}
            </div>}
            {roots.length === 0 && <div className="muted">No files found.</div>}
            {roots.map(r => {
              const isOpen = !!open[r.id]
              const matches = matchesByRoot?.get(r.id) || r.files
              const files = isOpen ? matches : null
              return (
                <div className="art-root" key={r.id}>
                  <button type="button" className="art-root-h disclosure-button" title={r.path}
                       aria-expanded={isOpen} onClick={() => setOpen(o => ({ ...o, [r.id]: !isOpen }))}>
                    <span className="art-chev">{isOpen ? '▾' : '▸'}</span>
                    <b>{r.label}</b>
                    <span className="muted art-root-n">
                      {ql ? `${matches.length} ${matches.length === 1 ? 'match' : 'matches'} · ${r.n_files} loaded`
                        : `${r.n_files}${r.truncated ? ' loaded · cap reached' : ''}`}</span>
                  </button>
                  {isOpen && <div className="art-files">
                    {files.length === 0 ? <div className="muted art-empty">{ql
                      ? (r.truncated ? 'no match in loaded subset' : 'no match in this root')
                      : 'empty'}</div>
                      : files.map(f => (
                        <button type="button" key={f.path} title={f.path + (f.is_text ? '' : ' · looks binary')}
                             aria-pressed={!!(sel && sel.root === r.id && sel.path === f.path)}
                             className={'art-file disclosure-button' + (sel && sel.root === r.id && sel.path === f.path ? ' sel' : '')
                               + (f.is_text ? '' : ' bin')}
                             onClick={() => view(r.id, f)}>
                          <span className="art-name">{f.path}</span>
                          <span className="art-size">{fmtBytes(f.size)}</span>
                        </button>))}
                    {r.truncated && <div className="muted art-empty">{ql
                      ? `Filter checked ${r.n_files} loaded files; this root may contain more matches.`
                      : `Listing stopped at ${r.n_files} files; this root may contain more.`}</div>}
                  </div>}
                </div>
              )
            })}
          </div>
          <div className="art-view">
            {!sel ? <div className="muted art-hint">Select a file to view its contents.</div> : <>
              <div className="art-view-h">
                <span className="art-view-path" title={sel.path}>{sel.path}</span>
                {content && <span className="muted">{fmtBytes(content.size)}</span>}
              </div>
              {busy ? <div className="muted">Loading…</div>
                : binary ? <div className="notice">Binary file — not shown inline ({fmtBytes(content.size)}).</div>
                : content ? <>
                    {content.truncated && <div className="notice art-trunc">Showing the first {fmtBytes(_MAX_VIEW)} — the file is larger.</div>}
                    <pre className="art-pre" role="region" aria-label={`File ${sel.path} contents`} tabIndex={0}>{content.content}</pre>
                  </> : <div className="muted art-hint">Could not load this file.</div>}
            </>}
          </div>
        </div>}
    </Panel>
  )
}
