import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import { get, post, fmt, runApiPath } from './util.js'
import { deadlineRequest } from './requestDeadline.js'
import {
  visibleConceptRows, conceptLeaf, deltaTone, fmtCell,
  CONCEPT_COLUMNS, DEFAULT_COLUMNS,
} from './conceptViewModel.js'

const TIMEOUT_MS = 12_000
const LENS_PROMPT_MAX_CHARS = 800
const LENS_PROMPT_MAX_BYTES = 2_048
const record = value => value !== null && typeof value === 'object' && !Array.isArray(value)
const metric = value => value === null || (typeof value === 'number' && Number.isFinite(value))
const count = value => Number.isSafeInteger(value) && value >= 0
const sequence = value => Number.isSafeInteger(value) && value >= -1
const conceptId = value => typeof value === 'string' && value.length > 0
const derivedLensId = value => typeof value === 'string' && value.length <= 64
  && /^[a-z0-9][a-z0-9-]*$/.test(value)
const generationId = value => value === null
  || (typeof value === 'string' && /^[0-9a-f]{64}$/.test(value))
const invalidPayload = () => { throw new TypeError('Invalid concept projection') }
const countRecord = value => record(value) && Object.values(value).every(item => count(item))
const validList = (value, test) => Array.isArray(value) && value.every(test)
const same = (left, right) => JSON.stringify(left) === JSON.stringify(right)
const sortedKeys = value => Object.keys(value).sort()

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
      || typeof historical !== 'boolean' || historical !== (capturedSeq < maxSeq)
      || !conceptId(lens) || !conceptId(effectiveLens) || lens !== effectiveLens
      || !conceptId(requestedLens) || !Array.isArray(lenses) || !lenses.length
      || typeof edgesPresent !== 'boolean' || typeof lensEdgesPresent !== 'boolean'
      || !record(touch) || !record(experimentRefs) || !record(tree) || tree.lens !== lens
      || !validList(tree.roots, conceptId) || !record(tree.nodes)
      || !record(metrics) || !record(metrics.rows) || !metric(metrics.baseline)
      || !['min', 'max'].includes(metrics.direction)
      || typeof authoritative !== 'boolean' || typeof complete !== 'boolean'
      || !record(authority) || !record(provenance) || !record(completeness)
      || !record(spec) || !record(contract)) invalidPayload()

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
        || !validList(item.rels, conceptId) || !item.rels.length
        || item.rels.length > 8 || new Set(item.rels).size !== item.rels.length
        || !['path', 'edge'].includes(item.kind) || lensNames.has(item.name)) invalidPayload()
    lensNames.add(item.name)
    shippedByName.set(item.name, item)
  }
  const derived = expected.derived === true
  if (!conceptId(spec.name) || spec.name !== requestedLens
      || !validList(spec.rels, conceptId) || !spec.rels.length
      || spec.rels.length > 8 || new Set(spec.rels).size !== spec.rels.length
      || !['path', 'edge'].includes(spec.kind)
      || !['shipped', 'ephemeral-validated'].includes(spec.registration)
      || spec.registration !== (derived ? 'ephemeral-validated' : 'shipped')
      || (derived && Array.isArray(expected.rels) && !same(spec.rels, expected.rels))
      || contract.requested !== requestedLens || contract.effective !== effectiveLens
      || contract.registration !== spec.registration
      || !(contract.fallback === null || contract.fallback === 'no_matching_edges')
      || (contract.fallback === null) !== (requestedLens === effectiveLens)
      || !lensNames.has('is_a') || (!derived && !lensNames.has(requestedLens))
      || (!lensNames.has(effectiveLens) && effectiveLens !== requestedLens)
      || (lensEdgesPresent && effectiveLens !== requestedLens)
      || (lensEdgesPresent && !edgesPresent)
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
  const nodeIds = Object.keys(tree.nodes)
  const rootIds = new Set(tree.roots)
  if (rootIds.size !== tree.roots.length) invalidPayload()
  for (const [id, node] of Object.entries(tree.nodes)) {
    if (!conceptId(id) || !record(node) || !(node.parent === null || conceptId(node.parent))
        || !count(node.depth) || typeof node.tagged !== 'boolean'
        || !validList(node.children, conceptId)
        || new Set(node.children).size !== node.children.length
        || (hierarchyTree && Object.hasOwn(node, 'cross_parents'))
        || (!hierarchyTree && (!validList(node.cross_parents, conceptId)
          || new Set(node.cross_parents).size !== node.cross_parents.length))) invalidPayload()
  }
  for (const id of tree.roots) {
    if (!Object.hasOwn(tree.nodes, id)) invalidPayload()
  }
  for (const [id, node] of Object.entries(tree.nodes)) {
    const isRoot = rootIds.has(id)
    if ((node.parent === null) !== isRoot || (isRoot && node.depth !== 0)) invalidPayload()
    if (node.parent !== null) {
      const parent = tree.nodes[node.parent]
      if (!record(parent) || !parent.children.includes(id) || node.depth !== parent.depth + 1) {
        invalidPayload()
      }
    }
    for (const childId of node.children) {
      if (!record(tree.nodes[childId]) || tree.nodes[childId].parent !== id) invalidPayload()
    }
    if (hierarchyTree) {
      const parts = id.split('/')
      const pathParent = parts.length === 1 ? null : parts.slice(0, -1).join('/')
      if (node.parent !== pathParent || node.depth !== parts.length - 1) invalidPayload()
    } else if (node.cross_parents.some(parentId => parentId === id || parentId === node.parent
        || !Object.hasOwn(tree.nodes, parentId))) invalidPayload()
  }
  for (const [id, valueCount] of Object.entries(touch)) {
    if (!conceptId(id) || !Object.hasOwn(tree.nodes, id) || !count(valueCount)) invalidPayload()
  }
  for (const [id, row] of Object.entries(metrics.rows)) {
    if (!conceptId(id) || !Object.hasOwn(tree.nodes, id) || !record(row)
        || !count(row.touched) || !count(row.evaluated) || row.evaluated > row.touched
        || !(row.first_touch === null || count(row.first_touch))
        || !['best', 'mean', 'worst', 'delta_best', 'delta_mean'].every(key => metric(row[key]))) {
      invalidPayload()
    }
  }

  let referenceCount = 0
  const provenanceCounts = Object.create(null)
  const lifecycleByNode = new Map()
  const membershipCountsByNode = new Map()
  for (const [id, refs] of Object.entries(experimentRefs)) {
    if (!conceptId(id) || !Object.hasOwn(tree.nodes, id) || !Array.isArray(refs) || !refs.length
        || touch[id] !== refs.length || !Object.hasOwn(metrics.rows, id)
        || metrics.rows[id].touched !== refs.length) invalidPayload()
    const lifecycle = new Set()
    let evaluated = 0
    for (const ref of refs) {
      if (!record(ref) || !count(ref.node_id) || !count(ref.node_generation)
          || !metric(ref.metric) || ref.metric_kind !== 'robust_metric' || !conceptId(ref.status)
          || !(ref.feasible === null || typeof ref.feasible === 'boolean')
          || typeof ref.is_best !== 'boolean' || !conceptId(ref.membership_provenance)) invalidPayload()
      const key = `${ref.node_id}:${ref.node_generation}`
      if (lifecycle.has(key)) invalidPayload()
      lifecycle.add(key)
      const signature = JSON.stringify([ref.node_generation, ref.metric, ref.metric_kind, ref.status,
        ref.feasible, ref.is_best, ref.membership_provenance])
      if (lifecycleByNode.has(ref.node_id) && lifecycleByNode.get(ref.node_id) !== signature) {
        invalidPayload()
      }
      lifecycleByNode.set(ref.node_id, signature)
      membershipCountsByNode.set(ref.node_id, (membershipCountsByNode.get(ref.node_id) || 0) + 1)
      if (ref.metric !== null && ref.feasible !== false) evaluated += 1
      referenceCount += 1
      provenanceCounts[ref.membership_provenance]
        = (provenanceCounts[ref.membership_provenance] || 0) + 1
    }
    if (metrics.rows[id].evaluated !== evaluated) invalidPayload()
  }
  for (const [id, node] of Object.entries(tree.nodes)) {
    if (node.tagged !== Object.hasOwn(experimentRefs, id)) invalidPayload()
  }

  const reasons = completeness.reasons
  const sourceIntegrity = completeness.source_integrity
  const limitKeys = ['concepts_per_node', 'edge_endpoints', 'edges', 'membership_nodes',
    'memberships', 'tree_nodes']
  const sourceKeys = ['edges', 'membership_nodes']
  const includedKeys = ['concepts', 'edges', 'experiment_refs', 'membership_nodes',
    'memberships', 'tree_nodes']
  if (typeof completeness.complete !== 'boolean' || completeness.complete !== complete
      || typeof completeness.truncated !== 'boolean' || !Array.isArray(reasons)
      || !reasons.every(conceptId) || new Set(reasons).size !== reasons.length
      || !same(reasons, [...reasons].sort())
      || completeness.truncated !== reasons.some(reason => reason.endsWith('_cap'))
      || status !== (complete ? 'complete' : 'partial') || complete !== (reasons.length === 0)
      || !countRecord(completeness.limits) || !countRecord(completeness.source)
      || !countRecord(completeness.included) || !record(sourceIntegrity)
      || !same(sortedKeys(completeness.limits), limitKeys)
      || limitKeys.some(key => completeness.limits[key] <= 0)
      || !same(sortedKeys(completeness.source), sourceKeys)
      || !same(sortedKeys(completeness.included), includedKeys)
      || typeof sourceIntegrity.complete !== 'boolean'
      || typeof sourceIntegrity.generation_identified !== 'boolean'
      || sourceIntegrity.generation_identified !== (generation !== null)
      || (sourceIntegrity.complete
        ? !same(sortedKeys(sourceIntegrity), ['complete', 'generation_identified'])
        : !same(sortedKeys(sourceIntegrity),
          ['complete', 'corrupt_line', 'dropped_lines', 'generation_identified']))
      || (!sourceIntegrity.complete
        && (!count(sourceIntegrity.corrupt_line) || !count(sourceIntegrity.dropped_lines)))
      || completeness.included.tree_nodes !== nodeIds.length
      || completeness.included.membership_nodes !== lifecycleByNode.size
      || completeness.included.concepts !== Object.keys(experimentRefs).length
      || completeness.included.experiment_refs !== referenceCount
      || completeness.included.memberships !== referenceCount
      || edgesPresent !== (completeness.included.edges > 0)
      || completeness.included.membership_nodes > completeness.limits.membership_nodes
      || completeness.included.memberships > completeness.limits.memberships
      || completeness.included.tree_nodes > completeness.limits.tree_nodes
      || completeness.included.edges > completeness.limits.edges
      || completeness.source.membership_nodes < completeness.included.membership_nodes
      || completeness.source.edges < completeness.included.edges
      || [...membershipCountsByNode.values()]
        .some(valueCount => valueCount > completeness.limits.concepts_per_node)
      || !same(sortedKeys(touch), sortedKeys(experimentRefs))
      || !same(sortedKeys(metrics.rows), sortedKeys(experimentRefs))) invalidPayload()

  if (typeof authority.authoritative !== 'boolean'
      || typeof authority.source_authoritative !== 'boolean'
      || typeof authority.complete !== 'boolean'
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

  const completeProjection = visibleConceptRows(tree, new Set(nodeIds))
  if (completeProjection.projectionStatus.state !== 'current'
      || completeProjection.length !== nodeIds.length) invalidPayload()
  return value
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

function StateCard({ tone, title, body, action, pending = false, stale = false }) {
  return <section className={`cv-state-card ${tone}`} role={tone === 'error' || stale ? 'alert' : 'status'}
    aria-live={tone === 'error' || stale ? 'assertive' : 'polite'} aria-atomic="true">
    <span className="cv-state-mark" aria-hidden="true">{tone === 'loading' ? '' : tone === 'error' ? '!' : '◇'}</span>
    <span className="cv-state-eyebrow">Concept map</span><h2>{title}</h2><p>{body}</p>
    {tone === 'empty' && <div className="cv-empty-flow" aria-label="How the concept view is built">
      <span>Experiments</span><i aria-hidden="true">→</i><span>Concept hierarchy</span>
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
  const hasExactGeneration = typeof generation === 'string' && generationId(generation)
  const [lensSelection, setLensSelection] = useState({ scope: lensScope, name: 'is_a' })
  const lens = lensSelection.scope === lensScope ? lensSelection.name : 'is_a'
  const setLens = name => setLensSelection({ scope: lensScope, name })
  const [resource, setResource] = useState(initial)
  const [retry, setRetry] = useState(0)
  const [expanded, setExpanded] = useState(() => new Set())
  const [evidenceExpanded, setEvidenceExpanded] = useState(() => new Set())
  const [columns, setColumns] = useState(DEFAULT_COLUMNS)
  // Derived lenses are view-state specs: the paid derivation happens once, then GET replays their
  // exact relation subset so ordinary semantic refreshes remain deterministic and read-only.
  const [derivedLenses, setDerivedLenses] = useState([])
  const [lensFormState, setLensFormState] = useState(() => emptyLensForm(lensScope))
  const lensCreates = useRef(new Map())
  const lensForm = lensFormState.scope === lensScope ? lensFormState : emptyLensForm(lensScope)
  const { prompt: lensPrompt, error: lensErr } = lensForm
  const lensBusy = lensForm.busy || lensCreates.current.has(lensScope)
  const setCurrentLensForm = update => setLensFormState(previous => {
    const currentForm = previous.scope === lensScope ? previous : emptyLensForm(lensScope)
    const next = typeof update === 'function' ? update(currentForm) : update
    return { ...next, scope: lensScope }
  })
  const currentLensScope = useRef(lensScope)
  currentLensScope.current = lensScope
  const request = useRef(null)
  const projectionKey = useMemo(() => conceptProjectionKey(state), [state])
  const requestedSeq = displayedSequence == null ? null : displayedSequence
  const runDerivedLenses = derivedLenses.filter(item => item.scope === lensScope)
  const activeDerived = runDerivedLenses.find(item => item.name === lens)
  const activeRels = activeDerived ? (activeDerived.rels || []).join(',') : ''
  const scope = JSON.stringify([runKey, generation ?? null, requestedSeq, lens])
  const requestVersion = JSON.stringify([scope, activeRels, projectionKey])
  const expectedVersion = useRef(requestVersion)
  expectedVersion.current = requestVersion

  useEffect(() => {
    request.current?.controller.abort()
    const query = new URLSearchParams({ lens })
    if (activeRels) query.set('rels', activeRels)
    if (requestedSeq !== null) query.set('seq', String(requestedSeq))
    const timed = deadlineRequest(signal => get(`${runApiPath(runId, '/concepts')}?${query}`, {
      signal, cache: 'no-store',
    }), TIMEOUT_MS)
    const owner = { ...timed, scope, requestVersion }
    request.current = owner
    setResource(previous => previous.scope === scope && previous.data
      ? { ...previous, requestVersion, status: 'refreshing', timeout: false }
      : { scope, requestVersion, status: 'loading', data: null, timeout: false })

    let done = false
    const finish = (ok, data = null) => {
      if (done) return
      done = true
      // Render-time semantic identity closes the gap before effect cleanup; owner
      // identity fences late results from a superseded semantic projection.
      if (request.current !== owner || expectedVersion.current !== requestVersion) return
      request.current = null
      setResource(previous => {
        if (previous.scope !== scope || previous.requestVersion !== requestVersion) return previous
        if (ok) return { scope, requestVersion, status: 'ready', data, timeout: false }
        return previous.data
          ? { ...previous, status: 'stale', timeout: timed.timedOut() }
          : { scope, requestVersion, status: 'error', data: null, timeout: timed.timedOut() }
      })
    }
    timed.promise.then(value => validateConceptPayload(value, {
      runId,
      ...(generation === undefined ? {} : { generation }),
      requestedSeq,
      requestedLens: lens,
      direction: ['min', 'max'].includes(state?.direction) ? state.direction : null,
      derived: !!activeDerived,
      rels: activeDerived?.rels,
    })).then(data => finish(true, data), () => finish(false))
    return () => {
      done = true
      timed.controller.abort()
      if (request.current === owner) request.current = null
    }
  }, [runId, generation, requestedSeq, lens, activeRels, projectionKey, retry])

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
  const rows = useMemo(() => visibleConceptRows(data?.tree, expanded), [data, expanded])
  const projectionStatus = useMemo(() => data
    ? visibleConceptRows(data.tree, new Set(Object.keys(data.tree.nodes))).projectionStatus
    : { state: 'unavailable', reasons: [] }, [data])
  const roots = data?.tree?.roots || []
  const empty = !!data && roots.length === 0
  const refreshing = current.status === 'refreshing'
  const refresh = () => setRetry(value => value + 1)
  const toggle = id => setExpanded(previous => {
    const next = new Set(previous); next.has(id) ? next.delete(id) : next.add(id); return next
  })
  const toggleEvidence = id => setEvidenceExpanded(previous => {
    const next = new Set(previous); next.has(id) ? next.delete(id) : next.add(id); return next
  })
  const toggleColumn = key => setColumns(value => value.includes(key)
    ? value.length === 1 ? value : value.filter(item => item !== key) : [...value, key])
  const cols = CONCEPT_COLUMNS.filter(column => columns.includes(column.key))

  const createLens = async event => {
    event.preventDefault()
    const prompt = lensPrompt.trim()
    if (!prompt || displayedSequence != null || !hasExactGeneration
        || lensCreates.current.has(lensScope)) return
    if (prompt.length > LENS_PROMPT_MAX_CHARS
        || new TextEncoder().encode(prompt).length > LENS_PROMPT_MAX_BYTES) {
      setCurrentLensForm(form => ({ ...form,
        error: 'Lens description is too long. Keep it within 800 characters and 2,048 bytes.' }))
      return
    }
    const owner = { scope: lensScope }
    lensCreates.current.set(lensScope, owner)
    setCurrentLensForm(form => ({ ...form, busy: true, error: '' }))
    try {
      const response = await post(runApiPath(runId, '/concepts/lens'), {
        prompt, expected_generation: generation,
      })
      if (lensCreates.current.get(lensScope) !== owner || currentLensScope.current !== lensScope) return
      const spec = response?.spec
      if (response?.ok === true && record(spec) && conceptId(spec.name)
          && typeof spec.label === 'string' && Array.isArray(spec.rels)
          && spec.rels.length && spec.rels.every(conceptId)) {
        validateConceptPayload(response, {
          runId,
          ...(generation === undefined ? {} : { generation }),
          requestedSeq: null,
          requestedLens: spec.name,
          direction: ['min', 'max'].includes(state?.direction) ? state.direction : null,
          derived: true,
          rels: spec.rels,
        })
        if (!response.authoritative) invalidPayload()
        const derived = { scope: lensScope, name: spec.name,
          label: spec.label || spec.name, rels: [...spec.rels] }
        setDerivedLenses(list => [...list.filter(item => item.scope !== lensScope
          || item.name !== derived.name), derived])
        setExpanded(new Set())
        setEvidenceExpanded(new Set())
        setLens(derived.name)
        setCurrentLensForm(form => ({ ...form, prompt: '', error: '' }))
      } else {
        setCurrentLensForm(form => ({ ...form, error: response?.reason === 'no_model'
          ? 'No model is configured for lens creation.'
          : 'Could not derive a lens from that request; try naming a relation to group by.' }))
      }
    } catch (error) {
      if (lensCreates.current.get(lensScope) === owner
          && currentLensScope.current === lensScope) {
        const safelyRejected = ['invalid_run_generation', 'run_generation_unavailable',
          'concept_lens_prompt_too_large'].includes(error?.code)
        setCurrentLensForm(form => ({ ...form, error: error?.code === 'run_generation_changed'
          ? 'Run changed. Reload Concepts before creating another paid lens.'
          : safelyRejected
            ? 'The paid request was rejected before model work began. Reload Concepts and review the request.'
            : 'Outcome unknown; provider charges may have occurred. Reload before deciding whether to submit a new paid request.' }))
      }
    } finally {
      if (lensCreates.current.get(lensScope) === owner) {
        lensCreates.current.delete(lensScope)
        if (currentLensScope.current === lensScope) {
          setCurrentLensForm(form => ({ ...form, busy: false }))
        }
      }
    }
  }
  const deleteLens = name => {
    setDerivedLenses(list => list.filter(item => item.scope !== lensScope || item.name !== name))
    if (lens === name) { setExpanded(new Set()); setEvidenceExpanded(new Set()); setLens('is_a') }
  }
  // Fence paid lens derivation by owner and run; disabled state alone cannot stop a
  // double submit or keep a late paid response out of a same-id replacement run.
  const lensUnavailable = displayedSequence != null || !hasExactGeneration
  const lensCreator = <form className="cv-lensnew" onSubmit={createLens}>
    <input className="text" value={lensPrompt} maxLength={LENS_PROMPT_MAX_CHARS}
      onChange={event => setCurrentLensForm(form => ({ ...form, prompt: event.target.value, error: '' }))}
      placeholder={displayedSequence != null ? 'live view required'
        : hasExactGeneration ? 'describe a grouping lens…' : 'verified generation required'}
      aria-label="Describe a lens to create" aria-describedby="paid-concept-lens-status"
      disabled={lensBusy || lensUnavailable} />
    <button type="submit" className="btn sm"
      aria-describedby="paid-concept-lens-status"
      title={!hasExactGeneration ? 'Reload the run and wait for its verified generation.' : undefined}
      disabled={lensBusy || lensUnavailable || !lensPrompt.trim()}>
      {lensBusy ? 'Creating…' : 'Create lens · paid'}</button>
    <span id="paid-concept-lens-status" className="muted" role="note">
      Uses the configured model provider; provider charges may apply.</span>
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
  if (stateCard) return <div className="concept-view cv-state-layout" data-route-main tabIndex={-1}
    aria-label="Concept tree"><StateCard {...stateCard} /></div>

  // Null-prototype rehydrate (same prototype-safety as byConcept): metrics.rows is raw JSON, and a
  // "constructor"/"__proto__" intermediate concept id would otherwise read an inherited Object.prototype
  // value at metricRows[id] instead of the correct "no row" undefined.
  const metricRows = Object.create(null)
  for (const key of Object.keys(data.metrics.rows)) metricRows[key] = data.metrics.rows[key]
  const experimentCount = new Set(Object.values(byConcept).flat()
    .map(ref => `${ref.node_id}:${ref.node_generation}`)).size
  const shippedLenses = data.edges_present ? data.lenses : data.lenses.filter(item => item.name === 'is_a')
  const derivedNames = new Set(runDerivedLenses.map(item => item.name))
  const availableLenses = [
    ...shippedLenses.filter(item => !derivedNames.has(item.name)),
    ...runDerivedLenses.map(item => ({ ...item, derived: true })),
  ]
  const linkKind = data.requested_lens_spec.rels.join(', ')
  return <div className="concept-view" data-route-main tabIndex={-1} aria-label="Concept tree"
    aria-busy={refreshing}>
    <header className="cv-bar">
      <div className="cv-heading"><strong>Concept tree</strong><span>{Object.keys(data.tree.nodes).length} concepts · {experimentCount} tagged experiments · frame seq {data.captured_seq}</span></div>
      <div className="cv-lensctl">
        <label className="cv-lenspick"><span>Hierarchy lens</span><select className="text" value={lens}
          onChange={event => { setExpanded(new Set()); setEvidenceExpanded(new Set()); setLens(event.target.value) }} aria-label="Concept hierarchy lens">
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
        const open = expanded.has(id)
        const evidenceOpen = evidenceExpanded.has(id)
        const crossParents = Array.isArray(node?.cross_parents) ? node.cross_parents : []
        const edgeProjection = Array.isArray(node?.cross_parents)
        const conceptLabel = edgeProjection ? id : conceptLeaf(id)
        const crossParentSummary = crossParents.length
          ? `Secondary ${linkKind} ${crossParents.length === 1 ? 'parent' : 'parents'}: ${crossParents.join(', ')}`
          : ''
        return <Fragment key={id}><tr className={'cv-crow' + (node?.tagged ? ' tagged' : ' ghost')}>
          <td className="cv-name" style={{ paddingLeft: 12 + depth * 18 }}>
            {hasChildren ? <button type="button" className="cv-chev" onClick={() => toggle(id)}
              aria-expanded={open} aria-label={`${open ? 'Collapse' : 'Expand'} ${id}`}>{open ? '▾' : '▸'}</button>
              : <span className="cv-chev-placeholder" aria-hidden="true">·</span>}
            <span className="cv-cid" title={id}>{conceptLabel}</span>
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
          {evidenceOpen && experiments.map(ref => {
            const displayed = state.nodes?.[ref.node_id]
            const lifecycleMatches = !!displayed
              && Number.isSafeInteger(displayed.attempt)
              && displayed.attempt === ref.node_generation
            const constraint = ref.feasible === false ? 'infeasible'
              : ref.feasible === true ? 'feasible' : 'constraint status not reported'
            const rollup = ref.metric === null ? 'not included in the concept rollup: robust metric unavailable'
              : ref.feasible === false ? 'excluded from the concept rollup because it is infeasible'
                : 'included in the concept rollup under the current eligibility rule'
            const refSummary = `Experiment #${ref.node_id}, attempt ${ref.node_generation}, ${ref.status}, ${constraint}, membership ${ref.membership_provenance}, ${rollup}`
            return <tr key={`${id}:${ref.node_id}:${ref.node_generation}`} className="cv-erow"><td className="cv-name" style={{ paddingLeft: 12 + (depth + 1) * 18 }}>
              <button type="button" className="cv-exp-button" disabled={!lifecycleMatches}
                onClick={() => onPickNode?.(ref.node_id)} title={refSummary}
                aria-label={`${refSummary}. ${lifecycleMatches
                  ? 'Open in Inspector'
                  : 'This attempt is not in the displayed run snapshot'}`}>
                <span className="cv-exp">Experiment #{ref.node_id} · attempt {ref.node_generation}</span>
                <span className="badge">{ref.status}</span>
                {ref.feasible === false && <span className="badge reason">infeasible</span>}
                {ref.feasible === null && <span className="badge">constraint?</span>}
                {ref.is_best
                  && <span className="cv-best" title="Frame champion" aria-label="Frame champion">★</span>}</button></td>
              <td className="cv-num cv-expmetric" colSpan={cols.length} title={rollup}>
                {ref.metric === null ? 'metric unavailable' : `${fmt(ref.metric)}${ref.feasible === false ? ' · excluded' : ''}`}</td></tr>
          })}</Fragment>
      })}
    </tbody></table></div>
  </div>
}
