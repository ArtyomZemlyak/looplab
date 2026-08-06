// The per-run settings panel, lifted out of panels.jsx (doc 25 UI-04). It is a component with its
// own draft persistence, mutation fencing and reconcile machinery, which is what makes it a module
// rather than one more function in the hub. panels.jsx re-exports it, so RunView still funnels every
// panel through one lazy chunk.
import React, { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { deadlineGet, fmtElapsedSeconds, CONTROL, saveRunConfig, commandFeedback,
  runApiPath } from './util.js'
import { OpIcon } from './icons.jsx'
import SettingsForm from './SettingsForm.jsx'
import { toForm, fromForm, settingsValidationErrors, loadSettingsSchema } from './settingsSchema.js'
import {
  reconcileAcceptedRecord, reconcileUnknownRecord, runConfigWriteDisposition, splitRunConfigPayload,
  validateRunConfigSaveAck,
} from './settingsModel.js'
import Panel from './PanelShell.jsx'
import { DataTable } from './accessibility.jsx'
import { deadlineRequest } from './requestDeadline.js'
import { installNavigationLossGuard } from './navigationLossGuard.js'
import { isRecord, PANEL_REQUEST_TIMEOUT_MS, RUN_GENERATION_RE } from './panelPrimitives.js'

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
