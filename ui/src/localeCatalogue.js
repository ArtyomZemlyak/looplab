// Loaded only when Russian copy is requested. User data stays in opaque substitutions.
export function decodeCatalogue(page) {
  const data = page?.messages
  if (page?.schema !== 1 || page.language !== 'ru' || !data || Array.isArray(data)
    || typeof data !== 'object' || page.count !== Object.keys(data).length
    || typeof data.Runs !== 'string' || typeof data.Settings !== 'string'
    || !Object.values(data).every(value => typeof value === 'string')) throw new Error('Invalid language catalogue')
  const patterns = Object.keys(data).filter(key => /\{\d+\}/.test(key)).map(key => {
    const pieces = key.split(/(\{\d+\})/), indices = []
    const literals = pieces.filter(piece => !/^\{\d+\}$/.test(piece))
    const regex = pieces.map(piece => {
      if (/^\{\d+\}$/.test(piece)) { indices.push(Number(piece.slice(1, -1))); return '(.*?)' }
      return piece.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    }).join('')
    return { key, indices, literals, regex: new RegExp('^' + regex + '$') }
  })
  return value => {
    const key = value.replace(/\s+/g, ' ').trim()
    let translated = Object.hasOwn(data, key) ? data[key] : undefined
    if (translated === undefined) for (const pattern of patterns) {
      if (!pattern.literals.every(literal => !literal || key.includes(literal))) continue
      const match = pattern.regex.exec(key)
      if (!match) continue
      const values = new Map()
      let consistent = true
      pattern.indices.forEach((index, position) => {
        if (values.has(index) && values.get(index) !== match[position + 1]) consistent = false
        values.set(index, match[position + 1])
      })
      if (!consistent) continue
      translated = data[pattern.key].replace(/\{(\d+)\}/g, (_, index) => values.get(Number(index)))
      break
    }
    return translated === undefined ? value : value.replace(/\S[\s\S]*\S|\S/, () => translated)
  }
}
