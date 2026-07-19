// Pure projection: splice EVERY in-flight build (`node_building` marker) into `state.nodes` as a
// synthetic `status:'building'` node, so the DAG / list / panels render each the INSTANT work starts on
// it — before its node_created folds. Kept out of the real event-sourced node set on the backend (id
// allocation), so this transform is a pure UI-side overlay.
//
// Under parallel_build>1 several nodes build at once: the server sends them all in `state.buildings`
// (a node_id->marker object), with the singular `state.building` kept as the LAST-appended one for
// back-compat. We render every entry. Falls back to the singular marker for a serial-build run or an
// older server that doesn't send `buildings`. Extracted from hooks.js so it can be unit-tested without
// pulling in React.
export function withBuilding(state) {
  // Don't splice phantom "building…" cards once the run is over: a finished run clears the marker
  // server-side, but a STALLED run (engine died mid-build, engine_running===false, not finished) would
  // otherwise leave a breathing card for a node that will never appear.
  if (!state || state.finished || state.engine_running === false || !state.nodes) return state
  const bag = state.buildings
  const markers = (bag && typeof bag === 'object') ? Object.values(bag)
    : (state.building ? [state.building] : [])
  if (!markers.length) return state
  const nodes = { ...state.nodes }
  const seen = new Set()
  let changed = false
  for (const b of markers) {
    // Skip a marker with no id, a duplicate (a server sending both `building` and `buildings`), or one
    // whose real node already landed (node_created folded) — never overwrite a real node with a ghost.
    if (!b || b.node_id == null || seen.has(b.node_id) || nodes[b.node_id]) continue
    seen.add(b.node_id); changed = true
    nodes[b.node_id] = {
      id: b.node_id, operator: b.operator || 'improve', parent_ids: b.parent_ids || [],
      status: 'building', building: true, idea: { operator: b.operator || 'improve', rationale: 'building…' },
    }
  }
  return changed ? { ...state, nodes } : state
}
