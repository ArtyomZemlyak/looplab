export const MAP_COLUMNS = 6
export const MAP_COLLAPSE_THRESHOLD = 24
export const UNASSIGNED_CLUSTER = '__map_unassigned__'

export function gridColumns(count, max = MAP_COLUMNS) {
  if (count <= 1) return 1
  return Math.min(max, Math.max(2, Math.ceil(Math.sqrt(count * 1.5))))
}

export function packRunGrid(runs, { x = 0, y = 0, dx = 214, dy = 122, maxColumns = MAP_COLUMNS } = {}) {
  const columns = gridColumns(runs.length, maxColumns)
  const positions = new Map()
  runs.forEach((run, index) => positions.set(run.run_id, {
    x: x + (index % columns) * dx,
    y: y + Math.floor(index / columns) * dy,
  }))
  const rows = runs.length ? Math.ceil(runs.length / columns) : 0
  return { positions, columns, rows, height: rows * dy }
}

export function defaultCollapsedClusters(projects, runs, subtree) {
  const collapsed = new Set()
  const unassigned = runs.filter(run => !run.project_id)
  if (unassigned.length > MAP_COLLAPSE_THRESHOLD) collapsed.add(UNASSIGNED_CLUSTER)
  projects.forEach(project => {
    const ids = subtree(project.id)
    const count = runs.filter(run => ids.has(run.project_id)).length
    if (count > MAP_COLLAPSE_THRESHOLD) collapsed.add(project.id)
  })
  return collapsed
}
