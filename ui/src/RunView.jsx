import React, {
  lazy, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore,
} from 'react'
import { useMediaQuery, useRunState, useScopedResource } from './hooks.js'
import { useToast } from './useToast.js'
import { useTimeline } from './useTimeline.js'
import { takeRunPanelHistoryEntry, useRunRouteState } from './useRunRouteState.js'
import { reviewInspectorTabs, reviewPanelAllowed, runRouteStateHasTarget } from './runRouteState.js'
import { deadlineGet, get, fmt, fmtInt, fmtElapsedSeconds, phaseLabel, workingId, isSweep, CONTROL, commandFeedback,
  storageGet, storageSet, runApiPath } from './util.js'
import { computeGroups, autoCollapseSet } from './grouping.js'
import EnergyToggle from './EnergyToggle.jsx'
import GlobalMenu from './GlobalMenu.jsx'
import { OpIcon } from './icons.jsx'
import LazyBoundary from './LazyBoundary.jsx'
import WhyStrip from './WhyStrip.jsx'
import { DIALOG_PRIORITY, useDialogFocus } from './useDialogFocus.js'
import { nextRovingIndex } from './accessibility.jsx'
import {
  clearRunAccess, historyMatches, liveHistory, reconcileHistoricalSelection, rejectHistory,
  requestHistory, resolveHistory, setRunAccess,
} from './runMode.js'
import {
  approvalCommandFor, dagEmptyPresentation, lifecyclePhaseLabel, runLifecycle, terminalReady,
} from './runIndex.js'
import { initialDagOverviewDecision } from './dagViewport.js'
import {
  captureMergeIntent, mergeIntentCommand, mergeIntentMatches, selectMergeTarget,
} from './mergeIntent.js'
import { GLOBAL_DESTINATIONS, INSTALLATION_ROUTE_VIEWS } from './globalNav.js'
import { nodeIsActive } from './nodeProjection.js'
import { createInspectorDraftStore } from './inspectorDraftStore.js'
import { installNavigationLossGuard } from './navigationLossGuard.js'
import {
  authoringRecoveryStorageKey, inspectAuthoringRecoveryStorage,
} from './authoringRecoveryStorage.js'
import {
  clearCommentOperationIntent, clearDamagedCommentOperation,
  listCommentOperationRecoveries, readCommentOperationRecoveryRevision,
  refreshCommentOperationRecoveries, subscribeCommentOperationRecoveries,
} from './commentRecoveryStorage.js'
import { useStartOverCoordination, useStartOverRecovery } from './useStartOverRecovery.js'
import RunScreen from './RunScreen.jsx'

const lazyNamed = (load, name) => lazy(() => load().then(module => ({
  default: name === 'default' ? module.default : module[name],
})))

const Dag = lazy(() => import('./Dag.jsx'))
const Dock = lazy(() => import('./Dock.jsx'))
const ReportView = lazy(() => import('./Report.jsx'))
const ConceptView = lazy(() => import('./ConceptView.jsx'))
const ConceptChipBar = lazy(() => import('./ConceptChipBar.jsx'))

let inspectorPromise
const loadInspector = () => inspectorPromise || (inspectorPromise = import('./Inspector.jsx'))
const Inspector = lazyNamed(loadInspector, 'default')
const GroupSummary = lazyNamed(loadInspector, 'GroupSummary')

// Trace clear can outlive the conditionally mounted Inspector (drawer close, pane collapse, route
// change). Keep only client-owned lifecycle state here so a late POST outcome still fences a newly
// mounted Inspector and orders its next detail read after the mutation.
const traceClearRecoveryRegistry = new Map()
let traceClearRecoveryRevision = 0
let traceClearRecoverySnapshot = { revision: 0, signals: new Map() }
const traceClearRecoveryListeners = new Set()
const subscribeTraceClearRecovery = listener => {
  traceClearRecoveryListeners.add(listener)
  return () => traceClearRecoveryListeners.delete(listener)
}
const readTraceClearRecoverySnapshot = () => traceClearRecoverySnapshot
const publishTraceClearRecovery = (scope, kind) => {
  const signal = {
    scope, kind, revision: ++traceClearRecoveryRevision,
  }
  const signals = new Map(traceClearRecoverySnapshot.signals)
  signals.delete(scope)
  signals.set(scope, signal)
  while (signals.size > 64) signals.delete(signals.keys().next().value)
  traceClearRecoverySnapshot = { revision: signal.revision, signals }
  for (const listener of traceClearRecoveryListeners) listener()
}

const commentDraftEntryUnsafe = ([scope, fields], runId) => {
  if ((!scope.startsWith(`comment-composer:${runId}@`)
      && !scope.startsWith(`comment-card:${runId}@`))
      || !fields || typeof fields !== 'object' || Array.isArray(fields)) return false
  const busy = fields.busy === true || (typeof fields.busy === 'string' && !!fields.busy)
  const draft = (typeof fields.text === 'string' && fields.text.length > 0)
    || (fields.dirty === true && typeof fields.draftText === 'string')
  const recovery = !!fields.retryIntent || !!fields.uncertainIntent
    || !!fields.editRetryIntent || !!fields.uncertainEdit
    || !!fields.resolutionRetryIntent || !!fields.uncertainResolution
    || !!fields.damagedRecovery
  return busy || draft || recovery
}

const commentDraftText = ([, fields]) => {
  if (typeof fields?.text === 'string' && fields.text.length > 0) return fields.text
  if (fields?.dirty === true && typeof fields.draftText === 'string') return fields.draftText
  return ''
}

// All optional panels intentionally share one deferred module request. The first opened panel pays
// the cost once; list/settings/report-only routes cannot reach this owner-only hub statically.
let panelsPromise
const loadPanels = () => panelsPromise || (panelsPromise = import('./panels.jsx'))
const TrustPanel = lazyNamed(loadPanels, 'TrustPanel')
const SensitivityPanel = lazyNamed(loadPanels, 'SensitivityPanel')
const FailuresPanel = lazyNamed(loadPanels, 'FailuresPanel')
const ParetoPanel = lazyNamed(loadPanels, 'ParetoPanel')
const DataQualityPanel = lazyNamed(loadPanels, 'DataQualityPanel')
const ConfigPanel = lazyNamed(loadPanels, 'ConfigPanel')
const AuthoringPanel = lazyNamed(loadPanels, 'AuthoringPanel')
const MemoryPanel = lazyNamed(loadPanels, 'MemoryPanel')
const RegistryPanel = lazyNamed(loadPanels, 'RegistryPanel')
const EventExplorer = lazyNamed(loadPanels, 'EventExplorer')
const ComparePanel = lazyNamed(loadPanels, 'ComparePanel')
const GpuPanel = lazyNamed(loadPanels, 'GpuPanel')
const HyperImportancePanel = lazyNamed(loadPanels, 'HyperImportancePanel')
const CrossRunPanel = lazyNamed(loadPanels, 'CrossRunPanel')
const CollabPanel = lazy(() => import('./CollabPanel.jsx'))
const OverviewPanel = lazyNamed(loadPanels, 'OverviewPanel')
const ResearchPanel = lazyNamed(loadPanels, 'ResearchPanel')
const ArtifactsPanel = lazyNamed(loadPanels, 'ArtifactsPanel')
const HypothesisBoard = lazyNamed(loadPanels, 'HypothesisBoard')
const QueuePanel = lazyNamed(loadPanels, 'QueuePanel')

// The panel bar, grouped by importance then process order (Report is the [Search|Report] toggle, and
// the deep-research/policy/strategist "why" cards now live in the chat — so those panels are gone).
//   rigor → analysis → data/lab → ops
// Run-view panel IA (design audit 2026-06-28): keep the few most-used panels inline; fold the long
// tail into a grouped "More ▾" overflow so the bar never wraps to a second row (was 17 buttons).
// Panel IA consolidated into 4 hubs (was 7 inline + a 12-item "More" overflow). Each hub is a
// dropdown of related panels; the hub button lights when its open panel is active. Report + Overview
// stay in the view-toggle above; Settings stays a dedicated button — both are one-click essentials.
// This bar is THIS RUN's menu. Everything in it answers a question about this run's event log — its
// queue, its cards, its trust gates, its files, its comments, its raw events. Three entries used to
// sit in `Lab` and did not: Memory (`/api/memory` + `/api/knowledge`), Authoring (`/api/{kind}` —
// the prompts/skills/knowledge every run shares) and GPU (`/api/gpu` — this host's cards). None of
// them takes a run id, so opening them from a run implied a scope they never had. They now live in
// the LoopLab menu (globalNav.js::GLOBAL_DESTINATIONS) beside Runs, Atlas and Settings, which is
// reachable from this header too — the split is about which QUESTION each menu answers, not about
// making the operator leave the run to ask it. The panel components still mount below for old
// `?panel=memory|authoring|gpu` links.
//
// BUT REMOVING THE ENTRIES REMOVED THE PATH. Reported by the operator on 2026-08-06: "you took
// something out of the run menus and now it is nowhere". The reasoning above is about SCOPE and it
// is right; what it got wrong is that an operator standing in a run does not re-derive it. They open
// the run's menus, do not find Memory, and conclude it is gone — the LoopLab menu one row above
// does not announce that it is where memory went. So the three are offered here again, at the foot
// of `Lab`, as LINKS to their installation home rather than as run panels: they navigate to
// `#/memory` / `#/knowledge` / `#/gpu`, under a heading that says whose they are. The scope split
// survives (nothing pretends to be run-scoped, and the destination is the same one screen the
// LoopLab menu reaches); the path back is what returns.
// Cross-run and Registry stay: both are anchored on THIS run (its task id, its champion and
// promotions) and read the run list only as context for it.
const HUBS = [
  ['Progress', [['queue', 'Queue'], ['hypotheses', 'Cards'], ['research', 'Research'], ['failures', 'Failures']]],
  ['Trust', [['trust', 'Trust'], ['pareto', 'Pareto / diversity'], ['data', 'Data quality']]],
  ['Analysis', [['compare', 'Compare'], ['sensitivity', 'Sensitivity'], ['importance', 'Importance'], ['crossrun', 'Cross-run']]],
  ['Lab', [['artifacts', 'Files'], ['registry', 'Registry'], ['collab', 'Comments & sharing'], ['events', 'Events']]],
]
const HUB_OF = Object.fromEntries(HUBS.flatMap(([label, items]) => items.map(([k]) => [k, label])))
// The hub whose menu also carries the installation links, and the links themselves — derived from
// the same two tables the LoopLab menu uses, so a destination added or renamed there cannot leave a
// stale copy here. `Lab` because that is where Files/Registry/Events already sit: the group that is
// about the workbench rather than about this run's search.
const INSTALLATION_LINK_HUB = 'Lab'
const INSTALLATION_LINKS = GLOBAL_DESTINATIONS.filter(
  entry => INSTALLATION_ROUTE_VIEWS.includes(entry.key))
// Historical overlays must derive only from the exact folded snapshot. Panels that fetch current
// config, raw detail, another run, or a sidecar stay closed so `seq + panel` cannot create a hybrid.
const HISTORY_SAFE_PANELS = new Set(['sensitivity', 'importance', 'failures', 'pareto', 'data'])
const START_OVER_SAFE_PANELS = new Set([
  'overview', 'trust', 'sensitivity', 'importance', 'failures', 'pareto', 'data',
  'compare', 'crossrun', 'artifacts', 'registry', 'memory', 'events', 'gpu',
])
const LIVE_INSPECT_TABS = ['Overview', 'Comments', 'Trials', 'Trace', 'Code', 'Metrics', 'Trust', 'Cost']
const READ_ONLY_INSPECT_TABS = ['Overview', 'Code', 'Trust', 'Cost']

const TRANSPORT_EMPTY_ACTIONS = new Set(['resume', 'finalize'])
// Expanded Timeline controls + pager + transport need enough room to leave a usable event viewport.
// Heal old persisted splitter values instead of allowing a technically scrollable ~15 px sliver.
const MIN_DOCK_HEIGHT = 200
const RUN_CONFIG_REQUEST_TIMEOUT_MS = 15_000
const HISTORICAL_SNAPSHOT_REQUEST_TIMEOUT_MS = 15_000
const RUN_GROUP_HISTORY_KEY = 'looplab.runGroupNavigation'
const RUN_GROUP_HISTORY_MODES = new Set(['theme', 'operator', 'metric', 'niche'])
const RUN_GROUP_KEY_LIMIT = 512
const RUN_GROUP_CONTROL_RE = /[\u0000-\u001f\u007f]/

function normalizeRunGroupNavigation(value, runId, generation = null) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  if (value.runId !== String(runId) || !/^[0-9a-f]{64}$/.test(String(value.generation || ''))
      || (generation && value.generation !== generation)
      || !RUN_GROUP_HISTORY_MODES.has(value.mode)
      || typeof value.key !== 'string' || !value.key || value.key.length > RUN_GROUP_KEY_LIMIT
      || RUN_GROUP_CONTROL_RE.test(value.key)) return null
  const returnNodeId = value.returnNodeId == null ? null : Number(value.returnNodeId)
  if (returnNodeId != null && (!Number.isSafeInteger(returnNodeId) || returnNodeId < 0)) return null
  const rawScrollTop = Number(value.scrollTop)
  const scrollTop = Number.isFinite(rawScrollTop) && rawScrollTop >= 0
    ? Math.min(rawScrollTop, 10_000_000) : 0
  return {
    runId: String(runId), generation: value.generation, mode: value.mode, key: value.key,
    returnNodeId, scrollTop,
  }
}

function readRunGroupNavigation(runId, generation = null) {
  if (typeof history === 'undefined') return null
  const state = history.state
  if (!state || typeof state !== 'object' || Array.isArray(state)) return null
  return normalizeRunGroupNavigation(state[RUN_GROUP_HISTORY_KEY], runId, generation)
}

function writeRunGroupNavigation(value, mode = 'replace') {
  if (typeof history === 'undefined' || typeof location === 'undefined') return false
  try {
    const state = history.state && typeof history.state === 'object' && !Array.isArray(history.state)
      ? history.state : {}
    const next = { ...state }
    if (value) next[RUN_GROUP_HISTORY_KEY] = value
    else delete next[RUN_GROUP_HISTORY_KEY]
    history[mode === 'push' ? 'pushState' : 'replaceState'](next, '', location.href)
    return true
  } catch { return false }
}

const hubMenuId = label => `panel-hub-${label.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`

function DagEmptyOverlay({ presentation, transport, onAction }) {
  if (!presentation) return null
  let actions = presentation.actions
  if (transport?.failure) {
    actions = [
      { id: 'events', label: 'Review command failure', emphasis: 'primary' },
      ...actions.filter(item => !TRANSPORT_EMPTY_ACTIONS.has(item.id) && item.id !== 'events'),
    ]
  }
  const pendingAction = transport?.pendingAction
  return <section className={`dag-empty-card ${presentation.tone}`}
    role={presentation.liveRegion === 'assertive' ? 'alert' : 'status'}
    aria-live={presentation.liveRegion} aria-atomic="true">
    {presentation.tone === 'progress' && <span className="dag-empty-spinner" aria-hidden="true" />}
    <span className="dag-empty-eyebrow">Search canvas</span>
    <h2>{presentation.title}</h2>
    <p>{presentation.body}</p>
    {transport?.failure && <p className="dag-empty-command-note">
      The last run command needs attention. Its exact identity is preserved in Events &amp; timeline.
    </p>}
    {transport?.busy && <p className="dag-empty-command-note">
      {pendingAction ? `${pendingAction} command is already in progress.` : 'A run command is already in progress.'}
    </p>}
    {actions.length > 0 && <div className="dag-empty-actions">
      {actions.map(item => {
        const isTransport = TRANSPORT_EMPTY_ACTIONS.has(item.id)
        const generationReady = /^[0-9a-f]{64}$/.test(transport?.expectedGeneration || '')
        const disabled = isTransport && (!!transport?.busy || !transport || !generationReady)
        return <button type="button" key={item.id}
          className={'btn' + (item.emphasis === 'primary' ? ' primary' : item.emphasis === 'danger' ? ' danger' : '')}
          disabled={disabled} onClick={() => onAction(item.id)}>{item.label}</button>
      })}
    </div>}
  </section>
}

