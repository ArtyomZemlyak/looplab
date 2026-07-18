import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import { fmt, get, runApiPath } from './util.js'
import {
  abandonUnknownConceptLens, acquireConceptLensIntent, clearConceptLensIntent,
  createConceptLensResolutionKey, discoverConceptLensRecovery, peekConceptLensIntent,
  pollDiscoveredConceptLens, requestConceptLens, resolveOrphanedConceptLens,
  updateConceptLensIntent,
} from './conceptLensRecovery.js'
import { deadlineRequest } from './requestDeadline.js'
import { getRunAccess } from './runMode.js'
import {
  visibleConceptRows, conceptLeaf, deltaTone, fmtCell,
  CONCEPT_COLUMNS, DEFAULT_COLUMNS,
} from './conceptViewModel.js'
import { filterConceptTree, experimentRefMatches } from './conceptSearch.js'
import { Marked } from './Highlight.jsx'

const TIMEOUT_MS = 12_000
const LENS_PROMPT_MAX_CHARS = 800
const LENS_PROMPT_MAX_BYTES = 2_048
const record = value => value !== null && typeof value === 'object' && !Array.isArray(value)
const metric = value => value === null || (typeof value === 'number' && Number.isFinite(value))
const count = value => Number.isSafeInteger(value) && value >= 0
const counted = (value, singular, plural = `${singular}s`) =>
  `${value} ${value === 1 ? singular : plural}`
const sequence = value => Number.isSafeInteger(value) && value >= -1
const conceptId = value => typeof value === 'string' && value.length > 0
const derivedLensId = value => typeof value === 'string' && value.length <= 64
  && /^[a-z0-9][a-z0-9-]*$/.test(value)
export const validDerivedLensLabel = value => typeof value === 'string'
  && value.length >= 1 && value.length <= 60 && value === value.trim()
  && /\S/u.test(value) && !/[\p{C}]/u.test(value)
// # CODEX AGENT: Keep this contract explicit and aligned with
// concept_frame.TRUNCATION_CAP_REASONS.
// A reason merely ending in "_cap" is not necessarily monotone truncation:
// rename_hop_cap, for example, means taxonomy resolution could not be trusted.
const TRUNCATION_CAP_REASONS = new Set([
  'node_membership_cap', 'concepts_per_node_cap', 'membership_cap', 'tree_node_cap',
  'edge_cap', 'edge_endpoint_cap', 'experiment_ref_cap',
])
const generationId = value => value === null
  || (typeof value === 'string' && /^[0-9a-f]{64}$/.test(value))
const paidLensReadOnlyMessage = access => access?.mode === 'review'
  ? 'Paid lens actions are disabled in a review link; no provider request was sent.'
  : access?.mode === 'stale-link'
    ? 'This diagnostic link targets an earlier generation. Open the current generation before creating a paid lens; no provider request was sent.'
    : 'Paid lens actions are disabled in a historical snapshot; return to live first. No provider request was sent.'
const invalidPayload = () => { throw new TypeError('Invalid concept projection') }
const countRecord = value => record(value) && Object.values(value).every(item => count(item))
const validList = (value, test) => Array.isArray(value) && value.every(test)
const uniqueList = (value, test) => validList(value, test) && new Set(value).size === value.length
const fieldNames = names => names.split(' ')
const fields = (value, names, test) => fieldNames(names).every(name => test(value[name]))
const exactRecord = (value, names, test) => {
  const required = fieldNames(names)
  return record(value)
    && required.every(name => Object.hasOwn(value, name) && (!test || test(value[name])))
}
const same = (left, right) => JSON.stringify(left) === JSON.stringify(right)
const bool = value => typeof value === 'boolean'
const KINDS = ['path', 'edge']
const METRIC_FIELDS = 'best mean worst delta_best delta_mean'
const LIMIT_FIELDS = 'concepts_per_node edge_endpoints edges membership_nodes memberships tree_nodes'
const SOURCE_FIELDS = 'edges membership_nodes'
const INCLUDED_FIELDS = 'concepts edges experiment_refs membership_nodes memberships tree_nodes'

// HTTP 200 is transport success, not projection truth. Require the versioned frame,
// lifecycle identity, authority receipt, bounds receipt, and self-contained experiment references
// before an empty tree can become an authoritative "no concepts" state.
export function validateConceptPayload(value, expected = {}) {
  if (!record(value)) invalidPayload()
  const {
    status, run_id: runId, generation, requested_seq: requestedSeq, captured_seq: capturedSeq,
    max_seq: maxSeq, historical, lens, effective_lens: effectiveLens,
    requested_lens: requestedLens, lenses, edges_present: edgesPresent,
    lens_edges_present: lensEdgesPresent, touch, experiment_refs: experimentRefs, tree, metrics,
    authoritative, complete, authority, provenance, completeness,
    requested_lens_spec: spec, lens_contract: contract,
  } = value
  if (value.schema !== 1 || !['complete', 'partial'].includes(status)
      || typeof runId !== 'string' || !generationId(generation)
      || !(requestedSeq === null || sequence(requestedSeq))
      || !sequence(capturedSeq) || !sequence(maxSeq) || capturedSeq > maxSeq
      || !fields(value, 'historical edges_present lens_edges_present authoritative complete', bool)
      || historical !== (capturedSeq < maxSeq)
      || !conceptId(lens) || !conceptId(effectiveLens) || lens !== effectiveLens
      || !conceptId(requestedLens) || !Array.isArray(lenses) || !lenses.length
      || !fields(value, 'touch experiment_refs tree metrics authority provenance completeness '
        + 'requested_lens_spec lens_contract', record)
      || tree.lens !== lens || !validList(tree.roots, conceptId) || !record(tree.nodes)
      || !record(metrics.rows) || !metric(metrics.baseline)
      || !['min', 'max'].includes(metrics.direction)) invalidPayload()

  const nodes = tree.nodes
  const roots = tree.roots
  const metricRows = metrics.rows
  const unidentifiedHistoricalPrefix = requestedSeq !== null && generation === null
  if ((expected.runId != null && runId !== String(expected.runId))
      || (Object.hasOwn(expected, 'generation') && generation !== expected.generation
        && !unidentifiedHistoricalPrefix)
      || (Object.hasOwn(expected, 'requestedSeq') && requestedSeq !== expected.requestedSeq)
      || (requestedSeq !== null && capturedSeq > requestedSeq)) invalidPayload()

  const lensNames = new Set()
  const shippedByName = new Map()
  for (const item of lenses) {
    if (!record(item) || !conceptId(item.name) || typeof item.label !== 'string'
        || !uniqueList(item.rels, conceptId) || !item.rels.length || item.rels.length > 8
        || !KINDS.includes(item.kind) || lensNames.has(item.name)) invalidPayload()
    lensNames.add(item.name)
    shippedByName.set(item.name, item)
  }
  const derived = expected.derived === true
  if (!conceptId(spec.name) || spec.name !== requestedLens
      || !uniqueList(spec.rels, conceptId) || !spec.rels.length || spec.rels.length > 8
      || !KINDS.includes(spec.kind)
      || spec.registration !== (derived ? 'ephemeral-validated' : 'shipped')
      || (derived && Array.isArray(expected.rels) && !same(spec.rels, expected.rels))
      || contract.requested !== requestedLens || contract.effective !== effectiveLens
      || contract.registration !== spec.registration
      || contract.fallback !== (requestedLens === effectiveLens ? null : 'no_matching_edges')
      || !lensNames.has('is_a') || (!derived && !lensNames.has(requestedLens))
      || (!lensNames.has(effectiveLens) && effectiveLens !== requestedLens)
      || (lensEdgesPresent && (effectiveLens !== requestedLens || !edgesPresent))
      || (spec.kind === 'path' && (!same(spec.rels, ['is_a'])
        || lensEdgesPresent || effectiveLens !== requestedLens))
      || (spec.kind === 'edge'
        && lensEdgesPresent !== (effectiveLens === requestedLens))
      || (expected.requestedLens != null && requestedLens !== expected.requestedLens)
      || (expected.direction != null && metrics.direction !== expected.direction)) invalidPayload()
  const hierarchyLens = shippedByName.get('is_a')
  if (!hierarchyLens || hierarchyLens.kind !== 'path' || !same(hierarchyLens.rels, ['is_a'])
      || (derived && (!derivedLensId(spec.name) || lensNames.has(spec.name)
        || spec.rels.join(',').length > 192))) invalidPayload()
  if (!derived) {
    const shipped = shippedByName.get(requestedLens)
    if (!shipped || shipped.kind !== spec.kind || !same(shipped.rels, spec.rels)) invalidPayload()
  } else {
    const registeredRelations = new Set([...shippedByName.values()].flatMap(item => item.rels))
    if (spec.rels.some(relation => !registeredRelations.has(relation))) invalidPayload()
  }

  const hierarchyTree = spec.kind === 'path' || contract.fallback === 'no_matching_edges'
  const nodeIds = Object.keys(nodes)
  let treeReferences = roots.length
  if (nodeIds.length > 10_000 || treeReferences > nodeIds.length) invalidPayload()
  // # CODEX AGENT: One bounded walk proves ownership, topology, reachability and tagged receipts;
  // the reference budget prevents an invalid fan-out from allocating an unbounded pending stack.
  const seenNodes = new Set()
  const pending = roots.map(id => [id, null, 0])
  while (pending.length) {
    const [id, parent, depth] = pending.pop()
    const node = nodes[id]
    if (seenNodes.has(id) || !conceptId(id) || !Object.hasOwn(nodes, id) || !record(node)
        || node.parent !== parent || node.depth !== depth || depth > 256
        || !Array.isArray(node.children)
        || node.tagged !== Object.hasOwn(experimentRefs, id)) invalidPayload()
    seenNodes.add(id)
    if (hierarchyTree) {
      const parts = id.split('/')
      const pathParent = parts.length === 1 ? null : parts.slice(0, -1).join('/')
      if (parent !== pathParent || depth !== parts.length - 1
          || Object.hasOwn(node, 'cross_parents')) invalidPayload()
    } else if (!uniqueList(node.cross_parents, conceptId)
        || node.cross_parents.some(parentId => parentId === id || parentId === parent
        || !Object.hasOwn(nodes, parentId))) invalidPayload()
    for (const child of node.children) {
      treeReferences += 1
      if (treeReferences > nodeIds.length) invalidPayload()
      pending.push([child, id, depth + 1])
    }
  }
  if (seenNodes.size !== nodeIds.length) invalidPayload()

  let referenceCount = 0
  const provenanceCounts = Object.create(null)
  const lifecycleByNode = new Map()
  const lifecycleMemberships = new Map()
  for (const [id, refs] of Object.entries(experimentRefs)) {
    if (!conceptId(id) || !Object.hasOwn(nodes, id) || !Array.isArray(refs) || !refs.length
        || !Object.hasOwn(touch, id) || !count(touch[id]) || touch[id] !== refs.length
        || !Object.hasOwn(metricRows, id) || !record(metricRows[id])
        || !fields(metricRows[id], 'touched evaluated', count)
        || metricRows[id].evaluated > metricRows[id].touched
        || !(metricRows[id].first_touch === null || count(metricRows[id].first_touch))
        || !fields(metricRows[id], METRIC_FIELDS, metric)
        || metricRows[id].touched !== refs.length) invalidPayload()
    const lifecycle = new Set()
    let evaluated = 0
    for (const ref of refs) {
      if (!record(ref) || !fields(ref, 'node_id node_generation', count)
          || !metric(ref.metric) || ref.metric_kind !== 'robust_metric' || !conceptId(ref.status)
          || !(ref.feasible === null || typeof ref.feasible === 'boolean')
          || !bool(ref.is_best) || !conceptId(ref.membership_provenance)) invalidPayload()
      const key = `${ref.node_id}:${ref.node_generation}`
      if (lifecycle.has(key)) invalidPayload()
      lifecycle.add(key)
      const signature = JSON.stringify([ref.node_generation, ref.metric, ref.metric_kind, ref.status,
        ref.feasible, ref.is_best, ref.membership_provenance])
      const prior = lifecycleByNode.get(ref.node_id)
      if (prior && prior !== signature) {
        invalidPayload()
      }
      lifecycleByNode.set(ref.node_id, signature)
      lifecycleMemberships.set(ref.node_id, (lifecycleMemberships.get(ref.node_id) || 0) + 1)
      if (ref.metric !== null && ref.feasible !== false) evaluated += 1
      referenceCount += 1
      provenanceCounts[ref.membership_provenance]
        = (provenanceCounts[ref.membership_provenance] || 0) + 1
    }
    if (metricRows[id].evaluated !== evaluated) invalidPayload()
  }

  const { reasons, limits, source, included, source_integrity: sourceIntegrity } = completeness
  const integrityFields = sourceIntegrity?.complete
    ? 'complete generation_identified'
    : 'complete corrupt_line dropped_lines generation_identified'
  if (!fields(completeness, 'complete truncated', bool)
      || completeness.complete !== complete || !uniqueList(reasons, conceptId)
      || !same(reasons, [...reasons].sort())
      || completeness.truncated !== reasons.some(reason => TRUNCATION_CAP_REASONS.has(reason))
      || status !== (complete ? 'complete' : 'partial') || complete !== (reasons.length === 0)
      || !exactRecord(limits, LIMIT_FIELDS, count)
      || fieldNames(LIMIT_FIELDS).some(key => limits[key] <= 0)
      || !exactRecord(source, SOURCE_FIELDS, count)
      || !exactRecord(included, INCLUDED_FIELDS, count)
      || !exactRecord(sourceIntegrity, integrityFields)
      || !fields(sourceIntegrity, 'complete generation_identified', bool)
      || sourceIntegrity.generation_identified !== (generation !== null)
      || (!sourceIntegrity.complete
        && !fields(sourceIntegrity, 'corrupt_line dropped_lines', count))
      || included.tree_nodes !== nodeIds.length
      || included.membership_nodes !== lifecycleByNode.size
      || included.concepts !== Object.keys(experimentRefs).length
      || included.experiment_refs !== referenceCount || included.memberships !== referenceCount
      || edgesPresent !== (included.edges > 0)
      || fieldNames('membership_nodes memberships tree_nodes edges')
        .some(key => included[key] > limits[key])
      || fieldNames(SOURCE_FIELDS).some(key => source[key] < included[key])
      || [...lifecycleMemberships.values()].some(valueCount => valueCount > limits.concepts_per_node)
      || Object.keys(touch).length !== Object.keys(experimentRefs).length
      || Object.keys(metricRows).length !== Object.keys(experimentRefs).length) invalidPayload()

  if (!fields(authority, 'authoritative source_authoritative complete', bool)
      || authority.authoritative !== authoritative || authority.complete !== complete
      || authoritative !== (authority.source_authoritative && complete)
      || authority.source_authoritative !== (sourceIntegrity.complete
        && sourceIntegrity.generation_identified)
      || authority.scope !== 'captured_recoverable_event_prefix'
      || authority.semantic_claims_verified !== false
      || provenance.source !== 'events.jsonl' || provenance.projection !== 'event_log_fold'
      || provenance.membership_semantics !== 'recorded_claims'
      || !countRecord(provenance.membership_counts)
      || !same(Object.entries(provenance.membership_counts).sort(),
        Object.entries(provenanceCounts).sort())) invalidPayload()

  return value
}

