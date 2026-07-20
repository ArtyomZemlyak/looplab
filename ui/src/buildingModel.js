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
// # CODEX AGENT: Keep legacy-singular and parallel marker decoding in one pure projection. Dock status,
// trace polling and synthetic DAG nodes previously disagreed at the edges because each decoded it.
export function buildingMarkers(state) {
  const bag = state?.buildings
  const markers = bag && typeof bag === 'object' ? Object.values(bag) : state?.building ? [state.building] : []
  return markers.filter(marker => marker?.node_id != null && typeof marker.node_id !== 'boolean')
}

export function buildingGenerations(state) {
  const generations = {}
  let found = false
  for (const marker of buildingMarkers(state)) {
    const nodeId = Number(marker.node_id)
    const generation = Object.hasOwn(marker, 'generation')
      ? marker.generation : (state?.nodes?.[nodeId]?.attempt ?? 0)
    if (!Number.isInteger(nodeId) || nodeId < 0 || !Number.isInteger(generation) || generation < 0) continue
    generations[nodeId] = generation
    found = true
  }
  return found ? generations : null
}

export function withBuilding(state) {
  // Don't splice phantom "building…" cards once the run is over: a finished run clears the marker
  // server-side, but a STALLED run (engine died mid-build, engine_running===false, not finished) would
  // otherwise leave a breathing card for a node that will never appear.
  if (!state || state.finished || state.engine_running === false || !state.nodes) return state
  const markers = buildingMarkers(state)
  if (!markers.length) return state
  const nodes = { ...state.nodes }
  let changed = false
  for (const b of markers) {
    // The cloned node map is also the duplicate set: after the first marker lands, the same id and any
    // real node are both already present. Never overwrite either with a ghost.
    if (nodes[b.node_id]) continue
    const operator = b.operator || 'improve'
    changed = true
    nodes[b.node_id] = {
      id: b.node_id, operator, parent_ids: b.parent_ids || [], status: 'building', building: true,
      idea: { operator, rationale: 'building…' },
    }
  }
  return changed ? { ...state, nodes } : state
}
