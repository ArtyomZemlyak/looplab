import { nodeBase } from './baseRevision.js'

const index = value => Number.isSafeInteger(value) && value >= 0
const textFiles = value => value && typeof value === 'object' && !Array.isArray(value)
  && Object.values(value).every(body => typeof body === 'string')
const paths = value => Array.isArray(value) && value.every(path => typeof path === 'string')

// A conservative spelling-risk key, NOT an identity for any particular filesystem. Case
// expansion (ß/SS), canonical Unicode forms and Windows separators may name one slot there.
// Never use it to select an edit, rewrite a path or declare runtime deletion.
const pathRiskKey = path => path.replace(/\\/g, '/').normalize('NFD')
  .toUpperCase().toLowerCase().normalize('NFD')

// Describes saved source versions, not applied runtime files or command execution.
export function recordedFileOverlay(node, path) {
  if (typeof path !== 'string' || !textFiles(node?.files) || !paths(node?.deleted)) return { kind: 'unknown', text: null }
  // Main code is stored outside files and the sandbox writes it separately. A protected
  // solution.py helper/deletion must not hide that independently saved version.
  if (path === 'solution.py' && typeof node.code === 'string' && node.code.length > 0) {
    return { kind: 'main_code', text: node.code }
  }
  const key = pathRiskKey(path)
  const otherSpelling = candidate => candidate !== path && pathRiskKey(candidate) === key
  if (Object.keys(node.files).some(otherSpelling) || node.deleted.some(otherSpelling)
      || (typeof node.code === 'string' && node.code.length > 0 && otherSpelling('solution.py'))) {
    return { kind: 'path_ambiguity', text: null }
  }
  if (node.deleted.includes(path)) return { kind: 'deleted', text: null }
  if (Object.hasOwn(node.files, path)) return { kind: 'override', text: node.files[path] }
  return { kind: 'inherited', text: null }
}

// These are saved overlays, not complete Git trees. Removing an override may restore an inherited
// file; it must never be reported as deletion of that file from the materialized program.
export function nodeCodeModel(node, state) {
  const files = textFiles(node?.files) ? node.files : {}
  const deleted = paths(node?.deleted) ? node.deleted : []
  const source = node?.parent_edit
  const parentId = node?.parent_ids?.[0]
  const liveParent = state?.nodes?.[parentId]
  const available = Boolean(source?.version === 1 && source.scope === 'node_edit_overlay'
    && index(source.node_id) && source.node_id === parentId && index(source.attempt)
    && typeof source.code === 'string' && textFiles(source.files) && paths(source.deleted)
    && (typeof node.code === 'string' || node.code === null) && textFiles(node.files) && paths(node.deleted)
    && (!state?.nodes || (liveParent && !liveParent.tombstoned && liveParent.attempt === source.attempt)))
  const before = available ? source.files : {}
  const removed = new Set(available ? source.deleted : [])
  const afterDeleted = new Set(deleted)
  const rows = [...new Set([...Object.keys(before), ...Object.keys(files), ...removed, ...deleted])]
    .sort().map(path => {
      const oldKind = removed.has(path) ? 'deleted' : Object.hasOwn(before, path) ? 'override' : 'inherited'
      const newKind = afterDeleted.has(path) ? 'deleted' : Object.hasOwn(files, path) ? 'override' : 'inherited'
      const oldText = oldKind === 'override' ? before[path] : ''
      const newText = newKind === 'override' ? files[path] : ''
      return { path, oldKind, newKind, oldText, newText,
        changed: oldKind !== newKind || oldText !== newText }
    })
  const base = nodeBase(node)
  const parentBase = available ? nodeBase({ id: source.node_id, attempt: source.attempt,
    metric_provenance: { base_revision: source.base_revision } }) : null
  const code = typeof node?.code === 'string' ? node.code : ''
  const oldCode = available ? source.code : ''
  return { files, deleted, rows, available, parentId, parentAttempt: source?.attempt,
    base, parentBase, hasParent: Array.isArray(node?.parent_ids) && node.parent_ids.length > 0,
    code, oldCode, mainChanged: code !== oldCode }
}
