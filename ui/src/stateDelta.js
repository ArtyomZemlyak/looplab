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
//
// A KEY IS DATA (review 2026-09-22, UI-03). A path is a list of dict keys and the Python half treats
// every one as an ordinary key, `__proto__` and `constructor` included. `node[key]` does not: it
// reads `__proto__` off Object.prototype, and ASSIGNING it re-parents the object instead of adding a
// key. Driven: a server-legal delta setting `…agent_report.__proto__` silently dropped the key, and
// the next one walked `…agent_report.__proto__.status` into Object.prototype, so every object in the
// tab answered `.status`. So the walk reads OWN properties only and every write DEFINES the key; a
// `delete` only ever removes an own key already. `tests/fixtures/state_delta_cases.json`, generated
// from the Python half, is applied row by row in `ui/test/stateDelta.test.js`.

export const STATE_DELTA_VERSION = 1

const isDoc = value => !!value && typeof value === 'object' && !Array.isArray(value)
const ownValue = (doc, key) => (Object.hasOwn(doc, key) ? doc[key] : undefined)
const define = (doc, key, value) => Object.defineProperty(doc, key, {
  value, writable: true, enumerable: true, configurable: true,
})

// Whether `frame` is a well-formed delta against a payload whose seq is `heldSeq`.
export function deltaApplies(frame, heldSeq) {
  if (!isDoc(frame) || frame.version !== STATE_DELTA_VERSION) return false
  if (!Number.isSafeInteger(frame.base_seq) || !Number.isSafeInteger(frame.seq)) return false
  if (!Array.isArray(frame.ops)) return false
  return String(frame.base_seq) === String(heldSeq)
}

// COPY-ON-WRITE along each op's path (review 2026-09-22, UI-03). This used to deep-clone every dict
// of the held payload on EVERY frame, so a delta that moved one node's metric still handed React a
// new object for every node, card and memo of the run, and no identity memo downstream (the RunView
// group members, the Dag base layout, the Card board rows) could survive a frame. Now a dict is
// copied only when an op writes through it, and at most once per apply: `fresh` holds this apply's
// own copies, the only objects it ever writes to. Every untouched subtree is the held payload's own
// object, and neither the held payload nor the frame's values are mutated — a value an op set is
// copied like any other dict before a later op writes inside it. `ui/test/stateDelta.test.js` drives
// this against the full-clone algorithm it replaced on seeded random deltas.
export function applyOps(base, ops) {
  const fresh = new Set()
  const writable = doc => {
    if (fresh.has(doc)) return doc
    const copy = isDoc(doc) ? { ...doc } : {}
    fresh.add(copy)
    return copy
  }
  let root = base
  for (const op of ops) {
    if (!Array.isArray(op) || !Array.isArray(op[1])) throw new Error('malformed delta op')
    const [kind, path] = op
    if (kind === 'set' && op.length === 3) {
      if (!path.length) { root = op[2]; continue }
    } else if (kind === 'del' && op.length === 2) {
      if (!path.length) throw new Error('cannot delete the root')
    } else throw new Error('malformed delta op')
    if (!isDoc(root)) throw new Error('malformed delta op')
    root = writable(root)
    let node = root
    for (const key of path.slice(0, -1)) {
      const child = writable(ownValue(node, key))
      define(node, key, child)
      node = child
    }
    const leaf = path[path.length - 1]
    if (kind === 'set') define(node, leaf, op[2])
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