export default function RunView({ runId, onBack, reviewMode = false, reviewMeta = null }) {
  const { live, seq, generation, eventCount: liveEventCount, connected,
    status: runStatus, error: runError, retry: retryRun } =
    useRunState(runId, { pollOnly: reviewMode })
  const retryRunRef = useRef(retryRun)
  retryRunRef.current = retryRun
  // The destructive Start-over saga lives in useStartOverRecovery.js (doc 25 UI-03). Its durable
  // state is read here, at the top, because startOverMutationBlocked gates the published run
  // access, the panel allow-list and every node mutation; its coordinating effects are installed
  // further down, at the exact position they used to occupy.
  const startOver = useStartOverRecovery({ runId, generation, reviewMode })
  const {
    startOverRecovery, startOverIntent, startOverRequestPending, startOverHandoff,
    startOverReplacementSuperseded, startOverRouteSyncFailed, startOverMutationBlocked,
    startOverNoticeRef,
  } = startOver
  const gen = generation?.slice(0, 8) || 'unknown'
  const route = useRunRouteState({ generation, reviewMode })
  const { state: routeState, generationMismatch, generationPending } = route
  const traceClearRecoveryStore = useRef(traceClearRecoveryRegistry)
  const inspectorDraftStoreRef = useRef(null)
  if (!inspectorDraftStoreRef.current) inspectorDraftStoreRef.current = createInspectorDraftStore()
  const panelNavigationGuardRef = useRef(null)
  const [panelNavigationGuard, setPanelNavigationGuard] = useState(null)
  const publishPanelNavigationGuard = useCallback(controller => {
    if (!controller || !['config', 'authoring'].includes(controller.route)
        || typeof controller.dispose !== 'function') return undefined
    const registration = { ...controller }
    panelNavigationGuardRef.current = registration
    setPanelNavigationGuard(registration)
    return () => {
      if (panelNavigationGuardRef.current !== registration) return
      // An unsafe child can disappear behind a generation/history fence before the user chooses
      // what to do. Keep its controller reachable until an explicit discard or a replacement panel
      // registers; the child's unmount fence separately prevents late async writes.
      if (registration.unsafe) return
      panelNavigationGuardRef.current = null
      setPanelNavigationGuard(null)
    }
  }, [])
  const commentRecoveryRevision = useSyncExternalStore(
    subscribeCommentOperationRecoveries,
    readCommentOperationRecoveryRevision,
    readCommentOperationRecoveryRevision,
  )
  // useSyncExternalStore closes the render→effect subscription gap: a clear POST may settle while
  // RunView is being remounted, and that outcome must never be missed by the replacement Inspector.
  const traceClearRecoverySnapshot = useSyncExternalStore(
    subscribeTraceClearRecovery, readTraceClearRecoverySnapshot, readTraceClearRecoverySnapshot)
  const inspectorDraftRevision = useSyncExternalStore(
    inspectorDraftStoreRef.current.subscribeAll,
    inspectorDraftStoreRef.current.revision,
    inspectorDraftStoreRef.current.revision,
  )
  const retainedCommentEntries = useMemo(() => reviewMode ? []
    : inspectorDraftStoreRef.current.entries()
      .filter(entry => commentDraftEntryUnsafe(entry, String(runId))),
  [reviewMode, runId, inspectorDraftRevision])
  const retainedCommentScopes = useMemo(
    () => [...new Set(retainedCommentEntries.map(([scope]) => scope))],
    [retainedCommentEntries],
  )
  const retainedCommentDrafts = useMemo(() => retainedCommentEntries
    .map(commentDraftText).filter(Boolean), [retainedCommentEntries])
  const retainedCommentRecovery = useMemo(
    () => reviewMode
      ? { available: true, valid: [], damaged: [] }
      : listCommentOperationRecoveries(String(runId)),
    [reviewMode, runId, commentRecoveryRevision],
  )
  const retainedCommentDurableCount = retainedCommentRecovery.valid.length
    + retainedCommentRecovery.damaged.length
  const retainedCommentRecoveryUnavailable = !reviewMode && !retainedCommentRecovery.available
  const retainedCommentProtectedCreateCandidates = [
    ...retainedCommentRecovery.valid.filter(intent => intent.kind === 'create'
      && (!generation || intent.expectedGeneration === generation)),
    ...retainedCommentRecovery.damaged.filter(recovery => recovery.identity?.kind === 'create'
      && (!generation || recovery.identity.expectedGeneration === generation))
      .map(recovery => recovery.identity),
    ...retainedCommentEntries.flatMap(([, fields]) => {
      const candidates = [fields?.uncertainIntent?.recovery, fields?.damagedRecovery?.identity]
      return candidates.filter(identity => identity?.kind === 'create'
        && (!generation || identity.expectedGeneration === generation))
    }),
  ]
  const retainedCommentProtectedCreates = [...new Map(
    retainedCommentProtectedCreateCandidates.map(identity => [[
      identity.expectedGeneration, identity.nodeId, identity.nodeGeneration,
    ].join('\u0000'), identity]),
  ).values()]
  const retainedCommentDamagedProtectedCreateCount = retainedCommentProtectedCreates
    .filter(identity => !identity.operationId).length
  const retainedCommentValidProtectedCreateCount = retainedCommentProtectedCreates.length
    - retainedCommentDamagedProtectedCreateCount
  const retainedCommentScopeHasProtectedCreate = scope => retainedCommentProtectedCreates.some(
    identity => {
      const base = `comment-composer:${String(runId)}@${identity.expectedGeneration}:${identity.nodeId}:${identity.nodeGeneration}`
      return scope === base || scope.startsWith(`${base}:`)
    },
  )
  const retainedCommentReleasableEntries = retainedCommentRecoveryUnavailable ? []
    : retainedCommentEntries.filter(([scope]) => !retainedCommentScopeHasProtectedCreate(scope))
  const retainedCommentReleasableIntents = retainedCommentRecovery.valid.filter(
    intent => intent.kind !== 'create' || (generation && intent.expectedGeneration !== generation))
  const retainedCommentReleasableDamaged = retainedCommentRecovery.damaged.filter(
    recovery => recovery.identity?.kind !== 'create'
      || (generation && recovery.identity.expectedGeneration !== generation))
  const retainedCommentReleasableCount = retainedCommentReleasableEntries.length
    + retainedCommentReleasableIntents.length + retainedCommentReleasableDamaged.length
  const retainedCommentWorkUnsafe = retainedCommentEntries.length > 0
    || retainedCommentDurableCount > 0 || retainedCommentRecoveryUnavailable
  const viewSeq = routeState.sequence
  const selectedId = routeState.nodeId
  const inspectTab = routeState.inspectTab
  const panel = routeState.panel
  const activePanelNavigationGuard = panelNavigationGuard?.route === panel
    ? panelNavigationGuard : null
  const retainedConfigDraft = panel === 'config'
    ? inspectorDraftStoreRef.current.readField(`panel:config:${String(runId)}`, 'draft', null)
    : null
  const retainedConfigStoredDraftUnsafe = retainedConfigDraft?.schema === 'looplab.config-draft/v1'
    && retainedConfigDraft?.unsafe === true
  const retainedConfigDraftUnsafe = retainedConfigStoredDraftUnsafe
    || (activePanelNavigationGuard?.route === 'config'
      && activePanelNavigationGuard.unsafe === true)
  const retainedConfigScope = `panel:config:${String(runId)}`
  const retainedAuthoringScope = 'panel:authoring'
  const retainedAuthoringDocuments = panel === 'authoring'
    ? inspectorDraftStoreRef.current.readField(retainedAuthoringScope, 'documents', null)
    : null
  const retainedAuthoringDocumentEntries = retainedAuthoringDocuments
    && typeof retainedAuthoringDocuments === 'object' && !Array.isArray(retainedAuthoringDocuments)
    ? Object.entries(retainedAuthoringDocuments).filter(([, document]) => document
      && typeof document === 'object' && !Array.isArray(document))
    : []
  const retainedAuthoringDocumentValues = retainedAuthoringDocumentEntries
    .map(([, document]) => document)
  const retainedAuthoringDraftCount = retainedAuthoringDocumentValues
    .filter(document => document.draftText !== document.savedText).length
  const retainedAuthoringUncertainSaves = panel === 'authoring'
    ? inspectorDraftStoreRef.current.readField(
      retainedAuthoringScope, 'uncertainSaves', null) : null
  const retainedAuthoringDamagedRecoveries = panel === 'authoring'
    ? inspectorDraftStoreRef.current.readField(
      retainedAuthoringScope, 'damagedRecoveries', null) : null
  const retainedAuthoringUncertainSaveEntries = retainedAuthoringUncertainSaves
    && typeof retainedAuthoringUncertainSaves === 'object'
    && !Array.isArray(retainedAuthoringUncertainSaves)
    ? Object.entries(retainedAuthoringUncertainSaves).filter(([, recovery]) => recovery
      && typeof recovery === 'object' && !Array.isArray(recovery)) : []
  const retainedAuthoringDamagedRecoveryValues = retainedAuthoringDamagedRecoveries
    && typeof retainedAuthoringDamagedRecoveries === 'object'
    && !Array.isArray(retainedAuthoringDamagedRecoveries)
    ? Object.values(retainedAuthoringDamagedRecoveries).filter(recovery => recovery
      && typeof recovery === 'object' && !Array.isArray(recovery)) : []
  const retainedAuthoringFenceActive = panel === 'authoring'
    && (generationMismatch || generationPending)
  const retainedAuthoringStorageRecovery = retainedAuthoringFenceActive
    ? inspectAuthoringRecoveryStorage()
    : { available: true, count: 0, records: [] }
  const retainedAuthoringRecoveryIdentities = new Map()
  const addRetainedAuthoringRecovery = (key, raw, source) => {
    if (typeof key !== 'string' || typeof raw !== 'string') return false
    let byRaw = retainedAuthoringRecoveryIdentities.get(key)
    if (!byRaw) {
      byRaw = new Map()
      retainedAuthoringRecoveryIdentities.set(key, byRaw)
    }
    const previous = byRaw.get(raw) || { durable: false, memory: false }
    byRaw.set(raw, {
      durable: previous.durable || source === 'storage',
      memory: previous.memory || source === 'memory',
    })
    return true
  }
  const retainedAuthoringMemoryRecoveryDocumentScopes = new Set()
  const retainedAuthoringHasMemoryRecovery = retainedAuthoringUncertainSaveEntries.length > 0
    || retainedAuthoringDamagedRecoveryValues.length > 0
    || retainedAuthoringDocumentEntries.some(([, document]) =>
      document.recoveryOperationId || document.recoveryStorageRaw)
  let retainedAuthoringOpaqueMemoryRecoveryCount = 0
  if (retainedAuthoringFenceActive) {
    for (const recovery of retainedAuthoringStorageRecovery.records) {
      addRetainedAuthoringRecovery(recovery.key, recovery.raw, 'storage')
    }
    for (const [scope, recovery] of retainedAuthoringUncertainSaveEntries) {
      retainedAuthoringMemoryRecoveryDocumentScopes.add(scope)
      addRetainedAuthoringRecovery(recovery.storageKey, recovery.storageRaw, 'memory')
    }
    for (const recovery of retainedAuthoringDamagedRecoveryValues) {
      if (typeof recovery.identity?.scope === 'string') {
        retainedAuthoringMemoryRecoveryDocumentScopes.add(recovery.identity.scope)
      }
      addRetainedAuthoringRecovery(recovery.key, recovery.raw, 'memory')
    }
    for (const [scope, document] of retainedAuthoringDocumentEntries) {
      if (!document.recoveryOperationId && !document.recoveryStorageRaw) continue
      const represented = addRetainedAuthoringRecovery(
        authoringRecoveryStorageKey(document.kind, document.name),
        document.recoveryStorageRaw,
        'memory',
      )
      if (!represented && !retainedAuthoringMemoryRecoveryDocumentScopes.has(scope)) {
        retainedAuthoringOpaqueMemoryRecoveryCount += 1
      }
    }
  }
  let retainedAuthoringDurableRecoveryCount = 0
  let retainedAuthoringMemoryOnlyRecoveryCount = retainedAuthoringFenceActive
    ? retainedAuthoringOpaqueMemoryRecoveryCount
    : retainedAuthoringHasMemoryRecovery ? 1 : 0
  for (const byRaw of retainedAuthoringRecoveryIdentities.values()) {
    for (const recovery of byRaw.values()) {
      if (recovery.durable) retainedAuthoringDurableRecoveryCount += 1
      else if (recovery.memory) retainedAuthoringMemoryOnlyRecoveryCount += 1
    }
  }
  const retainedAuthoringRecoveryCount = retainedAuthoringDurableRecoveryCount
    + retainedAuthoringMemoryOnlyRecoveryCount
  const retainedAuthoringDraftUnsafe = retainedAuthoringDraftCount > 0
    || retainedAuthoringRecoveryCount > 0
    || (activePanelNavigationGuard?.route === 'authoring'
      && activePanelNavigationGuard.unsafe === true)
  const retainedPanelDraftUnsafe = retainedConfigDraftUnsafe || retainedAuthoringDraftUnsafe
  const retainedPanelScope = retainedConfigDraftUnsafe
    ? retainedConfigScope : retainedAuthoringDraftUnsafe ? retainedAuthoringScope : ''
  const retainedPanelRoute = retainedConfigDraftUnsafe
    ? 'config' : retainedAuthoringDraftUnsafe ? 'authoring' : null
  const retainedAuthoringDiscardItems = [
    retainedAuthoringDraftCount > 0
      ? `${retainedAuthoringDraftCount} unsaved in-memory Authoring draft${retainedAuthoringDraftCount === 1 ? '' : 's'}` : '',
    retainedAuthoringMemoryOnlyRecoveryCount > 0
      ? `${retainedAuthoringMemoryOnlyRecoveryCount} recovery snapshot${retainedAuthoringMemoryOnlyRecoveryCount === 1 ? '' : 's'} that ${retainedAuthoringMemoryOnlyRecoveryCount === 1 ? 'exists' : 'exist'} only in this tab` : '',
  ].filter(Boolean)
  const retainedAuthoringDiscardStatement = retainedAuthoringDiscardItems.length > 0
    ? `Leaving this run will discard ${retainedAuthoringDiscardItems.join(' and ')}.`
    : 'No in-memory Authoring draft will be discarded.'
  const retainedPanelLeaveSummary = activePanelNavigationGuard?.unsafe
      && activePanelNavigationGuard.route === retainedPanelRoute
      && activePanelNavigationGuard.leaveSummary
    ? activePanelNavigationGuard.leaveSummary
    : retainedConfigDraftUnsafe
      ? 'Leaving this run will discard an unsaved Run settings draft.'
      : `${retainedAuthoringDiscardStatement}${retainedAuthoringDurableRecoveryCount > 0
        ? ` ${retainedAuthoringDurableRecoveryCount} durable recovery record${retainedAuthoringDurableRecoveryCount === 1 ? '' : 's'} will remain protected in browser storage.`
        : ''}`
  const retainedPanelLeaveMessage = `${retainedPanelLeaveSummary} Leave this run?`
  const retainedPanelCloseMessage = activePanelNavigationGuard?.unsafe
      && activePanelNavigationGuard.route === retainedPanelRoute
      && activePanelNavigationGuard.closeMessage
    ? activePanelNavigationGuard.closeMessage
    : retainedConfigDraftUnsafe
      ? 'This tab is retaining an unsaved Run settings draft. Close the panel and discard it?'
      : `${retainedAuthoringDiscardItems.length > 0
        ? `Closing Authoring will discard ${retainedAuthoringDiscardItems.join(' and ')}.`
        : 'No in-memory Authoring draft will be discarded.'}${retainedAuthoringDurableRecoveryCount > 0
        ? ` ${retainedAuthoringDurableRecoveryCount} durable recovery record${retainedAuthoringDurableRecoveryCount === 1 ? '' : 's'} will remain protected in browser storage.`
        : ''} Close Authoring?`
  const retainedCommentLeaveMessage = [
    retainedCommentDrafts.length > 0
      ? `${retainedCommentDrafts.length} unsaved comment draft${retainedCommentDrafts.length === 1 ? '' : 's'} will leave this in-memory workspace`
      : '',
    retainedCommentDurableCount > 0
      ? `${retainedCommentDurableCount} exact comment recovery record${retainedCommentDurableCount === 1 ? '' : 's'} will remain protected in this browser tab`
      : '',
    retainedCommentEntries.length > retainedCommentDrafts.length
      ? `${retainedCommentEntries.length - retainedCommentDrafts.length} other active Comments state${retainedCommentEntries.length - retainedCommentDrafts.length === 1 ? '' : 's'} will leave the in-memory workspace`
      : '',
    retainedCommentRecoveryUnavailable
      ? 'Comments recovery storage cannot be inspected, so an exact saved command may still be protected in this tab'
      : '',
  ].filter(Boolean).join('; ')
  const retainedRunLeaveMessage = [
    retainedPanelDraftUnsafe ? retainedPanelLeaveSummary : '',
    retainedCommentWorkUnsafe ? retainedCommentLeaveMessage : '',
  ].filter(Boolean).join(' ') + ' Leave this run?'
  const retainedNavigationAllowRef = useRef(false)
  const clearRetainedCommentMemory = () => {
    for (const scope of retainedCommentScopes) inspectorDraftStoreRef.current.clear(scope)
  }
  const clearRetainedPanelMemory = () => {
    const controller = panelNavigationGuardRef.current
    if (controller?.route === retainedPanelRoute) {
      controller.dispose()
      if (panelNavigationGuardRef.current === controller) {
        panelNavigationGuardRef.current = null
        setPanelNavigationGuard(current => current === controller ? null : current)
      }
    }
    if (retainedPanelDraftUnsafe && retainedPanelScope) {
      inspectorDraftStoreRef.current.clear(retainedPanelScope)
    }
  }
  const retainedNavigationTarget = targetHash => {
    const runHash = `#/run/${encodeURIComponent(String(runId))}`
    const sameRun = targetHash === runHash || targetHash.startsWith(`${runHash}?`)
    if (!sameRun) return { sameRun: false, panel: null, keepsMutablePanel: false }
    const queryIndex = targetHash.indexOf('?')
    const params = queryIndex < 0
      ? new URLSearchParams() : new URLSearchParams(targetHash.slice(queryIndex + 1))
    const targetPanels = params.getAll('panel')
    const targetGenerations = params.getAll('gen')
    const targetPanel = targetPanels.length === 1 ? targetPanels[0] : null
    const targetGeneration = targetGenerations.length === 1 ? targetGenerations[0] : null
    const keepsMutablePanel = targetPanels.length === 1 && targetGenerations.length === 1
      && targetPanel === retainedPanelRoute && !params.has('seq')
      && targetGeneration === generation
    return { sameRun: true, panel: targetPanel, keepsMutablePanel }
  }
  const retainedNavigationShouldBlock = targetHash => {
    const target = retainedNavigationTarget(targetHash)
    if (!target.sameRun) return retainedPanelDraftUnsafe || retainedCommentWorkUnsafe
    return retainedPanelDraftUnsafe && !target.keepsMutablePanel
  }
  const retainedGuardedHash = location.hash
  const retainedGuardedHistoryState = window.history.state
  useEffect(() => {
    retainedNavigationAllowRef.current = false
    if (!retainedPanelDraftUnsafe && !retainedCommentWorkUnsafe) return undefined
    return installNavigationLossGuard({
      allowRef: retainedNavigationAllowRef,
      guardedHash: retainedGuardedHash,
      guardedState: retainedGuardedHistoryState,
      message: targetHash => retainedNavigationTarget(targetHash).sameRun
        ? retainedPanelCloseMessage : retainedRunLeaveMessage,
      shouldBlock: retainedNavigationShouldBlock,
      onAllow: targetHash => {
        const target = retainedNavigationTarget(targetHash)
        if (retainedPanelDraftUnsafe && !target.keepsMutablePanel) {
          clearRetainedPanelMemory()
        }
        if (!target.sameRun && retainedCommentWorkUnsafe) clearRetainedCommentMemory()
      },
    })
  }, [runId, generation, retainedCommentWorkUnsafe, retainedRunLeaveMessage, retainedPanelCloseMessage,
    retainedCommentScopes.join('\u0000'), retainedPanelDraftUnsafe, retainedPanelRoute,
    retainedPanelScope, retainedGuardedHash, retainedGuardedHistoryState])
  const confirmRetainedPanelClose = () => {
    if (!retainedPanelDraftUnsafe) return true
    if (!window.confirm(retainedPanelCloseMessage)) return false
    retainedNavigationAllowRef.current = true
    clearRetainedPanelMemory()
    return true
  }
  const leaveRetainedPanelRoute = () => {
    if (!retainedPanelDraftUnsafe && !retainedCommentWorkUnsafe) { onBack?.(); return }
    if (!window.confirm(retainedRunLeaveMessage)) return
    if (retainedPanelDraftUnsafe) {
      retainedNavigationAllowRef.current = true
      clearRetainedPanelMemory()
    }
    if (retainedCommentWorkUnsafe) {
      retainedNavigationAllowRef.current = true
      clearRetainedCommentMemory()
    }
    onBack?.()
  }
  const requestedRouteView = routeState.view
  // The obsolete Direction focus route is retired by runRouteState with a visible migration notice.
  // Keep this compatibility argument null until the old aggregate API is removed; Concepts owns the
  // only active semantic filter and is propagated separately through conceptHighlight.
  const themeFilter = null
  const [groupNavigation, setGroupNavigation] = useState(
    () => readRunGroupNavigation(runId))
  const groupNavigationRef = useRef(groupNavigation)
  groupNavigationRef.current = groupNavigation
  const selectedGroup = groupNavigation?.key ?? null
  const routeFenceBlocked = generationMismatch || generationPending
  const historyActive = viewSeq != null
  const landedRef = useRef(false)                            // auto-land on Report once, on finish
  const deepLinkLandingRef = useRef(route.hadState)          // even invalid state must not be hidden by auto-report
  const liveTerminalReady = terminalReady(live || {})
  // Choose Report in the same render that first observes terminal readiness. Waiting for the passive
  // URL effect would start DAG chunks and the owner log-page request for a surface never displayed.
  const autoReport = !historyActive && liveTerminalReady && !landedRef.current
    && !deepLinkLandingRef.current && !groupNavigation
    && !runRouteStateHasTarget(routeState, { reviewMode })
  // review capabilities do not expose concept frames. Owner history does, through an
  // exact seq; redirect only the unsupported review route instead of hiding owner history.
  const requestedView = reviewMode && requestedRouteView === 'concepts' ? 'dag' : requestedRouteView
  const view = autoReport ? 'report' : requestedView
  const routeMainRef = useRef(null)
  const workspaceToolbarRef = useRef(null)
  const [workspaceTabStop, setWorkspaceTabStop] = useState(view)
  useEffect(() => {
    setWorkspaceTabStop(view)
  }, [view, historyActive, reviewMode])
  const onWorkspaceToolbarKeyDown = event => {
    const controls = [...(workspaceToolbarRef.current
      ?.querySelectorAll('[data-workspace-control]:not(:disabled)') || [])]
    const current = event.target.closest?.('[data-workspace-control]')
    const next = nextRovingIndex(event.key, controls.indexOf(current), controls.length)
    if (next == null) return
    event.preventDefault()
    setWorkspaceTabStop(controls[next].dataset.workspaceControl)
    controls[next].focus({ preventScroll: true })
  }
  const onWorkspaceToolbarFocus = event => {
    const key = event.target.closest?.('[data-workspace-control]')?.dataset.workspaceControl
    if (key) setWorkspaceTabStop(key)
  }
  const [timelineActivation, setTimelineActivation] = useState({
    runId: null, active: false, focusPending: false,
  })
  const reportTimelineActivated = timelineActivation.runId === runId && timelineActivation.active
  const reportTimelineFocusPending = timelineActivation.runId === runId
    && timelineActivation.focusPending
  // A finished run's primary lifecycle actions, including Start over, live in Dock. Keep them visible
  // on the default Report landing instead of hiding them behind an unrelated diagnostics expander.
  const timelineDeferred = view === 'report' && panel !== 'events' && !reportTimelineActivated
    && !live?.finished
  // One owner-only paged controller feeds both Dock and EventExplorer. Opening the explorer therefore
  // adds no second raw-log scan, cursor stream, retained window, or competing generation fence.
  const timeline = useTimeline(runId, {
    liveSeq: seq, liveEventCount, expectedGeneration: generation,
    enabled: !reviewMode && !routeFenceBlocked && !timelineDeferred,
  })
  const compactWorkspace = useMediaQuery('(max-width: 900px)')
  const [compactGoalExpanded, setCompactGoalExpanded] = useState(false)
  useEffect(() => setCompactGoalExpanded(false), [runId, generation, compactWorkspace])
  const [history, setHistory] = useState(liveHistory)
  const [historyRetry, setHistoryRetry] = useState(0)
  const readOnlyMode = reviewMode || historyActive || routeFenceBlocked
  // A route id is not mutation authority. Keep every persistent control fail-closed until the
  // resource has supplied both the current run state and its generation; 404/410 and transport
  // failures must never inherit the previous route's live access.
  const runAuthorityBlocked = runStatus !== 'ready' || !live || !generation
  const mutationReadOnlyMode = readOnlyMode || runAuthorityBlocked || startOverMutationBlocked
  const mutationReadOnlyReason = reviewMode ? 'review'
    : startOverMutationBlocked ? 'start-over' : 'history'
  const reviewEvidence = reviewMode && (reviewMeta?.scopes || []).includes('evidence')
  const panelAllowed = (name) => {
    if (reviewMode) return reviewPanelAllowed(name, reviewEvidence)
    if (historyActive) return HISTORY_SAFE_PANELS.has(name)
    if (startOverMutationBlocked) return START_OVER_SAFE_PANELS.has(name)
    return true
  }
  const routeWithSelectedNode = (current, next) => ({
    ...current,
    nodeId: next,
    nodeGeneration: next === current.nodeId ? current.nodeGeneration : null,
    inspectTab: next == null ? 'Overview' : current.inspectTab,
    commentId: next === current.nodeId ? current.commentId : null,
  })
  const setSelectedId = (value, options = {}) => route.update(current => {
    const next = typeof value === 'function' ? value(current.nodeId) : value
    return routeWithSelectedNode(current, next)
  }, options)
  const setInspectTab = (value, options = {}) => route.update(current => {
    const next = typeof value === 'function' ? value(current.inspectTab) : value
    return { ...current, inspectTab: next,
      nodeGeneration: current.nodeGeneration,
      commentId: next === 'Comments' ? current.commentId : null }
  }, options)
  const setPanel = (value, options = {}) => {
    const nextPanel = typeof value === 'function' ? value(routeState.panel) : value
    if (retainedPanelDraftUnsafe && routeState.panel === retainedPanelRoute
        && nextPanel !== retainedPanelRoute && !confirmRetainedPanelClose()) return routeState
    return route.update(current => ({ ...current, panel: nextPanel }), options)
  }
  const setView = (value, options = {}) => route.update(current => ({
    ...current, view: typeof value === 'function' ? value(current.view) : value,
  }), options)
  const changeViewSeq = (next) => {
    if (reviewMode || routeFenceBlocked) return routeState
    const value = Number(next)
    const n = next == null || value >= seq ? null
      : Number.isSafeInteger(value) && value >= 0 ? value : null
    return route.update(current => ({ ...current, sequence: n }))
  }
  const returnToLive = () => {
    const nextRoute = changeViewSeq(null)
    // A failed push leaves the historical address authoritative. Keep its notices and timeline state
    // intact too, otherwise the visible workspace would claim to be live while reload remains historical.
    if (nextRoute?.sequence != null) return false
    // Historical route warnings describe the snapshot address that produced them. Once the user
    // explicitly returns live, keeping that copy would misdescribe the visible state.
    if (historyActive) {
      setRouteNotice('')
      setAttemptFenceNotice('')
      attemptFenceFocusPendingRef.current = false
    }
    timeline.jumpToLive()
    return true
  }
  const returnToLiveAndFocusWorkspace = () => {
    if (!returnToLive()) return
    requestAnimationFrame(() => routeMainRef.current?.focus({ preventScroll: true }))
  }
  useLayoutEffect(() => {
    // The mutation boundary follows URL hydration too. A generation-fenced link is fail-closed until
    // current truth confirms it. Publish in the same layout-effect phase as useRunState's observed
    // generation so the persistent Assistant/Dock cannot see generation B with generation A's old
    // live access during the commit-to-passive-effect window.
    setRunAccess(runId, { readOnly: mutationReadOnlyMode, seq: viewSeq,
      mode: reviewMode ? 'review' : startOverMutationBlocked ? 'start-over'
        : routeFenceBlocked ? 'stale-link' : historyActive ? 'history'
          : runStatus === 'loading' ? 'loading' : runAuthorityBlocked ? 'unavailable' : 'live' })
    // An unresolved destructive operation outlives this route. Leave its published access lock in
    // place when navigating Back; getRunAccess also reconstructs it from session storage after reload.
    return () => { if (!startOverMutationBlocked) clearRunAccess(runId) }
  }, [runId, reviewMode, mutationReadOnlyMode, viewSeq, historyActive, routeFenceBlocked,
    runStatus, runAuthorityBlocked, startOverMutationBlocked])
  const [toast, showToast] = useToast()   // shared timer discipline (doc 25 UI-13)
  const [routeNotice, setRouteNotice] = useState('')
  const [attemptFenceNotice, setAttemptFenceNotice] = useState('')
  const routeNoticeRef = useRef(null)
  const attemptFenceFocusPendingRef = useRef(false)
  // Route warnings belong only to the address that produced them. Back/Forward or a newly opened
  // deep link starts a fresh navigation; route.update() intentionally does not bump this value, so a
  // mismatch handler can canonicalize its own URL without erasing the warning it just raised.
  useLayoutEffect(() => {
    setRouteNotice('')
    setAttemptFenceNotice('')
  }, [route.navigationRevision])
  const [copyFallback, setCopyFallback] = useState('')
  const [groupModePreference, setGroupModePreference] = useState('theme')
  const [collapsed, setCollapsed] = useState(() => new Set())
  const groupMode = groupNavigation?.mode ?? groupModePreference
  const groupSurfaceReady = runStatus === 'ready' && !!live && !routeFenceBlocked
    && (!historyActive || (historyMatches(history, viewSeq, generation) && history.status === 'ready'))
  const [groupExitFocus, setGroupExitFocus] = useState(null)
  const groupExitFocusAppliedRef = useRef(null)
  const groupSurfaceFocusAppliedRef = useRef(null)
  const groupInspectorFocusSequenceRef = useRef(0)
  const groupInspectorFocusAppliedRef = useRef(null)
  const [groupInspectorFocusRequest, setGroupInspectorFocusRequest] = useState(null)
  const commitGroupNavigation = (next, { mode = 'replace', focusExit = false } = {}) => {
    const normalized = next
      ? normalizeRunGroupNavigation(next, runId, generation) : null
    const previous = groupNavigationRef.current
    if (focusExit && previous?.key) {
      setGroupExitFocus({ mode: previous.mode, key: previous.key })
    } else if (normalized) setGroupExitFocus(null)
    if (!normalized) setGroupInspectorFocusRequest(null)
    writeRunGroupNavigation(normalized, mode)
    groupNavigationRef.current = normalized
    setGroupNavigation(normalized)
    if (normalized?.mode) setGroupModePreference(normalized.mode)
    return normalized
  }
  useEffect(() => {
    const hydrateGroupNavigation = () => {
      const previous = groupNavigationRef.current
      const browserState = window.history.state
      const hasSnapshot = !!browserState && typeof browserState === 'object'
        && !Array.isArray(browserState)
        && Object.prototype.hasOwnProperty.call(browserState, RUN_GROUP_HISTORY_KEY)
      const next = readRunGroupNavigation(runId, generation)
      if (hasSnapshot && !next) writeRunGroupNavigation(null, 'replace')
      const previousBelongsHere = previous?.runId === String(runId)
        && (!generation || previous.generation === generation)
      if (previous?.key && previousBelongsHere && !next) {
        setGroupExitFocus({ mode: previous.mode, key: previous.key })
      } else if (next) setGroupExitFocus(null)
      if (!next) setGroupInspectorFocusRequest(null)
      groupNavigationRef.current = next
      setGroupNavigation(next)
      if (next?.mode) setGroupModePreference(next.mode)
    }
    hydrateGroupNavigation()
    window.addEventListener('popstate', hydrateGroupNavigation)
    return () => window.removeEventListener('popstate', hydrateGroupNavigation)
  }, [runId, generation])
  useLayoutEffect(() => {
    if (!groupNavigation) return
    setGroupModePreference(groupNavigation.mode)
    setCollapsed(current => current.has(groupNavigation.key)
      ? current : new Set(current).add(groupNavigation.key))
  }, [groupNavigation?.mode, groupNavigation?.key])
  // View 2: the set of graph nodes matching the concept-chip-bar selection (null = no concept filter).
  // Set by ConceptChipBar, consumed by the Dag to dim non-matching nodes. setState is a stable setter.
  const [conceptHighlight, setConceptHighlight] = useState(null)
  const graphPreferenceTouchedRef = useRef(false)
  const largeOverviewAppliedRef = useRef(false)
  const liveNodeCount = Object.keys(live?.nodes || {}).length
  const selectedLiveGroupValid = useMemo(() => {
    if (!groupNavigation || !generation || groupNavigation.generation !== generation || !live?.nodes) {
      return false
    }
    return computeGroups(live.nodes, groupNavigation.mode, live).has(groupNavigation.key)
  }, [live, generation, groupNavigation?.generation, groupNavigation?.mode, groupNavigation?.key])
  // Theme labels are agent-authored and often fragment a large search into dozens of one-off groups.
  // Start a previously untouched 80+ node run as a small operator-aggregate overview. It is truthful,
  // bounded, and fully reversible through the group selector / Expand all.
  useLayoutEffect(() => {
    if (largeOverviewAppliedRef.current || graphPreferenceTouchedRef.current) return
    const decision = initialDagOverviewDecision({
      ready: runStatus === 'ready',
      nodeCount: liveNodeCount,
      // A direct node/history route keeps its exact canvas. Group drill-down already declares its
      // grouping mode; for the large-run operator overview, rebuilding the normal collapsed overview
      // is more faithful than reloading one collapsed group amid dozens of tiny expanded cards.
      explicitContext: historyActive || (selectedId != null && !selectedLiveGroupValid)
        || (selectedLiveGroupValid && groupMode !== 'operator'),
    })
    if (decision === 'wait') return
    // Decide exactly once from the first authoritative non-empty snapshot. A run first seen at 79 nodes
    // must not jump modes when live node 80 arrives, and an exact route must retain its camera context.
    largeOverviewAppliedRef.current = true
    if (decision === 'preserve') return
    const groups = computeGroups(live.nodes, 'operator', live)
    if (!groups.size) return
    setGroupModePreference('operator')
    setCollapsed(new Set(groups.keys()))
  }, [live, liveNodeCount, runStatus, selectedId, selectedGroup, selectedLiveGroupValid,
    groupMode, historyActive])
  useEffect(() => {
    if (panel && !panelAllowed(panel)) {
      const reason = historyActive
        ? `${panel} is not snapshot-safe and was not opened with historical data.`
        : startOverMutationBlocked
          ? `${panel} is unavailable while Start over is being resolved.`
          : `${panel} is not included in this review link.`
      setRouteNotice(current => [current, reason].filter(Boolean).join(' '))
      setPanel(null, { mode: 'replace', preserveIssues: true, forceGeneration: true })
    }
  }, [panel, reviewMode, reviewEvidence, historyActive, startOverMutationBlocked])
  const panelReturnFocusRef = useRef(null)
  const panelBackCloseRef = useRef(false)
  const hubTriggerRef = useRef(null)
  const hubMenuRef = useRef(null)
  const closePanel = () => {
    if (panelBackCloseRef.current) return false
    if (!confirmRetainedPanelClose()) return false
    // App-opened panels own one marked history entry, so dismissing them should consume that entry.
    // A panel reached from a copied/deep link has no marker and is safely dismissed in place instead.
    const panelEntry = takeRunPanelHistoryEntry()
    if (panelEntry?.version === 1 && window.history.length > 1) {
      panelBackCloseRef.current = true
      window.history.back()
      return true
    }
    route.update(current => ({ ...current, panel: null }), {
      mode: 'replace', forceGeneration: true,
    })
    return true
  }
  const [openHub, setOpenHub] = useState(null)               // which panel-hub dropdown is open
  const closeHub = (restoreFocus = false) => {
    setOpenHub(null)
    if (restoreFocus) requestAnimationFrame(() => {
      const target = hubTriggerRef.current
      if (target?.isConnected) target.focus({ preventScroll: true })
    })
  }
  const onHubKeyDown = event => {
    if (event.key === 'Escape') { event.preventDefault(); closeHub(true); return }
    // Menu items are roving-focus targets rather than Tab stops. Shift+Tab therefore returns to the
    // owning trigger; close first so the trigger never retains focus behind a stale open backdrop.
    if (event.key === 'Tab' && event.shiftKey) { event.preventDefault(); closeHub(true); return }
    const items = [...(hubMenuRef.current?.querySelectorAll('[role="menuitem"]:not(:disabled)') || [])]
    const next = nextRovingIndex(event.key, Math.max(0, items.indexOf(document.activeElement)), items.length)
    if (next == null) return
    event.preventDefault(); items[next]?.focus()
  }
  useEffect(() => {
    if (!openHub) return
    requestAnimationFrame(() => hubMenuRef.current?.querySelector('[role="menuitem"]:not(:disabled)')?.focus({ preventScroll: true }))
  }, [openHub])
  // Resizable / collapsible panes (standard multi-pane layout), persisted across sessions.
  const [sideW, setSideW] = useState(() => +storageGet('ll.sideW', 420) || 420)
  const [dockH, setDockH] = useState(() => +storageGet('ll.dockH', 230) || 230)
  const [sideC, setSideC] = useState(() => storageGet('ll.sideC') === '1')
  // A new workspace starts canvas-first; an explicit prior open/collapsed choice is preserved.
  const [dockC, setDockC] = useState(() => storageGet('ll.dockC', '1') === '1')
  // Dock remains the sole owner of durable run-command recovery. It publishes a reactive controller
  // only after its exact run/generation render commits, allowing the zero-node canvas CTA to reuse the
  // same command identity, lock, persistence, and observation path without duplicating transport.
  const [transportController, setTransportController] = useState(null)
  // Desktop panes keep their persisted layout. On tablet/mobile they become temporary surfaces so
  // neither Inspector nor Timeline permanently taxes the graph. Compact state is intentionally not
  // persisted: every narrow-screen visit starts with the canvas unobstructed.
  const [compactInspectorOpen, setCompactInspectorOpen] = useState(false)
  const [compactTimelineOpen, setCompactTimelineOpen] = useState(false)
  const overlayPanelOpen = !!(panel && panelAllowed(panel))
  const timelineCollapsed = compactWorkspace ? !compactTimelineOpen : dockC
  const previousCompactWorkspaceRef = useRef(compactWorkspace)
  const compactInspectorCloseRef = useRef(null)
  const compactInspectorTriggerRef = useRef(null)
  const compactInspectorRef = useRef(null)
  const sideRailRef = useRef(null)
  const timelineCollapseRef = useRef(null)
  const workspaceFocusOwnerRef = useRef(null)
  const focusInspectorFromGroup = (nodeId) => {
    const targetId = Number(nodeId)
    if (!Number.isSafeInteger(targetId) || targetId < 0) return
    setGroupInspectorFocusRequest({
      id: targetId, sequence: ++groupInspectorFocusSequenceRef.current,
    })
  }
  const collapseSideInspector = () => {
    setSideC(true)
    requestAnimationFrame(() => sideRailRef.current?.focus({ preventScroll: true }))
  }
  const closeCompactInspector = () => {
    setCompactInspectorOpen(false)
    requestAnimationFrame(() => {
      const target = compactInspectorTriggerRef.current
        || [...(document.querySelectorAll('[data-node-select-id]') || [])]
          .find(element => element.dataset.nodeSelectId === String(selectedId))
      target?.focus({ preventScroll: true })
    })
  }
  // This drawer intentionally coexists with the header and timeline. Give it dialog navigation
  // (initial focus, Escape, focus restoration) without claiming or enforcing modal containment.
  useDialogFocus(compactInspectorRef, closeCompactInspector,
    compactWorkspace && compactInspectorOpen && !overlayPanelOpen,
    { modal: false, priority: DIALOG_PRIORITY.NONMODAL })
  useLayoutEffect(() => {
    if (!groupSurfaceReady || !compactWorkspace || !compactInspectorOpen || overlayPanelOpen) return
    // Route hydration can schedule main-focus before the drawer commit. Reassert the explicit
    // destination after that commit without turning this coexisting surface back into a modal.
    const frame = requestAnimationFrame(() => {
      const surface = compactInspectorRef.current
      if (!surface || surface.contains(document.activeElement)) return
      const trueModal = document.querySelector('[aria-modal="true"]')
      if (trueModal && !surface.contains(trueModal)) return
      ;(surface.querySelector('[data-dialog-initial-focus]') || surface)
        .focus({ preventScroll: true })
    })
    return () => cancelAnimationFrame(frame)
  }, [groupSurfaceReady, compactWorkspace, compactInspectorOpen, overlayPanelOpen,
    route.navigationRevision])
  useLayoutEffect(() => {
    if (!groupSurfaceReady || !groupInspectorFocusRequest
        || groupInspectorFocusAppliedRef.current === groupInspectorFocusRequest
        || selectedId !== groupInspectorFocusRequest.id
        || view !== 'dag' || overlayPanelOpen) return
    const request = groupInspectorFocusRequest
    let cancelled = false
    let userInteracted = false
    let attempts = 0
    let stableFrames = 0
    let lastTarget = null
    const onUserIntent = () => { userInteracted = true }
    const stopTrackingUserIntent = () => {
      window.removeEventListener('keydown', onUserIntent, true)
      window.removeEventListener('pointerdown', onUserIntent, true)
    }
    const finish = () => {
      groupInspectorFocusAppliedRef.current = request
      stopTrackingUserIntent()
    }
    window.addEventListener('keydown', onUserIntent, true)
    window.addEventListener('pointerdown', onUserIntent, true)
    const focus = () => {
      if (cancelled) return
      if (userInteracted) { finish(); return }
      const surface = compactInspectorRef.current
      const target = surface?.querySelector('[role="tab"][aria-selected="true"]')
        || surface?.querySelector('.insp-body')
      if (target && !document.querySelector('[aria-modal="true"]')) {
        if (target !== lastTarget || document.activeElement !== target) {
          target.focus({ preventScroll: true })
          stableFrames = 0
          lastTarget = target
        } else {
          stableFrames += 1
        }
        if (document.activeElement === target && stableFrames >= 12) {
          finish()
          return
        }
      }
      attempts += 1
      if (attempts < 120) requestAnimationFrame(focus)
      else finish()
    }
    const frame = requestAnimationFrame(focus)
    return () => { cancelled = true; cancelAnimationFrame(frame); stopTrackingUserIntent() }
  }, [groupSurfaceReady, groupInspectorFocusRequest, selectedId, view, overlayPanelOpen])
  useLayoutEffect(() => {
    if (!groupSurfaceReady || view !== 'dag' || selectedId != null
        || !groupNavigation || groupSurfaceFocusAppliedRef.current === groupNavigation
        || overlayPanelOpen) return
    const navigation = groupNavigation
    if (compactWorkspace) setCompactInspectorOpen(true)
    else setSideC(false)
    let cancelled = false
    let userInteracted = false
    let attempts = 0
    const onUserIntent = () => { userInteracted = true }
    const stopTrackingUserIntent = () => {
      window.removeEventListener('keydown', onUserIntent, true)
      window.removeEventListener('pointerdown', onUserIntent, true)
    }
    const finish = () => {
      groupSurfaceFocusAppliedRef.current = navigation
      stopTrackingUserIntent()
    }
    window.addEventListener('keydown', onUserIntent, true)
    window.addEventListener('pointerdown', onUserIntent, true)
    const focus = () => {
      if (cancelled) return
      if (userInteracted) { finish(); return }
      const surface = compactInspectorRef.current
      const body = surface?.querySelector('.insp-body')
      const member = navigation.returnNodeId == null ? null
        : [...(surface?.querySelectorAll('[data-group-member-id]') || [])]
          .find(element => element.dataset.groupMemberId === String(navigation.returnNodeId))
      const target = member || surface?.querySelector('[data-group-summary-title]')
      const modalBlocksFocus = !!document.querySelector('[aria-modal="true"]')
      if (!surface || !target || modalBlocksFocus) {
        attempts += 1
        if (attempts < 120) requestAnimationFrame(focus)
        else finish()
        return
      }
      if (body) body.scrollTop = Math.min(navigation.scrollTop, Math.max(0, body.scrollHeight - body.clientHeight))
      target.focus({ preventScroll: true })
      if (member && body) {
        const memberRect = member.getBoundingClientRect()
        const bodyRect = body.getBoundingClientRect()
        if (memberRect.top < bodyRect.top || memberRect.bottom > bodyRect.bottom) {
          member.scrollIntoView({ block: 'nearest', inline: 'nearest' })
        }
      }
      finish()
    }
    const frame = requestAnimationFrame(focus)
    return () => { cancelled = true; cancelAnimationFrame(frame); stopTrackingUserIntent() }
  }, [view, selectedId, groupNavigation, groupSurfaceReady, compactWorkspace, overlayPanelOpen])
  useLayoutEffect(() => {
    if (!groupSurfaceReady || groupNavigation || !groupExitFocus
        || groupExitFocusAppliedRef.current === groupExitFocus
        || selectedId != null || view !== 'dag') return
    const returning = groupExitFocus
    let cancelled = false
    let userInteracted = false
    let attempts = 0
    let stableFrames = 0
    let lastTarget = null
    const onUserIntent = () => { userInteracted = true }
    const stopTrackingUserIntent = () => {
      window.removeEventListener('keydown', onUserIntent, true)
      window.removeEventListener('pointerdown', onUserIntent, true)
    }
    const finish = () => {
      groupExitFocusAppliedRef.current = returning
      stopTrackingUserIntent()
    }
    window.addEventListener('keydown', onUserIntent, true)
    window.addEventListener('pointerdown', onUserIntent, true)
    const focus = () => {
      if (cancelled) return
      if (userInteracted) { finish(); return }
      const target = [...(document.querySelectorAll('[data-group-select-key]') || [])]
        .find(element => element.dataset.groupSelectKey === returning.key)
      if (target) {
        if (target !== lastTarget || document.activeElement !== target) {
          if (target.dataset.restoredFocus !== 'true') {
            target.dataset.restoredFocus = 'true'
            const clearMarker = () => { if (target.isConnected) delete target.dataset.restoredFocus }
            target.addEventListener('blur', clearMarker, { once: true })
            target.addEventListener('pointerdown', clearMarker, { once: true })
          }
          target.focus({ preventScroll: true })
          stableFrames = 0
          lastTarget = target
        } else {
          stableFrames += 1
        }
        if (document.activeElement === target && stableFrames >= 12) {
          finish()
          return
        }
      }
      attempts += 1
      if (attempts < 120) {
        requestAnimationFrame(focus)
        return
      }
      routeMainRef.current?.focus({ preventScroll: true })
      finish()
    }
    const frame = requestAnimationFrame(focus)
    return () => { cancelled = true; cancelAnimationFrame(frame); stopTrackingUserIntent() }
  }, [groupSurfaceReady, groupNavigation, groupExitFocus, selectedId, view, groupMode])
  useEffect(() => {
    const rememberWorkspaceFocus = event => {
      const target = event.target
      if (compactInspectorTriggerRef.current?.contains(target)) {
        workspaceFocusOwnerRef.current = 'compact-inspector-trigger'
      } else if (sideRailRef.current?.contains(target)) {
        workspaceFocusOwnerRef.current = 'desktop-side-rail'
      } else if (target.closest?.('.workspace-scrim')) {
        workspaceFocusOwnerRef.current = 'inspector-surface'
      } else if (target.closest?.('.splitter.v')) {
        workspaceFocusOwnerRef.current = 'side-splitter'
      } else if (target.closest?.('.splitter.h')) {
        workspaceFocusOwnerRef.current = 'timeline-splitter'
      } else if (compactInspectorRef.current?.contains(target)) {
        workspaceFocusOwnerRef.current = 'inspector-surface'
      } else if (target.closest?.('#run-events-timeline')) {
        workspaceFocusOwnerRef.current = 'timeline-body'
      } else if (timelineCollapseRef.current?.contains(target)) {
        workspaceFocusOwnerRef.current = 'timeline-collapse'
      } else {
        workspaceFocusOwnerRef.current = null
      }
    }
    document.addEventListener('focusin', rememberWorkspaceFocus)
    return () => document.removeEventListener('focusin', rememberWorkspaceFocus)
  }, [])
  useEffect(() => {
    if (previousCompactWorkspaceRef.current === compactWorkspace) return
    const wasCompact = previousCompactWorkspaceRef.current
    previousCompactWorkspaceRef.current = compactWorkspace
    const focusOwner = workspaceFocusOwnerRef.current
    requestAnimationFrame(() => {
      if (overlayPanelOpen || document.querySelector('[aria-modal="true"]')) return
      const selectedNode = [...(document.querySelectorAll('[data-node-select-id]') || [])]
        .find(element => element.dataset.nodeSelectId === String(selectedId))
      let target = null
      if (focusOwner === 'timeline-splitter' || (focusOwner === 'timeline-body' && timelineCollapsed)) {
        target = timelineCollapseRef.current
      } else if (wasCompact && ['compact-inspector-trigger', 'inspector-surface'].includes(focusOwner)) {
        target = sideC
          ? sideRailRef.current
          : compactInspectorCloseRef.current || compactInspectorRef.current
      } else if (!wasCompact && ['desktop-side-rail', 'inspector-surface', 'side-splitter'].includes(focusOwner)) {
        target = compactInspectorTriggerRef.current || selectedNode
      }
      target?.focus({ preventScroll: true })
    })
    // Compact surfaces are temporary. Never replay stale open state after crossing the breakpoint.
    setCompactInspectorOpen(false)
    setCompactTimelineOpen(false)
  }, [compactWorkspace, overlayPanelOpen, selectedGroup, selectedId, sideC, timelineCollapsed])
  useEffect(() => {
    if (compactWorkspace) return
    storageSet('ll.sideW', sideW); storageSet('ll.dockH', dockH)
    storageSet('ll.sideC', sideC ? '1' : '0'); storageSet('ll.dockC', dockC ? '1' : '0')
  }, [sideW, dockH, sideC, dockC, compactWorkspace])
  const clampSide = (value) => Math.max(280, Math.min(Math.max(280, window.innerWidth - 486), value))
  const clampDock = (value) => Math.max(MIN_DOCK_HEIGHT,
    Math.min(Math.max(MIN_DOCK_HEIGHT, window.innerHeight - 470), value))
  useEffect(() => {
    if (compactWorkspace) return
    const clampPanes = () => {
      // A phone-sized resize must not overwrite the user's persisted desktop pane geometry.
      if (window.matchMedia?.('(max-width: 900px)').matches) return
      setSideW(w => clampSide(w)); setDockH(h => clampDock(h))
    }
    clampPanes()
    window.addEventListener('resize', clampPanes)
    return () => window.removeEventListener('resize', clampPanes)
  }, [compactWorkspace])
  // Drag a splitter: the side panel grows when dragged left, the dock when dragged up.
  const startDrag = (axis) => (e) => {
    e.preventDefault()
    const target = e.currentTarget, pointerId = e.pointerId
    target.setPointerCapture?.(pointerId)
    const x0 = e.clientX, y0 = e.clientY, w0 = sideW, h0 = dockH
    const onMove = (ev) => {
      if (axis === 'side') setSideW(clampSide(w0 - (ev.clientX - x0)))
      else setDockH(clampDock(h0 - (ev.clientY - y0)))
    }
    const onUp = () => {
      window.removeEventListener('pointermove', onMove); window.removeEventListener('pointerup', onUp)
      window.removeEventListener('pointercancel', onUp)
      if (target.hasPointerCapture?.(pointerId)) target.releasePointerCapture(pointerId)
      document.body.style.userSelect = ''; dragCleanupRef.current = null
    }
    dragCleanupRef.current = onUp   // unmount mid-drag must also detach the window listeners
    window.addEventListener('pointermove', onMove); window.addEventListener('pointerup', onUp)
    window.addEventListener('pointercancel', onUp)
    document.body.style.userSelect = 'none'
  }
  const resizeWithKeys = (axis) => (e) => {
    const sideDelta = e.key === 'ArrowLeft' ? 20 : e.key === 'ArrowRight' ? -20 : 0
    const dockDelta = e.key === 'ArrowUp' ? 20 : e.key === 'ArrowDown' ? -20 : 0
    const delta = axis === 'side' ? sideDelta : dockDelta
    if (!delta) return
    e.preventDefault()
    if (axis === 'side') setSideW(w => clampSide(w + delta))
    else setDockH(h => clampDock(h + delta))
  }
  const dragCleanupRef = useRef(null)
  useEffect(() => () => dragCleanupRef.current?.(), [])
  const configAuthority = reviewMode
    ? `review:${reviewMeta?.id || runId}`
    : `owner:${runId}`
  const configKey = runStatus === 'ready' && !routeFenceBlocked
    ? `${configAuthority}:${generation || 'pending'}`
    : null
  // The shared resource machine (doc 25 UI-06). The separate `retrying` flag this effect used to
  // carry is exactly `pending === 'retry'`: the only way to re-read the same config key is the Retry
  // control below. The notice renders no server text, so no failure classifier is passed.
  const configResource = useScopedResource(
    signal => get(runApiPath(runId, '/config'), { cache: 'no-store', signal }),
    { scope: configKey || '', gate: configKey ? null : 'idle',
      timeout: RUN_CONFIG_REQUEST_TIMEOUT_MS, deps: [runId] })
  // Auto-land only after terminal write-out genuinely completed. A run_finished(error) during an
  // explicit finalize is recovery state, not a completed report, and a still-live engine may still be
  // writing report/lessons/cost after the terminal event appeared.
  useEffect(() => {
    if (autoReport) {
      landedRef.current = true; setView('report')
    }
  }, [autoReport])
  useEffect(() => {
    if (viewSeq == null) { setHistory(liveHistory()); return }
    if (!generation || routeFenceBlocked || reviewMode) return
    let alive = true
    const want = Number(viewSeq)
    setHistory(requestHistory(want, generation))
    const request = deadlineGet(
      runApiPath(runId, `/state?seq=${want}`),
      HISTORICAL_SNAPSHOT_REQUEST_TIMEOUT_MS,
    )
    request.promise
      .then(p => { if (alive) setHistory(current => resolveHistory(current, want, generation, p)) })
      .catch(error => {
        if (!alive) return
        const failure = error?.name === 'TimeoutError'
          ? new Error('Historical snapshot loading timed out. Check the connection and retry.')
          : error
        setHistory(current => rejectHistory(current, want, generation, failure))
      })
    return () => {
      alive = false
      request.controller.abort()
    }
  }, [viewSeq, runId, historyRetry, generation, routeFenceBlocked, reviewMode])
  const currentHistory = historyMatches(history, viewSeq, generation) ? history : null
  const hist = currentHistory?.status === 'ready' ? currentHistory.data : null
  // Report source reads are always sequence-bound. Use the generation that owns the rendered
  // snapshot: current useRunState truth for live/review, or the generation receipt already checked
  // by resolveHistory for an historical fold. A stale generation-fenced route returns above before
  // Report can mount, so this fallback never weakens an explicit link fence.
  const reportGeneration = historyActive ? currentHistory?.resolvedGeneration : generation
  const copyRetainedCommentWork = async () => {
    const lines = [
      ...retainedCommentDrafts.map((text, index) => `Unsaved comment draft ${index + 1}:\n${text}`),
      ...retainedCommentRecovery.valid.map((intent, index) => {
        const subject = `experiment #${intent.nodeId}, attempt ${intent.nodeGeneration}`
        if (intent.kind === 'resolution') {
          return `Saved resolution command ${index + 1}: ${intent.resolved ? 'resolve' : 'reopen'} comment ${intent.commentId} on ${subject}`
        }
        return `Saved ${intent.kind === 'create' ? 'new comment' : `edit for ${intent.commentId}`} ${index + 1} on ${subject}:\n${intent.text}`
      }),
    ]
    if (!lines.length) { showToast('There is no readable comment text to copy.'); return }
    try {
      await navigator.clipboard.writeText(lines.join('\n\n'))
      showToast('Retained Comments work copied.')
    } catch {
      showToast('Clipboard is unavailable. Open the recovery details and copy the text manually.')
    }
  }
  const discardRetainedCommentWork = () => {
    if (retainedCommentRecoveryUnavailable) {
      showToast('Comments recovery storage is unavailable. Retry it before discarding any retained work.')
      return
    }
    const total = retainedCommentReleasableCount
    if (!total) {
      showToast(retainedCommentProtectedCreates.length > 0
        ? 'Current-generation new-comment recovery cannot be discarded without a terminal server outcome or intact exact recovery data.'
        : 'There is no releasable Comments work in this tab.')
      return
    }
    if (!window.confirm(
      `Discard ${total} releasable Comments work item${total === 1 ? '' : 's'} from this browser tab? Current-generation append-only recovery stays protected because it is not safe to release.`,
    )) return
    let cleared = true
    for (const intent of retainedCommentReleasableIntents) {
      if (!clearCommentOperationIntent(intent)) cleared = false
    }
    for (const recovery of retainedCommentReleasableDamaged) {
      if (!clearDamagedCommentOperation(recovery)) cleared = false
    }
    if (cleared) {
      for (const [scope] of retainedCommentReleasableEntries) {
        inspectorDraftStoreRef.current.clear(scope)
      }
    }
    showToast(cleared
      ? retainedCommentProtectedCreates.length > 0
        ? 'Releasable Comments work discarded. Exact new-comment recovery remains protected.'
        : 'Retained Comments work discarded. Review the thread before creating another command.'
      : 'Some Comments recovery changed and was not discarded. Refresh this run before continuing.')
  }
  const currentCommentRecovery = retainedCommentRecovery.valid.find(
    intent => intent.expectedGeneration === generation)
  const openCurrentCommentRecovery = () => {
    if (!currentCommentRecovery) return
    if (!confirmRetainedPanelClose()) return
    route.update(current => ({
      ...current,
      view: 'dag', sequence: null, panel: null,
      nodeId: currentCommentRecovery.nodeId,
      nodeGeneration: currentCommentRecovery.nodeGeneration,
      inspectTab: 'Comments',
      commentId: currentCommentRecovery.commentId,
    }))
  }
  // Verified Start over hands the route to a brand-new generation. Every landing latch, camera
  // decision, transient overlay and route notice below belongs to the archived run, so the
  // coordination hook resets them at exactly the point the saga used to reset them inline.
  const resetWorkspaceForNewGeneration = () => {
    landedRef.current = false
    deepLinkLandingRef.current = false
    largeOverviewAppliedRef.current = false
    commitGroupNavigation(null, { mode: 'replace' })
    setConceptHighlight(null)
    setCompactInspectorOpen(false)
    setCompactTimelineOpen(false)
    setTransportController(null)
    setTimelineActivation({ runId: null, active: false, focusPending: false })
    setRouteNotice('')
  }
  const { beginStartOver, retryStartOver, retryStartOverStorage } = useStartOverCoordination(
    startOver, {
      runId, generation, reviewMode, runStatus, route, retryRunRef, routeMainRef, showToast,
      resetWorkspace: resetWorkspaceForNewGeneration,
    })
  // Whether a Start over may be STARTED is this component's question rather than the saga's: it is
  // a statement about the retained Comments / Run settings / Authoring work RunView itself owns.
  const submitStartOver = (confirmedGeneration) => {
    // The confirmation dialog can outlive the render that opened it. Re-read both memory and
    // durable recovery synchronously so a just-created comment command can never race Start over.
    const currentCommentMemoryUnsafe = inspectorDraftStoreRef.current.entries()
      .some(entry => commentDraftEntryUnsafe(entry, String(runId)))
    const currentCommentRecovery = listCommentOperationRecoveries(String(runId))
    const currentCommentWorkUnsafe = currentCommentMemoryUnsafe
      || !currentCommentRecovery.available
      || currentCommentRecovery.valid.length > 0
      || currentCommentRecovery.damaged.length > 0
    if (currentCommentWorkUnsafe) {
      refreshCommentOperationRecoveries()
      showToast('Start over is blocked while Comments has retained work or recovery storage cannot be verified. Review the Comments notice and resolve, restore, or retry it first.')
      return
    }
    const currentPanelController = panelNavigationGuardRef.current
    const currentConfigDraft = inspectorDraftStoreRef.current.readField(
      `panel:config:${String(runId)}`, 'draft', null)
    const currentAuthoringDocuments = inspectorDraftStoreRef.current.readField(
      'panel:authoring', 'documents', {})
    const currentAuthoringRecoveryUnsafe = ['uncertainSaves', 'damagedRecoveries'].some(field => {
      const records = inspectorDraftStoreRef.current.readField('panel:authoring', field, {})
      return records && typeof records === 'object' && !Array.isArray(records)
        && Object.keys(records).length > 0
    })
    const currentPanelWorkUnsafe = currentPanelController?.unsafe === true
      || (currentConfigDraft?.schema === 'looplab.config-draft/v1'
        && currentConfigDraft.unsafe === true)
      || (currentAuthoringDocuments && typeof currentAuthoringDocuments === 'object'
        && !Array.isArray(currentAuthoringDocuments)
        && Object.values(currentAuthoringDocuments).some(document => document
          && typeof document === 'object' && !Array.isArray(document)
          && (document.draftText !== document.savedText
            || document.recoveryOperationId || document.recoveryStorageRaw)))
      || currentAuthoringRecoveryUnsafe
    if (currentPanelWorkUnsafe) {
      showToast('Start over is blocked while Run settings or Authoring has retained work. Finish it or explicitly discard it first.')
      return
    }
    beginStartOver(confirmedGeneration)
  }
  const groupState = historyActive ? hist : live
  useEffect(() => {
    if (!groupSurfaceReady || !groupNavigation || !groupState?.nodes) return
    const groups = computeGroups(groupState.nodes, groupNavigation.mode, groupState)
    const members = groups.get(groupNavigation.key)
    if (!members) {
      const staleKey = groupNavigation.key
      commitGroupNavigation(null, { mode: 'replace' })
      setCollapsed(current => {
        if (!current.has(staleKey)) return current
        const next = new Set(current)
        next.delete(staleKey)
        return next
      })
      setRouteNotice(current => [current,
        `Saved group “${staleKey}” is no longer available in this run state; opened the graph.`]
        .filter(Boolean).join(' '))
      if (selectedId == null) {
        setCompactInspectorOpen(false)
        requestAnimationFrame(() => routeMainRef.current?.focus({ preventScroll: true }))
      }
      return
    }
    if (groupNavigation.returnNodeId != null
        && !members.some(id => Number(id) === groupNavigation.returnNodeId)) {
      commitGroupNavigation({ ...groupNavigation, returnNodeId: null, scrollTop: 0 },
        { mode: 'replace' })
    }
  }, [groupSurfaceReady, groupState, groupNavigation?.mode, groupNavigation?.key,
    groupNavigation?.returnNodeId, selectedId])
  // Members of the selected group — memoized so unrelated re-renders (toast, live ticks) don't
  // re-walk all nodes; only recomputes when the node set / mode / selection actually changes.
  const groupMembers = useMemo(() => {
    const ns = groupState?.nodes
    return (selectedGroup != null && ns)
      ? (computeGroups(ns, groupMode, groupState).get(selectedGroup) || []) : []
  }, [groupState, groupMode, selectedGroup])
  // The Inspector takes visual precedence while a node is selected. A per-entry group snapshot can
  // therefore stay attached as its Back/Forward parent without rendering two detail surfaces at once.
  const revealSelectedNode = id => {
    if (id != null) {
      if (groupNavigationRef.current) commitGroupNavigation(null, { mode: 'replace' })
      setRouteNotice('')
      if (compactWorkspace) setCompactInspectorOpen(true)
      else setSideC(false)
    }
  }
  const selectNode = id => {
    setSelectedId(id)
    revealSelectedNode(id)
  }
  // Panel rows call onSelect and then onClose. Commit the selection and dismissal atomically into
  // the panel's current route entry so Back returns to the pre-panel workspace. The trailing child
  // onClose becomes a harmless no-op because the route already has no panel.
  const selectNodeFromPanel = id => {
    if (!confirmRetainedPanelClose()) return
    panelReturnFocusRef.current = null
    route.update(current => ({
      ...routeWithSelectedNode(current, id), view: 'dag', panel: null,
    }), { mode: 'replace' })
    revealSelectedNode(id)
    if (id != null) focusInspectorFromGroup(id)
  }
  // U3 · canvas as a control surface: right-click actions + drag-to-merge + a "merge with…" arm mode.
  const [mergeIntent, setMergeIntent] = useState(null)
  const mergeFrom = mergeIntent?.sourceId ?? null
  const mergeTarget = mergeIntent?.targetId == null ? '' : String(mergeIntent.targetId)
  const [mergeSubmitting, setMergeSubmitting] = useState(false)
  const mergeSubmittingRef = useRef(false)   // synchronous guard: two submit events can precede a render
  const mergeReturnFocusRef = useRef(null)
  const mergeSelectRef = useRef(null)
  const mergeConfirmRef = useRef(null)
  const closeMergeChooser = (restoreFocus = true) => {
    const source = mergeFrom
    const captured = mergeReturnFocusRef.current
    mergeReturnFocusRef.current = null
    setMergeIntent(null)
    if (!restoreFocus || source == null) return
    requestAnimationFrame(() => {
      const fallback = document.querySelector(`[data-node-action-id="${source}"]`)
        || document.querySelector(`[data-node-select-id="${source}"]`)
      const target = captured?.isConnected ? captured : fallback || document.querySelector('[data-route-main]')
      target?.focus?.({ preventScroll: true })
    })
  }
  const openMergeChooser = (source, target = null, returnFocus = null) => {
    const sourceId = Number(source)
    const targetId = target == null ? null : Number(target)
    if (!Number.isSafeInteger(sourceId)
      || (targetId != null && (!Number.isSafeInteger(targetId) || targetId === sourceId))) {
      showToast('Choose two different experiments to merge')
      return
    }
    // Confirmation owns this exact run generation and both attempt identities; a
    // later render may invalidate the chooser but must never silently rebind the operator's intent.
    const intent = captureMergeIntent({
      runId, runGeneration: generation, nodes: live?.nodes, sourceId, targetId,
    })
    if (!intent) {
      showToast('The run or experiment attempts changed while opening merge. Refresh and choose again.')
      return
    }
    mergeReturnFocusRef.current = returnFocus || null
    setMergeIntent(intent)
    showToast(targetId == null
      ? `Choose a destination for #${sourceId}, then confirm the merge`
      : `Review merge #${sourceId} + #${targetId}, then confirm`)
    requestAnimationFrame(() => {
      const control = targetId == null ? mergeSelectRef.current : mergeConfirmRef.current
      control?.focus?.({ preventScroll: true })
    })
  }
  const [comparePair, setComparePair] = useState(null)   // seed ComparePanel for "diff vs champion"
  // Browser history can dismiss Compare without calling the panel's onClose handler. Scope the
  // one-shot seed to the compare route so an ordinary later open cannot resurrect an old diff.
  useEffect(() => {
    if (panel !== 'compare') setComparePair(null)
  }, [panel])
  useEffect(() => {
    if (!historyActive) return
    setMergeIntent(null)
    mergeReturnFocusRef.current = null
    setComparePair(null)
    setOpenHub(null)
    commitGroupNavigation(null, { mode: 'replace' })
    if (history.status === 'ready') {
      route.update(current => {
        const nextNode = reconcileHistoricalSelection(current.nodeId, history.data)
        if (current.nodeId != null && nextNode == null) setCompactInspectorOpen(false)
        return {
          ...current,
          nodeId: nextNode,
          nodeGeneration: nextNode === current.nodeId ? current.nodeGeneration : null,
          inspectTab: nextNode == null ? 'Overview' : current.inspectTab,
          commentId: nextNode === current.nodeId ? current.commentId : null,
          sequence: Number.isSafeInteger(history.resolvedSeq) ? history.resolvedSeq : current.sequence,
        }
      }, { mode: 'replace', preserveIssues: true })
    }
  }, [historyActive, history.status, history.resolvedSeq])
  const live2 = hist || live
  useEffect(() => {
    if (!mergeIntent || mergeSubmittingRef.current || mergeIntentMatches(mergeIntent, {
      runId, runGeneration: generation, nodes: live?.nodes,
    })) return
    showToast('Merge cancelled because the run or one of its experiment attempts changed.')
    closeMergeChooser(true)
  }, [mergeIntent, runId, generation, live?.nodes])
  const allowedInspectTabs = useMemo(() => {
    if (reviewMode) return reviewInspectorTabs(reviewEvidence)
    if (historyActive) return READ_ONLY_INSPECT_TABS
    const node = selectedId == null ? null : live2?.nodes?.[selectedId]
    return node && !isSweep(node) ? LIVE_INSPECT_TABS.filter(tab => tab !== 'Trials') : LIVE_INSPECT_TABS
  }, [reviewMode, reviewEvidence, historyActive, live2, selectedId])
  const effectiveInspectTab = allowedInspectTabs.includes(inspectTab) ? inspectTab : 'Overview'
  useEffect(() => {
    if (inspectTab === effectiveInspectTab) return
    setRouteNotice(current => [current,
      `${inspectTab} is unavailable for this node or access level; opened Overview instead.`]
      .filter(Boolean).join(' '))
    setInspectTab(effectiveInspectTab, { mode: 'replace', preserveIssues: true })
  }, [inspectTab, effectiveInspectTab])
  useEffect(() => {
    if (startOverHandoff || !live2 || selectedId == null
        || (historyActive && hist == null)) return
    if (nodeIsActive(live2.nodes?.[selectedId], live2)) return
    setRouteNotice(current => [current, `Experiment #${selectedId} is not available in this run state.`]
      .filter(Boolean).join(' '))
    route.update(current => ({ ...current, nodeId: null, nodeGeneration: null,
      inspectTab: 'Overview', commentId: null }),
      { mode: 'replace', preserveIssues: true,
        forceGeneration: routeState.nodeGeneration != null })
    setCompactInspectorOpen(false)
  }, [live2, selectedId, historyActive, hist, startOverHandoff,
    routeState.nodeGeneration])
  const currentSelectedAttempt = selectedId == null ? null : live2?.nodes?.[selectedId]?.attempt
  const nodeAttemptEvidenceReady = !historyActive || hist != null
  const exactNodeAttemptMismatch = nodeAttemptEvidenceReady && routeState.nodeGeneration != null
    && live2?.nodes?.[selectedId] != null
    && routeState.nodeGeneration !== currentSelectedAttempt
  const commentAttemptMatches = !routeState.commentId
    || (Number.isSafeInteger(routeState.nodeGeneration)
      && routeState.nodeGeneration === currentSelectedAttempt)
  useLayoutEffect(() => {
    if (!exactNodeAttemptMismatch) return
    if (compactInspectorRef.current?.contains(document.activeElement)) {
      attemptFenceFocusPendingRef.current = true
    }
    setAttemptFenceNotice(historyActive
      ? routeState.commentId
        ? `The linked comment targets attempt ${routeState.nodeGeneration}, but this historical snapshot contains attempt ${currentSelectedAttempt ?? 'unavailable'} for experiment #${selectedId}. The requested attempt was not opened.`
        : `The link targets attempt ${routeState.nodeGeneration}, but this historical snapshot contains attempt ${currentSelectedAttempt ?? 'unavailable'} for experiment #${selectedId}. The requested attempt was not opened.`
      : routeState.commentId
        ? `The linked comment belongs to attempt ${routeState.nodeGeneration}, but experiment #${selectedId} is now attempt ${currentSelectedAttempt ?? 'unavailable'}. The newer attempt was not opened.`
        : `The link targets attempt ${routeState.nodeGeneration}, but experiment #${selectedId} is now attempt ${currentSelectedAttempt ?? 'unavailable'}. The newer attempt was not opened.`)
    route.update(current => ({ ...current, nodeId: null, nodeGeneration: null,
      inspectTab: 'Overview', commentId: null }),
      { mode: 'replace', preserveIssues: true, forceGeneration: true })
    setCompactInspectorOpen(false)
  }, [exactNodeAttemptMismatch, routeState.nodeGeneration, routeState.commentId,
    selectedId, currentSelectedAttempt, historyActive])
  useLayoutEffect(() => {
    if (!attemptFenceFocusPendingRef.current || !attemptFenceNotice) return
    attemptFenceFocusPendingRef.current = false
    ;(routeNoticeRef.current || routeMainRef.current)?.focus({ preventScroll: true })
  }, [attemptFenceNotice])
  useLayoutEffect(() => {
    // An in-app DAG selection is a new, current target even though it uses route.update() rather than
    // browser navigation. Retire only the attempt-fence warning; unrelated route notices remain.
    if (selectedId != null && !exactNodeAttemptMismatch) setAttemptFenceNotice('')
  }, [selectedId, currentSelectedAttempt, routeState.nodeGeneration, exactNodeAttemptMismatch])
  useEffect(() => {
    // Initial hydration and Back/Forward are navigation, not a hidden selection: reveal the actual
    // Inspector destination even when a persisted desktop rail or a compact drawer was closed.
    if (selectedId == null) return
    if (compactWorkspace) setCompactInspectorOpen(true)
    else setSideC(false)
    if (groupNavigationRef.current) focusInspectorFromGroup(selectedId)
  }, [route.navigationRevision])
  const pendingDescendants = (rootId) => {   // for "kill branch": abort the node + its PENDING subtree
    const ns = live2?.nodes || {}
    const kids = {}; Object.values(ns).forEach(n => (n.parent_ids || []).forEach(p => { (kids[p] ||= []).push(n.id) }))
    const out = []; const stack = [rootId]; const seen = new Set()
    while (stack.length) { const x = stack.pop(); if (seen.has(x)) continue; seen.add(x)
      if (ns[x] && ns[x].status === 'pending') out.push(x)
      ;(kids[x] || []).forEach(k => stack.push(k)) }
    return out
  }
  const checkedCommand = (record, labels) => {
    const feedback = commandFeedback(record, labels)
    if (feedback.kind === 'error') throw new Error(feedback.message)
    return feedback
  }
  const onNodeAction = async (action, arg, context = null) => {
    if (mutationReadOnlyMode) {
      showToast(startOverMutationBlocked
        ? 'Start over must be resolved before changing this run.'
        : reviewMode ? 'This review link is read-only'
          : `Historical snapshot seq ${viewSeq} is read-only`)
      return
    }
    if (action === 'merge' && mergeSubmittingRef.current) {
      showToast('A merge is already being submitted'); return
    }
    try {
      if (action === 'merge' && arg && typeof arg === 'object') {   // drag drop only preselects A + B
        openMergeChooser(arg.from, arg.to, context?.returnFocus || null)
        return
      }
      const id = arg
      const generation = live2?.nodes?.[id]?.attempt
      if (action === 'explore') {
        const f = checkedCommand(await CONTROL.fork(runId, id, generation), {
          success: `Fork from #${id} applied — the engine is processing it`, noop: `Branch from #${id} already exists`,
          executing: `Fork from #${id} requested — waiting for the engine`, failure: 'Fork failed',
        }); showToast(f.message)
      }
      else if (action === 'ablate') {
        const f = checkedCommand(await CONTROL.forceAblate(runId, id, generation), {
          success: `Ablation of #${id} applied — the engine is processing it`, noop: `Ablation of #${id} was already satisfied`,
          executing: `Ablation of #${id} requested — waiting for the engine`, failure: 'Ablation failed',
        }); showToast(f.message)
      }
      else if (action === 'inspect') {
        selectNode(id)
        if (compactWorkspace) setCompactInspectorOpen(true)
        else setSideC(false)
      }   // un-collapse: with the side
      // panel folded (sideC persists in localStorage!) selecting alone changes nothing visible
      else if (action === 'diff') {
        panelReturnFocusRef.current = context?.returnFocus || null
        setComparePair([live2?.best_node_id ?? id, id]); setPanel('compare')
      }
      else if (action === 'merge') {
        openMergeChooser(id, null, context?.returnFocus || null)
      }
      else if (action.startsWith('reset:')) {   // re-run this node IN PLACE from a stage (no new node)
        const stage = action.split(':')[1]
        const f = checkedCommand(await CONTROL.resetNode(runId, id, stage, generation), {
          success: `Reset #${id} from ${stage} applied — the engine is processing it`, noop: `#${id} already reflects that reset`,
          executing: `Reset #${id} from ${stage} requested — waiting for the engine`, failure: 'Reset failed',
        }); showToast(f.message)
      }
      else if (action === 'kill') {
        const ids = pendingDescendants(id)
        if (!ids.length) { showToast(`#${id}: nothing pending to kill (already evaluated)`); return }
        let waiting = 0
        for (const k of ids) {
          const f = checkedCommand(await CONTROL.nodeAbort(runId, k, live2?.nodes?.[k]?.attempt), {
            success: `Cancelled #${k}`, noop: `#${k} was already settled`,
            executing: `Cancellation of #${k} requested`, failure: `Could not cancel #${k}`,
          })
          if (f.kind === 'pending') waiting++
        }
        showToast(waiting
          ? `Cancellation requested for ${ids.length} experiment(s); ${waiting} still pending`
          : `Cancelled ${ids.length} pending experiment(s) under #${id}`)
      }
    } catch (e) { showToast(e.message || 'Run command failed') }
  }
  // When armed for merge, a node click only preselects the destination. The form's explicit Confirm
  // action is the sole mutation boundary for context-menu, keyboard, canvas, and drag gestures.
  const onCanvasSelect = (id) => {
    if (mergeFrom != null && mergeSubmittingRef.current) { showToast('A merge is already being submitted'); return }
    if (mutationReadOnlyMode && mergeFrom != null) {
      closeMergeChooser(false); return
    }
    if (mergeFrom != null && id != null && id !== mergeFrom) {
      const next = selectMergeTarget(mergeIntent, {
        runId, runGeneration: generation, nodes: live?.nodes,
      }, Number(id))
      if (!next) {
        showToast('Merge cancelled because the run or one of its experiment attempts changed.')
        closeMergeChooser(true)
        return
      }
      setMergeIntent(next)
      showToast(`Review merge #${mergeFrom} + #${id}, then confirm`)
      requestAnimationFrame(() => mergeConfirmRef.current?.focus?.({ preventScroll: true }))
      return
    }
    if (mergeFrom != null && id === mergeFrom) {
      showToast('Choose a different destination experiment')
      requestAnimationFrame(() => mergeSelectRef.current?.focus?.({ preventScroll: true }))
      return
    }
    if (mergeFrom != null) closeMergeChooser(true)
    selectNode(id)
    if (compactWorkspace && id != null) setCompactInspectorOpen(true)
  }
  useEffect(() => {   // Esc cancels the merge-arm
    if (mergeFrom == null) return
    const h = (e) => {
      if (e.key !== 'Escape' || e.defaultPrevented || mergeSubmittingRef.current) return
      if (document.querySelector('[aria-modal="true"]')) return
      closeMergeChooser(true)
    }
    window.addEventListener('keydown', h); return () => window.removeEventListener('keydown', h)
  }, [mergeFrom])
  // Drill-down from the dock/timeline: select a node, optionally open a tab + jump the scrubber.
  const focusNode = (id, tab, eventSeq, {
    preserveGroup = false, nodeGeneration = null, preserveExactSequence = false,
  } = {}) => {
    const value = Number(eventSeq)
    const validEventSeq = Number.isSafeInteger(value) && value >= 0 ? value : null
    const targetSeq = eventSeq == null ? null
      : preserveExactSequence ? validEventSeq
        : validEventSeq != null && validEventSeq < seq ? validEventSeq : null
    const targetNodeGeneration = Number.isSafeInteger(nodeGeneration) && nodeGeneration >= 0
      ? nodeGeneration : null
    route.update(current => {
      const nextTab = tab || current.inspectTab
      const preserveComment = id === current.nodeId && nextTab === 'Comments'
      return { ...current, view: 'dag', nodeId: id,
        nodeGeneration: preserveComment ? current.nodeGeneration : targetNodeGeneration,
        inspectTab: nextTab,
        commentId: preserveComment ? current.commentId : null,
        sequence: reviewMode ? null : targetSeq }
    })
    if (!preserveGroup && groupNavigationRef.current) {
      commitGroupNavigation(null, { mode: 'replace' })
    }
    if (compactWorkspace) {
      setCompactTimelineOpen(false)
      setCompactInspectorOpen(true)
    }
    else setSideC(false)     // drill-down means "show me the inspector" — un-fold a collapsed panel
  }
  const focusGroupMember = (id, tab, eventSeq) => {
    const memberId = Number(id)
    const current = groupNavigationRef.current
    if (current && Number.isSafeInteger(memberId) && memberId >= 0) {
      const body = compactInspectorRef.current?.querySelector('.insp-body')
      commitGroupNavigation({
        ...current,
        returnNodeId: memberId,
        scrollTop: body?.scrollTop || 0,
      }, { mode: 'replace' })
    }
    focusNode(id, tab, eventSeq, { preserveGroup: true })
    if (current) focusInspectorFromGroup(memberId)
  }
  // --- semantic-zoom grouping controls ---
  const toggleGroup = (key) => {
    graphPreferenceTouchedRef.current = true
    setCollapsed(s => { const n = new Set(s); n.has(key) ? n.delete(key) : n.add(key); return n })
  }
  const changeMode = (m) => {
    graphPreferenceTouchedRef.current = true
    setGroupModePreference(m)
    setCollapsed(new Set())
    if (groupNavigationRef.current) {
      // Grouping is a canvas preference, not an addressable route. Replacing the attached group
      // context avoids a Forward entry that cannot truthfully reconstruct this no-group mode.
      commitGroupNavigation(null, { mode: 'replace' })
    }
  }
  const collapseAllGroups = keys => {
    graphPreferenceTouchedRef.current = true
    setCollapsed(new Set(keys))
  }
  const expandAllGroups = () => {
    graphPreferenceTouchedRef.current = true
    setCollapsed(new Set())
  }
  const selectGroup = (key) => {
    if (key != null) graphPreferenceTouchedRef.current = true
    if (key != null) {
      const next = normalizeRunGroupNavigation({
        runId, generation, mode: groupMode, key, returnNodeId: null, scrollTop: 0,
      }, runId, generation)
      if (!next) return
      const current = groupNavigationRef.current
      if (selectedId == null && current?.mode === next.mode && current.key === next.key) {
        setCollapsed(value => value.has(key) ? value : new Set(value).add(key))
        if (compactWorkspace) setCompactInspectorOpen(true)
        else setSideC(false)
        return
      }
      const opensNewRoute = selectedId != null
      if (opensNewRoute) setSelectedId(null)
      commitGroupNavigation(next, { mode: opensNewRoute ? 'replace' : 'push' })
      setCollapsed(current => current.has(key) ? current : new Set(current).add(key))
      if (compactWorkspace) setCompactInspectorOpen(true)
      else setSideC(false)
      return
    }
    if (!groupNavigationRef.current) return
    commitGroupNavigation(null, {
      mode: selectedId != null ? 'replace' : 'push', focusExit: true,
    })
    setCompactInspectorOpen(false)
  }
  // Phase 0: auto-collapse settled groups into the existing `collapsed` Set (one-shot fill, not a live
  // policy — so it never fights the user's manual toggles). Keeps the champion/selected/working groups
  // open. Wired to the "⊟ settled" button and fired once when a run finishes (only if nothing's been
  // collapsed yet, so it never clobbers a layout the user arranged by hand).
  const autoCollapse = (userInitiated = false) => {
    if (userInitiated) graphPreferenceTouchedRef.current = true
    const st = hist || live; const ns = st?.nodes; if (!ns) return
    setCollapsed(autoCollapseSet(ns, computeGroups(ns, groupMode, st),
      { mode: groupMode, bestId: st.best_node_id, selectedId, workId: workingId(st) }))
  }
  const autoCollapsedRef = useRef(false)
  // groupMode is in the deps so that if the run finishes in a non-banded mode (operator/metric/none),
  // a later switch to theme/niche still triggers the one-shot fold (the ref keeps it firing only once).
  useEffect(() => {
    if (!historyActive && live?.finished && !autoCollapsedRef.current && (groupMode === 'theme' || groupMode === 'niche') && collapsed.size === 0) {
      autoCollapsedRef.current = true; autoCollapse()
    }
  }, [live?.finished, groupMode, historyActive])

  const mergeCandidates = useMemo(() => Object.values(live2?.nodes || {})
    .filter(node => node.id !== mergeFrom).sort((left, right) => left.id - right.id), [live2, mergeFrom])
  const submitMergeTarget = async (event) => {
    event.preventDefault()
    if (mergeTarget === '') return
    const intent = mergeIntent
    const command = mergeIntentCommand(intent)
    if (!command || !mergeIntentMatches(intent, {
      runId, runGeneration: generation, nodes: live?.nodes,
    }, true)) {
      showToast('Merge cancelled because the run or one of its experiment attempts changed.')
      closeMergeChooser(true)
      return
    }
    const [source, target] = command.ids
    if (mergeSubmittingRef.current) { showToast('A merge is already being submitted'); return }
    mergeSubmittingRef.current = true
    setMergeSubmitting(true)
    try {
      const feedback = checkedCommand(await CONTROL.merge(
        command.runId, command.ids, command.parentGenerations,
        { expectedGeneration: command.expectedGeneration }), {
        success: `Merge #${source} + #${target} applied — the engine is processing it`, noop: 'That merge was already satisfied',
        executing: `Merge #${source} + #${target} requested — waiting for the engine`, failure: 'Merge failed',
      })
      showToast(feedback.message)
      closeMergeChooser(true)
    } catch (error) {
      showToast(error.message || 'Merge failed')
      requestAnimationFrame(() => mergeConfirmRef.current?.focus?.({ preventScroll: true }))
    } finally {
      mergeSubmittingRef.current = false
      setMergeSubmitting(false)
    }
  }
  const openComment = comment => {
    if (!comment || !Number.isSafeInteger(comment.nodeId)) return
    if (comment.legacy) {
      showToast('Legacy notes have no attempt identity and stay in the run-wide comment feed.')
      return
    }
    const currentAttempt = live2?.nodes?.[comment.nodeId]?.attempt
    if (currentAttempt !== comment.nodeGeneration) {
      showToast(`This comment belongs to attempt ${comment.nodeGeneration}; the current node is attempt ${currentAttempt ?? 'unavailable'}.`)
      return
    }
    // This is a deliberate drill-down destination, not a panel dismissal. Let route focus move into
    // the workspace instead of restoring the hub button that originally opened Comments.
    if (!confirmRetainedPanelClose()) return
    panelReturnFocusRef.current = null
    route.update(current => ({
      ...current,
      view: 'dag',
      nodeId: comment.nodeId,
      nodeGeneration: comment.nodeGeneration,
      inspectTab: 'Comments',
      commentId: comment.id,
      panel: null,
    }))
    commitGroupNavigation(null, { mode: 'replace' })
    if (compactWorkspace) setCompactInspectorOpen(true)
    else setSideC(false)
  }

  const copyViewLink = async () => {
    const relative = route.exactHref(routeState, { forReview: reviewMode })
    const url = new URL(relative, window.location.origin).href
    setCopyFallback('')
    try {
      if (!navigator.clipboard?.writeText) throw new Error('clipboard unavailable')
      await navigator.clipboard.writeText(url)
      showToast(reviewMode ? 'Read-only review view copied' : 'View link copied — owner access is still required')
    } catch {
      setCopyFallback(url)
      showToast('Clipboard access was denied; select the visible link and copy it manually')
    }
  }

  const routeFocusPhase = !live ? `resource:${runStatus}`
    : routeFenceBlocked ? `fence:${generationMismatch ? 'mismatch' : 'pending'}`
    : historyActive && !hist ? `history:${history.status}` : 'ready'
  const workspaceRouteLabel = `${live?.label || live?.run_id || runId} run workspace · ${
    view === 'concepts' ? 'Concepts' : view === 'report' ? 'Report' : 'Search'}`
  useEffect(() => {
    // The dialog owns focus while it is mounting and open. Keeping the opener intact here also lets
    // an explicit Close consume the panel's history entry and restore the exact launching control.
    if (overlayPanelOpen) return undefined
    panelBackCloseRef.current = false
    const panelTarget = panelReturnFocusRef.current
    panelReturnFocusRef.current = null
    if (view === 'dag' && groupNavigationRef.current) return undefined
    const frame = requestAnimationFrame(() => {
      if (!document.querySelector('[aria-modal="true"], [data-route-focus-guard="true"]')) {
        ;(panelTarget && document.contains(panelTarget)
          ? panelTarget
          : routeMainRef.current || document.querySelector('[data-route-main]'))
          ?.focus({ preventScroll: true })
      }
    })
    return () => cancelAnimationFrame(frame)
  }, [routeFocusPhase, route.navigationRevision, overlayPanelOpen])

  if (!live && startOverRecovery.kind !== 'none' && !startOverIntent) return <RunScreen
    onBack={onBack} onLeave={leaveRetainedPanelRoute} toast={toast}>
    <main ref={startOverNoticeRef} className="run-resource-state" data-route-main tabIndex={-1}
      aria-labelledby="run-state" role="alert">
      <div className="resource-state-icon" aria-hidden="true">!</div>
      <h1 id="run-state">{startOverRecovery.kind === 'unavailable'
        ? 'Start over recovery storage is unavailable'
        : 'Saved Start over recovery is invalid'}</h1>
      <p>{startOverRecovery.kind === 'unavailable'
        ? 'This tab cannot safely preserve a Start over request. No request can be submitted until recovery storage works again.'
        : 'LoopLab cannot identify an earlier Start over request safely. Run changes remain locked until its saved recovery evidence is resolved.'}</p>
      <div className="resource-state-actions">
        {startOverRecovery.kind === 'unavailable' && <button type="button"
          className="btn primary" onClick={retryStartOverStorage}>Try storage again</button>}
        {onBack && <button type="button" className="btn" onClick={leaveRetainedPanelRoute}>Back to runs</button>}
      </div>
    </main>
  </RunScreen>
  if (!live && startOverIntent) return <RunScreen
    onBack={onBack} onLeave={leaveRetainedPanelRoute} toast={toast}>
    <main ref={startOverNoticeRef} className="run-resource-state" data-route-main tabIndex={-1}
      aria-labelledby="run-state" role={runStatus === 'error' ? 'alert' : 'status'}>
      {runStatus === 'loading' && <div className="history-spinner" aria-hidden="true" />}
      <h1 id="run-state">{startOverRequestPending
        ? 'Submitting the same Start over request…'
        : runStatus === 'not_found'
          ? 'Run files are switching…'
          : runStatus === 'error'
            ? 'Start over status is unavailable'
            : startOverIntent.phase === 'unknown'
              ? 'Start over outcome is not confirmed'
              : startOverIntent.phase === 'pending'
                ? 'Checking the saved Start over request…'
              : 'Starting this run over…'}</h1>
      <p>{runStatus === 'error'
        ? `${runError || 'The server could not read this run.'} Recovery remains locked so another operation cannot overlap it.`
        : runStatus === 'not_found'
          ? 'The previous event log may already be archived while the replacement generation is starting. The exact request identity is preserved.'
          : startOverIntent.phase === 'unknown'
            ? 'The result is not confirmed. Retry the exact saved request; LoopLab will not create a second Start over operation.'
            : startOverIntent.phase === 'pending'
              ? 'The server preserved this exact request. LoopLab is checking the same operation automatically; no duplicate will be created.'
            : startOverIntent.phase === 'submitting'
              ? 'The exact request is being submitted. No archive is assumed until the server returns its matching operation receipt.'
              : 'The server accepted this exact operation. Waiting for the replacement run to become readable.'}</p>
      <div className="resource-state-actions">
        <button type="button" className="btn primary" onClick={retryStartOver}
          disabled={startOverRequestPending}>
          {startOverRequestPending ? 'Waiting for response…'
            : 'Retry exact request'}
        </button>
      </div>
    </main>
  </RunScreen>
  if (!live) return <RunScreen reviewMode={reviewMode} reviewPill
    onBack={onBack} onLeave={leaveRetainedPanelRoute}>
    <main className="run-resource-state" data-route-main tabIndex={-1} aria-live="polite"
      aria-labelledby="run-state">
      {runStatus === 'not_found' ? <>
        <div className="resource-state-icon" aria-hidden="true">404</div>
        <h1 id="run-state">Run not found</h1>
        <p><code>{runId}</code> does not exist or may have been removed.</p>
        <div className="resource-state-actions">{onBack && <button className="btn primary" onClick={leaveRetainedPanelRoute}>Back to runs</button>}<button className="btn" onClick={retryRun}>Retry</button></div>
      </> : runStatus === 'gone' ? <>
        <div className="resource-state-icon" aria-hidden="true">×</div>
        <h1 id="run-state">Review access ended</h1>
        <p>{runError || 'This review link expired or was revoked.'}</p>
      </> : runStatus === 'error' ? <>
        <div className="resource-state-icon" aria-hidden="true">!</div>
        <h1 id="run-state">Could not load run</h1>
        <p>{runError || 'Check that the LoopLab server is reachable.'}</p>
        <div className="resource-state-actions"><button className="btn primary" onClick={retryRun}>Retry</button>{onBack && <button className="btn" onClick={leaveRetainedPanelRoute}>Back to runs</button>}</div>
      </> : <>
        <div className="history-spinner" aria-hidden="true" />
        <h1 id="run-state">Opening run…</h1><p>Loading the latest search state.</p>
      </>}
    </main>
  </RunScreen>
  if (routeFenceBlocked && startOverIntent) return <RunScreen
    onBack={onBack} onLeave={leaveRetainedPanelRoute} toast={toast}>
    <main ref={startOverNoticeRef} className="run-resource-state stale-route-state"
      data-route-main tabIndex={-1} aria-labelledby="run-state" role="alert">
      <div className="resource-state-icon" aria-hidden="true">↻</div>
      <h1 id="run-state">{startOverReplacementSuperseded
        ? 'Start over completed, and the run changed again'
        : startOverHandoff
          ? 'Opening the verified replacement run…'
          : 'The run changed while Start over is unresolved'}</h1>
      <p>{startOverReplacementSuperseded
        ? 'The exact saved operation produced its verified replacement generation. A newer generation is now visible, so this resolved recovery record can be cleared before opening the current run.'
        : startOverHandoff
          ? startOverRouteSyncFailed
            ? 'The matching operation receipt is verified, but this page could not update its address. Changes remain locked.'
            : 'The matching operation receipt is verified. Updating this diagnostic address to the new generation.'
          : 'A different generation is visible, but that alone does not prove which operation created it. Retry the exact saved request to read its receipt.'}</p>
      <div className="resource-state-actions">
        <button type="button" className="btn primary" onClick={retryStartOver}
          disabled={startOverRequestPending}>
          {startOverRequestPending ? 'Waiting for response…'
            : startOverReplacementSuperseded ? 'Open current run'
              : startOverHandoff ? 'Retry opening new run' : 'Retry exact request'}
        </button>
        {onBack && <button type="button" className="btn" onClick={leaveRetainedPanelRoute}>Back to runs</button>}
      </div>
    </main>
  </RunScreen>
  if (routeFenceBlocked) return <RunScreen reviewMode={reviewMode} reviewPill
    onBack={onBack} onLeave={leaveRetainedPanelRoute}>
    <main className="run-resource-state stale-route-state" data-route-main tabIndex={-1}
      aria-labelledby="run-state" role={generationMismatch ? 'alert' : 'status'}>
      <div className="resource-state-icon" aria-hidden="true">{generationMismatch ? '↺' : '…'}</div>
      <h1 id="run-state">{generationMismatch ? 'This diagnostic link targets an earlier run generation' : 'Verifying diagnostic link…'}</h1>
      {generationMismatch ? <>
        <p>The run was reset or replaced after this link was created. Node ids and sequence numbers may now mean something else, so LoopLab will not reinterpret the link.</p>
        {retainedConfigDraftUnsafe && <p className="notice" role="status">
          {retainedConfigStoredDraftUnsafe ? <>
            <b>Your unsaved Run settings draft is retained in this tab.</b> Nothing was sent automatically.
            Open the current generation to load its authoritative settings and review the retained fields.
          </> : <>
            <b>A Run settings operation was interrupted by this generation change.</b>{' '}
            Its server-side outcome may need verification; nothing will be replayed automatically.
            Open the current generation to inspect the authoritative state.
          </>}
        </p>}
        {retainedAuthoringDraftUnsafe && <p className="notice" role="status">
          {retainedAuthoringDraftCount > 0 && <>
            <b>{retainedAuthoringDraftCount} unsaved in-memory Authoring draft{retainedAuthoringDraftCount === 1 ? ' is' : 's are'} retained in this tab.</b>
            {' '}Nothing was saved automatically.{' '}
          </>}
          {retainedAuthoringDurableRecoveryCount > 0 && <>
            <b>{retainedAuthoringDurableRecoveryCount} durable Authoring recovery record{retainedAuthoringDurableRecoveryCount === 1 ? ' remains' : 's remain'} protected in browser storage.</b>
            {' '}It will be reconciled without automatic replay.{' '}
          </>}
          {retainedAuthoringMemoryOnlyRecoveryCount > 0 && <>
            <b>{retainedAuthoringMemoryOnlyRecoveryCount} exact recovery snapshot{retainedAuthoringMemoryOnlyRecoveryCount === 1 ? ' exists' : 's exist'} only in this tab.</b>
            {' '}Opening the current generation preserves it; leaving this run discards it.{' '}
          </>}
          Open the current generation to refresh the Authoring source and review the retained text.
        </p>}
        {retainedCommentWorkUnsafe && <div className="notice"
          role={retainedCommentRecoveryUnavailable || retainedCommentRecovery.damaged.length > 0
            ? 'alert' : 'status'}>
          <b>Comments work is retained in this tab.</b>{' '}
          {retainedCommentDrafts.length > 0
            ? `${retainedCommentDrafts.length} unsaved draft${retainedCommentDrafts.length === 1 ? '' : 's'} remain in memory. `
            : ''}
          {retainedCommentRecovery.valid.length > 0
            ? `${retainedCommentRecovery.valid.length} exact command recover${retainedCommentRecovery.valid.length === 1 ? 'y is' : 'ies are'} protected in browser storage. `
            : ''}
          {retainedCommentRecovery.damaged.length > 0
            ? `${retainedCommentRecovery.damaged.length} damaged recovery record${retainedCommentRecovery.damaged.length === 1 ? ' needs' : 's need'} review. `
            : ''}
          {retainedCommentRecoveryUnavailable
            ? 'Recovery storage cannot be inspected, so Start over remains blocked until it is available again. '
            : ''}
          Nothing will be replayed automatically or rebound to the replacement generation.
          {(retainedCommentDrafts.length > 0 || retainedCommentRecovery.valid.length > 0) && <details>
            <summary>View retained Comments work</summary>
            {retainedCommentDrafts.map((text, index) => <pre key={`draft:${index}`}
              className="comment-recovery-payload">{text}</pre>)}
            {retainedCommentRecovery.valid.map(intent => <div key={intent.storageKey}
              className="comment-recovery-payload">
              <b>{intent.kind === 'create' ? 'New comment' : intent.kind === 'edit'
                ? 'Comment edit' : intent.resolved ? 'Resolve comment' : 'Reopen comment'}</b>
              {' '}· experiment #{intent.nodeId} · attempt {intent.nodeGeneration}
              {intent.text != null && <pre>{intent.text}</pre>}
            </div>)}
          </details>}
          <div className="resource-state-actions">
            {(retainedCommentDrafts.length > 0 || retainedCommentRecovery.valid.length > 0)
              && <button type="button" className="btn"
                onClick={copyRetainedCommentWork}>Copy Comments work</button>}
            {retainedCommentRecoveryUnavailable && <button type="button" className="btn"
              onClick={refreshCommentOperationRecoveries}>Retry recovery storage</button>}
            {retainedCommentReleasableCount > 0 && <button type="button" className="btn danger"
              onClick={discardRetainedCommentWork}>{retainedCommentProtectedCreates.length > 0
                ? 'Discard other work' : 'Discard Comments work'}</button>}
          </div>
        </div>}
        <div className="route-generation-detail">
          <code>link {routeState.generation?.slice(0, 12)}</code><span>≠</span><code>current {generation?.slice(0, 12)}</code>
        </div>
        <div className="resource-state-actions">
          <button className="btn primary" onClick={() => {
            setRouteNotice('')
            if (retainedConfigDraftUnsafe && generation) {
              const scope = `panel:config:${String(runId)}`
              inspectorDraftStoreRef.current.updateField(scope, 'draft', current => current
                ? { ...current, reconcileGeneration: generation } : current, null)
            }
            route.openCurrentGeneration(retainedPanelRoute
              ? { mode: 'replace', panel: retainedPanelRoute } : undefined)
          }}>{retainedConfigDraftUnsafe
              ? retainedConfigStoredDraftUnsafe
                ? 'Open current generation with settings draft'
                : 'Open current generation after settings operation'
              : retainedAuthoringDraftUnsafe
                ? 'Open current generation with Authoring work'
                : retainedCommentWorkUnsafe
                  ? 'Open current generation with Comments recovery'
                : 'Open current generation'}</button>
          {onBack && <button className="btn" onClick={leaveRetainedPanelRoute}>Back to runs</button>}
        </div>
      </> : <p>Confirming that this node and sequence still belong to the run generation named by the link.</p>}
    </main>
  </RunScreen>
  // Liveness reflects the ACTUAL run, not the viewed snapshot: green+breathing only while a
  // connected run is still going; a finished run shows a calm "finished", a dropped SSE "offline".
  // A ZOMBIE (not finished, but no engine holds the lock) gets its own "stalled" badge — otherwise it
  // would falsely breathe green "live" while nothing actually runs.
  const lifecycle = runLifecycle(live)
  const finalizing = lifecycle.mode === 'finalizing' || lifecycle.mode === 'finalization-stalled'
    || lifecycle.mode === 'finishing'
  const liveStatus = !connected ? 'off'
    : finalizing ? 'finalizing'
      : lifecycle.mode === 'finished' ? 'done'
        : lifecycle.mode === 'stalled' ? 'stalled'
          : lifecycle.mode === 'unknown' ? 'off'
          : (lifecycle.mode === 'paused' || lifecycle.mode === 'approval') ? 'paused' : 'on'
  const liveLabel = !connected ? 'offline'
    : lifecycle.mode === 'finalization-stalled' ? 'finalization stalled'
      : lifecycle.mode === 'finishing' ? 'finishing'
        : lifecycle.mode === 'finalizing' ? 'finalizing'
          : lifecycle.mode === 'finished' ? 'finished'
            : lifecycle.mode === 'stalled' ? 'stalled'
              : lifecycle.mode === 'unknown' ? 'engine ownership unknown'
              : lifecycle.mode === 'paused' ? 'paused'
                : lifecycle.mode === 'approval' ? 'approval needed' : 'live'
  if (historyActive && !hist) return <RunScreen reviewPill
    onBack={onBack} onLeave={leaveRetainedPanelRoute} head={<>
      <span className="spacer" />
      <span className={'live ' + liveStatus}><span className="led" />current run: {liveLabel}</span>
    </>}>
    <div className="history-banner" role="status">
      <span className="history-lock" aria-hidden="true">◷</span>
      <b>Historical snapshot · gen {gen} · seq {viewSeq} of {seq}</b>
      <span>read-only</span>
      <button className="btn sm primary" onClick={returnToLiveAndFocusWorkspace}>Return to live</button>
    </div>
    <main className="history-resource" data-route-main tabIndex={-1}
      aria-labelledby="run-state">
      {currentHistory?.status === 'error'
        ? <><h1 id="run-state">Snapshot unavailable</h1><p>{currentHistory.error}</p>
            <button className="btn" onClick={() => setHistoryRetry(n => n + 1)}>Retry</button></>
        : <><div className="history-spinner" aria-hidden="true" /><h1 id="run-state">Loading snapshot seq {viewSeq}…</h1>
            <p>The live workspace is hidden until this exact historical state resolves.</p></>}
    </main>
  </RunScreen>
  const state = historyActive ? hist : live
  const displayedPhase = historyActive ? phaseLabel(state) : lifecyclePhaseLabel(live)
  const evalSec = state.total_eval_seconds || 0
  // The hook already fences the resource to the current key, so there is no second `activeResource`
  // read here. A retry keeps the verdict the operator can see (error stays 'error', stale stays
  // 'stale') and announces itself through `pending`, which is what this notice reports as "Retrying".
  const maxEval = configResource.data?.max_eval_seconds
  const configNoticeStatus = configResource.pending === 'retry'
    ? 'retrying'
    : ['error', 'stale'].includes(configResource.status) ? configResource.status : null
  const cost = state.llm_cost
  const hasInspectorContext = selectedId != null || selectedGroup != null
  const groupDetailsOpen = selectedId == null && selectedGroup != null
  const showInspector = compactWorkspace ? (compactInspectorOpen && hasInspectorContext) : (!sideC && hasInspectorContext)
  const activateReportTimeline = () => {
    if (reportTimelineActivated) return
    setTimelineActivation({ runId, active: true, focusPending: true })
    if (compactWorkspace) setCompactTimelineOpen(true)
    else setDockC(false)
  }
  const emptyPresentation = dagEmptyPresentation({
    displayed: state, live, resourceStatus: runStatus, connected,
    historyActive, reviewMode, sequence: history.resolvedSeq ?? viewSeq,
  })
  const approvalCommand = approvalCommandFor(live)
  const revealEvents = () => {
    if (compactWorkspace) setCompactTimelineOpen(true)
    else setDockC(false)
  }
  const onEmptyAction = (action) => {
    if (action === 'events') { revealEvents(); return }
    if (action === 'report') { setView('report'); return }
    if (action === 'return-live') { returnToLiveAndFocusWorkspace(); return }
    if (action === 'retry-connection') { retryRun(); return }
    if (action === 'assistant') {
      if (!approvalCommand) { revealEvents(); showToast('Approval target is missing; inspect the timeline before acting.'); return }
      window.dispatchEvent(new CustomEvent('ll:focus-assistant', { detail: { text: approvalCommand } }))
      return
    }
    if (!TRANSPORT_EMPTY_ACTIONS.has(action)) return
    const exactController = transportController
    if (!exactController || exactController.runId !== runId
        || exactController.expectedGeneration !== generation) {
      showToast('Run controls are refreshing for the displayed generation. Try again in a moment.')
      return
    }
    if (exactController.failure) { revealEvents(); return }
    exactController.invoke(action)
  }

  return (
    <main ref={routeMainRef} className={'app' + (reviewMode ? ' review-mode' : '')}
      data-route-main tabIndex={-1} aria-label={workspaceRouteLabel}>
      <h1 className="sr-only">{workspaceRouteLabel}</h1>
      <div className="topbar run-head">
        <span className="brand"><span className="dot">◉</span> LoopLab</span>
        {/* The LoopLab menu is deliberately absent in review mode: that route is public (it bypasses
            OwnerAuth) and must not advertise, let alone reach, installation-wide owner surfaces. */}
        {!reviewMode && <GlobalMenu />}
        {onBack ? <button className="btn sm ghost" onClick={leaveRetainedPanelRoute}>← runs</button>
          : <span className="pill">read-only review</span>}
        <button type="button" className="btn sm ghost copy-view-btn" onClick={copyViewLink}
          aria-label={reviewMode ? 'Copy read-only review context' : 'Copy shareable run context'}
          title={reviewMode
            ? 'Copy this read-only capability and route context; local visual filters are not included'
            : 'Copy the run route, selected evidence and snapshot; local graph filters are not included and recipients still need owner access'}>
          <OpIcon name="link" size={12} /> <span className="copy-view-label">Copy context</span>
        </button>
        <EnergyToggle />
        <div ref={workspaceToolbarRef} className="view-toggle" role="toolbar"
          aria-label="Run workspace controls" aria-orientation="horizontal"
          onKeyDown={onWorkspaceToolbarKeyDown} onFocus={onWorkspaceToolbarFocus}>
          <button type="button" data-workspace-control="dag" tabIndex={workspaceTabStop === 'dag' ? 0 : -1}
            aria-pressed={view === 'dag'} className={'vt' + (view === 'dag' ? ' on' : '')} onClick={() => setView('dag')}>Search</button>
          <button type="button" data-workspace-control="concepts" tabIndex={workspaceTabStop === 'concepts' ? 0 : -1}
            aria-pressed={view === 'concepts'} className={'vt' + (view === 'concepts' ? ' on' : '')}
            disabled={reviewMode} onClick={() => setView('concepts')}
            title={reviewMode ? 'Concept frames are not included in review capabilities'
              : 'concept tree — group experiments by concept, any lens'}>Concepts</button>
          <button type="button" data-workspace-control="report" tabIndex={workspaceTabStop === 'report' ? 0 : -1}
            aria-pressed={view === 'report'} className={'vt report' + (view === 'report' ? ' on' : '')} onClick={() => setView('report')}
            title="conclusion-first run report"><OpIcon name="doc" size={12} /> Report</button>
          <button type="button" data-workspace-control="overview" tabIndex={workspaceTabStop === 'overview' ? 0 : -1}
            aria-pressed={panel === 'overview'} aria-expanded={panel === 'overview'}
            className={'vt' + (panel === 'overview' ? ' on' : '')} disabled={historyActive}
            onClick={event => {
              if (panel === 'overview') closePanel()
              else { panelReturnFocusRef.current = event.currentTarget; setPanel('overview') }
            }}
            title="at-a-glance run summary — best metric, budget, strategy, hints">Overview</button>
        </div>
        <span className="pill phase">{displayedPhase}</span>
        {compactWorkspace
          ? <button type="button"
              className={'muted run-goal' + (compactGoalExpanded ? ' expanded' : '')}
              title={state.goal || state.task_id} aria-expanded={compactGoalExpanded}
              aria-label={compactGoalExpanded ? 'Show less task' : 'Show full task'}
              aria-controls="run-goal-text" aria-describedby="run-goal-meta run-goal-text"
              onClick={() => setCompactGoalExpanded(value => !value)}>
              <b id="run-goal-meta">{state.label || state.run_id || runId} · {displayedPhase} · gen {gen}</b>
              <span id="run-goal-text" className="run-goal-text">{state.goal || state.task_id}</span>
              <span className="run-goal-toggle" aria-hidden="true">
                {compactGoalExpanded ? 'show less' : 'show full task'}
              </span>
            </button>
          : <span className="muted" title={state.goal || state.task_id}>
              <b>{state.label || state.run_id || runId} · {displayedPhase} · gen {gen}</b>
              {state.goal || state.task_id}
            </span>}
        <span className={'live ' + (reviewMode ? 'off' : liveStatus)}
          role={reviewMode ? undefined : 'status'} aria-live={reviewMode ? undefined : 'polite'}
          aria-atomic={reviewMode ? undefined : true}>
          <span className="led" aria-hidden="true" />
          {!reviewMode && <span className="sr-only">Current run status: </span>}
          {reviewMode ? 'read-only' : liveLabel}
          {historyActive && <span aria-hidden="true"> · history</span>}
        </span>
        <span className="spacer" />
        {/* Wide workspaces keep only the two compact at-a-glance metrics here; the fuller set moved
            to Overview. Compact layouts trade those optional chips for canvas space, while urgent
            reward-hack alerts remain visible and every metric is one tap away through Overview. */}
        {evalSec > 0 && <button type="button" className="chip run-metric-chip" disabled={historyActive}
          title={historyActive ? 'Historical mode — return live to open Overview' : 'eval time — open Overview for the budget bar'}
          onClick={event => { panelReturnFocusRef.current = event.currentTarget; setPanel('overview') }}>
          <span className="k">eval</span> {fmtElapsedSeconds(evalSec)}{maxEval != null ? ` / ${fmtElapsedSeconds(maxEval)}` : ''}</button>}
        {cost && <button type="button" className="chip run-metric-chip" disabled={historyActive}
          title={historyActive ? 'Historical mode — return live to open Overview' : 'tokens — open Overview'}
          onClick={event => { panelReturnFocusRef.current = event.currentTarget; setPanel('overview') }}>
          <span className="k">tokens</span> {fmtInt(cost.total_tokens)}</button>}
        {state.reward_hacks?.length > 0 && <button type="button" className="chip alarm run-metric-chip" disabled={historyActive}
          title={historyActive ? 'Historical mode — return live to open Trust' : 'suspicious wins flagged (B5) — open Trust'}
          onClick={event => { panelReturnFocusRef.current = event.currentTarget; setPanel('trust') }}>
          <span className="k"><OpIcon name="alert" size={11} /> hack?</span> {state.reward_hacks.length}</button>}
        {finalizing
          ? <span className="chip warn"><OpIcon name="stop" size={11} />
              {lifecycle.mode === 'finalization-stalled' ? 'finalization stalled'
                : lifecycle.mode === 'finishing' ? 'finishing' : 'finalizing'}</span>
          : live.paused && <span className="chip warn"><OpIcon name="pause" size={11} /> paused</span>}
      </div>

      {configNoticeStatus && <div
        className={`route-state-notice run-config-notice ${configNoticeStatus}`}
        role={configNoticeStatus === 'error' ? 'alert' : 'status'}>
        <OpIcon name="alert" size={13} />
        <span>{configNoticeStatus === 'retrying'
          ? 'Retrying run budget…'
          : configNoticeStatus === 'stale'
            ? 'Run budget may be stale. Showing the last loaded limit.'
            : 'Run budget unavailable. Current eval time is visible, but its limit could not be loaded.'}</span>
        <button type="button" className="btn xs"
          disabled={configNoticeStatus === 'retrying'}
          onClick={() => configResource.retry()}>
          {configNoticeStatus === 'retrying' ? 'Retrying…' : 'Retry'}
        </button>
      </div>}
      {!reviewMode && retainedCommentWorkUnsafe && <div
        className="route-state-notice run-comments-recovery-notice"
        role={retainedCommentRecoveryUnavailable || retainedCommentRecovery.damaged.length > 0
          ? 'alert' : 'status'}>
        <OpIcon name="chat" size={13} />
        <span>{retainedCommentEntries.length > 0
          ? `${retainedCommentEntries.length} in-memory Comments work item${retainedCommentEntries.length === 1 ? ' is' : 's are'} retained in this tab. `
          : ''}
          {retainedCommentRecovery.valid.length > 0
            ? `${retainedCommentRecovery.valid.length} exact Comments command recover${retainedCommentRecovery.valid.length === 1 ? 'y is' : 'ies are'} saved in this tab.`
            : ''}
          {retainedCommentRecovery.damaged.length > 0
            ? ` ${retainedCommentRecovery.damaged.length} damaged recovery record${retainedCommentRecovery.damaged.length === 1 ? ' needs' : 's need'} review.`
            : ''}
          {retainedCommentRecoveryUnavailable
            ? ' Comments recovery storage cannot be inspected. Start over remains blocked; retry storage before continuing.'
            : ''}
          {retainedCommentValidProtectedCreateCount > 0
            ? ` ${retainedCommentValidProtectedCreateCount} current-generation new-comment recover${retainedCommentValidProtectedCreateCount === 1 ? 'y stays' : 'ies stay'} protected until the exact command reaches a terminal outcome.`
            : ''}
          {retainedCommentDamagedProtectedCreateCount > 0
            ? ` ${retainedCommentDamagedProtectedCreateCount} damaged new-comment recover${retainedCommentDamagedProtectedCreateCount === 1 ? 'y cannot' : 'ies cannot'} be safely released; restore the exact recovery data before continuing.`
            : ''}
          {currentCommentRecovery
            ? ' Open its experiment to check the same command; nothing replays automatically.'
            : retainedCommentDurableCount > 0
              ? ' The saved commands belong to an earlier generation and will not be rebound.'
              : ' Review the retained work before leaving this run or starting over.'}
        </span>
        {(retainedCommentDrafts.length > 0 || retainedCommentRecovery.valid.length > 0) && <details>
          <summary>View</summary>
          {retainedCommentDrafts.map((text, index) => <pre key={`draft:${index}`}
            className="comment-recovery-payload">{text}</pre>)}
          {retainedCommentRecovery.valid.map(intent => <div key={intent.storageKey}
            className="comment-recovery-payload">
            <b>{intent.kind === 'create' ? 'New comment' : intent.kind === 'edit'
              ? 'Comment edit' : intent.resolved ? 'Resolve comment' : 'Reopen comment'}</b>
            {' '}— experiment #{intent.nodeId} — attempt {intent.nodeGeneration}
            {intent.text != null && <pre>{intent.text}</pre>}
          </div>)}
        </details>}
        {currentCommentRecovery && <button type="button" className="btn xs"
          onClick={openCurrentCommentRecovery}>Open recovery</button>}
        {(retainedCommentDrafts.length > 0 || retainedCommentRecovery.valid.length > 0)
          && <button type="button" className="btn xs" onClick={copyRetainedCommentWork}>Copy</button>}
        {retainedCommentRecoveryUnavailable && <button type="button" className="btn xs"
          onClick={refreshCommentOperationRecoveries}>Retry storage</button>}
        {retainedCommentReleasableCount > 0 && <button type="button" className="btn xs ghost"
          onClick={discardRetainedCommentWork}>{retainedCommentProtectedCreates.length > 0
            ? 'Discard other work…' : 'Discard…'}</button>}
      </div>}
      {!reviewMode && startOverRecovery.kind !== 'none' && <div
        ref={startOverNoticeRef} tabIndex={-1}
        className={`route-state-notice run-start-over-notice ${
          startOverRecovery.kind === 'active' ? startOverIntent?.phase || '' : 'error'}`}
        role={startOverRecovery.kind === 'active' && startOverIntent?.phase !== 'unknown'
          ? 'status' : 'alert'} aria-live="polite" aria-atomic="true">
        <OpIcon name="alert" size={13} />
        <span>{startOverRecovery.kind === 'active'
          ? startOverReplacementSuperseded
            ? 'The saved Start over operation is verified, and a newer run generation is already visible. Open the current run to clear the resolved recovery lock.'
            : startOverHandoff && startOverRouteSyncFailed
              ? 'The new run is ready, but this page could not update its address. Changes stay locked until the current generation is opened safely.'
            : startOverIntent.phase === 'submitting'
            ? 'Submitting this exact Start over request. No archive is assumed yet; other changes are locked.'
            : startOverIntent.phase === 'pending'
              ? 'The server saved this exact Start over request. Checking the same operation automatically; other changes remain locked.'
            : startOverIntent.phase === 'accepted'
              ? 'Start over was accepted. Waiting for the new run generation…'
              : 'Start-over outcome is not confirmed. Retry the exact saved request; all other changes are locked.'
          : startOverRecovery.kind === 'unavailable'
            ? 'Start over is disabled because this tab cannot preserve recovery state.'
            : 'Saved Start over recovery state is invalid. Changes are locked because an earlier request cannot be identified safely. Close this tab and reopen LoopLab only after confirming no Start over is still running.'}
          {startOverRecovery.storageUnavailable
            ? ' This tab can still observe the current request, but its recovery record could not be updated.'
            : ''}
        </span>
        {startOverRecovery.kind === 'active' && <button type="button" className="btn xs"
          onClick={retryStartOver} disabled={startOverRequestPending}>
          {startOverRequestPending ? 'Waiting for response…'
            : startOverReplacementSuperseded ? 'Open current run'
              : startOverHandoff && startOverRouteSyncFailed ? 'Retry opening new run'
                : 'Retry exact request'}
        </button>}
        {startOverRecovery.kind === 'unavailable' && <button type="button" className="btn xs"
          onClick={retryStartOverStorage}>Try storage again</button>}
      </div>}
      {(routeNotice || attemptFenceNotice || route.issues.length > 0) && <div
        ref={routeNoticeRef} className="route-state-notice" role="status" tabIndex={-1}>
        <OpIcon name="info" size={13} />
        <span>{[...route.issues, routeNotice, attemptFenceNotice].filter(Boolean).join(' ')}</span>
        <button type="button" className="btn xs ghost" aria-label="Dismiss link-state notice"
          onClick={() => { setRouteNotice(''); setAttemptFenceNotice(''); route.clearIssues() }}>×</button>
      </div>}
      {copyFallback && <div className="copy-link-fallback" role="status">
        <label htmlFor="copy-view-fallback">Clipboard blocked — select and copy this link</label>
        <input id="copy-view-fallback" readOnly value={copyFallback} onFocus={event => event.currentTarget.select()} />
        <button type="button" className="btn xs ghost" onClick={() => setCopyFallback('')} aria-label="Close copy fallback">×</button>
      </div>}

      {reviewMode && <div className="review-banner" role="status">
        <span className="history-lock" aria-hidden="true">◈</span>
        <b>Read-only review</b>
        <span>{(reviewMeta?.scopes || []).includes('evidence') ? 'summary + redacted source evidence' : 'summary only'}</span>
        {reviewMeta?.expires_at && <span>expires {new Date(reviewMeta.expires_at * 1000).toLocaleString()}</span>}
        <span className="spacer" />
        {!connected && <span className="review-refresh-warn" role="alert">Refresh interrupted — showing the last received data.</span>}
        <span>Actions, Assistant, raw logs, artifacts, and owner settings are unavailable.</span>
      </div>}

      {historyActive && <div className="history-banner" role="status">
        <span className="history-lock" aria-hidden="true">◷</span>
        <b>Historical snapshot · gen {gen} · seq {history.resolvedSeq} of {seq}</b>
        <span>read-only · actions target live and are disabled</span>
        <button className="btn sm primary" onClick={returnToLiveAndFocusWorkspace}>Return to live</button>
      </div>}

      {/* All actions now run through the chat (type a /command or just say what to do). The approval
          phase shows an inline reminder of the exact command to type. */}
      {!mutationReadOnlyMode && (live.phase === 'approval' || live.phase === 'spec_approval') &&
        <div className="topbar" role={approvalCommand ? 'status' : 'alert'}
          style={{ background: 'rgba(74,163,255,.12)', borderBottom: '1px solid var(--accent-dim)' }}>
          {approvalCommand ? <>
            <b>{live.phase === 'approval'
              ? `Human approval required for experiment #${live.approval_subject} — type `
              : 'Eval spec needs ratification — type '}</b>
            <code className="cmd-hint">{approvalCommand}</code>
            <b>&nbsp;in the chat below.</b>
          </> : <>
            <b>Approval target is missing.</b>
            <span>Inspect Events before acting; no command has been guessed.</span>
            <button className="btn sm" onClick={revealEvents}>Show events</button>
          </>}
        </div>}

      <div className={'topbar panel-bar' + (openHub ? ' hub-open' : '')}
        style={{ minHeight: 38, paddingTop: 4, paddingBottom: 4 }}>
        <div className="menu">
          {/* 4 consolidated hubs — each a dropdown of related panels; the hub lights when its open
              panel is active. Report/Overview live in the view-toggle above; Settings is dedicated. */}
          <div className="panel-hubs">
            {HUBS.map(([label, items]) => <div className="more-wrap" key={label}>
              <button type="button" className={'btn sm ghost' + (HUB_OF[panel] === label ? ' on' : '')}
                      aria-haspopup="menu" aria-expanded={openHub === label} aria-controls={hubMenuId(label)}
                      disabled={!items.some(([key]) => panelAllowed(key))}
                      title={!items.some(([key]) => panelAllowed(key)) ? 'No panels in this group are available in the current view' : undefined}
                      onClick={event => { hubTriggerRef.current = event.currentTarget; setOpenHub(o => o === label ? null : label) }}>{label} ▾</button>
              {openHub === label && <>
                <div className="menu-backdrop" aria-hidden="true" onClick={() => closeHub(true)} />
                <div ref={hubMenuRef} id={hubMenuId(label)} className="run-menu more-menu" role="menu"
                  aria-label={`${label} panels`} onClick={e => e.stopPropagation()} onKeyDown={onHubKeyDown}
                  onBlur={event => {
                    if (event.relatedTarget !== hubTriggerRef.current && !event.currentTarget.contains(event.relatedTarget)) closeHub(false)
                  }}>
                  {items.map(([k, l]) => <button type="button" role="menuitem" tabIndex={-1}
                    key={k} className={'mi' + (panel === k ? ' on' : '')}
                    disabled={!panelAllowed(k)}
                    title={!panelAllowed(k)
                      ? reviewMode
                        ? k === 'compare' && !reviewEvidence
                          ? 'Requires a review link with redacted evidence'
                          : 'Unavailable in read-only review'
                        : 'Unavailable while viewing a historical snapshot'
                      : undefined}
                    onClick={() => {
                      if (!panelAllowed(k)) return
                      panelReturnFocusRef.current = hubTriggerRef.current; closeHub(false); setPanel(k)
                    }}>{l}</button>)}
                  {/* Anchors, not buttons, for the same reason the LoopLab menu uses them: the hash
                      is a bookmarkable URL, so middle-click and copy-link work. Hidden in review
                      mode, which is public and must not advertise installation-wide surfaces —
                      the same rule that keeps `<GlobalMenu />` out of that header. */}
                  {label === INSTALLATION_LINK_HUB && !reviewMode && <>
                    <div className="mi-label">Whole installation</div>
                    {INSTALLATION_LINKS.map(entry => <a key={entry.key} role="menuitem" tabIndex={-1}
                      className="mi" href={entry.hash} title={entry.title}
                      onClick={() => closeHub(false)}>{entry.label} ↗</a>)}
                  </>}
                </div>
              </>}
            </div>)}
          </div>
          <span className="panel-sep" />
          {/* "Run settings" in full: this edits THIS run's live config, while the LoopLab menu's
              Settings edits the engine defaults for every future run. Two buttons both labelled
              "Settings", one screen apart, was the sharpest edge of the same conflation. */}
          <button className={'btn sm ghost settings-panel-btn' + (panel === 'config' ? ' on' : '')}
                  disabled={mutationReadOnlyMode} title="Budgets and knobs for this run only"
                  onClick={event => { setOpenHub(null); panelReturnFocusRef.current = event.currentTarget; setPanel('config') }}>Run settings</button>
        </div>
      </div>

      {mergeFrom != null && !mutationReadOnlyMode && <form className="merge-destination-bar"
        aria-label={`Choose a merge destination for experiment ${mergeFrom}`} onSubmit={submitMergeTarget}>
        <label htmlFor="merge-destination-select">Merge <b>#{mergeFrom}</b> with</label>
        <select ref={mergeSelectRef} id="merge-destination-select" value={mergeTarget} disabled={mergeSubmitting}
          onChange={event => {
            if (!event.target.value) {
              setMergeIntent(current => current
                ? { ...current, targetId: null, targetAttempt: null } : current)
              return
            }
            const next = selectMergeTarget(mergeIntent, {
              runId, runGeneration: generation, nodes: live?.nodes,
            }, Number(event.target.value))
            if (next) setMergeIntent(next)
            else {
              showToast('Merge cancelled because the run or one of its experiment attempts changed.')
              closeMergeChooser(true)
            }
          }} autoFocus>
          <option value="">Choose an experiment…</option>
          {mergeCandidates.map(node => <option key={node.id} value={node.id}>
            #{node.id} · {node.operator || 'experiment'} · {node.status}
          </option>)}
        </select>
        <button ref={mergeConfirmRef} type="submit" className="btn sm primary" disabled={!mergeTarget || mergeSubmitting}>
          {mergeSubmitting ? 'Merging…' : 'Confirm merge'}
        </button>
        <button type="button" className="btn sm ghost" disabled={mergeSubmitting}
          onClick={() => closeMergeChooser(true)}>Cancel</button>
        <span className="muted">Graph gestures only choose the pair; nothing is sent until you confirm.</span>
      </form>}

      {view === 'report'
        ? <div className="main"><div className="report-scroll">
            <LazyBoundary label="run report" resetKey={`${runId}:${history.resolvedSeq ?? 'live'}`}>
              <ReportView state={state} runId={runId} onToast={showToast}
                readOnly={mutationReadOnlyMode} historySeq={history.resolvedSeq}
                observedSeq={historyActive ? history.resolvedSeq : seq}
                expectedGeneration={reportGeneration}
                readOnlyReason={mutationReadOnlyReason} evidenceAvailable={!reviewMode || reviewEvidence}
                onOpenPanel={(p, returnFocus) => {
                  if (!panelAllowed(p)) return
                  panelReturnFocusRef.current = returnFocus || null
                  setPanel(p)
                }}
                canOpenPanel={panelAllowed}
                onPickNode={(id) => {
                  route.update(current => ({ ...current, view: 'dag', nodeId: id,
                    nodeGeneration: null, commentId: null }))
                  commitGroupNavigation(null, { mode: 'replace' })
                  if (compactWorkspace) setCompactInspectorOpen(true)
                  else setSideC(false)
                }} />
            </LazyBoundary>
          </div></div>
        : view === 'concepts'
        ? <div className="main"><LazyBoundary label="concept tree"
            resetKey={`${runId}:${generation || 'pending'}:${historyActive ? viewSeq : 'live'}`}>
            <ConceptView runId={runId} generation={generation}
              sequence={historyActive ? viewSeq : null} state={state} onPickNode={(id) => {
              route.update(current => ({ ...current, view: 'dag', nodeId: id,
                nodeGeneration: null, commentId: null }))
              commitGroupNavigation(null, { mode: 'replace' })
              if (compactWorkspace) setCompactInspectorOpen(true)
              else setSideC(false)
            }} />
          </LazyBoundary></div>
        : <>
      <LazyBoundary label="concept filter" resetKey={`${runId}:${generation || 'pending'}`}>
        <ConceptChipBar key={`concept-filter:${runId}:${generation || 'pending'}`}
          state={state} onHighlight={setConceptHighlight} />
      </LazyBoundary>
      <WhyStrip state={state} onSelect={selectNode} />
      <div className={'main run-workspace' + (compactWorkspace ? ' compact' : '')}>
        <div className={'canvas-wrap' + (emptyPresentation ? ' dag-empty' : '')}
          inert={compactWorkspace && showInspector ? '' : undefined}
          aria-hidden={compactWorkspace && showInspector ? 'true' : undefined}>
          <LazyBoundary label="experiment graph" resetKey={`${runId}:${generation || 'pending'}`}>
            <Dag key={`experiment-graph:${runId}:${generation || 'pending'}`}
              state={state} selectedId={selectedId} onSelect={onCanvasSelect}
              compact={compactWorkspace}
              groupMode={groupMode} collapsed={collapsed} onToggleGroup={toggleGroup} onSetMode={changeMode}
              onCollapseAll={collapseAllGroups} onExpandAll={expandAllGroups}
              onAutoCollapse={autoCollapse} onNodeAction={mutationReadOnlyMode ? null : onNodeAction}
              mergeArm={mutationReadOnlyMode ? null : mergeFrom}
              selectedGroup={selectedGroup} onSelectGroup={selectGroup} themeFilter={themeFilter}
              highlightIds={conceptHighlight} />
          </LazyBoundary>
          <DagEmptyOverlay presentation={emptyPresentation} transport={transportController}
            onAction={onEmptyAction} />
        </div>
        {compactWorkspace && !showInspector && hasInspectorContext &&
          <button ref={compactInspectorTriggerRef} className="workspace-pane-toggle" onClick={() => setCompactInspectorOpen(true)}
                  aria-label={`Open ${groupDetailsOpen ? 'group' : 'inspector'} panel`}>
            {groupDetailsOpen ? 'Group' : `Inspector · #${selectedId}`}
          </button>}
        {compactWorkspace && showInspector &&
          <button type="button" className="workspace-scrim" tabIndex={-1}
                  onClick={closeCompactInspector}
                  aria-label={`Close ${groupDetailsOpen ? 'group' : 'inspector'} panel`} />}
        {!compactWorkspace && hasInspectorContext && !showInspector
          ? <button ref={sideRailRef} className="side-rail" title="show panel"
              onClick={() => setSideC(false)}>‹ {groupDetailsOpen ? 'group' : 'inspector'}</button>
          : showInspector && <>
              {!compactWorkspace && <div className="splitter v" onPointerDown={startDrag('side')} onKeyDown={resizeWithKeys('side')}
                role="separator" tabIndex={0} aria-orientation="vertical" aria-label="Resize inspector"
                aria-valuemin={280} aria-valuemax={Math.max(280, window.innerWidth - 486)} aria-valuenow={Math.round(sideW)} title="Drag or use arrow keys to resize" />}
              <aside className={'side' + (compactWorkspace ? ' compact-drawer' : '')} style={{ width: sideW }}
                     ref={compactInspectorRef} tabIndex={compactWorkspace ? -1 : undefined}
                     aria-label={groupDetailsOpen ? 'Group details' : 'Experiment inspector'}
                     role={compactWorkspace ? 'dialog' : 'complementary'}
                     data-route-focus-guard={compactWorkspace ? 'true' : undefined}>
                <div className="pane-grip">
                  <span className="muted">{groupDetailsOpen ? 'group' : 'inspector'}</span>
                  <span className="spacer" style={{ flex: 1 }} />
                  <button ref={compactInspectorCloseRef} className="btn sm ghost"
                          data-dialog-initial-focus={compactWorkspace ? true : undefined}
                          title={compactWorkspace ? 'close panel' : 'collapse panel'}
                          aria-label={`${compactWorkspace ? 'Close' : 'Collapse'} ${groupDetailsOpen ? 'group details' : 'experiment inspector'}`}
                          onClick={() => compactWorkspace ? closeCompactInspector() : collapseSideInspector()}>⟩</button>
                </div>
                <LazyBoundary label={groupDetailsOpen ? 'group details' : 'experiment inspector'}
                  resetKey={groupDetailsOpen ? `group:${selectedGroup}` : `node:${selectedId}`}>
                  {groupDetailsOpen
                    ? <GroupSummary groupKey={selectedGroup} memberIds={groupMembers}
                        state={state} themeFilter={themeFilter} highlightIds={conceptHighlight}
                        onSelectNode={focusGroupMember}
                        onClose={() => selectGroup(null)} />
                    : <Inspector runId={runId} nodeId={selectedId} state={state} live={live}
                         tab={effectiveInspectTab} setTab={setInspectTab} onToast={showToast}
                         draftStore={inspectorDraftStoreRef.current}
                         traceClearRecoveryStore={traceClearRecoveryStore}
                        traceClearRecoverySnapshot={traceClearRecoverySnapshot}
                        publishTraceClearRecovery={publishTraceClearRecovery}
                        readOnly={mutationReadOnlyMode} historySeq={history.resolvedSeq}
                        expectedGeneration={generation} commentsRevision={state?.comments_revision}
                        focusCommentId={commentAttemptMatches ? routeState.commentId : null}
                        readOnlyReason={mutationReadOnlyReason} evidenceAvailable={!reviewMode || reviewEvidence} />}
                </LazyBoundary>
              </aside>
            </>}
      </div>
        </>}

      {(view === 'dag' || view === 'report') && <>
      {!reviewMode && (timelineDeferred || reportTimelineFocusPending)
        && <div className="dock timeline-deferred-trigger" aria-busy={reportTimelineFocusPending}>
        <div className="dock-tabs">
          <strong>{reportTimelineFocusPending ? 'Loading events & timeline…' : 'Events & timeline'}</strong>
          <span className="spacer" />
          <button type="button" className="btn sm ghost dock-collapse"
            aria-label="Load events and timeline" aria-expanded="false"
            onClick={activateReportTimeline}>
            <OpIcon name="chevron-up" size={13} />
          </button>
        </div>
      </div>}
      {!reviewMode && !timelineDeferred && !compactWorkspace && !timelineCollapsed && <div className="splitter h" onPointerDown={startDrag('dock')} onKeyDown={resizeWithKeys('dock')}
        role="separator" tabIndex={0} aria-orientation="horizontal" aria-label="Resize timeline"
        aria-valuemin={MIN_DOCK_HEIGHT} aria-valuemax={Math.max(MIN_DOCK_HEIGHT, window.innerHeight - 470)} aria-valuenow={Math.round(dockH)} title="Drag or use arrow keys to resize" />}
      {!reviewMode && !timelineDeferred && <LazyBoundary label="timeline" resetKey={`${runId}:${generation || 'pending'}`}>
        <Dock runId={runId} live={live} liveSeq={seq} expectedGeneration={generation}
          timeline={timeline}
          viewSeq={viewSeq} setViewSeq={changeViewSeq} onReturnToLive={returnToLive} onFocus={focusNode}
          filter={routeState.timelineFilter}
          onFilterChange={value => route.update(current => ({ ...current, timelineFilter: value }), { mode: 'replace' })}
          kindFilters={routeState.timelineKinds}
          onKindFiltersChange={value => route.update(current => ({ ...current, timelineKinds: value }))}
          onToast={showToast}
          startOverState={{
            blocked: retainedPanelDraftUnsafe || retainedCommentWorkUnsafe
              || startOverRecovery.kind !== 'none'
              || !/^[0-9a-f]{64}$/.test(generation || ''),
            lifecycleBlocked: startOverRecovery.kind === 'active'
              || startOverRecovery.kind === 'corrupt',
            disabledReason: retainedPanelDraftUnsafe
              ? 'Finish or explicitly discard retained Run settings or Authoring work first'
              : retainedCommentWorkUnsafe
              ? 'Review retained Comments work or retry unavailable recovery storage first'
              : startOverRecovery.kind === 'active'
              ? 'Resolve the existing Start over outcome first'
              : startOverRecovery.kind === 'unavailable'
                ? 'Start over needs working recovery storage in this tab'
                : startOverRecovery.kind === 'corrupt'
                  ? 'Saved Start over recovery evidence is invalid'
                  : 'Wait for the current run generation before starting over',
            phase: startOverIntent?.phase || null,
          }}
          onStartOver={submitStartOver}
          readOnly={mutationReadOnlyMode}
          publishTransport={setTransportController}
          collapsed={timelineCollapsed}
          collapseControlRef={timelineCollapseRef}
          focusOnMount={reportTimelineFocusPending}
          onInitialFocus={() => setTimelineActivation(current => current.runId === runId
            ? { ...current, focusPending: false } : current)}
          onToggleCollapse={() => {
            if (compactWorkspace) setCompactTimelineOpen(v => !v)
            else setDockC(c => !c)
          }}
          height={dockH} />
      </LazyBoundary>}
      </>}

      {panel && panelAllowed(panel) && <LazyBoundary label={`${HUB_OF[panel] || panel} panel`}
        mode="overlay" resetKey={`${panel}:${runId}@${generation || 'pending'}`} onClose={closePanel}>
      <>
      {panel === 'overview' && panelAllowed('overview') && <OverviewPanel state={state} maxEval={maxEval} onClose={closePanel}
        onOpenPanel={p => { if (panelAllowed(p)) setPanel(p, { mode: 'replace' }) }} />}
      {panel === 'research' && panelAllowed('research') && <ResearchPanel state={state} runId={runId} onToast={showToast} onClose={closePanel} />}
      {panel === 'trust' && panelAllowed('trust') && <TrustPanel state={state} runId={runId} onSelect={selectNodeFromPanel} onToast={showToast} onClose={closePanel} readOnly={mutationReadOnlyMode} />}
      {panel === 'queue' && panelAllowed('queue') && <QueuePanel state={state} runId={runId} onSelect={selectNodeFromPanel} onToast={showToast} onClose={closePanel} />}
      {panel === 'hypotheses' && panelAllowed('hypotheses') && <HypothesisBoard state={state}
        runId={runId} runGeneration={generation} onSelect={selectNodeFromPanel} onToast={showToast}
        onClose={closePanel} />}
      {panel === 'sensitivity' && panelAllowed('sensitivity') && <SensitivityPanel state={state} onSelect={selectNodeFromPanel} onClose={closePanel} />}
      {panel === 'importance' && panelAllowed('importance') && <HyperImportancePanel state={state} onClose={closePanel} />}
      {panel === 'failures' && panelAllowed('failures') && <FailuresPanel state={state} onSelect={selectNodeFromPanel} onClose={closePanel} />}
      {panel === 'pareto' && panelAllowed('pareto') && <ParetoPanel state={state} onSelect={selectNodeFromPanel} onClose={closePanel} />}
      {panel === 'data' && panelAllowed('data') && <DataQualityPanel state={state} onClose={closePanel} />}
      {panel === 'compare' && panelAllowed('compare') && <ComparePanel state={state} runId={runId} initialPair={comparePair}
        expectedGeneration={generation} reviewMode={reviewMode}
        onClose={() => { closePanel(); setComparePair(null) }} />}
      {panel === 'crossrun' && panelAllowed('crossrun') && <CrossRunPanel state={state} onClose={closePanel} />}
      {panel === 'collab' && panelAllowed('collab') && <CollabPanel state={state} runId={runId}
        onSelect={selectNode} onOpenComment={openComment} onToast={showToast} onClose={closePanel}
        reviewRouteState={routeState} reviewMode={reviewMode}
        draftStore={inspectorDraftStoreRef.current}
        expectedGeneration={generation} refreshKey={state?.comments_revision} />}
      {panel === 'config' && panelAllowed('config') && <ConfigPanel runId={runId}
        expectedGeneration={generation} state={state} live={live} onToast={showToast}
        onClose={closePanel} draftStore={inspectorDraftStoreRef.current}
        navigationGuardOwner="run" publishNavigationGuard={publishPanelNavigationGuard} />}
      {panel === 'authoring' && panelAllowed('authoring') && <AuthoringPanel onToast={showToast}
        onClose={closePanel} draftStore={inspectorDraftStoreRef.current}
        navigationGuardOwner="run" publishNavigationGuard={publishPanelNavigationGuard} />}
      {panel === 'memory' && panelAllowed('memory') && <MemoryPanel onClose={closePanel} />}
      {panel === 'registry' && panelAllowed('registry') && <RegistryPanel state={state} onClose={closePanel} />}
      {panel === 'gpu' && panelAllowed('gpu') && <GpuPanel onClose={closePanel} />}
      {panel === 'events' && panelAllowed('events') && <EventExplorer runId={runId} timeline={timeline}
        historyActive={historyActive} onReturnToLive={returnToLive} onClose={closePanel} />}
      {panel === 'artifacts' && panelAllowed('artifacts') && <ArtifactsPanel
        key={`artifacts:${runId}@${generation || 'pending'}`} runId={runId}
        expectedGeneration={generation} onToast={showToast} onClose={closePanel} />}
      </>
      </LazyBoundary>}

      {toast && <div className="toast" role="status" aria-live="polite" aria-atomic="true">{toast}</div>}
    </main>
  )
}