// Paid derivation may use the same bounded cap-partial frame the operator can already inspect, but
// never a corruption/integrity partial. Keep this explicit allow-list aligned with the server gate;
// validateConceptPayload first proves the complete versioned frame and its consistency receipts.
export function validatePaidDerivedPayload(value, expected = {}) {
  const frame = validateConceptPayload(value, expected)
  const reasons = frame.completeness.reasons
  if (frame.completeness.source_integrity.complete !== true
      || (!frame.complete && !reasons.every(reason => TRUNCATION_CAP_REASONS.has(reason)))) {
    invalidPayload()
  }
  return frame
}

const entries = value => record(value) ? Object.keys(value).sort().map(key => [key, value[key]]) : []
const emptyLensForm = scope => ({ scope, prompt: '', busy: false, error: '' })

// Mirror all inputs used by projection + concept_metrics. Same-count retags, renames,
// lifecycle/status/provenance changes, champion changes, typed edges,
// feasibility and robust metrics refresh; an engine-liveness-only SSE tick does not.
export function conceptProjectionKey(state) {
  const nodes = entries(state?.nodes).map(([key, node]) => [
    key, node?.id, node?.attempt, node?.status, node?.metric, node?.confirmed_mean,
    node?.feasible, !!node?.idea,
  ])
  const edges = entries(state?.concept_edges).map(([key, edge]) => [
    key, edge?.src, edge?.rel, edge?.dst, edge?.confidence,
  ])
  return JSON.stringify([state?.direction || 'max', state?.best_node_id ?? null,
    entries(state?.node_concepts), entries(state?.node_concept_provenance),
    entries(state?.concept_consolidation), edges, nodes])
}

const initial = { scope: '', requestVersion: '', status: 'loading', data: null, timeout: false }

function StateCard({ tone, title, body, action, pending = false, stale = false,
  projectionLabel = 'Concept hierarchy' }) {
  return <section className={`cv-state-card ${tone}`} role={tone === 'error' || stale ? 'alert' : 'status'}
    aria-live={tone === 'error' || stale ? 'assertive' : 'polite'} aria-atomic="true">
    <span className="cv-state-mark" aria-hidden="true">{tone === 'loading' ? '' : tone === 'error' ? '!' : '◇'}</span>
    <span className="cv-state-eyebrow">Concept map</span><h2>{title}</h2><p>{body}</p>
    {tone === 'empty' && <div className="cv-empty-flow" aria-label="How the concept view is built">
      <span>Experiments</span><i aria-hidden="true">→</i><span>{projectionLabel}</span>
      <i aria-hidden="true">→</i><span>Outcome comparison</span>
    </div>}
    {stale && <p className="cv-state-warning">Refresh failed; this is the last loaded empty result.</p>}
    {action && <button type="button" className="btn primary" onClick={action} disabled={pending}>
      {pending ? 'Refreshing…' : tone === 'error' ? 'Retry' : 'Refresh concepts'}
    </button>}
  </section>
}

