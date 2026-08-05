// The one definition of the LoopLab/run navigation split.
//
// Two menus, two scopes, and the rule that tells them apart: a surface belongs to the RUN menu when
// its content is a function of that run's event log (its nodes, its trace, its budget, its cards);
// it belongs to the LOOPLAB menu when the same screen is true for the whole installation no matter
// which run — or no run — is open. The backend already draws that line: run-scoped reads live under
// `/api/runs/{run_id}/…` (serve/routers/runs.py, control.py, collaboration.py) while installation
// reads are bare `/api/…` (misc.py's `/api/memory`, `/api/gpu`, `/api/{kind}`; cross_run.py's
// `/api/cross-run/*`; `/api/settings`). Three run panels were on the wrong side of it — they take no
// run id and never did — so this module names them once and both menus read the same list.

// Ordered LoopLab (installation-wide) destinations. `key` matches App's route view so the open
// destination can be marked without a second mapping.
export const GLOBAL_DESTINATIONS = Object.freeze([
  Object.freeze({
    key: 'list', hash: '#/', label: 'Runs',
    title: 'Every run in this installation — list, map, portfolio comparison and projects',
  }),
  Object.freeze({
    key: 'research-atlas', hash: '#/atlas', label: 'Research Atlas',
    title: 'Experimental cross-run portfolio evidence: concepts, claims and steward proposals',
  }),
  Object.freeze({
    key: 'memory', hash: '#/memory', label: 'Cross-run memory',
    title: 'Lessons, solved-task cases and meta-notes carried between runs',
  }),
  Object.freeze({
    key: 'knowledge', hash: '#/knowledge', label: 'Knowledge & prompts',
    title: 'Authored prompts, skills and knowledge notes shared by every run',
  }),
  Object.freeze({
    key: 'gpu', hash: '#/gpu', label: 'Host & GPU',
    title: 'Live nvidia-smi telemetry for the machine this server runs on',
  }),
  Object.freeze({
    key: 'settings', hash: '#/settings', label: 'Settings',
    title: 'Engine defaults applied to every new run',
  }),
])

// Route view -> the run-workspace panel it hosts. These three panel components are unchanged; the
// split only moves where they are REACHED from. Keeping them mounted in the run workspace too is
// what makes every `#/run/<id>?panel=memory|authoring|gpu` bookmark and doc URL keep working.
export const INSTALLATION_ROUTE_PANEL = Object.freeze({
  memory: 'memory',
  knowledge: 'authoring',
  gpu: 'gpu',
})

export const INSTALLATION_ROUTE_VIEWS = Object.freeze(Object.keys(INSTALLATION_ROUTE_PANEL))

// Run panels whose content is installation-wide. They stay in `RUN_ROUTE_PANELS` (deep links must
// not 404) but are no longer offered by the run menu — the LoopLab menu owns them now.
export const INSTALLATION_SCOPED_RUN_PANELS = Object.freeze(
  new Set(Object.values(INSTALLATION_ROUTE_PANEL)),
)

const HASH_FOR_PANEL = new Map(
  Object.entries(INSTALLATION_ROUTE_PANEL).map(([view, panel]) => [
    panel, GLOBAL_DESTINATIONS.find(entry => entry.key === view)?.hash || '#/',
  ]),
)

/** The LoopLab destination that owns a run panel key, or null when the panel is genuinely run-scoped. */
export function globalHashForRunPanel(panel) {
  return HASH_FOR_PANEL.get(panel) || null
}

/** Is this a LoopLab route view (as opposed to `list`, `run`, `settings`, …)? */
export function isInstallationRouteView(view) {
  return Object.hasOwn(INSTALLATION_ROUTE_PANEL, String(view))
}

export function globalDestination(key) {
  return GLOBAL_DESTINATIONS.find(entry => entry.key === key) || null
}

/** Route label for the document title / live region. */
export function installationRouteLabel(view) {
  return globalDestination(view)?.label || 'LoopLab'
}
