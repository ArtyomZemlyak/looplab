// Continue each collection at the number of WHOLE rows actually returned. A global offset cannot
// represent pages whose byte budget gave different collections different numbers of rows.
const names = ['uploads', 'cards', 'research_memos', 'reviews', 'decisions', 'experiments', 'nodes', 'reports']
export const initialMapPage = () => ({ offset: 0, cursors: null, collections: null, history: [] })
const position = current => ({ offset: current.offset, cursors: current.cursors, collections: current.collections })
export const mapHasMore = page => names.some(name => page?.[name]?.selected !== false && page?.[name]?.has_more)
export function nextMapPage(current, page) {
  const prior = [...current.history, position(current)]
  if (!page?.response_budget) return { ...position(current), offset: current.offset + 20, history: prior }
  if (page.response_budget.version !== 1) throw new Error('Unsupported research-map pagination')
  const cursors = Object.fromEntries(names.map(name => {
    const value = page[name]?.next_cursor
    if (typeof value !== 'string' || !/^map1:(0|[1-9][0-9]{0,9}):[a-f0-9]{64}$/.test(value))
      throw new Error('Incomplete research-map continuation')
    return [name, value]
  }))
  return { offset: 0, cursors, collections: current.collections, history: prior }
}
export function previousMapPage(current) {
  if (!current.history.length) return initialMapPage()
  return { ...current.history.at(-1), history: current.history.slice(0, -1) }
}
export function sectionMapPage(current, name) {
  return { offset: 0, cursors: null, collections: [name], history: [...current.history, position(current)] }
}
