// What code and configuration an experiment ran with (doc 77 §1–2), as a pure projection.
//
// The run pins its editable sources at `run_started.workspace` — `{"editable:.": "git:<sha>"}` for a
// git checkout, `hash:<digest>` for a copy without `.git` (every MiniOneRec run since 2026-10-13),
// `file:…`, or `absent` — and the files that were uncommitted then (`dirty_inputs`). Nothing showed
// either. A non-git copy must read as "commit unknown", never as clean: the digest proves which
// bytes, not which commit.
const SHA_RE = /^[0-9a-f]{7,64}$/i

export function workspaceRows(workspace) {
  if (!workspace || typeof workspace !== 'object' || Array.isArray(workspace)) return []
  return Object.entries(workspace).slice(0, 32).map(([source, raw]) => {
    const value = typeof raw === 'string' ? raw : ''
    const name = String(source).replace(/^editable:/, '').replace(/^data:/, 'data: ') || '.'
    if (value.startsWith('git:') && SHA_RE.test(value.slice(4)))
      return { source: name, kind: 'git', status: 'captured', commit: value.slice(4), digest: '' }
    if (value.startsWith('hash:'))
      return { source: name, kind: 'copy', status: 'unknown', commit: '', digest: value.slice(5, 69) }
    if (value.startsWith('file:'))
      return { source: name, kind: 'file', status: 'unknown', commit: '', digest: value.slice(5, 69) }
    if (value === 'absent') return { source: name, kind: 'absent', status: 'unknown', commit: '', digest: '' }
    return { source: name, kind: 'other', status: 'unknown', commit: '', digest: value.slice(0, 64) }
  })
}

export function dirtyInputRows(dirty) {
  if (!Array.isArray(dirty)) return []
  return dirty.slice(0, 200).map(row => {
    if (typeof row === 'string') return { path: row, status: '' }
    if (!row || typeof row !== 'object') return null
    const path = typeof row.path === 'string' ? row.path : typeof row.file === 'string' ? row.file : ''
    const status = typeof row.status === 'string' ? row.status.slice(0, 8) : ''
    return path ? { path: path.slice(0, 400), status } : null
  }).filter(Boolean)
}

// `runtime-params` rows grouped by their carrier, and the declared-vs-document comparison, with the
// statuses that must never read as "empty": a withheld secret file, an unparsed or absent carrier.
export function runtimeParamsView(value) {
  if (!value || typeof value !== 'object') return null
  const carriers = (Array.isArray(value.carriers) ? value.carriers : []).filter(c => c && typeof c.path === 'string')
  const params = (Array.isArray(value.params) ? value.params : []).filter(p => p && typeof p.key === 'string')
  const comparisons = (Array.isArray(value.comparisons) ? value.comparisons : [])
    .filter(c => c && typeof c.param === 'string')
  return { carriers, params, comparisons, truncated: value.truncated === true }
}

// The attempt's own row of `GET /api/runs/{id}/code` (doc 79 §3, §6): its commit in the run-owned
// repository, the base it was seeded from, the changed paths, and the effective configuration the
// evaluation actually used. `null` when the run's repository has no row for this attempt.
export function attemptCodeRow(code, nodeId, attempt) {
  const rows = Array.isArray(code?.attempts) ? code.attempts : []
  const mine = rows.filter(r => r && r.node_id === nodeId && r.generation === attempt)
  return mine.find(r => r.current) || mine[mine.length - 1] || null
}

const scalar = value => value == null ? '' : typeof value === 'object'
  ? (Object.hasOwn(value, 'value') ? String(value.value) : JSON.stringify(value)).slice(0, 200)
  : String(value).slice(0, 200)

export function effectiveConfigRows(config) {
  const effective = config && typeof config.effective === 'object' && !Array.isArray(config.effective)
    ? config.effective : {}
  return Object.entries(effective).slice(0, 64).map(([key, value]) => ({
    key, value: scalar(value),
    source: value && typeof value === 'object' && typeof value.source === 'string' ? value.source.slice(0, 120) : '',
  }))
}