export default function ConceptView({ runId, generation, sequence: displayedSequence, state, onPickNode }) {
  const runKey = String(runId)
  const lensScope = JSON.stringify([runKey, generation ?? null])
  const paidLensScope = JSON.stringify([runKey, generation ?? null, displayedSequence ?? null])
  const hasExactGeneration = typeof generation === 'string' && generationId(generation)
  const [lensSelection, setLensSelection] = useState({ scope: lensScope, name: 'is_a' })
  const lens = lensSelection.scope === lensScope ? lensSelection.name : 'is_a'
  const setLens = name => setLensSelection({ scope: lensScope, name })
  const [resource, setResource] = useState(initial)
  const [retry, setRetry] = useState(0)
  const [expanded, setExpanded] = useState(() => new Set())
  const [evidenceExpanded, setEvidenceExpanded] = useState(() => new Set())
  const [columns, setColumns] = useState(DEFAULT_COLUMNS)
  const [query, setQuery] = useState('')                 // live concept/experiment tree filter
  // Derived lenses are view-state specs: the paid derivation happens once, then GET replays their
  // exact relation subset so ordinary semantic refreshes remain deterministic and read-only.
  const [derivedLenses, setDerivedLenses] = useState([])
  const [lensFormState, setLensFormState] = useState(() => emptyLensForm(lensScope))
  const [lensIntentState, setLensIntentState] = useState({
    scope: '', storageReady: null, intent: null,
  })
  const [lensRecoveryState, setLensRecoveryState] = useState({
    scope: '', status: 'idle', receipt: null, resolutionKey: null, error: '', notice: '',
  })
  const [lensRecoveryRetry, setLensRecoveryRetry] = useState(0)
  const [runAccess, setRunAccessState] = useState(() => getRunAccess(runKey))
  const lensCreates = useRef(new Map())
  const lensRecoveryRequest = useRef(null)
  const lensForm = lensFormState.scope === lensScope ? lensFormState : emptyLensForm(lensScope)
  const { prompt: lensPrompt, error: lensErr } = lensForm
  const currentIntentState = lensIntentState.scope === lensScope
    ? lensIntentState : { storageReady: null, intent: null }
  const savedLensIntent = currentIntentState.intent
  const lensStorageReady = currentIntentState.storageReady === true
  const currentRecovery = lensRecoveryState.scope === paidLensScope
    ? lensRecoveryState
    : { status: 'idle', receipt: null, resolutionKey: null, error: '', notice: '' }
  const intentMatchesScope = !!savedLensIntent && savedLensIntent.runId === runKey
    && savedLensIntent.generation === generation
  const recoveryBusy = ['checking', 'polling', 'resolving'].includes(currentRecovery.status)
  const lensBusy = lensForm.busy || lensCreates.current.has(paidLensScope) || recoveryBusy
  const lensReadOnly = !!runAccess.readOnly
  const setCurrentLensForm = update => setLensFormState(previous => {
    const currentForm = previous.scope === lensScope ? previous : emptyLensForm(lensScope)
    const next = typeof update === 'function' ? update(currentForm) : update
    return { ...next, scope: lensScope }
  })
  const currentPaidLensScope = useRef(paidLensScope)
  currentPaidLensScope.current = paidLensScope
  const request = useRef(null)
  const pendingRequest = useRef(null)
  const latestRequest = useRef(null)
  const launchLatest = useRef(null)
  const projectionKey = useMemo(() => conceptProjectionKey(state), [state])
  const requestedSeq = displayedSequence == null ? null : displayedSequence
  const runDerivedLenses = derivedLenses.filter(item => item.scope === lensScope)
  const activeDerived = runDerivedLenses.find(item => item.name === lens)
  const activeRels = activeDerived ? (activeDerived.rels || []).join(',') : ''
  const scope = JSON.stringify([runKey, generation ?? null, requestedSeq, lens])
  const transportScope = JSON.stringify([scope, activeRels])
  const requestVersion = JSON.stringify([transportScope, projectionKey])
  const expectedTransportScope = useRef(transportScope)
  expectedTransportScope.current = transportScope
  latestRequest.current = {
    transportScope, scope, requestVersion, runId, generation, requestedSeq, lens, activeRels,
    direction: ['min', 'max'].includes(state?.direction) ? state.direction : null,
    derived: !!activeDerived, rels: activeDerived?.rels ? [...activeDerived.rels] : undefined,
  }

  useEffect(() => {
    setRunAccessState(getRunAccess(runKey))
    const onAccess = event => {
      if (String(event.detail?.runId) === runKey) setRunAccessState(getRunAccess(runKey))
    }
    window.addEventListener('ll:run-access', onAccess)
    return () => window.removeEventListener('ll:run-access', onAccess)
  }, [runKey])

  // Projection ticks coalesce behind one bounded request. A semantic navigation boundary still
  // aborts immediately, but a busy live run cannot starve the concept view by cancelling every
  // request before it completes: the current flight settles, then the newest pending projection runs.
  launchLatest.current = () => {
    if (request.current) return
    const target = pendingRequest.current
    if (!target || expectedTransportScope.current !== target.transportScope) return
    pendingRequest.current = null
    const query = new URLSearchParams({ lens: target.lens })
    if (target.activeRels) query.set('rels', target.activeRels)
    if (target.requestedSeq !== null) query.set('seq', String(target.requestedSeq))
    const timed = deadlineRequest(signal => get(
      `${runApiPath(target.runId, '/concepts')}?${query}`, { signal, cache: 'no-store' }), TIMEOUT_MS)
    const owner = { ...timed, ...target }
    request.current = owner
    setResource(previous => previous.scope === target.scope && previous.data
      ? { ...previous, requestVersion: target.requestVersion, status: 'refreshing', timeout: false }
      : { scope: target.scope, requestVersion: target.requestVersion,
          status: 'loading', data: null, timeout: false })

    let done = false
    const finish = (ok, data = null) => {
      if (done) return
      done = true
      if (request.current !== owner) return
      request.current = null
      if (expectedTransportScope.current !== owner.transportScope) return
      const superseded = latestRequest.current?.requestVersion !== owner.requestVersion
      setResource(previous => {
        if (previous.scope !== owner.scope) return previous
        if (ok) return { scope: owner.scope, requestVersion: owner.requestVersion,
          status: superseded ? 'refreshing' : 'ready', data, timeout: false }
        if (superseded) return previous.data
          ? { ...previous, status: 'refreshing', timeout: false } : previous
        return previous.data
          ? { ...previous, status: 'stale', timeout: timed.timedOut() }
          : { scope: owner.scope, requestVersion: owner.requestVersion,
              status: 'error', data: null, timeout: timed.timedOut() }
      })
      launchLatest.current?.()
    }
    timed.promise.then(value => validateConceptPayload(value, {
      runId: target.runId,
      ...(target.generation === undefined ? {} : { generation: target.generation }),
      requestedSeq: target.requestedSeq,
      requestedLens: target.lens,
      direction: target.direction,
      derived: target.derived,
      rels: target.rels,
    })).then(data => finish(true, data), () => finish(false))
  }

  useEffect(() => {
    pendingRequest.current = latestRequest.current
    const owner = request.current
    if (owner && owner.transportScope !== transportScope) {
      request.current = null
      owner.controller.abort()
    }
    launchLatest.current?.()
  }, [transportScope, requestVersion, retry])
  useEffect(() => () => {
    pendingRequest.current = null
    const owner = request.current
    request.current = null
    owner?.controller.abort()
  }, [])

  useEffect(() => {
    for (const [ownerScope, owner] of lensCreates.current) {
      if (ownerScope === paidLensScope) continue
      owner.controller?.abort()
      lensCreates.current.delete(ownerScope)
    }
    const recoveryOwner = lensRecoveryRequest.current
    if (recoveryOwner && recoveryOwner.scope !== paidLensScope) {
      lensRecoveryRequest.current = null
      recoveryOwner.controller?.abort()
    }
    let intent = null
    try {
      intent = peekConceptLensIntent(runId)
      setLensIntentState({ scope: lensScope, storageReady: true, intent })
      setLensRecoveryState({
        scope: paidLensScope,
        status: intent ? 'local'
          : displayedSequence != null || !hasExactGeneration ? 'inactive' : 'checking',
        receipt: null, resolutionKey: null, error: '', notice: '',
      })
      setCurrentLensForm(form => ({
        ...form, busy: false,
        prompt: intent?.generation === generation ? intent.prompt : '',
        error: intent && intent.generation !== generation
          ? 'A paid receipt belongs to an older generation. Archive it locally to work in this verified replacement; old provider outcome and billing remain unknown.'
          : '',
      }))
    } catch {
      setLensIntentState({ scope: lensScope, storageReady: false, intent: null })
      setLensRecoveryState({
        scope: paidLensScope,
        status: displayedSequence != null || !hasExactGeneration ? 'inactive' : 'checking',
        receipt: null, resolutionKey: null, error: '', notice: '',
      })
      setCurrentLensForm(form => ({ ...form, busy: false,
        error: 'This tab cannot save a new paid identity. Durable server recovery is still being checked.' }))
    }
  }, [paidLensScope, lensScope, runId, generation, displayedSequence, hasExactGeneration])
  useEffect(() => () => {
    for (const owner of lensCreates.current.values()) owner.controller?.abort()
    lensCreates.current.clear()
    lensRecoveryRequest.current?.controller?.abort()
    lensRecoveryRequest.current = null
  }, [])

  const current = resource.scope !== scope ? initial
    : resource.requestVersion === requestVersion ? resource
      : resource.data ? { ...resource, status: 'refreshing' } : initial
  const data = current.data
  useEffect(() => {
    if (data?.edges_present === false && lens !== 'is_a' && !activeDerived) {
      setExpanded(new Set())
      setEvidenceExpanded(new Set())
      setLens('is_a')
    }
  }, [data, lens, activeDerived])

  // experiment_refs is raw JSON (carries Object.prototype), and a concept id can be an untrusted prototype
  // key — the intermediate node of a "constructor/foo" tag is "constructor", so reading
  // byConcept["constructor"] on the raw object resolves UP the chain to Object.prototype.constructor (a
  // function); expanding that row then calls `.map` on the function and crashes the whole Concept view.
  // Rehydrate into a null-prototype map (own array values only) so every by-concept read is safe.
  const byConcept = useMemo(() => {
    const out = Object.create(null)
    const src = data?.experiment_refs
    if (src && typeof src === 'object') {
      for (const key of Object.keys(src)) if (Array.isArray(src[key])) out[key] = src[key]
    }
    return out
  }, [data])
  // Free-text filter: match concept ids/labels and (per-experiment) the frame's node id + status. The
  // matched rows keep every ancestor on their path and force it open, so a nested match stays reachable
  // in the same DFS visibleConceptRows already runs — search only widens `expanded` and trims the result.
  const edgeProjection = data?.requested_lens_spec?.kind === 'edge'
    && data?.lens_contract?.fallback !== 'no_matching_edges'
  const relationshipTypes = data?.requested_lens_spec?.rels?.join(', ') || ''
  const projectionLabel = edgeProjection ? 'Concept relationships' : 'Concept hierarchy'
  const projectionAriaLabel = edgeProjection
    ? `Concept relationship view for recorded ${relationshipTypes} links`
    : 'Concept hierarchy'
  const projectionDescription = data ? [
    'concept-metric-context',
    ...(edgeProjection ? ['concept-relationship-legend'] : []),
  ].join(' ') : undefined
  const metricOrientation = data?.metrics?.direction === 'min' ? 'minimize' : 'maximize'
  const metricContext = data && <div id="concept-metric-context"
    className="cv-resource-note metric-context" role="note">
    Primary objective metric · Unnamed metric · unit not recorded · {metricOrientation}.
    {' '}Δ columns are orientation-normalized; positive values mean better.
  </div>
  const relationshipLegend = edgeProjection && <div id="concept-relationship-legend"
    className="cv-resource-note relationship-legend" role="note">
    Relationship view · recorded {relationshipTypes} links. Indentation shows one primary display
    parent; “+N links” exposes additional recorded parents. This is not a taxonomy hierarchy.
  </div>
  const filter = useMemo(() => filterConceptTree(data?.tree, byConcept, query, { edgeProjection }),
    [data, byConcept, query, edgeProjection])
  const searching = query.trim().length > 0 && !!filter
  const effectiveExpanded = useMemo(
    () => searching ? new Set([...expanded, ...filter.expand]) : expanded,
    [searching, expanded, filter])
  const allRows = useMemo(
    () => visibleConceptRows(data?.tree, effectiveExpanded), [data, effectiveExpanded])
  const rows = searching ? allRows.filter(row => filter.visible.has(row.id)) : allRows
  const projectionStatus = useMemo(() => data
    ? visibleConceptRows(data.tree, new Set(Object.keys(data.tree.nodes))).projectionStatus
    : { state: 'unavailable', reasons: [] }, [data])
  const roots = data?.tree?.roots || []
  const empty = !!data && roots.length === 0
  const refreshing = current.status === 'refreshing'
  const refresh = () => setRetry(value => value + 1)
  const recoveryFrameReady = !!data

  useEffect(() => {
    if (!recoveryFrameReady || displayedSequence != null || !hasExactGeneration
        || currentIntentState.scope !== lensScope || savedLensIntent
        || currentRecovery.status !== 'checking') return undefined
    if (lensRecoveryRequest.current?.scope === paidLensScope) return undefined
    const controller = typeof AbortController === 'undefined' ? null : new AbortController()
    const owner = { scope: paidLensScope, controller }
    lensRecoveryRequest.current = owner
    const ownsScope = () => lensRecoveryRequest.current === owner
      && currentPaidLensScope.current === paidLensScope
    const setRecovery = update => {
      if (!ownsScope()) return
      setLensRecoveryState(previous => previous.scope === paidLensScope
        ? { ...previous, ...update } : previous)
    }
    Promise.resolve().then(async () => {
      let receipt = await discoverConceptLensRecovery(runId, generation, {
        signal: controller?.signal,
      })
      if (!ownsScope()) return
      if (receipt.state === 'running') {
        setRecovery({ status: 'polling', receipt, error: '', notice: '' })
        const result = await pollDiscoveredConceptLens(runId, receipt, {
          signal: controller?.signal,
        })
        if (!ownsScope()) return
        if (result?.ambiguous === true) {
          const refreshed = await discoverConceptLensRecovery(runId, generation, {
            signal: controller?.signal,
          })
          if (!ownsScope()) return
          // job_unknown/contact-loss may only cause another owner-plane read. If that read still
          // points at a process job, stop and let the operator retry instead of polling forever.
          if (refreshed.state === 'running' && refreshed.status === 'done') {
            // The process receipt is terminal but did not yield a trustworthy durable terminal.
            // The fresh owner-plane read still proves the exact unresolved claim, and the recovery
            // POST independently rechecks that no worker is running before it can append anything.
            // Drop only the volatile job fields so the operator can explicitly resolve this orphan;
            // never resubmit the paid prompt or its lost idempotency key.
            receipt = {
              schema: refreshed.schema, generation: refreshed.generation, state: 'orphaned',
              request_id: refreshed.request_id, started_seq: refreshed.started_seq,
              input_seq: refreshed.input_seq,
            }
          } else if (refreshed.state === 'running' || refreshed.state === 'none') {
            throw Object.assign(new Error('The discovered job is still not safely terminal.'), {
              code: result.code || 'concept_lens_recovery_pending',
            })
          } else receipt = refreshed
        } else {
          receipt = {
            schema: 1, generation: receipt.generation, state: 'terminal',
            request_id: receipt.request_id, started_seq: receipt.started_seq,
            input_seq: receipt.input_seq, terminal: result,
          }
        }
      }
      setRecovery({
        status: 'ready', receipt,
        resolutionKey: receipt.state === 'orphaned' ? currentRecovery.resolutionKey : null,
        error: '', notice: '',
      })
    }).catch(error => {
      if (!ownsScope() || controller?.signal?.aborted) return
      setRecovery({
        status: 'error',
        error: error?.code === 'run_generation_changed'
          ? 'The run changed while paid recovery was inspected. Reload Concepts.'
          : 'Durable paid-request recovery could not be verified. New paid work remains disabled.',
      })
    }).finally(() => {
      if (lensRecoveryRequest.current === owner) lensRecoveryRequest.current = null
    })
    return () => {
      if (lensRecoveryRequest.current === owner) lensRecoveryRequest.current = null
      controller?.abort()
    }
  }, [paidLensScope, recoveryFrameReady, currentIntentState.scope,
    currentIntentState.storageReady, savedLensIntent, lensRecoveryRetry])

  // A local receipt owns recovery while it exists. Once that exact receipt is terminalized or
  // explicitly archived, require a fresh owner-plane read before another paid identity is enabled.
  useEffect(() => {
    if (savedLensIntent || currentRecovery.status !== 'local'
        || displayedSequence != null || !hasExactGeneration) return
    setLensRecoveryState(previous => previous.scope === paidLensScope
      ? { ...previous, status: 'checking', receipt: null, error: '', notice: '' } : previous)
    setLensRecoveryRetry(value => value + 1)
  }, [paidLensScope, savedLensIntent, currentRecovery.status,
    displayedSequence, hasExactGeneration])

  // A durable terminal is safe to present only after both the recovery envelope and the complete
  // concept-frame contract have been validated. A valid terminal is historical truth, not a lock:
  // it may restore an ephemeral lens, but it never prevents a deliberate new paid request.
  useEffect(() => {
    const receipt = currentRecovery.receipt
    if (currentRecovery.status !== 'ready' || receipt?.state !== 'terminal') return
    const response = receipt.terminal
    try {
      const spec = response?.spec
      const derived = response?.ok === true && record(spec) && conceptId(spec.name)
        && validDerivedLensLabel(spec.label) && Array.isArray(spec.rels)
        && spec.rels.length && spec.rels.every(conceptId)
      if (derived) {
        validatePaidDerivedPayload(response, {
          runId, generation, requestedSeq: null, requestedLens: spec.name,
          direction: ['min', 'max'].includes(state?.direction) ? state.direction : null,
          derived: true, rels: spec.rels,
        })
        const recovered = { scope: lensScope, name: spec.name,
          label: spec.label || spec.name, rels: [...spec.rels] }
        setDerivedLenses(list => [...list.filter(item => item.scope !== lensScope
          || item.name !== recovered.name), recovered])
        setExpanded(new Set())
        setEvidenceExpanded(new Set())
        setLens(recovered.name)
      } else {
        const terminalReason = response?.code === 'concept_lens_abandoned'
          || ['declined', 'invalid_spec', 'no_model', 'accounting_pending',
            'concept_frame_partial'].includes(response?.reason)
          || (response?.code === 'job_capacity' && response?.reason === 'capacity')
        if (response?.ok !== false || !terminalReason) invalidPayload()
        validateConceptPayload(response, {
          runId, generation, requestedSeq: null, requestedLens: 'is_a',
          direction: ['min', 'max'].includes(state?.direction) ? state.direction : null,
          derived: false,
        })
      }
      const notice = derived
        ? 'Recovered a validated paid lens from the durable server terminal. No provider request was replayed.'
        : response.code === 'concept_lens_abandoned'
          ? 'The durable claim is abandoned. Provider completion, billing, and usage remain unknown; no provider retry was sent.'
          : response.reason === 'no_model'
            ? 'Recovered terminal: no model was configured, so no provider request was replayed.'
            : 'Recovered the durable terminal for the previous paid lens. No provider request was replayed.'
      setLensRecoveryState(previous => previous.scope === paidLensScope
        && previous.status === 'ready' && previous.receipt === receipt
        ? { ...previous, status: 'settled', notice, error: '' } : previous)
    } catch {
      setLensRecoveryState(previous => previous.scope === paidLensScope
        && previous.receipt === receipt
        ? { ...previous, status: 'error',
            error: 'The recovered terminal failed concept-frame validation. New paid work remains disabled.' }
        : previous)
    }
  }, [paidLensScope, currentRecovery.status, currentRecovery.receipt,
    runId, generation, lensScope, state?.direction])

  const toggle = id => setExpanded(previous => {
    const next = new Set(previous); next.has(id) ? next.delete(id) : next.add(id); return next
  })
  const toggleEvidence = id => setEvidenceExpanded(previous => {
    const next = new Set(previous); next.has(id) ? next.delete(id) : next.add(id); return next
  })
  const toggleColumn = key => setColumns(value => value.includes(key)
    ? value.length === 1 ? value : value.filter(item => item !== key) : [...value, key])
  const cols = CONCEPT_COLUMNS.filter(column => columns.includes(column.key))
  const serverRecoveryAllowsNew = (currentRecovery.status === 'ready'
      && currentRecovery.receipt?.state === 'none')
    || (currentRecovery.status === 'settled' && currentRecovery.receipt?.state === 'terminal')

  const createLens = async event => {
    event.preventDefault()
    const access = getRunAccess(runId)
    if (access.readOnly) {
      setCurrentLensForm(form => ({ ...form, error: paidLensReadOnlyMessage(access) }))
      return
    }
    const prompt = intentMatchesScope ? savedLensIntent.prompt : lensPrompt.trim()
    if (!prompt || displayedSequence != null || !hasExactGeneration
        || !lensStorageReady || lensCreates.current.has(paidLensScope)
        || (!savedLensIntent && !serverRecoveryAllowsNew)
        || (savedLensIntent && !intentMatchesScope)) return
    if (prompt.length > LENS_PROMPT_MAX_CHARS
        || new TextEncoder().encode(prompt).length > LENS_PROMPT_MAX_BYTES) {
      setCurrentLensForm(form => ({ ...form,
        error: 'Lens description is too long. Keep it within 800 characters and 2,048 bytes.' }))
      return
    }
    let intent
    try {
      intent = savedLensIntent || acquireConceptLensIntent(runId, generation, prompt)
    } catch (error) {
      setLensIntentState({ scope: lensScope,
        storageReady: error?.code === 'CONCEPT_LENS_INTENT_CONFLICT', intent: savedLensIntent })
      setCurrentLensForm(form => ({ ...form, error: error?.code === 'CONCEPT_LENS_INTENT_CONFLICT'
        ? 'Another saved paid lens must be reconciled before creating a new request.'
        : 'Paid lens creation needs working session storage; no paid request was sent.' }))
      return
    }
    const owner = { scope: paidLensScope, intent, resuming: !!savedLensIntent,
      controller: typeof AbortController === 'undefined' ? null : new AbortController() }
    lensCreates.current.set(paidLensScope, owner)
    try {
      if (intent.state === 'ready') {
        owner.intent = updateConceptLensIntent(runId, intent.idempotencyKey, {
          state: 'submitting',
        })
      }
    } catch {
      lensCreates.current.delete(paidLensScope)
      setLensIntentState({ scope: lensScope, storageReady: false, intent })
      setCurrentLensForm(form => ({ ...form, busy: false,
        error: 'Paid lens recovery could not be staged; no paid request was sent.' }))
      return
    }
    intent = owner.intent
    setLensIntentState({ scope: lensScope, storageReady: true, intent: owner.intent })
    setCurrentLensForm(form => ({ ...form, busy: true, error: '' }))
    try {
      const response = await requestConceptLens(runId, intent, {
        signal: owner.controller?.signal,
        onReceipt: receipt => {
          const updated = updateConceptLensIntent(runId, owner.intent.idempotencyKey, receipt)
          owner.intent = updated
          if (lensCreates.current.get(paidLensScope) === owner
              && currentPaidLensScope.current === paidLensScope) {
            setLensIntentState({ scope: lensScope, storageReady: true, intent: updated })
          }
        },
      })
      if (lensCreates.current.get(paidLensScope) !== owner
          || currentPaidLensScope.current !== paidLensScope) return
      if (response?.ambiguous === true) {
        try {
          const requestId = typeof response.request_id === 'string'
            && /^[0-9a-f]{64}$/.test(response.request_id) ? response.request_id : undefined
          const updated = updateConceptLensIntent(runId, owner.intent.idempotencyKey, {
            state: 'unknown', requestId,
          })
          owner.intent = updated
          setLensIntentState({ scope: lensScope, storageReady: true, intent: updated })
          setCurrentLensForm(form => ({ ...form,
            error: 'Outcome unknown. Resume checks this same saved request; do not create another paid lens.' }))
        } catch {
          setLensIntentState({ scope: lensScope, storageReady: false, intent: owner.intent })
          setCurrentLensForm(form => ({ ...form,
            error: 'Outcome unknown and its receipt could not be updated. Reload; do not create another paid lens.' }))
        }
        return
      }
      const spec = response?.spec
      if (response?.ok === true && record(spec) && conceptId(spec.name)
          && validDerivedLensLabel(spec.label) && Array.isArray(spec.rels)
          && spec.rels.length && spec.rels.every(conceptId)) {
        validatePaidDerivedPayload(response, {
          runId,
          ...(generation === undefined ? {} : { generation }),
          requestedSeq: null,
          requestedLens: spec.name,
          direction: ['min', 'max'].includes(state?.direction) ? state.direction : null,
          derived: true,
          rels: spec.rels,
        })
        let cleared = true
        try { cleared = clearConceptLensIntent(runId, owner.intent.idempotencyKey) }
        catch { cleared = false }
        const derived = { scope: lensScope, name: spec.name,
          label: spec.label || spec.name, rels: [...spec.rels] }
        setDerivedLenses(list => [...list.filter(item => item.scope !== lensScope
          || item.name !== derived.name), derived])
        setExpanded(new Set())
        setEvidenceExpanded(new Set())
        setLens(derived.name)
        setLensIntentState({ scope: lensScope, storageReady: cleared, intent: cleared ? null : owner.intent })
        setCurrentLensForm(form => ({ ...form, prompt: '', error: cleared ? ''
          : 'Lens created, but its saved identity could not be cleared. Reload before another paid request.' }))
      } else {
        const terminalReason = response?.code === 'concept_lens_abandoned'
          || ['declined', 'invalid_spec', 'no_model', 'accounting_pending',
          'concept_frame_partial'].includes(response?.reason)
          || (response?.code === 'job_capacity' && response?.reason === 'capacity')
        if (response?.ok !== false || !terminalReason) invalidPayload()
        validateConceptPayload(response, {
          runId, generation, requestedSeq: null, requestedLens: 'is_a',
          direction: ['min', 'max'].includes(state?.direction) ? state.direction : null,
          derived: false,
        })
        let cleared = true
        try { cleared = clearConceptLensIntent(runId, owner.intent.idempotencyKey) }
        catch { cleared = false }
        setLensIntentState({ scope: lensScope, storageReady: cleared, intent: cleared ? null : owner.intent })
        setCurrentLensForm(form => ({ ...form,
          error: !cleared
            ? 'The request ended, but its saved identity could not be cleared. Reload before another paid request.'
            : response.code === 'concept_lens_abandoned'
              ? response.reason === 'operator_recovered_abandon'
                ? 'Another recovery view durably abandoned this claim. Provider completion, billing, and usage remain unknown.'
                : 'This claim was durably abandoned. Provider completion, billing, and usage remain unknown.'
            : response.reason === 'no_model'
              ? 'No model is configured for lens creation.'
              : response.code === 'job_capacity'
                ? 'The paid-lens service is at capacity. No request was started; retry later.'
              : response.reason === 'accounting_pending'
                ? 'Durable cost accounting is pending. Recheck provider storage before another paid lens.'
              : response.reason === 'concept_frame_partial'
                ? 'The concept frame is incomplete; refresh it before creating a paid lens.'
                : 'The model declined that lens; name a concrete relation and try again intentionally.' }))
      }
    } catch (error) {
      if (lensCreates.current.get(paidLensScope) === owner
          && currentPaidLensScope.current === paidLensScope) {
        const localReadOnly = ['STALE_LINK_READ_ONLY', 'HISTORICAL_READ_ONLY',
          'REVIEW_READ_ONLY'].includes(error?.code)
        const safelyRejected = ['invalid_run_generation', 'run_generation_unavailable',
          'run_generation_changed', 'concept_lens_prompt_too_large', 'concept_lens_body_too_large',
          'concept_lens_in_progress', 'concept_lens_ledger_conflict', 'job_capacity'].includes(error?.code)
          || ([400, 401, 403, 404, 413, 422].includes(Number(error?.status))
            && error?.code !== 'idempotency_key_reused')
        if (localReadOnly) {
          let cleared = !owner.resuming
          if (!owner.resuming) {
            try { cleared = clearConceptLensIntent(runId, owner.intent.idempotencyKey) }
            catch { cleared = false }
          }
          setLensIntentState({ scope: lensScope, storageReady: owner.resuming || cleared,
            intent: owner.resuming || !cleared ? owner.intent : null })
          setCurrentLensForm(form => ({ ...form,
            error: !owner.resuming && !cleared
              ? 'No provider request was sent, but its locally staged identity could not be cleared. Reload before acting.'
              : paidLensReadOnlyMessage(getRunAccess(runId)) }))
        } else if (safelyRejected && !owner.resuming) {
          let cleared = true
          try { cleared = clearConceptLensIntent(runId, owner.intent.idempotencyKey) }
          catch { cleared = false }
          setLensIntentState({ scope: lensScope, storageReady: cleared,
            intent: cleared ? null : owner.intent })
          setCurrentLensForm(form => ({ ...form,
            error: !cleared
              ? 'The request was rejected, but its saved identity could not be cleared. Reload.'
              : error?.code === 'run_generation_changed'
                ? 'Run changed. Reload Concepts before creating another paid lens.'
                : error?.code === 'concept_lens_in_progress'
                  ? 'Another paid lens already owns this run generation. Wait, then reload Concepts.'
                  : error?.code === 'job_capacity'
                    ? 'The paid-lens service is at capacity. No request was started; retry later.'
                    : error?.code === 'concept_lens_ledger_conflict'
                      ? 'Another conflicting paid-lens ledger needs operator repair. No new request was started.'
                  : 'The paid request was rejected before model work began. Reload Concepts and review it.' }))
          if (cleared && ['invalid_run_generation', 'run_generation_unavailable',
            'run_generation_changed', 'concept_lens_in_progress',
            'concept_lens_ledger_conflict'].includes(error?.code)) {
            setLensRecoveryState(previous => previous.scope === paidLensScope
              ? { ...previous, status: 'checking', receipt: null, error: '', notice: '' }
              : previous)
            setLensRecoveryRetry(value => value + 1)
          }
        } else {
          try {
            const updated = updateConceptLensIntent(runId, owner.intent.idempotencyKey, {
              state: 'unknown',
            })
            owner.intent = updated
            setLensIntentState({ scope: lensScope, storageReady: true, intent: updated })
            setCurrentLensForm(form => ({ ...form,
              error: 'Outcome unknown; provider charges may have occurred. Resume this same saved request; do not create another key.' }))
          } catch {
            setLensIntentState({ scope: lensScope, storageReady: false, intent: owner.intent })
            setCurrentLensForm(form => ({ ...form,
              error: 'Outcome unknown and recovery storage failed. Reload; do not submit another paid lens.' }))
          }
        }
      }
    } finally {
      if (lensCreates.current.get(paidLensScope) === owner) {
        lensCreates.current.delete(paidLensScope)
        if (currentPaidLensScope.current === paidLensScope) {
          setCurrentLensForm(form => ({ ...form, busy: false }))
        }
      }
    }
  }
  const discardSavedLens = () => {
    if (lensBusy || !savedLensIntent) return
    setCurrentLensForm(form => ({ ...form,
      error: 'Saved request retained. Local discard cannot cancel or unlock server-side paid work; Resume it or ask an operator to abandon its durable claim.' }))
  }
  const archiveOldLens = () => {
    if (lensBusy || !savedLensIntent || !hasExactGeneration
        || savedLensIntent.generation === generation) return
    try {
      if (!clearConceptLensIntent(runId, savedLensIntent.idempotencyKey)) throw new Error('changed')
      setLensIntentState({ scope: lensScope, storageReady: true, intent: null })
      setCurrentLensForm(form => ({ ...form, prompt: savedLensIntent.prompt,
        error: 'Old-generation receipt archived locally. Its provider outcome, billing, and old server claim remain unknown; the verified replacement generation is independent.' }))
    } catch {
      setLensIntentState({ scope: lensScope, storageReady: false, intent: savedLensIntent })
      setCurrentLensForm(form => ({ ...form,
        error: 'The old-generation receipt could not be archived because recovery storage is unavailable.' }))
    }
  }
  const abandonSavedLens = async () => {
    if (getRunAccess(runId).readOnly || lensBusy || !intentMatchesScope
        || savedLensIntent?.state !== 'unknown'
        || !/^[0-9a-f]{64}$/.test(savedLensIntent.requestId || '')) return
    const owner = { scope: paidLensScope, intent: savedLensIntent,
      controller: typeof AbortController === 'undefined' ? null : new AbortController() }
    lensCreates.current.set(paidLensScope, owner)
    setCurrentLensForm(form => ({ ...form, busy: true, error: '' }))
    try {
      const response = await abandonUnknownConceptLens(runId, savedLensIntent, {
        signal: owner.controller?.signal,
      })
      if (lensCreates.current.get(paidLensScope) !== owner
          || currentPaidLensScope.current !== paidLensScope) return
      if (response?.ambiguous === true) {
        setCurrentLensForm(form => ({ ...form,
          error: 'Abandonment is still uncertain. The saved request is retained; Resume it before any new paid work.' }))
        return
      }
      const spec = response?.spec
      const derived = response?.ok === true && record(spec) && conceptId(spec.name)
        && validDerivedLensLabel(spec.label) && Array.isArray(spec.rels)
        && spec.rels.length && spec.rels.every(conceptId)
      if (derived) {
        validatePaidDerivedPayload(response, {
          runId, generation, requestedSeq: null, requestedLens: spec.name,
          direction: ['min', 'max'].includes(state?.direction) ? state.direction : null,
          derived: true, rels: spec.rels,
        })
      } else {
        const terminal = response?.ok === false && (response.code === 'concept_lens_abandoned'
          || ['declined', 'invalid_spec', 'no_model', 'accounting_pending',
            'concept_frame_partial'].includes(response.reason))
        if (!terminal) invalidPayload()
        validateConceptPayload(response, {
          runId, generation, requestedSeq: null, requestedLens: 'is_a',
          direction: ['min', 'max'].includes(state?.direction) ? state.direction : null,
          derived: false,
        })
      }
      let cleared = true
      try { cleared = clearConceptLensIntent(runId, owner.intent.idempotencyKey) }
      catch { cleared = false }
      if (derived) {
        const next = { scope: lensScope, name: spec.name,
          label: spec.label || spec.name, rels: [...spec.rels] }
        setDerivedLenses(list => [...list.filter(item => item.scope !== lensScope
          || item.name !== next.name), next])
        setExpanded(new Set())
        setEvidenceExpanded(new Set())
        setLens(next.name)
      }
      setLensIntentState({ scope: lensScope, storageReady: cleared,
        intent: cleared ? null : owner.intent })
      setCurrentLensForm(form => ({ ...form,
        error: !cleared
          ? 'The server returned a terminal receipt, but its local identity could not be cleared. Reload before new paid work.'
          : response.code === 'concept_lens_abandoned'
            ? 'Unknown request abandoned on the server. Provider work may already have completed and been billed; usage can remain unavailable.'
            : derived
              ? 'The provider completed before abandonment; its validated lens was restored instead.'
              : 'The provider reached a terminal result before abandonment; no new request was created.'
      }))
    } catch (error) {
      if (lensCreates.current.get(paidLensScope) !== owner
          || currentPaidLensScope.current !== paidLensScope) return
      setCurrentLensForm(form => ({ ...form, error: error?.code === 'concept_lens_still_running'
        ? 'The provider worker is still running. Resume its receipt; abandonment is not safe yet.'
        : 'Abandonment was not durably confirmed. The saved request is retained; Resume it before any new paid work.' }))
    } finally {
      if (lensCreates.current.get(paidLensScope) === owner) {
        lensCreates.current.delete(paidLensScope)
        if (currentPaidLensScope.current === paidLensScope) {
          setCurrentLensForm(form => ({ ...form, busy: false }))
        }
      }
    }
  }
  const retryServerRecovery = () => {
    if (lensBusy || savedLensIntent || displayedSequence != null || !hasExactGeneration) return
    lensRecoveryRequest.current?.controller?.abort()
    lensRecoveryRequest.current = null
    setLensRecoveryState(previous => previous.scope === paidLensScope
      ? { ...previous, status: 'checking', receipt: null, error: '', notice: '' } : previous)
    setLensRecoveryRetry(value => value + 1)
  }
  const resolveRecoveredLens = async () => {
    const recovery = currentRecovery.receipt
    if (getRunAccess(runId).readOnly || lensBusy || lensCreates.current.has(paidLensScope)
        || savedLensIntent || currentRecovery.status !== 'ready'
        || recovery?.state !== 'orphaned' || displayedSequence != null || !hasExactGeneration) return
    const resolutionKey = currentRecovery.resolutionKey || createConceptLensResolutionKey()
    const owner = { scope: paidLensScope,
      controller: typeof AbortController === 'undefined' ? null : new AbortController() }
    lensCreates.current.set(paidLensScope, owner)
    setLensRecoveryState(previous => previous.scope === paidLensScope
      ? { ...previous, status: 'resolving', resolutionKey, error: '', notice: '' } : previous)
    try {
      const response = await resolveOrphanedConceptLens(
        runId, recovery, resolutionKey, { signal: owner.controller?.signal })
      if (lensCreates.current.get(paidLensScope) !== owner
          || currentPaidLensScope.current !== paidLensScope) return
      if (response?.ambiguous === true) {
        setLensRecoveryState(previous => previous.scope === paidLensScope
          ? { ...previous, status: 'checking', receipt: null,
              error: 'Resolution outcome is uncertain; inspecting the durable ledger before any retry.' }
          : previous)
        setLensRecoveryRetry(value => value + 1)
        return
      }
      setLensRecoveryState(previous => previous.scope === paidLensScope ? {
        ...previous, status: 'ready', error: '', notice: '',
        receipt: {
          schema: 1, generation: recovery.generation, state: 'terminal',
          request_id: recovery.request_id, started_seq: recovery.started_seq,
          input_seq: recovery.input_seq, terminal: response,
        },
      } : previous)
    } catch (error) {
      if (lensCreates.current.get(paidLensScope) !== owner
          || currentPaidLensScope.current !== paidLensScope) return
      const shouldRecheck = error?.code === 'concept_lens_still_running'
        || error?.submissionMayHaveSucceeded === true
      setLensRecoveryState(previous => previous.scope === paidLensScope ? {
        ...previous, status: shouldRecheck ? 'checking' : 'error',
        receipt: shouldRecheck ? null : recovery,
        error: error?.code === 'concept_lens_still_running'
          ? 'The original worker is still running. Polling its existing job; no provider retry is being sent.'
          : 'Orphan resolution was not durably confirmed. New paid work remains disabled.',
      } : previous)
      if (shouldRecheck) setLensRecoveryRetry(value => value + 1)
    } finally {
      if (lensCreates.current.get(paidLensScope) === owner) {
        lensCreates.current.delete(paidLensScope)
      }
    }
  }
  const deleteLens = name => {
    setDerivedLenses(list => list.filter(item => item.scope !== lensScope || item.name !== name))
    if (lens === name) { setExpanded(new Set()); setEvidenceExpanded(new Set()); setLens('is_a') }
  }
  // Fence paid lens derivation by owner and run; disabled state alone cannot stop a
  // double submit or keep a late paid response out of a same-id replacement run.
  const serverRecoveryBlocksNew = !savedLensIntent && !serverRecoveryAllowsNew
  const lensUnavailable = displayedSequence != null || !hasExactGeneration
    || lensReadOnly || !lensStorageReady || serverRecoveryBlocksNew
    || (!!savedLensIntent && !intentMatchesScope)
  const canArchiveLens = !!savedLensIntent && hasExactGeneration && lensStorageReady
    && savedLensIntent.generation !== generation
  const canAbandonLens = intentMatchesScope && savedLensIntent?.state === 'unknown'
    && /^[0-9a-f]{64}$/.test(savedLensIntent.requestId || '')
    && displayedSequence == null && hasExactGeneration && lensStorageReady
  const lensStatus = currentIntentState.storageReady === null
    ? 'Checking paid-request recovery storage…'
    : lensReadOnly
      ? paidLensReadOnlyMessage(runAccess)
      : savedLensIntent && !intentMatchesScope
        ? 'This receipt belongs to an older generation. Archive is local only: it cannot resolve old provider work or billing, but that ledger cannot block this replacement generation.'
        : savedLensIntent?.state === 'unknown'
          ? canAbandonLens
            ? 'Outcome unknown. Resume checks the same request. Abandon resolves its durable claim, but cannot undo provider work or billing.'
            : 'Outcome unknown. Resume must first reconcile this same request before server-side abandonment is available.'
          : savedLensIntent?.state === 'running'
            ? 'Paid job saved. Resume polls its existing receipt and reconciles the same request if needed.'
            : savedLensIntent?.state === 'submitting'
              ? 'Paid request dispatch may have started. Resume sends only this same saved identity.'
            : savedLensIntent
              ? 'Paid request saved before dispatch. Resume sends only this same request identity.'
              : currentRecovery.status === 'checking'
                ? 'Checking the durable server ledger before enabling another paid identity…'
                : currentRecovery.status === 'polling'
                  ? 'A paid job survived this tab. Polling that exact job without a prompt or paid key; no provider request is being sent.'
                  : currentRecovery.status === 'resolving'
                    ? 'Resolving the exact orphaned claim. This cannot undo provider work or billing.'
                    : currentRecovery.receipt?.state === 'orphaned'
                      ? 'An orphaned paid claim blocks new work. Resolve it explicitly; provider completion and billing remain unknown.'
                      : currentRecovery.receipt?.state === 'conflict'
                        ? 'The durable paid ledger conflicts. Automatic recovery and new paid work are disabled until it is repaired.'
                        : currentRecovery.status === 'error'
                          ? currentRecovery.error
                          : !lensStorageReady
                            ? 'Server receipts are reconciled, but new paid work is disabled because this tab cannot save one request identity.'
                            : currentRecovery.notice
                              || 'Paid AI action: provider charges may apply. The server ledger is clear; a run-, generation-, and prompt-bound identity will be saved before dispatch.'
  const lensCreator = <form className="cv-lensnew" onSubmit={createLens}>
    <input className="text" value={lensPrompt} maxLength={LENS_PROMPT_MAX_CHARS}
      onChange={event => setCurrentLensForm(form => ({ ...form, prompt: event.target.value, error: '' }))}
      placeholder={displayedSequence != null ? 'live view required'
        : hasExactGeneration ? 'describe a grouping lens…' : 'verified generation required'}
      aria-label="Describe a lens to create" aria-describedby="paid-concept-lens-status"
      disabled={lensBusy || lensUnavailable || !!savedLensIntent} />
    <button type="submit" className="btn sm"
      aria-describedby="paid-concept-lens-status"
      title={!hasExactGeneration ? 'Reload the run and wait for its verified generation.' : undefined}
      disabled={lensBusy || lensUnavailable || (!savedLensIntent && !lensPrompt.trim())}>
      {lensBusy ? currentRecovery.status === 'polling' ? 'Polling existing paid job…'
        : currentRecovery.status === 'checking' ? 'Checking paid recovery…'
          : currentRecovery.status === 'resolving' ? 'Resolving orphaned claim…'
            : 'Reconciling paid lens…'
        : savedLensIntent ? 'Resume paid lens' : 'Create lens · paid'}</button>
    {savedLensIntent && (canArchiveLens
      ? <button type="button" className="btn sm danger cv-lensdiscard"
        disabled={lensBusy} onClick={archiveOldLens}>Archive old-generation receipt</button>
      : canAbandonLens ? <button type="button" className="btn sm danger cv-lensdiscard"
        disabled={lensBusy || lensReadOnly} onClick={abandonSavedLens}>Abandon unknown request</button>
      : <button type="button" className="btn sm ghost cv-lensdiscard"
        disabled={lensBusy} onClick={discardSavedLens}>Why no local discard?</button>)}
    {!savedLensIntent && currentRecovery.status === 'ready'
      && currentRecovery.receipt?.state === 'orphaned'
      && <button type="button" className="btn sm danger cv-lensdiscard"
        disabled={lensBusy || lensReadOnly} onClick={resolveRecoveredLens}>Resolve orphaned paid claim</button>}
    {!savedLensIntent && (currentRecovery.status === 'error'
      || currentRecovery.receipt?.state === 'conflict')
      && <button type="button" className="btn sm ghost cv-lensdiscard"
        disabled={lensBusy} onClick={retryServerRecovery}>Recheck paid recovery</button>}
    <span id="paid-concept-lens-status" className="muted" role="note">
      {lensStatus}</span>
    {lensErr && <span className="cv-lenserr" role="alert">{lensErr}</span>}
  </form>

  let stateCard
  if (current.status === 'loading') stateCard = { tone: 'loading',
    title: 'Building the concept view',
    body: 'Loading the latest hierarchy and outcome rollups for this run.' }
  else if (current.status === 'error') stateCard = { tone: 'error',
    title: 'Concepts are unavailable', action: refresh, body: current.timeout
      ? 'The concept projection did not respond in time. The run is unchanged; retry this read.'
      : 'The concept projection could not be read. The run is unchanged; retry when the server is reachable.' }
  else if (projectionStatus.state === 'unavailable') stateCard = { tone: 'error',
    title: 'Concepts are unavailable', action: refresh, pending: refreshing,
    body: 'The server returned no safe concept projection. The run is unchanged; retry this read.' }
  else if (empty && !data.authoritative) stateCard = { tone: 'error',
    title: 'Concept frame is incomplete', action: refresh, pending: refreshing,
    body: 'No safe concepts were included in this bounded frame. That is not evidence that the run has no concepts; retry for an authoritative frame.' }
  else if (empty) stateCard = { tone: 'empty', title: 'No concepts have been tagged yet',
    action: refresh, pending: refreshing, stale: current.status === 'stale',
    body: 'This view fills automatically after the Researcher assigns concepts to experiments. Until then, LoopLab keeps the canvas honest instead of inventing a taxonomy.' }
  const recoveryNeedsSurface = !!savedLensIntent
    || ['checking', 'polling', 'resolving', 'error', 'settled'].includes(currentRecovery.status)
    || ['orphaned', 'conflict'].includes(currentRecovery.receipt?.state)
  if (stateCard) return <div className="concept-view cv-state-layout" data-route-main tabIndex={-1}
    aria-label={projectionAriaLabel} aria-describedby={projectionDescription}>
    <StateCard {...stateCard} projectionLabel={projectionLabel} />
    {metricContext}{relationshipLegend}
    {data && recoveryNeedsSurface && (currentRecovery.status === 'settled' && !savedLensIntent
      ? <p className="cv-state-warning" role="status">{currentRecovery.notice}</p>
      : lensCreator)}</div>

  // Null-prototype rehydrate (same prototype-safety as byConcept): metrics.rows is raw JSON, and a
  // "constructor"/"__proto__" intermediate concept id would otherwise read an inherited Object.prototype
  // value at metricRows[id] instead of the correct "no row" undefined.
  const metricRows = Object.create(null)
  for (const key of Object.keys(data.metrics.rows)) metricRows[key] = data.metrics.rows[key]
  // Overlay the SUBTREE rollup ONLY on PURE parents (a concept with no direct experiment_refs, e.g. an
  // axis root never tagged itself) so their row shows metrics aggregated from descendants instead of a
  // blank "·". A directly-tagged concept keeps its own direct row so its metric and its (direct) evidence
  // stay consistent — the rollup's descendant experiments would otherwise not appear in its evidence list.
  const directRefs = data.experiment_refs || {}
  for (const key of Object.keys(data.metrics.rollup || {})) {
    if (!Object.hasOwn(directRefs, key)) metricRows[key] = data.metrics.rollup[key]
  }
  const experimentCount = new Set(Object.values(byConcept).flat()
    .map(ref => `${ref.node_id}:${ref.node_generation}`)).size
  const taggedConceptCount = Object.keys(byConcept).length
  const hierarchyNodeCount = Object.keys(data.tree.nodes).length
  const shippedLenses = data.edges_present ? data.lenses : data.lenses.filter(item => item.name === 'is_a')
  const derivedNames = new Set(runDerivedLenses.map(item => item.name))
  const availableLenses = [
    ...shippedLenses.filter(item => !derivedNames.has(item.name)),
    ...runDerivedLenses.map(item => ({ ...item, derived: true })),
  ]
  const linkKind = relationshipTypes
  return <div className="concept-view" data-route-main tabIndex={-1}
    aria-label={projectionAriaLabel} aria-describedby={projectionDescription}
    aria-busy={refreshing}>
    <header className="cv-bar">
      <div className="cv-heading"><strong>{projectionLabel}</strong><span>
        {counted(taggedConceptCount, 'tagged concept')} · {counted(hierarchyNodeCount,
          edgeProjection ? 'relationship node' : 'hierarchy node')} · {counted(experimentCount, 'tagged experiment')} · frame seq {data.captured_seq}
      </span></div>
      <div className="cv-search cs">
        <div className={'cs-box' + (searching ? ' focus' : '')}>
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" aria-hidden="true">
            <circle cx="11" cy="11" r="7" /><path d="M21 21l-4.3-4.3" /></svg>
          <input className="cs-input" style={{ width: 210 }} value={query} autoComplete="off"
            placeholder="filter concepts & experiments…" aria-label="Filter concepts and experiments"
            onChange={event => setQuery(event.target.value)}
            onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); setQuery('') } }} />
          {query &&
            <button type="button" className="cs-clear" aria-label="Clear filter" onClick={() => setQuery('')}>×</button>}
        </div>
      </div>
      <div className="cv-lensctl">
        <label className="cv-lenspick"><span>{edgeProjection ? 'Relationship lens' : 'Hierarchy lens'}</span><select className="text" value={lens}
          onChange={event => { setExpanded(new Set()); setEvidenceExpanded(new Set()); setLens(event.target.value) }}
          aria-label={edgeProjection ? 'Concept relationship lens' : 'Concept hierarchy lens'}>
          {availableLenses.map(item => <option key={item.name} value={item.name}>
            {(item.derived ? '✦ ' : '') + (item.label || item.name)}</option>)}
        </select></label>
        {activeDerived && <button type="button" className="cv-lensdel"
          title={`Delete lens “${activeDerived.label}”`} aria-label={`Delete lens ${activeDerived.label}`}
          onClick={() => deleteLens(activeDerived.name)}>×</button>}
        {lensCreator}
      </div>
      <div className="cv-tree-actions">
        <button type="button" className="btn sm ghost"
          onClick={() => setExpanded(new Set(Object.keys(data.tree.nodes)))}>Expand hierarchy</button>
        <button type="button" className="btn sm ghost" onClick={() => setExpanded(new Set())}>Collapse hierarchy</button>
        <button type="button" className="btn sm" onClick={refresh} disabled={refreshing}>{refreshing ? 'Refreshing…' : 'Refresh'}</button>
      </div>
      <div className="cv-cols" role="group" aria-label="Visible metric columns"><span>Metrics</span>
        {CONCEPT_COLUMNS.map(column => <button key={column.key} type="button" aria-pressed={columns.includes(column.key)}
          disabled={columns.includes(column.key) && columns.length === 1}
          className={'cv-col' + (columns.includes(column.key) ? ' on' : '')}
          onClick={() => toggleColumn(column.key)}>{column.label}</button>)}
        {data.metrics.baseline != null && <span className="cv-baseline" role="note"
          title="Median robust metric across eligible evaluated experiments (metric available and not explicitly infeasible); Δ columns are direction-normalized relative to this median."
          aria-label={`Run median robust metric ${fmt(data.metrics.baseline)} across eligible evaluated experiments: metric available and not explicitly infeasible. Delta columns are direction-normalized relative to this median.`}>
          run median {fmt(data.metrics.baseline)}</span>}
      </div>
    </header>
    {metricContext}{relationshipLegend}
    {refreshing && <div className="cv-resource-note" role="status" aria-live="polite"><span className="cv-inline-spinner" aria-hidden="true" />Refreshing concepts… Last loaded view remains visible.</div>}
    {current.status === 'stale' && <div className="cv-resource-note stale" role="alert"><span>Showing the last loaded concept view; refresh {current.timeout ? 'timed out' : 'failed'}.</span><button type="button" className="btn sm" onClick={refresh}>Retry</button></div>}
    {!data.authoritative && <div className="cv-resource-note partial" role="status">
      {data.authority.source_authoritative
        ? `This is a bounded partial frame${data.completeness.truncated ? '; configured limits omitted records' : ''}. The included projection is visible, but it is not a complete absence or coverage claim.`
        : 'This frame comes from a non-authoritative recoverable event prefix. Treat every included membership as provisional.'}
    </div>}
    {data.lens_contract.fallback === 'no_matching_edges' && <div className="cv-resource-note" role="status">
      No matching edges were recorded for the requested {data.requested_lens} lens; showing the is-a hierarchy instead.
    </div>}
    {data.historical && <div className="cv-resource-note" role="status">Historical concept frame at sequence {data.captured_seq} of {data.max_seq}.</div>}
    <div className="cv-resource-note epistemic" role="note">Concept memberships are recorded claims; taxonomy semantics are not independently verified.</div>
    <div className="cv-table-wrap"><table className="cv-table"><thead><tr><th className="cv-name" scope="col">Concept / experiment</th>
      {cols.map(column => <th key={column.key} className="cv-num" scope="col">{column.label}</th>)}</tr></thead><tbody>
      {rows.map(({ id, depth, hasChildren }) => {
        const node = data.tree.nodes[id]
        const experiments = byConcept[id] || []
        // Reflect the EFFECTIVE expansion (search force-opens ancestor paths) so the chevron/aria state
        // matches what is actually rendered; toggling still writes to the user-controlled `expanded` set.
        const open = effectiveExpanded.has(id)
        // Under an active filter a concept can have children that are all filtered out (it matched only
        // via a tagged experiment); don't offer an expander that would reveal nothing when opened.
        const showExpander = hasChildren && (!searching
          || (Array.isArray(node?.children) && node.children.some(child => filter.visible.has(child))))
        // A search that matched an EXPERIMENT (not the concept id) auto-opens that concept's evidence,
        // narrowed to the matching refs; a concept-id match (or a manual toggle) shows all its refs.
        const conceptHit = searching && filter.conceptHit.has(id)
        const evidenceOpen = evidenceExpanded.has(id) || (searching && filter.evidenceOpen.has(id))
        const shownExperiments = (searching && filter.evidenceOpen.has(id) && !conceptHit
          && !evidenceExpanded.has(id))
          ? experiments.filter(ref => experimentRefMatches(ref, query))
          : experiments
        const crossParents = Array.isArray(node?.cross_parents) ? node.cross_parents : []
        const edgeProjection = Array.isArray(node?.cross_parents)
        const conceptLabel = edgeProjection ? id : conceptLeaf(id)
        const crossParentSummary = crossParents.length
          ? `Additional display ${crossParents.length === 1 ? 'parent' : 'parents'} via recorded ${linkKind} ${crossParents.length === 1 ? 'link' : 'links'}: ${crossParents.join(', ')}`
          : ''
        return <Fragment key={id}><tr className={'cv-crow' + (node?.tagged ? ' tagged' : ' ghost') + (conceptHit ? ' hit' : '')}>
          <td className="cv-name" style={{ paddingLeft: 12 + depth * 18 }}>
            {showExpander ? <button type="button" className="cv-chev" onClick={() => toggle(id)}
              aria-expanded={open} aria-label={`${open ? 'Collapse' : 'Expand'} ${id}`}>{open ? '▾' : '▸'}</button>
              : <span className="cv-chev-placeholder" aria-hidden="true">·</span>}
            <span className="cv-cid" title={id}><Marked text={conceptLabel} query={searching ? query : ''} /></span>
            {!!crossParents.length && <span className="cv-badge" title={crossParentSummary}
              aria-label={crossParentSummary}>+{crossParents.length} links</span>}
            {!!experiments.length && <button type="button" className="cv-badge btn xs"
              onClick={() => toggleEvidence(id)} aria-expanded={evidenceOpen}
              title={`${evidenceOpen ? 'Hide' : 'Show'} tagged experiments for ${id}`}
              aria-label={`${evidenceOpen ? 'Hide' : 'Show'} ${experiments.length} tagged ${experiments.length === 1 ? 'experiment' : 'experiments'} for ${id}`}>
              {experiments.length} refs</button>}
          </td>{cols.map(column => {
            const value = metricRows[id]?.[column.key]
            const tone = column.delta ? deltaTone(value) : ''
            return <td key={column.key} className={'cv-num' + (tone ? ` d-${tone}` : '')}>{fmtCell(value)}</td>
          })}</tr>
          {/* Render the frame's generation-bound refs, never a live-state join that can
              attach historical concepts to a replaced node with the same numeric id. */}
          {evidenceOpen && shownExperiments.map(ref => {
            const displayed = state.nodes?.[ref.node_id]
            const lifecycleMatches = !!displayed
              && Number.isSafeInteger(displayed.attempt)
              && displayed.attempt === ref.node_generation
            const constraint = ref.feasible === false ? 'infeasible'
              : ref.feasible === true ? 'feasible' : 'constraint status not reported'
            const rollup = ref.metric === null ? 'not included in the concept rollup: robust metric unavailable'
              : ref.feasible === false ? 'excluded from the concept rollup because it is infeasible'
                : 'included in the concept rollup under the current eligibility rule'
            const rollupLabel = ref.metric === null ? 'unavailable'
              : ref.feasible === false ? 'excluded' : 'eligible'
            const refSummary = `Experiment #${ref.node_id}, attempt ${ref.node_generation}, ${ref.status}, ${constraint}, membership ${ref.membership_provenance}, ${rollup}`
            return <tr key={`${id}:${ref.node_id}:${ref.node_generation}`} className="cv-erow"><td className="cv-name" style={{ paddingLeft: 12 + (depth + 1) * 18 }}>
              <button type="button" className="cv-exp-button" disabled={!lifecycleMatches}
                onClick={() => onPickNode?.(ref.node_id)} title={refSummary}
                aria-label={`${refSummary}. ${lifecycleMatches
                  ? 'Open in Inspector'
                  : 'This attempt is not in the displayed run snapshot'}`}>
                <span className="cv-exp"><Marked text={`Experiment #${ref.node_id} · attempt ${ref.node_generation}`} query={searching ? query : ''} /></span>
                <span className="badge"><Marked text={ref.status} query={searching ? query : ''} /></span>
                {ref.feasible === true && <span className="badge">feasible</span>}
                {ref.feasible === false && <span className="badge reason">infeasible</span>}
                {ref.feasible === null && <span className="badge">constraint?</span>}
                <span className="badge">membership · {ref.membership_provenance}</span>
                <span className={'badge' + (rollupLabel === 'excluded' ? ' reason' : '')}>
                  rollup · {rollupLabel}
                </span>
                {ref.is_best
                  && <span className="cv-best" title="Frame champion" aria-label="Frame champion">★</span>}</button></td>
              <td className="cv-num cv-expmetric" colSpan={cols.length} title={rollup}>
                {ref.metric === null ? 'metric unavailable' : `${fmt(ref.metric)}${ref.feasible === false ? ' · excluded' : ''}`}</td></tr>
          })}</Fragment>
      })}
      {searching && rows.length === 0 &&
        <tr><td className="cv-name cv-nomatch" colSpan={cols.length + 1}>
          No concept or experiment matches “{query.trim()}”.</td></tr>}
    </tbody></table></div>
  </div>
}
