// STATE DELTAS on the run's SSE stream (doc 52 row 29): the browser half of
// `looplab/events/state_delta.py`. The server sends a full `state` frame first and, when it is smaller,
// a `state_delta` frame `{ version, base_seq, seq, generation, event_count, ops }` whose `ops` turn the
// payload the client holds into the next one: `["set", path, value]` replaces the subtree at `path`
// (lists are atomic — a changed list arrives whole), `["del", path]` removes a key. Pure and driven by
// `node --test` (`ui/test/stateDelta.test.js`); `hooks.js::useRunState` keeps only the choreography.
//
// A delta is applied ONLY to the exact snapshot it was computed against: `applyStateDelta` returns
// `null` when `base_seq` is not the held payload's `seq` (or the frame is malformed), and the hook
// then treats the stream as diverged and reconnects for a full frame — the same refusal a mismatched
// cursor gets. It never guesses, and it never mutates the held payload.

export const STATE_DELTA_VERSION = 1

const isDoc = value => !!value && typeof value === 'object' && !Array.isArray(value)

const cloneDicts = value => (isDoc(value)
  ? Object.fromEntries(Object.entries(value).map(([k, v]) => [k, cloneDicts(v)]))
  : value)

// Whether `frame` is a well-formed delta against a payload whose seq is `heldSeq`.
export function deltaApplies(frame, heldSeq) {
  if (!isDoc(frame) || frame.version !== STATE_DELTA_VERSION) return false
  if (!Number.isSafeInteger(frame.base_seq) || !Number.isSafeInteger(frame.seq)) return false
  if (!Array.isArray(frame.ops)) return false
  return String(frame.base_seq) === String(heldSeq)
}

export function applyOps(base, ops) {
  let root = cloneDicts(base)
  for (const op of ops) {
    if (!Array.isArray(op) || !Array.isArray(op[1])) throw new Error('malformed delta op')
    const [kind, path] = op
    if (kind === 'set' && op.length === 3) {
      if (!path.length) { root = cloneDicts(op[2]); continue }
    } else if (kind === 'del' && op.length === 2) {
      if (!path.length) throw new Error('cannot delete the root')
    } else throw new Error('malformed delta op')
    let node = root
    for (const key of path.slice(0, -1)) {
      let child = isDoc(node) ? node[key] : undefined
      if (!isDoc(child)) { child = {}; node[key] = child }
      node = child
    }
    const leaf = path[path.length - 1]
    if (kind === 'set') node[leaf] = cloneDicts(op[2])
    else delete node[leaf]
  }
  return root
}

// The next payload, or `null` when the delta cannot be applied to the held one.
export function applyStateDelta(held, frame) {
  if (!isDoc(held) || !deltaApplies(frame, held.seq)) return null
  let next
  try { next = applyOps(held, frame.ops) } catch { return null }
  if (!isDoc(next) || String(next.seq) !== String(frame.seq)) return null
  return next
}
