import React, { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { deadlineGet, get, post, fmt, fmtInt, fmtBytes, fmtElapsedSeconds, CONTROL,
  operatorMeta, runApiPath, runNodeApiPath, createIdempotencyKey,
  getAuthoringOperation, putAuthoringOperation, validAuthoringName, validAuthoringTargetRootId,
  getRunArtifactContent, getRunArtifactInventory, submitCommand,
} from './util.js'
import { usePoll, useScopedResource } from './hooks.js'
import {
  coverageSummary, filterRows, groupByConceptTree, memoryTierBlurb, rowConcepts, rowSource,
  shelfConcepts, SOURCE_RUN, UNTAGGED,
} from './conceptShelf.js'
import { Bars, ParallelCoords, Scatter } from './charts.jsx'
import { EXTRA_METRIC_CHANNEL_HELP, unverifiedExtraMetricKeys } from './extraMetrics.js'
import { hyperImportance } from './report.js'
import Markdown, { stripMd } from './markdown.jsx'
import { OpIcon } from './icons.jsx'
import CodeViewer from './CodeViewer.jsx'
import { diffLines } from './lineDiff.js'
import { driftStatus, leakageStatus, rewardHackStatus, OBJECTIVE_SOURCE_LABEL,
  objectiveMetricSource, objectiveSourceCaveated, objectiveSourceHelp } from './trustSemantics.js'
import { bestMetricCaveatLabel, metricComparable, sortRuns } from './runIndex.js'
import { crossRunGroups, groupClaim, rankCoverage } from './crossRunRank.js'
import VirtualTimeline from './VirtualTimeline.jsx'
import { timelineEventKey } from './timelineModel.js'
import { queuedGenerationControls } from './queue.js'
import Panel from './PanelShell.jsx'
import { DataTable } from './accessibility.jsx'
import { normalizeResearchMemos } from './researchMemoModel.js'
import ResearchMemoCard from './ResearchMemoCard.jsx'
import { deadlineRequest } from './requestDeadline.js'
import { installNavigationLossGuard } from './navigationLossGuard.js'
import { createInspectorDraftStore, useInspectorDraftField } from './inspectorDraftStore.js'
import {
  AUTHORING_OPERATION_STORAGE_PREFIX, authoringRecoveryStorageKey,
} from './authoringRecoveryStorage.js'
import {
  invalidPanelPayload, isRecord, nullableNumber, nullableText, PANEL_REQUEST_TIMEOUT_MS,
  RUN_GENERATION_RE,
} from './panelPrimitives.js'

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

const validReadOnlySkillDisplayName = value => {
  if (typeof value !== 'string' || /[\\\p{C}]/u.test(value)) return false
  const parts = value.split('/')
  return authoringUtf8Bytes(value) <= 4096 && parts.length >= 2 && parts.length <= 17
    && parts.at(-1) === 'SKILL.md'
    && parts.every(part => part && part !== '.' && part !== '..' && [...part].length <= 255)
}

function authoringPayload(value, kind) {
  if (!isRecord(value) || !nullableText(value.dir) || !Array.isArray(value.files)) invalidPanelPayload()
  const targetRootId = value.target_root_id == null ? null : value.target_root_id
  const truncatedFiles = value.truncated_files == null ? 0 : value.truncated_files
  const inventoryIncomplete = value.inventory_incomplete ?? false
  if (!Number.isSafeInteger(truncatedFiles) || truncatedFiles < 0) invalidPanelPayload()
  if (typeof inventoryIncomplete !== 'boolean'
      || (value.dir == null && (targetRootId !== null || value.files.length > 0
        || truncatedFiles > 0 || inventoryIncomplete))
      || (value.dir != null && !validAuthoringTargetRootId(targetRootId))) invalidPanelPayload()
  const files = value.files.map(file => {
    const readOnly = file?.read_only === true
    if (!isRecord(file) || typeof file.text !== 'string' || typeof file.truncated !== 'boolean'
        || (file.read_only != null && typeof file.read_only !== 'boolean')
        || (readOnly
          ? kind !== 'skills' || !validReadOnlySkillDisplayName(file.name)
          : !validAuthoringName(file.name))) invalidPanelPayload()
    const truncated = file.truncated
    const revision = typeof file.revision === 'string' && AUTHORING_REVISION_RE.test(file.revision)
      ? file.revision : null
    if ((truncated && file.revision !== null) || (!truncated && revision == null)) invalidPanelPayload()
    return { name: file.name, text: file.text, revision, truncated, readOnly }
  })
  return { dir: value.dir, targetRootId, files, truncatedFiles, inventoryIncomplete }
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

function clearAuthoringRecord(key, raw) {
  const storage = authoringStorage()
  if (!storage || !key || typeof raw !== 'string') return false
  try {
    if (storage.getItem(key) !== raw) return false
    storage.removeItem(key)
    return storage.getItem(key) == null
  } catch { return false }
}

const clearAuthoringOperationIntent = intent => clearAuthoringRecord(
  intent?.storageKey, intent?.storageRaw)
const clearDamagedAuthoringOperation = recovery => clearAuthoringRecord(
  recovery?.key, recovery?.raw)

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

// The concept SHELF envelope (`engine/concept_shelf.py::build_shelf`). Validated but OPTIONAL: an older
// server has no `concept_index`, and refusing the whole Memory payload over its absence would trade a
// missing feature for a dead panel. Absent -> null, and the panel falls back to its flat list.
function conceptIndexPayload(value) {
  if (value == null) return null
  if (!isRecord(value) || !isRecord(value.tree) || !isRecord(value.tree.nodes)
      || !isRecord(value.coverage)) invalidPanelPayload()
  const coverage = {}
  for (const key of ['cases', 'lessons', 'notes']) {
    const receipt = value.coverage[key]
    if (receipt == null) continue
    if (!isRecord(receipt) || !Number.isSafeInteger(receipt.total) || receipt.total < 0
        || !Number.isSafeInteger(receipt.tagged) || receipt.tagged < 0 || receipt.tagged > receipt.total
        || !Number.isSafeInteger(receipt.untagged)
        || receipt.untagged !== receipt.total - receipt.tagged) invalidPanelPayload()
    coverage[key] = receipt
  }
  return { tree: value.tree, coverage,
           runsIndexed: Number.isSafeInteger(value.runs_indexed) ? value.runs_indexed : 0,
           runsTagged: Number.isSafeInteger(value.runs_tagged) ? value.runs_tagged : 0 }
}

// A row's concept stamp travels on EVERY tier, so validate it in one place. `concepts` present with a
// non-array, or a source outside the closed vocabulary, is a contract break — not a field to ignore —
// because the panel renders a different affordance per source and a silently-dropped source would
// display a run-level guess as a record-level fact.
function validRowConcepts(row) {
  if (row.concepts != null && (!Array.isArray(row.concepts)
      || row.concepts.some(id => typeof id !== 'string'))) return false
  return row.concept_source == null
    || row.concept_source === 'record' || row.concept_source === 'run'
}

function memoryPayload(value) {
  if (!isRecord(value) || !nullableText(value.dir)
      || !['cases', 'lessons', 'notes'].every(key => Array.isArray(value[key]))) invalidPanelPayload()
  const cases = value.cases.map(row => {
    if (!isRecord(row) || !row.task_id || typeof row.task_id !== 'string' || typeof row.goal !== 'string'
        || !nullableNumber(row.metric) || !Object.hasOwn(row, 'params')
        || (row.direction != null && !['min', 'max'].includes(row.direction))
        || ['rationale', 'run_id'].some(key => row[key] != null && typeof row[key] !== 'string')
        || !validRowConcepts(row)
        || (row.params_truncated != null && typeof row.params_truncated !== 'boolean')) invalidPanelPayload()
    return { ...row, params_truncated: row.params_truncated === true }
  })
  const lessonText = ['role', 'kind', 'outcome', 'claim_stance', 'task_id', 'run_id', 'run_uid']
  for (const row of value.lessons) {
    if (!isRecord(row) || !row.statement || typeof row.statement !== 'string'
        || lessonText.some(key => row[key] != null && typeof row[key] !== 'string')
        || ['delta', 'confidence'].some(key => row[key] != null && !nullableNumber(row[key]))
        || !validRowConcepts(row)
        || ['evidence_count', 'evidence_traceable_count', 'evidence_untraceable_count'].some(
          key => row[key] != null && (!Number.isSafeInteger(row[key]) || row[key] < 0))) invalidPanelPayload()
  }
  const notes = value.notes.map(row => {
    const note = isRecord(row) && (row.note || row.statement)
    if (typeof note !== 'string' || !note || !validRowConcepts(row)
        || ['task_id', 'run_id', 'at'].some(key => row[key] != null && typeof row[key] !== 'string')) invalidPanelPayload()
    return { ...row, note }
  })
  if (value.dir == null) {
    if (cases.length || value.lessons.length || notes.length || value.projection != null || value.page != null) {
      invalidPanelPayload()
    }
    return { dir: null, cases, lessons: value.lessons, notes, projection: null, page: null,
             conceptIndex: null }
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
        || !Number.isSafeInteger(receipt.filtered) || receipt.filtered < 0
        || !Number.isSafeInteger(receipt.superseded) || receipt.superseded < 0
        || typeof receipt.source_window_truncated !== 'boolean'
        || typeof receipt.unavailable !== 'boolean'
        || (receipt.unavailable && receipt.returned !== 0)) invalidPanelPayload()
    tiers[key] = {
      limit: receipt.limit,
      returned: receipt.returned,
      skipped: receipt.skipped,
      filtered: receipt.filtered,
      superseded: receipt.superseded,
      sourceRows: Number.isSafeInteger(receipt.source_rows) && receipt.source_rows >= 0
        ? receipt.source_rows : null,
      sourceSize: Number.isSafeInteger(receipt.source_size) && receipt.source_size >= 0
        ? receipt.source_size : null,
      windowDigest: typeof receipt.window_digest === 'string'
        && /^[a-f0-9]{64}$/.test(receipt.window_digest) ? receipt.window_digest : '',
      sourceWindowTruncated: receipt.source_window_truncated,
      unavailable: receipt.unavailable,
    }
  }
  const truncated = Object.values(tiers).some(receipt => receipt.sourceWindowTruncated)
  const unavailable = Object.values(tiers).some(receipt => receipt.unavailable)
  if (typeof value.concept_index_available !== 'boolean') invalidPanelPayload()
  const partial = !value.concept_index_available || Object.values(tiers).some(
    receipt => receipt.sourceWindowTruncated || receipt.skipped > 0 || receipt.unavailable)
  if (value.page.truncated !== truncated || value.page.unavailable !== unavailable
      || value.page.partial !== partial) invalidPanelPayload()
  return {
    dir: value.dir,
    cases,
    lessons: value.lessons,
    notes,
    projection: value.projection,
    page: { tiers, truncated, unavailable, partial },
    conceptIndex: conceptIndexPayload(value.concept_index),
    conceptIndexAvailable: value.concept_index_available,
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
// Those rules are now the shared machine's (doc 25 UI-06); what stays here is the panel dialect —
// the normalize step that runs INSIDE the deadline so a malformed HTTP 200 fails the read, and the
// tuple shape the panels destructure. Panels never render a server message, so no failure classifier
// is passed and `resource.error` stays ''.
function usePanelResource(loader, normalize = value => value, key = '', pollMs = null) {
  const resource = useScopedResource(
    signal => Promise.resolve().then(() => loader(signal)).then(normalize),
    { scope: key, timeout: PANEL_REQUEST_TIMEOUT_MS, pollMs })
  return [resource, () => Boolean(resource.retry())]
}

function PanelResourceNotice({ resource, label, onRetry }) {
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
export function ResearchPanel({ state, runId, onToast, onClose, onSelect, onSelectEvidence }) {
  const memoProjection = useMemo(() => normalizeResearchMemos(state.research), [state.research])
  const memos = [...memoProjection.memos].reverse()   // newest retained first
  const newestMemoIndex = memos[0]?.sourceIndex ?? null
  const [openMemo, setOpenMemo] = useState(newestMemoIndex)
  const seenNewestMemo = useRef(newestMemoIndex)
  const [steeringDirection, setSteeringDirection] = useState('')
  useEffect(() => {
    if (newestMemoIndex === seenNewestMemo.current) return
    seenNewestMemo.current = newestMemoIndex
    setOpenMemo(newestMemoIndex)
  }, [newestMemoIndex])
  const steer = async (text) => {
    if (steeringDirection) return
    setSteeringDirection(text)
    try {
      await submitCommand(CONTROL.hint(runId, 'try this research direction: ' + text), {
        success: 'Steered the next proposal', noop: 'That direction was already queued',
        executing: 'Steer request accepted — waiting for the run', failure: 'Could not steer',
      }, onToast)
    } finally {
      setSteeringDirection('')
    }
  }
  return (
    <Panel title="Deep research" sub={memos.length ? `${memos.length} memo${memos.length === 1 ? '' : 's'}` : 'none yet'} onClose={onClose} wide>
      {!memos.length && <div className="research-empty-state" role="status">
        <div><OpIcon name="search" size={16} /><strong>No research memos yet</strong></div>
        <p>Deep research runs on the configured cadence or when the Strategist requests it.</p>
        <details><summary>Why can this be empty?</summary>
          <p>The run may not have created its first experiment, the LLM backend may be unavailable,
            or <code>deep_research_every</code> may be set to <code>-1</code>.</p>
        </details>
      </div>}
      {memoProjection.omitted > 0 && <div className="muted">
        Showing {memos.length} of {memoProjection.total} newest valid memos; older, malformed, or over-budget entries are omitted.
      </div>}
      <div className="research-memo-stack">{memos.map((memo, index) => (
        // Append-only sourceIndex is stable while the newest card is inserted at the top.
        <ResearchMemoCard key={memo.sourceIndex} memo={memo} memoNumber={memo.sourceIndex + 1}
          latest={index === 0} open={openMemo === memo.sourceIndex}
          onToggle={() => setOpenMemo(current => current === memo.sourceIndex ? null : memo.sourceIndex)}
          variant="panel" normalized
          onSteer={steer} steeringDirection={steeringDirection} onSelectNode={onSelect}
          onSelectEvidence={onSelectEvidence} />
      ))}</div>
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
  // Trust coverage is only verifiable against the detector config, so this panel DOES render the
  // failure message — hence its own classifier. It bounds the read at the panel deadline like every
  // sibling panel: before doc 25 UI-06 this was the one config read with no deadline at all, and a
  // hung /config left "Loading detector configuration" on screen forever with no way to retry.
  const configResource = useScopedResource(signal => get(runApiPath(runId, '/config'), { signal }), {
    scope: runId, timeout: PANEL_REQUEST_TIMEOUT_MS,
    classifyFailure: ({ error }) => ({ error: error?.message || 'Request failed' }),
  })
  // An in-flight read is what the operator is waiting on whether it started as the first load or as
  // a retry, and a retry here always starts from `error` (the Retry control only exists there), so
  // there is never last-good config to keep: both read as "loading", exactly as before.
  const configLoading = configResource.status === 'loading' || !!configResource.pending
  const cfg = configResource.data
  const quarantine = async (id) => {   // U6: act on a flagged node — remove it from the search
    await submitCommand(CONTROL.nodeAbort(runId, id, state.nodes?.[id]?.attempt), {
      success: `Quarantined #${id}`, noop: `#${id} was already settled`,
      executing: `Quarantine of #${id} requested — waiting for the run`, failure: `Could not quarantine #${id}`,
    }, onToast)
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
      {configLoading && <TrustState value={{ tone: 'loading', label: 'Loading detector configuration', detail: 'Checking which trust controls were actually enabled for this run.' }} />}
      {configResource.status === 'error' && !configLoading && <TrustState
        value={{ tone: 'unknown', label: 'Detector configuration unavailable', detail: `Coverage cannot be verified: ${configResource.error}` }}
        action={<button className="btn sm" onClick={() => configResource.retry()}>Retry</button>} />}

      <div className="cardgrid">
        <Stat n={cfg?.trust_mode || (configLoading ? 'Loading…' : 'Unknown')} l="sandbox tier" />
        <Stat n={cfg?.eval_trust_mode || (configLoading ? 'Loading…' : 'Unknown')} l="eval trust mode" />
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
            // `|| 'Multiple'` turned a recorded ZERO into the reassuring word, on a panel whose whole
            // job is to say whether seed luck was ruled out. A count of 0 beside a `confirmed_mean`
            // is a contradiction the operator needs to see, not one to smooth over. Print any real
            // number; reserve the word for an absent count. (Same fix in Inspector.jsx's Robustness.)
            ? { tone: 'ok', label: 'Winner is multi-seed confirmed', detail: `${typeof robust.confirmed_seeds === 'number' ? robust.confirmed_seeds : 'Multiple'} successful seeds produced ${fmt(robust.confirmed_mean)} ±${fmt(robust.confirmed_std)}.` }
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
    await submitCommand(CONTROL.nodeAbort(runId, id, state.nodes?.[id]?.attempt), {
      success: `Cancelled #${id}`, noop: `#${id} was already settled`,
      executing: `Cancellation of #${id} requested — waiting for the run`, failure: `Could not cancel #${id}`,
    }, onToast)
  }
  const queuedCount = pending.length + injects.length + forks.length + confirmReq.length + ablateReq.length
  return (
    <Panel title="Queue" sub={`${queuedCount} planned / in-flight`} onClose={onClose}>
      <div className="muted" style={{ marginBottom: 10 }}>
        {/* `/experiment` was never a command either (same dead end as the research panel's old
            `/deep-research`). The two shapes that DO append an inject/fork are the graph node menu's
            own items, so name them exactly as Dag.jsx spells them. */}
        The next experiment is chosen by the search policy; this is the live work-list — cancel a
        pending experiment, or add one from a node’s “Explore from here” / “Merge with…” menu on the
        graph.
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
const paretoMetric = node => node.confirmed_mean ?? node.metric

// Exported for the same reason `Inspector.jsx::Metrics` is: nothing in the suite MOUNTS a panel, and
// which nodes reach the front is a claim about a RECORD that has to be driven, not read off source.
export function paretoFront(nodes, direction) {
  const keys = [...new Set(nodes.flatMap(n => Object.keys(n.extra_metrics || {})))]
  const vec = (n) => [direction === 'min' ? paretoMetric(n) : -paretoMetric(n),
    ...keys.map(k => { const v = n.extra_metrics?.[k]; return v == null ? Infinity : v })]
  const dominates = (a, b) => { let strict = false; for (let i = 0; i < a.length; i++) { if (a[i] > b[i]) return false; if (a[i] < b[i]) strict = true } return strict }
  const pts = nodes.map(n => ({ n, v: vec(n) }))
  return { keys, front: pts.filter(p => !pts.some(q => q !== p && dominates(q.v, p.v))).map(p => p.n) }
}
// THE FRONT IS A SELECTION DISPLAY, and until 2026-08-15 it ranked a SALVAGED objective with no
// caveat at all — while `Inspector.jsx::Metrics`, one tab over, already labelled the same number
// `salvaged` from `trustSemantics.js`. Two surfaces, one record, opposite stories; the reading
// surface got the caveat and the DECIDING surface did not.
//
// DRIVEN, PRE-FIX, and it is worse than a missing word. A run with a measured 0.51 and a
// `metric_salvage: "select"` node at 0.81 rendered ONE row — `#4 👑 0.81` — because a Pareto front
// does not merely rank: a caveated point DOMINATES measured ones off the table entirely. The
// operator opens this panel to decide which configuration to reuse and is shown a single winner
// whose number the run recovered from an eval that failed its contract, with the measured result
// nowhere on screen. That is `runs/rubertlite-dr-unified-v6` node 4's defect drawn as a chart.
//
// CAVEAT, AND DELIBERATELY NOT UNRANK — the cross-run overlay's "shown, valued, NO rank" precedent
// (`crossRunRank.js::buildGroup`, for a prefix-folded run) does not transfer, for three reasons the
// record settles rather than taste:
//   1. A competition RANK is a per-row fact: drop one run from `crossRunRank`'s eligible set and
//      every other run's rank is exactly what it was. Pareto membership is a RELATION over a SET —
//      remove a point from the domination test and OTHER nodes join the front. So "unranked" here
//      is not a statement about the excluded row; it publishes a front the record does not support.
//   2. Under `select` the ENGINE ranks this node: it is in `feasible_nodes` and may be — in the
//      case driven above, IS — `best_node_id`. A front that dropped it would omit the run's own
//      champion while still drawing the crown, i.e. re-create the two-surfaces-one-record defect
//      this change exists to close. The rung where the engine DOES exclude it (`audit`, which mints
//      the violation row) is already excluded below by `feasible !== false`.
//   3. `select` is the OPERATOR's own recorded decision that this number competes. The panel's job
//      is to make sure they can see what they admitted, not to quietly overrule it.
// What the precedent DOES give is its other half — a fact that would otherwise be invisible stays on
// screen. Here the invisible fact is the measured node the caveated point displaced, so the panel
// names the front restricted to measured objectives whenever a caveated node is on this one.
export function ParetoPanel({ state, onClose, onSelect }) {
  const nodes = Object.values(state.nodes).filter(n => paretoMetric(n) != null && n.feasible !== false)
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
        const unverified = unverifiedExtraMetricKeys(nodes, keys)
        const sortedFront = [...front].sort((a, b) => (state.direction === 'min'
          ? paretoMetric(a) - paretoMetric(b) : paretoMetric(b) - paretoMetric(a)))
        // ONE read of the shared vocabulary per node — the same call the Metrics tab makes, over the
        // same three record facts (the violation ROWS, their `salvage.condition`, the folded
        // `metric_provenance`). Derived from the record, never inferred from `feasible`: a node
        // excluded for a breached BOUND is not salvaged, and a salvaged node admitted under `select`
        // is feasible.
        const sources = new Map(sortedFront.map(n => [n.id, objectiveMetricSource(n)]))
        const caveated = sortedFront.filter(n => objectiveSourceCaveated(sources.get(n.id)))
        // The front the operator would read if only measured objectives counted. Computed ONLY when
        // something is caveated, and reported only for the nodes it ADDS — a node already on the
        // real front needs no second mention, and a front that is unchanged is not a finding.
        const measuredFront = caveated.length
          ? paretoFront(nodes.filter(n => !objectiveSourceCaveated(objectiveMetricSource(n))),
            state.direction).front
          : []
        const displaced = measuredFront.filter(m => !front.some(f => f.id === m.id))
        return <>
          <div className="section-h">Pareto-optimal set (I5) {keys.length ? <span className="pill">{keys.length + 1} objectives</span> : <span className="pill">metric only</span>}</div>
          {sortedFront.length
            ? <DataTable caption="Pareto-optimal node metrics" card={false}><table className="tbl"><thead><tr><th>node</th><th>metric</th>
                {/* A column heading carries the caveat, because a whole objective axis is either
                    trustworthy or not — see `unverifiedExtraMetricKeys` for why ANY unverified
                    reporter marks the column. This panel treats every extra as a cost-like
                    objective, so an auto-captured value does not merely sit beside the metric: it
                    changes WHICH nodes are shown as non-dominated. */}
                {keys.map(k => <th key={k}>{k}{unverified.has(k)
                  ? <span className="warn" title={EXTRA_METRIC_CHANNEL_HELP.auto}> ⚠</span> : ''}</th>)}</tr></thead><tbody>
                {sortedFront.map(n =>
                  <tr key={n.id}><td>#{n.id}{n.id === state.best_node_id ? <OpIcon name="crown" size={10} /> : ''}</td>
                    {/* The caveat rides ON the number, not in a column of its own: this table's
                        columns are the OBJECTIVES, and an operator comparing two rows compares the
                        cells side by side. Same word and same sentence as the Metrics tab, from the
                        same call, so the two cannot drift. */}
                    <td>{fmt(n.confirmed_mean ?? n.metric)}{objectiveSourceCaveated(sources.get(n.id))
                      ? <span className="warn" title={objectiveSourceHelp(sources.get(n.id))}>
                        {' · '}{OBJECTIVE_SOURCE_LABEL[sources.get(n.id).channel]}</span>
                      : ''}</td>
                    {keys.map(k => <td key={k} className="muted">{fmt(n.extra_metrics?.[k])}</td>)}</tr>)}
              </tbody></table></DataTable>
            : <div className="muted">No feasible evaluated nodes yet.</div>}
          {/* PRINTED, not only hovered — the same argument the extras' footnote below already makes,
              and stronger here because this table is what an operator reads to pick a configuration
              to reuse. It renders the SAME sentence the cell's tooltip carries, from the same call. */}
          {caveated.length > 0 && <div className="muted" style={{ marginTop: 8 }}>
            {caveated.map(n => <div key={n.id}>
              ⚠ #{n.id}&rsquo;s objective is <b>{OBJECTIVE_SOURCE_LABEL[sources.get(n.id).channel]}</b>.{' '}
              {objectiveSourceHelp(sources.get(n.id))}
            </div>)}
            <div>It is ranked here because the run ranks it — this front shows the selection the
              engine is actually making, not a corrected one.
              {displaced.length > 0 && <> Set {caveated.length === 1 ? 'it' : 'those'} aside and{' '}
                {displaced.map(n => `#${n.id} (${fmt(n.confirmed_mean ?? n.metric)})`).join(', ')}{' '}
                {displaced.length === 1 ? 'is' : 'are'} non-dominated on measured objectives alone.</>}
            </div>
          </div>}
          {sortedFront.length > 0 && unverified.size > 0 && <div className="muted" style={{ marginTop: 8 }}>
            ⚠ {[...unverified].join(', ')} {unverified.size === 1 ? 'is' : 'are'} not a declared
            measurement: the value was taken from the experiment's own stdout, or the run predates
            the record that would say. The front below is computed over it anyway — read it as a
            trade-off the experiments <i>reported</i>, not one the engine verified.
          </div>}
          {sortedFront.length > 0 && <div className="muted" style={{ marginTop: 8 }}>
            Confirmed mean is used when available; otherwise the recorded metric is used.
            {!keys.length && <> With one objective, every feasible node tied at the best displayed metric is
              Pareto-optimal. Add extra_metrics (e.g. latency, size) to show trade-offs.</>}
          </div>}
        </>
      })()}
      <div className="section-h">Pareto (metric vs constraint)</div>
      {scatter || <div className="muted">No constraints/aux metrics in this task.</div>}
      <div className="section-h">Diversity archive {archive && <span className="pill">{archive.niches} niches</span>}</div>
      {archive?.elites?.length
        ? <DataTable caption="Diversity archive elite nodes" card={false}><table className="tbl"><thead><tr><th>node</th><th>metric</th><th>params</th></tr></thead><tbody>
          {/* The elites are the other "reuse this configuration" table on this panel, so they read
              the same vocabulary off the same node record. An elite naming a node this state does
              not hold answers `measured` by the model's own totality — no record, no caveat. */}
          {archive.elites.map((e, i) => {
            const source = objectiveMetricSource(state.nodes?.[e.node_id])
            return <tr key={i}><td>#{e.node_id}</td>
              <td>{fmt(e.metric)}{objectiveSourceCaveated(source)
                ? <span className="warn" title={objectiveSourceHelp(source)}>
                  {' · '}{OBJECTIVE_SOURCE_LABEL[source.channel]}</span> : ''}</td>
              <td className="muted">{JSON.stringify(e.params)}</td></tr>
          })}</tbody></table></DataTable>
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
          {/* `(s.missing_frac || 0) * 100` invented a measurement. Not every profiler records
              missingness — real logs carry rows that are only `{count, dtype, constant}`
              (`runs/live-nosignal`, `runs/live-periodic`, …) — and this cell then said "0% missing"
              while every OTHER cell in the same row correctly said `—`, because `fmtInt`/`fmt`
              already report an absent value as unknown. A fraction that was never measured is not
              a fraction of zero; only a number is a measurement. */}
          <td>{c}</td><td>{s.dtype}</td>
          <td>{fmt(typeof s.missing_frac === 'number' ? s.missing_frac * 100 : null, 3)}</td>
          <td>{fmtInt(s.n_unique)}</td>
          <td>{fmt(s.min)}</td><td>{fmt(s.max)}</td><td>{fmt(s.mean)}</td>
          <td>{s.constant && <span className="flag">constant </span>}{s.high_missing && <span className="flag">high-missing</span>}</td></tr>)}
      </tbody></table></DataTable>
    </Panel>
  )
}

// ConfigPanel moved to ./ConfigPanel.jsx (doc 25 UI-04); re-exported here so the hub stays the one
// module RunView lazily imports.
export { ConfigPanel, __testPublicConfigForm } from './ConfigPanel.jsx'

// What each authoring kind IS. All three are plain Markdown in a directory you point at, and the
// panel used to name them with three bare lowercase words — which is why "why are skills and prompts
// not in Memory?" is a fair question: they ARE the durable knowledge you write, they are just on the
// other panel because a different party writes them. Each entry says what a file here DOES.
// `disclosure` is the honest footnote: what the engine reads that this editor cannot show.
// The env var that configures each kind's directory. NOT `LOOPLAB_${kind.toUpperCase()}_DIR`: the
// settings field is `prompt_dir`, singular, and `LOOPLAB_* ` env names map 1:1 onto flat field names
// — so the derived `LOOPLAB_PROMPTS_DIR` this panel used to print is a variable nothing reads. An
// operator who followed that hint set it, saw the tab stay empty, and concluded prompts do not exist.
const AUTHORING_KIND_ENV = { prompts: 'LOOPLAB_PROMPT_DIR', skills: 'LOOPLAB_SKILLS_DIR', knowledge: 'LOOPLAB_KNOWLEDGE_DIR' }

const AUTHORING_KIND_PURPOSE = {
  prompts: {
    what: <><b>Role prompt overrides.</b> A file named <code>&lt;prompt key&gt;.md</code> REPLACES the
      built-in system prompt for that role, and is re-read on every call — an edit lands on the next
      agent turn with no restart.</>,
    disclosure: <>A file whose name is not a known prompt key is never looked up: the built-in default
      keeps running, silently. The key list is <code>looplab/core/prompts.py::PROMPT_KEYS</code>.</>,
  },
  skills: {
    what: <><b>Researcher techniques</b>: <code>list_skills</code> / <code>use_skill</code> loads
      recipes and code mid-run.</>,
    disclosure: <>Root <code>*.md</code>: writable. Nested{' '}
      <code>&lt;package&gt;/SKILL.md</code>: read-only; edit its package path. Flat Save/recovery API rejects
      slash paths. Auto-distilled <code>&lt;memory dir&gt;/skills/</code> candidates need cross-task
      promotion for production; not reviewed here.</>,
  },
  knowledge: {
    what: <><b>Free-form notes</b> for the agents to retrieve with <code>kb_search</code> — anything
      worth keeping that is not a per-run finding.</>,
    disclosure: <>The same directory the agents write to with their own <code>remember</code> tool, and
      the same files Lab → Memory → Knowledge shows read-only. So this is the one kind BOTH you and
      the runs write.</>,
  },
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
        message: `Saved operation for ${recovery.name} may be pending; check its durable receipt.`,
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
  const [source, retry] = usePanelResource(
    signal => get(`/api/${kind}`, { signal }), value => authoringPayload(value, kind), kind)
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
    if (source.status !== 'ready') {
      setReconciledSource(null)
      return
    }
    setDocuments(current => {
      let changed = false
      const next = { ...current }
      for (const file of data.files || []) {
        const scope = scopeFor(kind, file.name)
        const previous = current[scope]
        if (file.readOnly) {
          // A package row is an observation, never an editable/recoverable document. This also
          // scrubs stale shared-panel state if a forged/older draft used the same display scope:
          // the flat recovery protocol cannot legitimately create an identity containing `/`.
          const authoritative = {
            ...(previous || {}),
            kind, name: file.name, savedText: file.text, draftText: file.text,
            savedRevision: file.revision, observedText: file.text, observedRevision: file.revision,
            savedTargetRootId: data.targetRootId, observedTargetRootId: data.targetRootId,
            truncated: file.truncated, readOnly: true,
            conflict: false, rootConflict: false,
            observationIncomplete: false, error: '',
            recoveryOperationId: null, recoveryStorageRaw: null,
          }
          if (!previous || Object.keys(authoritative)
            .some(key => authoritative[key] !== previous[key])) {
            next[scope] = authoritative
            changed = true
          }
        } else if (!previous || previous.readOnly) {
          next[scope] = {
            kind, name: file.name, savedText: file.text, draftText: file.text,
            savedRevision: file.revision, observedText: file.text, observedRevision: file.revision,
            savedTargetRootId: data.targetRootId, observedTargetRootId: data.targetRootId,
            truncated: file.truncated, readOnly: file.readOnly,
            conflict: false, rootConflict: false,
            observationIncomplete: false, error: '',
            recoveryOperationId: null, recoveryStorageRaw: null,
          }
          changed = true
        } else if (previous.draftText === previous.savedText && !uncertainSaves[scope]) {
          if (previous.savedText !== file.text || previous.observedText !== file.text
              || previous.savedRevision !== file.revision || previous.truncated !== file.truncated
              || previous.readOnly !== file.readOnly
              || previous.savedTargetRootId !== data.targetRootId
              || previous.observedTargetRootId !== data.targetRootId
              || previous.conflict || previous.rootConflict
              || previous.observationIncomplete || previous.error) {
            next[scope] = { ...previous, savedText: file.text, draftText: file.text,
              savedRevision: file.revision, observedText: file.text, observedRevision: file.revision,
              savedTargetRootId: data.targetRootId, observedTargetRootId: data.targetRootId,
              truncated: file.truncated, readOnly: file.readOnly,
              conflict: false, rootConflict: false,
              observationIncomplete: false, error: '' }
            changed = true
          }
        } else if (previous.observedText !== file.text
            || previous.observedRevision !== file.revision
            || previous.observedTargetRootId !== data.targetRootId
            || previous.readOnly !== file.readOnly
            || previous.observationIncomplete
            || previous.conflict !== (previous.savedRevision !== file.revision
              || previous.savedTargetRootId !== data.targetRootId)) {
          // A refresh must never replace local text. Remember the newly observed server version and
          // make the conflict explicit while preserving both the draft and its original baseline.
          const rootConflict = previous.savedTargetRootId !== data.targetRootId
          next[scope] = { ...previous,
            observedText: file.text, observedRevision: file.revision,
            observedTargetRootId: data.targetRootId, truncated: file.truncated,
            readOnly: file.readOnly,
            rootConflict, observationIncomplete: false,
            conflict: rootConflict || previous.savedRevision !== file.revision }
          changed = true
        }
      }
      const visibleNames = new Set((data.files || []).map(file => file.name))
      const listComplete = data.truncatedFiles === 0 && !data.inventoryIncomplete
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
        const safeDraftText = previous.readOnly ? previous.savedText : previous.draftText
        if (previous.draftText !== safeDraftText
            || previous.observedText !== null || previous.observedRevision !== observedRevision
            || previous.observedTargetRootId !== data.targetRootId
            || previous.rootConflict !== rootConflict || previous.conflict !== conflict
            || previous.observationIncomplete !== observationIncomplete
            || (previous.readOnly
              && (previous.error || previous.recoveryOperationId || previous.recoveryStorageRaw))) {
          next[scope] = {
            ...previous, draftText: safeDraftText, observedText: null, observedRevision,
            observedTargetRootId: data.targetRootId, rootConflict, conflict,
            observationIncomplete,
            ...(previous.readOnly ? {
              error: '', recoveryOperationId: null, recoveryStorageRaw: null,
            } : {}),
          }
          changed = true
        }
      }
      for (const recovery of Object.values(uncertainSaves)) {
        if (recovery.kind !== kind || !validAuthoringName(recovery.name)) continue
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
          truncated: file?.truncated === true, readOnly: false, rootConflict: !sameRoot,
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
  }, [kind, source.status, source.data, uncertainSaves])
  const selected = selectedScope ? documents[selectedScope] || null : null
  const sourceReady = source.status === 'ready'
  const sourceReconciled = sourceReady
    && reconciledSource?.kind === kind && reconciledSource?.data === source.data
  const selectedSourceReconciled = sourceReconciled && selected?.kind === kind
  const selectedUncertainSave = selectedScope ? uncertainSaves[selectedScope] || null : null
  const damagedRows = Object.values(damagedRecoveries)
  const selectedDamagedRecovery = damagedRows.find(recovery => !recovery.identity
    || recovery.identity.scope === selectedScope) || null
  const uncertainSaveCount = Object.keys(uncertainSaves).length
  const damagedRecoveryCount = damagedRows.length
  const dirtyCount = Object.values(documents)
    .filter(document => !document.readOnly && document.draftText !== document.savedText).length
  const mutationBusy = !!saveState
  const navigationUnsafe = dirtyCount > 0 || mutationBusy
    || uncertainSaveCount > 0 || damagedRecoveryCount > 0
  const authoringNavigationSummary = [
    dirtyCount > 0
      ? `${dirtyCount} unsaved Authoring draft${dirtyCount === 1 ? '' : 's'} will be discarded.` : '',
    mutationBusy ? 'A save is in progress; its outcome may not remain visible.' : '',
    uncertainSaveCount > 0
      ? `${uncertainSaveCount} save outcome${uncertainSaveCount === 1 ? '' : 's'} may be unknown; review retained recovery before retrying.` : '',
    damagedRecoveryCount > 0
      ? `${damagedRecoveryCount} damaged recovery record${damagedRecoveryCount === 1 ? ' remains' : 's remain'} quarantined in this tab.` : '',
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
        ? `${uncertainSaveCount} save outcome${uncertainSaveCount === 1 ? '' : 's'} may be unknown. Leave Authoring?`
        : damagedRecoveryCount > 0
          ? `${damagedRecoveryCount} damaged recovery record${damagedRecoveryCount === 1 ? '' : 's'} remain quarantined. Leave Authoring?`
        : mutationBusy
          ? 'A save is in progress. Leave Authoring?'
          : `${dirtyCount} unsaved draft${dirtyCount === 1 ? '' : 's'} will be lost. Leave?`,
      onAllow: () => draftStore.clear(AUTHORING_PANEL_DRAFT_SCOPE),
    })
  }, [navigationGuardOwner, draftStore, navigationUnsafe, mutationBusy, uncertainSaveCount,
    damagedRecoveryCount, dirtyCount])
  useEffect(() => () => {
    const retained = draftStore.readField(AUTHORING_PANEL_DRAFT_SCOPE, 'documents', {})
    const hasUnsafeStoredDocument = retained && typeof retained === 'object'
      && !Array.isArray(retained) && Object.values(retained).some(document => document
        && typeof document === 'object' && !Array.isArray(document)
        && (!document.readOnly && document.draftText !== document.savedText
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
    if (!selected || selected.readOnly || selected.draftText === selected.savedText) return
    onToast?.(`${selected.name} draft remains while you ${destination}.`)
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
        truncated: file.truncated, readOnly: file.readOnly,
        conflict: false, rootConflict: false,
        observationIncomplete: false, error: '',
        recoveryOperationId: null, recoveryStorageRaw: null },
    })
    setSelectedScope(scope)
  }
  const editSelected = value => {
    if (!selectedScope || selected?.readOnly) return
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
  const setDocumentError = (scope, error) => setDocuments(current => current[scope] ? {
    ...current, [scope]: { ...current[scope], error },
  } : current)
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
        truncated: false, readOnly: false, conflict: false, rootConflict: false,
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
      const message = `Could not verify ${exact.name} retained contents: ${error?.message || error}. No request was sent.`
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
      const message = `Recovery storage became unavailable while verifying ${exact.name}. No request was sent.`
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
      onToast?.('Recovery record changed during verification; inspect the refreshed exact record.')
      return null
    }
    if (actualRevision !== exact.desiredRevision) {
      quarantineAuthoringRecovery(exact,
        `${exact.name} retained operation has an invalid digest; quarantined without server contact.`)
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
            message: `Saved operation for ${record.name} may be pending; check its durable receipt.` }]
      }))
      const damagedSnapshots = new Set(Object.values(inspected.damaged)
        .map(record => `${record.key}\u0000${record.raw}`))
      for (const [scope, previous] of Object.entries(current)) {
        if (next[scope] || damagedSnapshots.has(`${previous.storageKey}\u0000${previous.storageRaw}`)) continue
        next[scope] = {
          ...previous, phase: 'storage-missing', inspectedMissing: false,
          releaseAllowed: false, releaseInspected: false,
          message: `Recovery record for ${previous.name} changed or disappeared before a terminal receipt. Its exact draft remains quarantined; no save will be sent.`,
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
      message: `${token.name} settled, but its recovery record could not be released. Inspect it before another save.`,
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
      const message = `${token.name} is durably prepared, not complete. Resume the same save identity.`
      updateRecovery(token, {
        phase: 'prepared', inspectedMissing: false,
        releaseAllowed: false, releaseInspected: false, message,
      })
      setDocumentError(token.scope, message)
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
      ? `${token.name} changed after this save was prepared. Draft retained; inspect the server copy before saving.`
      : `${token.name} changed before saving. Draft retained; nothing was written.`
    const released = releaseTerminalRecovery(token)
    setDocuments(current => {
      const exact = current[token.scope]
      const retained = exact || {
        kind: token.kind, name: token.name, savedText: null,
        draftText: token.submittedText, savedRevision: token.expectedRevision,
        savedTargetRootId: token.expectedTargetRootId,
        observedTargetRootId: token.expectedTargetRootId,
        truncated: false, readOnly: false, rootConflict: false,
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
      message: `Submitting exact save for ${token.name}…`,
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
      const message = `${token.name} save outcome is unconfirmed. Its exact operation and draft remain in this tab; check the receipt before retrying.`
      updateRecovery(token, {
        phase: 'unknown', inspectedMissing: false,
        // A failed client request cannot prove that the exact PUT is no longer waiting on the
        // server-side lock or fsync. Keep it quarantined until a validated terminal receipt exists.
        releaseAllowed: false, releaseInspected: false, message,
      })
      setDocumentError(token.scope, message)
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
    if (!document || document.readOnly || saveRef.current
        || selectedUncertainSave || selectedDamagedRecovery) return
    if (document.draftText === document.savedText) return
    if (!selectedSourceReconciled) {
      onToast?.(`Load and reconcile current ${kind} before saving ${document.name}.`)
      return
    }
    if (document.rootConflict || !validAuthoringTargetRootId(document.observedTargetRootId)) {
      const message = `${document.name} belongs to another or unavailable Authoring directory. Draft not written; refresh the directory.`
      setDocumentError(selectedScope, message)
      onToast?.(message)
      return
    }
    if (document.truncated || !AUTHORING_REVISION_RE.test(document.observedRevision || '')) {
      const message = `${document.name} lacks a complete writable revision. Refresh, or edit outside this truncated view.`
      setDocumentError(selectedScope, message)
      onToast?.(message)
      return
    }
    if (document.conflict && !window.confirm(
      `${document.name} changed on the server. Overwrite its newer copy with this retained draft?`,
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
        const message = `${document.name} changed while preparing the save identity. Review draft and server copy; no request was sent.`
        setDocumentError(selectedScope, message)
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
        const message = `No save sent: recovery storage could not retain ${document.name}'s exact operation.`
        setDocumentError(selectedScope, message)
        refreshRecoveryStore()
        onToast?.(message)
        return
      }
      const recovery = {
        ...stored, phase: 'submitting', inspectedMissing: false,
        releaseAllowed: false, releaseInspected: false,
        message: `Submitting exact save for ${stored.name}…`,
      }
      setUncertainSaves(current => ({ ...current, [stored.scope]: recovery }))
      await submitAuthoringSave(recovery)
    } catch (error) {
      const message = `Could not prepare ${document.name} for saving: ${error?.message || error}`
      setDocumentError(selectedScope, message)
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
          ? `No durable receipt yet for ${recovery.name}. Resume the same identity; it cannot overwrite a newer revision.`
          : `Could not check ${recovery.name}: ${error?.message || error}. Its exact draft and identity remain retained.`
        updateRecovery(recovery, {
          phase: missing ? 'missing' : 'unknown', inspectedMissing: missing,
          // Even a 404 can race a still-running timed-out PUT before its prepared receipt is
          // published. Reuse/check the same identity; never release it from an unknown response.
          releaseAllowed: false,
          releaseInspected: false, message,
        })
        setDocumentError(recovery.scope, message)
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
      `Resume exact save for ${recovery.name}?\n\nThe same identity, revision, and contents cannot overwrite a newer version.`,
    )) return
    submitAuthoringSave(verifiedRecovery)
  }
  const adoptObservedServerCopy = document => {
    if (!selectedSourceReconciled || !document?.conflict || document.truncated
        || document.observedText == null
        || !/^sha256:[0-9a-f]{64}$/.test(document.observedRevision || '') || saveRef.current) return
    if (!window.confirm(
      `Use current server copy of ${document.name}?\n\nThis discards its retained draft.`,
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
    onToast?.(`Using current server copy of ${document.name}.`)
  }
  const releaseAuthoringRecovery = recovery => {
    const exact = uncertainSaves[recovery?.scope]
    if (!exact || !exact.releaseAllowed || !exact.releaseInspected
        || exact.operationId !== recovery.operationId) return
    if (!window.confirm(
      `Release exact saved operation for ${recovery.name}?\n\nNo write is sent. Its draft remains until this panel closes.`,
    )) return
    if (!clearAuthoringOperationIntent(exact)) {
      updateRecovery(exact, {
        releaseAllowed: false, releaseInspected: false,
        message: 'Recovery record changed or could not be released; it remains protected.',
      })
      refreshRecoveryStore()
      onToast?.('Exact recovery record changed or could not be released.')
      return
    }
    removeRecovery(exact)
    setDocuments(current => current[exact.scope] ? {
      ...current,
      [exact.scope]: {
        ...current[exact.scope], recoveryOperationId: null,
        recoveryStorageRaw: null,
        error: 'Old operation recovery released. Review draft and server version before saving.',
      },
    } : current)
    onToast?.('Exact recovery identity released. No save was sent.')
  }
  const releaseDamagedRecovery = recovery => {
    if (!recovery?.inspected) return
    if (!window.confirm(
      'Release this unreadable recovery record?\n\nContinue only if no retained save still needs recovery.',
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
        onToast?.('Recovery record changed or could not be released; it remains protected.')
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
          error: 'Old recovery record released. Review draft and server version before saving.',
        },
      } : current)
    }
    if (!storedRecordReleased) refreshRecoveryStore()
    onToast?.(storedRecordReleased
      ? 'Damaged recovery record released. No save was sent.'
      : 'Missing recovery snapshot released. No stored record changed.')
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
      ? `${uncertainSaveCount} save outcome${uncertainSaveCount === 1 ? '' : 's'} may be unknown; exact draft${uncertainSaveCount === 1 ? '' : 's'} retained here.`
      : damagedRecoveryCount > 0
        ? `${damagedRecoveryCount} damaged recovery record${damagedRecoveryCount === 1 ? ' remains' : 's remain'} quarantined here.`
      : mutationBusy ? 'A save is in progress.'
        : `${dirtyCount} unsaved draft${dirtyCount === 1 ? '' : 's'} will be lost.`
    if (!window.confirm(`${warning} Close Authoring?`)) return
    allowNavigationRef.current = true
    draftStore.clear(AUTHORING_PANEL_DRAFT_SCOPE)
    onClose?.()
  }
  const dirtyByKind = documentKind => Object.values(documents)
    .filter(document => document.kind === documentKind && !document.readOnly
      && document.draftText !== document.savedText).length
  const fileRows = [...(data.files || [])]
  for (const recovery of Object.values(uncertainSaves)) {
    if (recovery.kind === kind && validAuthoringName(recovery.name)
        && !fileRows.some(file => file.name === recovery.name)) {
      fileRows.push({ name: recovery.name, text: recovery.submittedText,
        revision: null, truncated: false, readOnly: false, recovered: true })
    }
  }
  for (const document of Object.values(documents)) {
    if (document.kind === kind
        && (document.draftText !== document.savedText || document.recoveryOperationId
          || scopeFor(document.kind, document.name) === selectedScope)
        && !fileRows.some(file => file.name === document.name)) {
      fileRows.push({
        name: document.name, text: document.readOnly ? document.savedText : document.draftText,
        revision: document.observedRevision || null,
        truncated: document.truncated === true, readOnly: document.readOnly === true, recovered: true,
      })
    }
  }
  // Scope in the subtitle: this edits the prompts/skills/knowledge EVERY run shares (`/api/{kind}`),
  // not anything belonging to whichever run a legacy `?panel=authoring` link was opened from.
  return (
    <Panel title="Authoring — configure the scientist" sub="every run · hot-reloaded next run" onClose={requestClose} wide>
      <div className="toolbar" style={{ marginBottom: 10 }}>
        {['prompts', 'skills', 'knowledge'].map(k => <button key={k} className={'btn sm' + (k === kind ? ' primary' : '')}
          onClick={() => chooseKind(k)}>{k}{dirtyByKind(k) ? ` (${dirtyByKind(k)} unsaved)` : ''}</button>)}
        {sourceReady && <span className="muted">{data.dir || `no ${kind} dir configured (set ${AUTHORING_KIND_ENV[kind]}, or the ${kind === 'prompts' ? 'Prompt' : kind === 'skills' ? 'Skills' : 'Knowledge'} dir in Settings)`}</span>}
        {sourceReady && data.truncatedFiles > 0
          && <span className="muted">{data.truncatedFiles}+ files omitted (cap)</span>}
        {sourceReady && data.inventoryIncomplete
          && <span className="muted">Package scan capped; omissions possible</span>}
      </div>
      {/* The Authoring / Memory boundary is DIRECTION, not subject matter: both hold durable
          knowledge, this one is the half a human writes. Saying so on both panels is the whole
          answer to "what is the difference between authoring and memory". */}
      <div className="muted" style={{ fontSize: 11, marginBottom: 10, lineHeight: 1.5 }}>
        <b>You</b> write these Markdown files; agents read them during runs. Run-written lessons,
        cases and meta-notes live in <b>Memory</b>. <b>Prompts</b> and <b>skills</b> have no default
        directory—configure one in Settings.
      </div>
      <div className="muted" style={{ fontSize: 11, marginBottom: 10, lineHeight: 1.5 }}>
        {AUTHORING_KIND_PURPOSE[kind]?.what}{' '}{AUTHORING_KIND_PURPOSE[kind]?.disclosure}
      </div>
      <PanelResourceNotice resource={source} label={`${kind} files`} onRetry={retry} />
      {dirtyCount > 0 && <div className="notice" role="status" style={{ marginBottom: 10 }}>
        {dirtyCount} draft{dirtyCount === 1 ? '' : 's'} retained. Switching is safe; closing loses them.
      </div>}
      {selected && !selectedSourceReconciled && <div className="notice" role="status"
        style={{ marginBottom: 10 }}>
        Save and server-copy stay disabled until current {kind} reconciles with this draft.
      </div>}
      {!storageAvailable && <div className="report-inline-state error" role="alert" style={{ marginBottom: 10 }}>
        <OpIcon name="alert" size={14} /><span>Recovery storage unavailable; saves are disabled to preserve ambiguous-write identity.</span>
      </div>}
      {damagedRows.map(recovery => <div key={recovery.scope}
        className="report-inline-state error" role="alert" style={{ marginBottom: 10 }}>
        <OpIcon name="alert" size={14} />
        <span>{recovery.storageMissing
          ? `Unreadable recovery${recovery.identity ? ` for ${recovery.identity.name}` : ''} changed or disappeared in storage; its exact snapshot is quarantined here.`
          : recovery.reason || `Unreadable recovery exists${recovery.identity
          ? ` for ${recovery.identity.name}` : ''}; saves for that scope are locked.`}</span>
        {!recovery.inspected
          ? <button className="btn sm" onClick={() => setDamagedRecoveries(current => ({
            ...current, [recovery.scope]: { ...current[recovery.scope], inspected: true },
          }))}>Inspect recovery</button>
          : <>
            <span className="muted">Stored record {recovery.raw.length} bytes; {recovery.reason
              ? 'integrity check failed.' : 'operation identity unverified.'}</span>
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
            {f.readOnly ? ' • read-only package' : ''}
            {!documents[scopeFor(kind, f.name)]?.readOnly
              && documents[scopeFor(kind, f.name)]?.draftText !== documents[scopeFor(kind, f.name)]?.savedText
              ? ' • unsaved' : ''}{uncertainSaves[scopeFor(kind, f.name)] ? ' • recovery' : ''}</button>)}
          {sourceReady && fileRows.length === 0 && <div className="muted">no files</div>}
        </div>
        <div className="authoring-editor">
          {selected ? <>
            {/* `readOnly`, not `disabled`. Both refuse the edit identically, but `disabled` also
                takes the control out of the tab order and out of AT forms/browse interaction — and
                this textarea is the ONLY rendering of the file's contents. A keyboard-only or
                screen-reader operator could select "…/SKILL.md · read-only package" (that button is
                focusable) and then have no way to reach the text at all: Tab skips it, a package
                longer than the box cannot be scrolled, and nothing can be selected or copied. */}
            <textarea className="text"
              aria-label={`${selected.readOnly ? 'View' : 'Edit'} ${selected.name}`}
              value={selected.draftText} readOnly={selected.truncated || selected.readOnly}
              onChange={e => editSelected(e.target.value)} />
            {selected.truncated && <div className="report-inline-state error" role="alert">
              <OpIcon name="alert" size={14} /><span>File exceeds the editor limit; only a prefix is shown, so Save is disabled.</span>
            </div>}
            {selected.conflict && <div className="report-inline-state error" role="alert">
              <OpIcon name="alert" size={14} /><span>{selected.rootConflict
                ? 'Authoring directory changed. Draft remains bound to the old directory and cannot write to the new one.'
                : selected.observationIncomplete
                  ? 'File list is incomplete, so its revision is unverified. Save stays disabled until a complete refresh.'
                : 'Server copy changed. Draft retained.'}</span>
              {!selected.truncated && !selected.readOnly && selected.observedText != null
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
                || selected.readOnly
                || !selectedSourceReconciled
                || selected.rootConflict
                || !validAuthoringTargetRootId(selected.observedTargetRootId)
                || !AUTHORING_REVISION_RE.test(selected.observedRevision || '')
                || selected.draftText === selected.savedText}>
              {saveState && !saveState.reconcile ? `Saving ${saveState.name}…` : 'Save'}</button>
          </> : sourceReady && <div className="muted">select a file to edit</div>}
        </div>
      </div>
    </Panel>
  )
}

// What each memory tier is FOR, in the operator's terms: who WRITES it, WHEN, and what actually
// changes because the row exists. The four tabs are not four views of one store — they behave
// differently in the one way that matters (whether a future run's PROMPT contains them), and until
// now the whole explanation for all four was a single shared "cross-run memory reused to guide
// future runs" sentence. That sentence is true of exactly two of them.
// Verified against the code, not the docs: `engine/lessons_priors.py::_scan_prior_context` reads
// meta_notes.jsonl + lessons.jsonl AND, since 2026-08-19, cases.jsonl — under the same
// (task_id, direction) key and the same fail-closed LessonScope, rendered as one Researcher-only
// line. Until then a case was injected into nothing and this copy said so; the kind was fixed
// rather than deleted because the meta-note beside it carries the STORY and not the recipe
// (docs/guide/memory.md measures that on the one real row in the shared store).
const MEMORY_TAB_PURPOSE = {
  lessons: <><b>What generalizes.</b> One distilled claim per theme — a verdict
    (supported / tested / failed) plus the nodes it came from — written by the run's own reflection
    when the run ends. <b>This is the tier that changes the next run</b>: the best fingerprint
    matches are pasted straight into the next Researcher / Developer prompt.</>,
  cases: <><b>The winning run's exact configuration.</b> The parameter dict that produced the best
    metric on this task, the metric itself, and the rationale that proposed them. Upserted at run end
    and only when the metric BEATS the stored one, so there is exactly one active row per task and
    objective — a leaderboard, not a history.{' '}
    <b>Pasted into the next Researcher prompt for the SAME task</b>, one line beside the note below —
    the note says why it won, this says what to set. Also readable through <code>kb_search</code>,
    where cases share one top-3 index with the knowledge notes.</>,
  notes: <><b>One line per finished run</b> — what won, and (when the model was reachable) why it may
    have won. Injected verbatim into the next run of the <b>same</b> task, last 3. This is the CAUSAL
    twin of a case: same win, written as the reason instead of as the recipe — and on a real run it
    names one hyperparameter where the case carries all of them.</>,
  knowledge: <><b>Free-form Markdown</b> — the only tier a human writes directly. Edit it under
    Lab → Authoring → knowledge; the agents also write here through their own <code>remember</code>{' '}
    tool. Read back through <code>kb_search</code>, alongside cases.</>,
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

// The concept chips on one memory card. `source` is rendered, not just carried: a `run`-attributed row
// says "the run that produced this touched these concepts", which is a materially weaker claim than a
// row tagged from its own evidence, and an operator deciding what to trust needs to see which they have.
function ConceptChips({ row, selected, onSelect }) {
  const concepts = rowConcepts(row)
  if (!concepts.length) return null
  const source = rowSource(row)
  const inherited = source === SOURCE_RUN
  return (
    <>{concepts.map(id => (
      <button key={id} type="button"
        className={'chip xs concept-chip' + (id === selected ? ' on' : '') + (inherited ? ' inherited' : '')}
        aria-pressed={id === selected}
        title={inherited
          ? `${id} — inherited from this row's run, not from its own evidence. Click to filter.`
          : `${id} — recorded with this entry. Click to filter.`}
        onClick={() => onSelect(id === selected ? '' : id)}>{id}{inherited ? ' ·' : ''}</button>
    ))}</>
  )
}

// The shared concept axis for every tier: the picker, the display-mode toggle, and the coverage receipt
// that has to sit beside them. Rendered even when coverage is zero — that is precisely when an operator
// most needs to be told the filter cannot help yet, rather than being handed an empty result.
function ConceptShelfBar({ tierLabel, receipt, concepts, selected, onSelect, mode, onMode, runsIndexed,
                           runsTagged }) {
  const summary = coverageSummary(receipt)
  return (
    <div className="concept-shelf-bar" style={{ marginBottom: 8 }}>
      <div className="conv-toggle" style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
        <label className="muted" style={{ fontSize: 11 }} htmlFor="mem-concept-pick">Concept</label>
        <select id="mem-concept-pick" className="sel sm" value={selected}
          onChange={event => onSelect(event.target.value)}>
          <option value="">All concepts</option>
          {concepts.map(({ id, depth, subtree }) => (
            <option key={id} value={id}>{' '.repeat(depth * 2)}{id.split('/').pop()} ({subtree})</option>
          ))}
          {/* `—`, never a bare 0: an absent or partial coverage receipt does not know how many rows
              are untagged, and "Untagged (0)" is a claim that everything is classified. */}
          <option value={UNTAGGED}>
            Untagged ({summary && summary.untagged !== null ? summary.untagged : '—'})</option>
        </select>
        <span className="spacer" style={{ width: 8 }} />
        {[['flat', 'List'], ['tree', 'Concept tree']].map(([key, label]) => (
          <button key={key} type="button" aria-pressed={mode === key}
            className={'seg' + (mode === key ? ' on' : '')} onClick={() => onMode(key)}>{label}</button>
        ))}
      </div>
      {summary && <div className="muted" style={{ fontSize: 11, marginTop: 6, lineHeight: 1.5 }}>
        {/* The non-negotiable disclosure. Sorting or filtering by an axis that only covers part of the
            data is legitimate; doing it without saying how much it covers is not. */}
        <b>{summary.tagged === null ? '—' : summary.tagged} of {summary.total}</b> {tierLabel.toLowerCase()} carry concepts
        {summary.untagged > 0 && <> · <b>{summary.untagged} untagged</b>{mode === 'tree'
          ? ' (shown under Untagged)' : ' (hidden by any concept filter)'}</>}
        {summary.coarse && <> · all attribution is run-level, so it is only as precise as a whole run</>}
        {runsIndexed > 0 && summary.untagged > 0
          && <> · {runsTagged} of {runsIndexed} runs are concept-tagged</>}
      </div>}
    </div>
  )
}

// One heading in the concept-tree display mode. Indented by depth so the id's path reads as the tree it
// is, and the Untagged group is visually marked rather than dressed up as a concept — it is the honest
// residue of the axis, and an operator scanning for what to tag next should be able to find it fast.
function ConceptGroup({ group, children }) {
  const empty = group.rows.length === 0
  return (
    <section className={'concept-group' + (group.untagged ? ' untagged' : '')}
      style={{ marginLeft: group.untagged ? 0 : group.depth * 14, marginBottom: 10 }}>
      <h4 style={{ margin: '8px 0 4px', fontSize: 12, display: 'flex', gap: 6, alignItems: 'baseline' }}>
        {group.untagged
          ? <span className="muted"><b>Untagged</b> — carries no concept</span>
          : <><span className="muted">{group.depth > 0 ? group.id.split('/').slice(0, -1).join('/') + '/' : ''}</span>
              <b>{group.id.split('/').pop()}</b></>}
        <span className="grp-n">{group.rows.length}</span>
      </h4>
      {empty
        ? <div className="muted" style={{ fontSize: 11 }}>Nothing here — every entry in this tier is attributed.</div>
        : children}
    </section>
  )
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
  // The concept AXIS: one selection and one display mode shared by all three memory tiers, because the
  // operator's question ("what has this lab learned about X") crosses tiers — resetting the concept when
  // they switch from Lessons to Notes would break exactly the navigation this axis exists for.
  const [concept, setConcept] = useState('')
  const [conceptMode, setConceptMode] = useState('flat')
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
  // ---------------------------------------------------------------- concept axis
  const conceptIndex = mem.conceptIndex
  const conceptTiers = { cases: mem.cases || [], lessons: mem.lessons || [], notes: mem.notes || [] }
  const shelfOptions = useMemo(() => shelfConcepts(conceptTiers),
    [mem.cases, mem.lessons, mem.notes])
  const conceptOn = tab !== 'knowledge' && !!conceptIndex
  // Applied AFTER the role split so the two filters compose the way the operator reads them ("developer
  // lessons about retrieval"), and reported separately so a zero result can say WHICH filter emptied it.
  const applyConcept = rows => (conceptOn && concept ? filterRows(rows, concept) : { shown: rows, hidden: 0, hiddenUntagged: 0 })
  const conceptBar = tierKey => conceptOn && <ConceptShelfBar
    tierLabel={tierKey === 'lessons' ? 'Lessons' : tierKey === 'cases' ? 'Cases' : 'Meta-notes'}
    receipt={conceptIndex.coverage?.[tierKey]} concepts={shelfOptions} selected={concept}
    onSelect={setConcept} mode={conceptMode} onMode={setConceptMode}
    runsIndexed={conceptIndex.runsIndexed} runsTagged={conceptIndex.runsTagged} />
  // In tree mode a filter still applies (drill into a subtree, then read it grouped), and the Untagged
  // group is always rendered so nothing the axis cannot classify goes missing from the view.
  const conceptGroups = rows => groupByConceptTree(applyConcept(rows).shown)
  const filterNotice = (result, noun) => result.hidden > 0 && <div className="muted"
    style={{ fontSize: 11, marginBottom: 8 }} role="status">
    Concept filter <b>{concept === UNTAGGED ? 'Untagged' : concept}</b> hid {result.hidden} {noun}
    {result.hiddenUntagged > 0 && <> — {result.hiddenUntagged} of them because they carry no concept at all</>}.
    {' '}<button type="button" className="btn xs" onClick={() => setConcept('')}>Clear concept</button>
  </div>
  return (
    <Panel title="Memory & knowledge — what the runs have learned" sub={memory.data ? (mem.dir || 'no memory dir') : ''} onClose={onClose} wide>
      <div className="conv-toggle memory-tabs" style={{ marginBottom: 12 }}>
        {tabs.map(([k, label, n, incomplete]) => <button key={k} aria-pressed={tab === k} className={'seg' + (tab === k ? ' on' : '')}
          onClick={() => setTab(k)}>{label} <span className="muted">{(k === 'knowledge' ? knowledge : memory).data
            ? `${n}${incomplete ? ' shown' : ''}` : '…'}</span></button>)}
      </div>
      {/* WHAT THIS TAB IS. Four tabs with counts and nothing else left the operator asking what
          Cases were even for. The tiers have different writers and different readers; say so. */}
      <p className="muted memory-tier-blurb">{memoryTierBlurb(tab)}</p>
      <PanelResourceNotice resource={selectedResource} label={tab === 'knowledge' ? 'Knowledge notes' : 'Cross-run memory'}
        onRetry={tab === 'knowledge' ? retryKnowledge : retryMemory} />
      {tab !== 'knowledge' && memory.status === 'ready' && selectedReceiptIncomplete
        && <MemoryCompletenessNotice resource={memory} onRetry={retryMemory} error={selectedReceipt.unavailable}>
          <b>{selectedTierLabel} data is incomplete.</b>{' '}{selectedReceiptIssues.join(' ')}
        </MemoryCompletenessNotice>}
      {tab === 'knowledge' && knowledge.status === 'ready' && knowledgeIncomplete
        && <MemoryCompletenessNotice resource={knowledge} onRetry={retryKnowledge}>
          <b>Knowledge notes are incomplete.</b>{' '}
          {kb.truncatedFiles > 0 && `${kb.truncatedFiles} Markdown ${kb.truncatedFiles === 1 ? 'entry was' : 'entries were'} not returned. `}
          {truncatedKnowledgePreviews > 0 && `${truncatedKnowledgePreviews} loaded ${truncatedKnowledgePreviews === 1 ? 'preview contains' : 'previews contain'} only a prefix.`}
        </MemoryCompletenessNotice>}
      {/* Orientation shown on every tab. The three durable-knowledge surfaces are easy to mistake for
          each other, so each one says what the OTHER two are: this panel is what the RUNS wrote,
          Authoring is what YOU write, Claims & Curation is the portfolio roll-up over several runs. */}
      <div className="muted" style={{ fontSize: 11, marginBottom: 10, lineHeight: 1.5 }}>
        {tab === 'knowledge'
          ? <>Written by <b>operators or agents</b> through the Knowledge authoring tools; no run
              provenance is inferred for legacy notes. Edit these in Lab → <b>Authoring</b>.</>
          : <>Written <b>by runs</b> during cadence/finalization and read-only here. Claims rolled up
              across the portfolio are <b>Claims &amp; Curation</b> (Lab → Claims &amp; Curation);
              the cross-run concept map is Runs → <b>Concepts</b>.</>}
      </div>
      {/* Per-tab purpose: the four tiers differ in who reads them back, which is the only difference
          an operator can act on. */}
      <div className="muted" style={{ fontSize: 11, marginBottom: 10, lineHeight: 1.5 }}>
        {MEMORY_TAB_PURPOSE[tab]}
      </div>
      {tab !== 'knowledge' && selectedReceipt?.windowDigest && <div className="muted"
        style={{ fontSize: 11, marginBottom: 10 }}>
        Snapshot <code>{selectedReceipt.windowDigest.slice(0, 16)}</code>
        {selectedReceipt.sourceRows != null && <> · {selectedReceipt.sourceRows} source rows</>}
        {selectedReceipt.sourceSize != null && <> · {selectedReceipt.sourceSize} bytes total</>}
      </div>}
      {tab === 'lessons' && <div className="muted" style={{ fontSize: 11, marginBottom: 10, lineHeight: 1.5 }}>
        Split by role (§role-split): the <b>Researcher</b> gets R&D / “what technique to try” lessons;
        the <b>Developer</b> gets only its own “what code change fixed a crash” lessons
        (untagged/legacy lessons are shared). Cases, notes and the knowledge base are not split.
      </div>}
      {tab === 'lessons' && <div className="conv-toggle memory-role-tabs" style={{ marginBottom: 8 }}>
        {[['all', 'All'], ['researcher', 'Researcher'], ['developer', 'Developer']].map(([r, label]) =>
          <button key={r} aria-pressed={lessonRole === r} className={'seg' + (lessonRole === r ? ' on' : '')}
            onClick={() => setLessonRole(r)}>{label}</button>)}
      </div>}
      {tab === 'lessons' && conceptBar('lessons')}
      {tab === 'lessons' && (() => {
        // Researcher/Developer filters ALSO include untagged (shared) lessons — mirrors the backend
        // routing where an untagged lesson reaches both roles.
        const byRole = (mem.lessons || []).filter(l => lessonRole === 'all' || !l.role || l.role === lessonRole)
        const result = applyConcept(byRole)
        const card = (l, key) => <div key={key} className="mem-card">
          <div>{l.statement}</div>
          <div className="mem-meta" style={{ marginTop: 4, display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
            <span className="chip xs">{l.role || 'shared'}</span>
            {l.kind && <span className="chip xs">{l.kind}</span>}
            {l.outcome && <span className="chip xs">{l.outcome}</span>}
            {l.delta != null && <span className={'chip xs' + (l.delta > 0 ? ' ok' : '')}>Δ{fmt(l.delta)}</span>}
            {l.confidence != null && <span className="muted" style={{ fontSize: 11 }}>conf {Math.round(l.confidence * 100)}%</span>}
            {l.evidence_count ? <span className="muted" style={{ fontSize: 11 }}>· {l.evidence_count} evidence</span> : null}
            {l.evidence_traceable_count != null && <span className="muted" style={{ fontSize: 11 }}>
              · {l.evidence_traceable_count}/{l.evidence_count || 0} sources traceable</span>}
            {l.claim_stance && <span className="chip xs">stance {l.claim_stance}</span>}
            {l.task_id && <span className="muted" style={{ fontSize: 11 }}>· {l.task_id}</span>}
            {l.run_id && <span className="muted" style={{ fontSize: 11 }}>· run {l.run_id}</span>}
            <ConceptChips row={l} selected={concept} onSelect={setConcept} />
          </div>
        </div>
        return <>
          {conceptOn && filterNotice(result, result.hidden === 1 ? 'lesson' : 'lessons')}
          {conceptOn && conceptMode === 'tree' && result.shown.length
            ? conceptGroups(byRole).map(group => <ConceptGroup key={group.id} group={group}>
                {group.rows.map((l, i) => card(l, i))}
              </ConceptGroup>)
            : result.shown.length
              ? result.shown.map((l, i) => card(l, i))
              : memory.status === 'ready' && <div className="muted">{memoryEmptyCopy(mem.page?.tiers?.lessons,
                `${lessonRole === 'all' ? '' : lessonRole + ' '}lessons`,
                concept ? `No lessons are attributed to ${concept === UNTAGGED ? 'no concept' : concept}.`
                  : lessonRole !== 'all' && mem.lessons.length > 0
                    ? `No ${lessonRole} lessons match the loaded lessons.`
                    : `No ${lessonRole === 'all' ? '' : lessonRole + ' '}lessons yet — they accrue as runs finish (reflection distils them into memory).`)}</div>}
        </>
      })()}
      {tab === 'cases' && conceptBar('cases')}
      {tab === 'cases' && (() => {
        const result = applyConcept(mem.cases || [])
        const rows = list => list.map((c, i) => <tr key={i}><td>{c.task_id}<div className="muted">{c.direction || 'direction unknown'}</div></td><td className="muted">{c.goal}{c.rationale && <div>{c.rationale}</div>}</td><td>{fmt(c.metric)}</td><td className="muted">{JSON.stringify(c.params)}
          {c.params_truncated && <span title="Only a bounded parameter projection was returned"
            aria-label="parameters truncated"> · partial</span>}</td>
          <td className="muted">{c.run_id || 'legacy / unknown'}</td>
          <td><div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
            <ConceptChips row={c} selected={concept} onSelect={setConcept} /></div></td></tr>)
        const table = list => <DataTable caption="Stored memory cases" card={false}><table className="tbl">
          <thead><tr><th>task / objective</th><th>goal / rationale</th><th>metric</th><th>params</th><th>run</th><th>concepts</th></tr></thead>
          <tbody>{rows(list)}</tbody></table></DataTable>
        return <>
          {conceptOn && filterNotice(result, result.hidden === 1 ? 'case' : 'cases')}
          {conceptOn && conceptMode === 'tree' && result.shown.length
            ? conceptGroups(mem.cases || []).map(group => <ConceptGroup key={group.id} group={group}>
                {table(group.rows)}</ConceptGroup>)
            : result.shown.length ? table(result.shown)
              : memory.status === 'ready' && <div className="muted">{memoryEmptyCopy(mem.page?.tiers?.cases,
                'cases', concept ? `No cases are attributed to ${concept === UNTAGGED ? 'no concept' : concept}.`
                  : 'No cases stored.')}</div>}
        </>
      })()}
      {tab === 'notes' && conceptBar('notes')}
      {tab === 'notes' && (() => {
        const result = applyConcept(mem.notes || [])
        const card = (n, key) => <div key={key} className="mem-card">
          {(n.task_id || n.run_id || n.at) && <div className="muted" style={{ fontSize: 11, marginBottom: 2 }}>
            {[n.task_id, n.run_id && `run ${n.run_id}`, n.at].filter(Boolean).join(' · ')}</div>}
          <Markdown text={n.note || n.statement || JSON.stringify(n)} />
          <div className="mem-meta" style={{ marginTop: 4, display: 'flex', gap: 6, flexWrap: 'wrap' }}>
            <ConceptChips row={n} selected={concept} onSelect={setConcept} /></div>
        </div>
        return <>
          {conceptOn && filterNotice(result, result.hidden === 1 ? 'meta-note' : 'meta-notes')}
          {conceptOn && conceptMode === 'tree' && result.shown.length
            ? conceptGroups(mem.notes || []).map(group => <ConceptGroup key={group.id} group={group}>
                {group.rows.map((n, i) => card(n, i))}</ConceptGroup>)
            : result.shown.length ? result.shown.map((n, i) => card(n, i))
              : memory.status === 'ready' && <div className="muted">{memoryEmptyCopy(mem.page?.tiers?.notes,
                'meta-notes', concept ? `No meta-notes are attributed to ${concept === UNTAGGED ? 'no concept' : concept}.`
                  : 'No meta-notes yet.')}</div>}
        </>
      })()}
      {tab === 'knowledge' && (kbFiles.length
        ? <><div className="muted" style={{ fontSize: 11, marginBottom: 6 }}>{kb.dir} — agents save + retrieve these via kb_search</div>
          {kbFiles.map((n, i) => <KbNote key={i} note={n} />)}</>
        : knowledge.status === 'ready' && <div className="muted">{kb.dir == null
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
  // THE CHAMPION IS THE SELECTION CLAIM, and this row printed its number bare. Under
  // `metric_salvage: "select"` the champion is precisely the node whose metric can be salvaged —
  // that rung mints NO violation row, so nothing else on this panel could have said so. Same
  // vocabulary, same call, same sentence as the Metrics tab and the Pareto front.
  const champSource = champ ? objectiveMetricSource(champ) : null
  const champCaveated = objectiveSourceCaveated(champSource)
  return (
    <Panel title="Solution registry & cross-run" onClose={onClose} wide>
      <div className="section-h">Champion (this run)</div>
      {champ ? <div className="kv"><div className="k">node</div><div className="v">#{champ.id} {state.champion != null ? '(promoted)' : '(auto-best)'}</div>
        <div className="k">metric</div><div className="v">{fmt(champ.confirmed_mean ?? champ.metric)}
          {champCaveated && <span className="warn" title={objectiveSourceHelp(champSource)}>
            {' · '}{OBJECTIVE_SOURCE_LABEL[champSource.channel]}</span>}</div></div>
        : <div className="muted">no champion yet</div>}
      {champCaveated && <div className="muted">
        This run&rsquo;s champion was selected on a number marked{' '}
        <b>{OBJECTIVE_SOURCE_LABEL[champSource.channel]}</b>. {objectiveSourceHelp(champSource)}
      </div>}
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
      {resource.status === 'ready' && !runs.length && <div className="muted">No runs in the registry yet.</div>}
    </Panel>
  )
}

// Live GPU telemetry (nvidia-smi via /api/gpu). Polls while open so an operator can watch
// utilization / VRAM / power during a real training run without leaving the browser.
export function GpuPanel({ onClose }) {
  const [resource, retry] = usePanelResource(signal => get('/api/gpu', { signal }), gpuPayload, '', 2000)
  const data = resource.data
  return (
    <Panel title="GPU monitor" sub="this host · nvidia-smi · live" onClose={onClose} wide>
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
  // Rows are sorted with the unmeasurable ones last, so the first NUMERIC importance is the max.
  // `rows[0]?.imp` alone would be `null` for a run where nothing could be measured, and `|| 1` then
  // silently rescaled every gauge against a denominator nobody chose.
  const top = rows.find(row => typeof row.imp === 'number')?.imp || 1
  return (
    <Panel title="Hyperparameter importance" sub={`${nodes.length} evaluated`} onClose={onClose}>
      {rows.length
        ? <><div className="section-h">|correlation| of each param with the metric (run-wide)</div>
            <DataTable caption="Run-wide hyperparameter importance" card={false}><table className="tbl"><thead><tr><th>param</th><th>importance</th><th>r</th><th>n</th><th>relative importance</th></tr></thead><tbody>
              {/* `row.r >= 0` is TRUE for null, so an unmeasurable param used to sign its own
                  absence as "+—". A param nothing varied gets the honest cell instead: a gauge at
                  0% is a claim, and this row has no measurement to make one from. */}
              {rows.map(row => <tr key={row.k}>
                <td>{row.k}</td><td>{fmt(row.imp, 3)}</td>
                <td className="muted">{row.r == null ? '—' : `${row.r >= 0 ? '+' : ''}${fmt(row.r, 3)}`}</td>
                <td className="muted">{row.n}</td>
                <td style={{ width: 160 }}>{row.imp == null
                  ? <span className="muted" title={`every evaluated node used the same ${row.k}, so its correlation with the metric is undefined — vary it to measure one`}>not varied</span>
                  : <MetricGauge value={row.imp} max={top}
                      label={`${row.k} relative importance`}
                      valueText={`${fmt(row.imp / top * 100, 1)}%`} />}</td></tr>)}
            </tbody></table></DataTable>
            <div className="muted" style={{ marginTop: 8 }}>Sign of r shows direction: with a {state.direction === 'min' ? 'minimize' : 'maximize'} objective,
              a {state.direction === 'min' ? 'negative' : 'positive'} r means a larger value tends to help. Needs ≥3 evaluated nodes per param,
              and a param every node set to the same value has no correlation to measure — it reads <b>—</b>, never 0.</div></>
        : <div className="muted">Not enough numeric-param data yet — run more experiments (≥3 evaluated nodes that share a numeric param).</div>}
    </Panel>
  )
}

// Same-task IDs are an operational lookup key, not a ComparisonContract, and this panel used to stop
// there: a flat list under "Cross-run ranking unavailable … values below remain per-run observations".
// That refusal was RIGHT about what a task id proves and WRONG about what follows from it — an
// operator with 45 runs on this box was left comparing numbers by eye, which is not a safer answer,
// only an unstated one. The overlay it now draws is a ranking inside each COMPARABLE GROUP (one task
// id + one direction, i.e. exactly `runIndex.js::metricComparable` applied per partition instead of
// over the whole payload), with competition ranks so ties are ties, with an integrity-incomplete run
// holding no rank at all, and with the two things a run row can never prove printed beside every
// group. The decisions and the wording are in `crossRunRank.js`, which `node --test` drives against
// the real corpus; this half is choreography. The claim ledger still owns contract-bound comparison.
//
// The fold runs over the payload this panel ALREADY fetches — no second request, and the whole corpus
// is grouped once so the panel can also say how many comparable groups exist outside this task.
export function CrossRunPanel({ state, onClose }) {
  const [resource, retry] = usePanelResource(signal => get('/api/runs', { signal }), runsPayload)
  const runs = resource.data || []
  const task = typeof state.task_id === 'string' && state.task_id.trim() ? state.task_id : ''
  const index = useMemo(() => crossRunGroups(runs), [runs])
  const coverage = rankCoverage(index)
  // The run's OWN direction first: this panel opened from a workspace, and the group that run belongs
  // to is the one being asked about. A second group only appears when sibling runs of the same task
  // recorded the opposite objective — which the corpus really does contain (`dataset_task` is 4
  // minimize runs plus `mnist-experiments` maximizing), and which is precisely what may not be
  // ordered into one column.
  const own = group => (group.direction === state.direction ? 0 : 1)
  const groups = index.groups.filter(group => group.taskId === task)
    .sort((a, b) => own(a) - own(b) || b.size - a.size)
  const elsewhere = index.comparable.filter(group => group.taskId !== task).length
  const observations = groups.reduce((sum, group) => sum + group.size, 0)
  const omitted = groups.reduce((sum, group) => sum + group.omitted, 0)
  const rankCell = row => (row.rank == null
    ? <span className="muted" title="no rank: this run's event log stops being readable, so the value beside it is the best of a PREFIX">—</span>
    : <span title={row.tied ? `tied with ${row.tied} other run(s) at this value` : 'rank within this comparable group'}>
        #{row.rank}{row.tied ? ' (tie)' : ''}</span>)
  // The caveat rides in the OBJECTIVE cell, beside the number it qualifies, and not in the status
  // column: this table's whole job is to say which recorded value led, and a qualifier one column
  // away from the value is a qualifier an operator scanning a leaderboard does not read. The row
  // keeps its rank — see `crossRunRank.js::buildGroup` for why caveat and not unrank.
  const metricCell = row => <>{fmt(row.value)}
    {row.confirmed ? ' (confirmed mean)' : ''}
    {row.caveats.map(slug => <span className="pill warn" key={slug} style={{ marginLeft: 6 }}
      title={row.caveatNotice}>{bestMetricCaveatLabel(slug)}</span>)}
    {row.provisional && <span className="muted" title="this run has not finished; its best can still improve"> · best so far</span>}
  </>
  return (
    <Panel title="Same-task run comparison"
      sub={resource.data ? `${observations} metric observation${observations === 1 ? '' : 's'}` : ''}
      onClose={onClose} wide>
      <PanelResourceNotice resource={resource} label="Cross-run results" onRetry={retry} />
      {resource.data && <div className="panel-resource-toolbar">
        <span className="muted">task ID:</span><code>{task || 'not recorded'}</code>
        <span className="muted">{groups.length} comparable group{groups.length === 1 ? '' : 's'} · ranked within a group only</span>
      </div>}
      {resource.data && !task && <div className="notice resource-warning" role="status">
        <b>Same-task observations unavailable.</b>
        <span> This run has no recorded task ID, so no portfolio row can be bound to it. Rows with
          missing identities are never grouped.</span>
      </div>}
      {groups.map(group => {
        const claim = groupClaim(group)
        return <div key={group.key} className="notice resource-warning" role="status">
          <b>{group.taskId} · {group.direction === 'min' ? 'minimize' : 'maximize'} · {group.size} run{group.size === 1 ? '' : 's'}.</b>
          <span> {claim.claim}</span>
          <ul style={{ margin: '4px 0 0 16px', padding: 0 }}>
            {claim.refusals.map((line, i) => <li key={i} className="muted">{line}</li>)}
          </ul>
        </div>
      })}
      {groups.length
        ? <DataTable caption="Same-task per-run metric observations by comparable group" card={false}><table className="tbl">
            <thead><tr><th>rank</th><th>run</th><th>recorded objective</th><th>nodes</th><th>status</th></tr></thead>
            {groups.map(group => <tbody key={group.key}>
              {/* One region, one caption: `dataTableMigration.test.js` reads captions statically, so a
                  per-group caption cannot exist. The group boundary is a header ROW instead, which is
                  also what keeps two objectives from reading as one ordered list. */}
              <tr className="xr-group"><th colSpan={5} scope="colgroup">
                {group.taskId} · {group.direction === 'min' ? 'lower is better' : 'higher is better'}
                {' · '}{group.ranked} of {group.size} ranked
              </th></tr>
              {[...group.rows, ...group.unranked].map(row => <tr key={row.runId}>
                <td>{rankCell(row)}</td>
                <td><a href={`#/run/${encodeURIComponent(row.runId)}`}>{row.label}</a></td>
                <td>{metricCell(row)}</td>
                <td className="muted">{row.nodes}</td>
                <td className="muted">{row.phase || (row.provisional ? '—' : 'finished')}</td>
              </tr>)}
            </tbody>)}
          </table></DataTable>
        : resource.status === 'ready' && task
          && <div className="muted">No per-run metric observations for this task ID yet.</div>}
      {omitted > 0 && <div className="muted" style={{ marginTop: 8 }}>
        {omitted} additional observation{omitted === 1 ? '' : 's'} omitted by the client render limit.
      </div>}
      {/* Coverage, in the spirit of `conceptForest.js::forestCoverage`: what this screen ranked, and
          the far larger population it says nothing about. */}
      {coverage && <div className="muted" style={{ marginTop: 8, fontSize: 11 }}>
        This server holds {coverage.runs} run{coverage.runs === 1 ? '' : 's'}; {coverage.comparableRuns} of
        them sit in {coverage.comparableGroups} comparable group{coverage.comparableGroups === 1 ? '' : 's'}.
        {' '}{coverage.noMetric} recorded no metric and {coverage.singletonTasks} task/direction
        {' '}combination{coverage.singletonTasks === 1 ? ' is' : 's are'} the only run of their kind, so
        nothing on this box ranks them.
        {elsewhere > 0 && ` ${elsewhere} comparable group(s) belong to other task IDs and are deliberately not shown here — their objectives are unrelated to this run.`}
      </div>}
    </Panel>
  )
}

// The Card board (HypothesisBoard, the _CardKanban it renders, and the legacy _HypothesisFallback)
// moved to ./CardBoard.jsx (doc 25 UI-04); re-exported here so the hub stays the one module RunView
// lazily imports.
export { HypothesisBoard } from './CardBoard.jsx'

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
