// The CAMPAIGN folders model — the pure half of `CampaignRuns.jsx` (the house pattern: decisions in a
// plain module `node --test` drives; the component keeps only the fetch and the markup).
//
// `GET /api/campaign-runs` lists the runs inside the root's campaign folders (`<root>/<folder>/<run>`,
// doc 70 70.1), which `/api/runs` does not: every per-run route addresses a run by ONE path segment,
// so a nested run has no id a route accepts. The rows are therefore READ-ONLY here — a row opens
// nothing, and its folder says how to serve that folder as a root, where its runs ARE addressable.

export const MAX_CAMPAIGN_FOLDERS = 200
export const MAX_CAMPAIGN_RUNS = 500

const text = (value, max = 255) => (typeof value === 'string' ? value.slice(0, max) : '')
const count = value => (Number.isInteger(value) && value > 0 ? value : 0)

// Every field is read defensively: the payload is the server's, but a row this view renders must
// never throw on a partial or older shape, and a folder with no usable run is not a folder to show.
export function campaignFolders(payload) {
  const folders = Array.isArray(payload?.folders) ? payload.folders : []
  return folders.slice(0, MAX_CAMPAIGN_FOLDERS).flatMap(folder => {
    const name = text(folder?.folder)
    const runRoot = text(folder?.run_root, 4096)
    if (!name || !runRoot) return []
    const runs = (Array.isArray(folder.runs) ? folder.runs : []).slice(0, MAX_CAMPAIGN_RUNS)
      .flatMap(run => {
        const runId = text(run?.run_id)
        if (!runId) return []
        return [{
          runId,
          taskId: text(run.task_id) || null,
          phase: text(run.phase, 64) || null,
          bestMetric: Number.isFinite(run.best_metric) ? run.best_metric : null,
          nodes: Number.isInteger(run.nodes) && run.nodes >= 0 ? run.nodes : null,
          running: run.engine_running === true,
          updated: Number.isFinite(run.mtime) ? run.mtime : null,
        }]
      })
    return runs.length ? [{ folder: name, runRoot, runs, runsSkipped: count(folder.runs_skipped) }] : []
  })
}

export const foldersSkipped = payload => count(payload?.folders_skipped)

// The command that serves `runRoot` as a root. Quoted for a POSIX shell unless the path is plain, and
// a single quote inside is closed, escaped and reopened — so what is copied runs as shown.
export function openCommand(runRoot) {
  const path = text(runRoot, 4096)
  if (!path) return ''
  const plain = /^[A-Za-z0-9_./:\\-]+$/.test(path)
  return `looplab ui --run-root ${plain ? path : `'${path.replace(/'/g, `'\\''`)}'`}`
}
