// Loaded only when Russian copy is requested. User data stays in opaque substitutions.
//
// A placeholder key is ALSO a pattern, so `uiText` can translate a string some helper already
// formatted (`${n} files`) — but only when the match is unambiguous. A key that is almost all
// placeholder (`{0} of {1}`, `by {0}`, `Run {0}`) matches any English phrase that happens to hold
// its one short word, and every server message, file path and already-translated label reaches
// `uiText` too: "Best of the batch" read `Best от the batch`, "by the way" read `:: :: the way`.
// So a key whose literal text does not identify the sentence by itself (fewer than
// `MIN_ANCHOR_WORDS` words or `MIN_ANCHOR_LETTERS` letters) may only bind VALUE-LIKE captures — one
// token holding a digit or no letter at all (`3`, `#12`, `e5small-v9`, `1.2`). An English phrase
// left untranslated is readable; a mistranslated one is wrong.
const MIN_ANCHOR_WORDS = 2, MIN_ANCHOR_LETTERS = 8
const valueLike = capture => !/\s/.test(capture) && (/\d/.test(capture) || !/\p{L}/u.test(capture))
export function decodeCatalogue(page) {
  const data = page?.messages
  if (page?.schema !== 1 || page.language !== 'ru' || !data || Array.isArray(data)
    || typeof data !== 'object' || page.count !== Object.keys(data).length
    || typeof data.Runs !== 'string' || typeof data.Settings !== 'string'
    || !Object.values(data).every(value => typeof value === 'string')) throw new Error('Invalid language catalogue')
  // `plurals` (optional): English `other` text -> the CLDR forms `uiPlural` picks among.
  const plurals = page.plurals ?? {}
  if (!plurals || typeof plurals !== 'object' || Array.isArray(plurals)
    || !Object.values(plurals).every(validPluralForms)) throw new Error('Invalid language catalogue')
  const patterns = Object.keys(data).filter(key => /\{\d+\}/.test(key)).map(key => {
    const pieces = key.split(/(\{\d+\})/), indices = []
    const literals = pieces.filter(piece => !/^\{\d+\}$/.test(piece))
    const regex = pieces.map(piece => {
      if (/^\{\d+\}$/.test(piece)) { indices.push(Number(piece.slice(1, -1))); return '(.*?)' }
      return piece.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    }).join('')
    const literal = literals.join(' ')
    const anchored = (literal.match(/\p{L}{2,}/gu) || []).length >= MIN_ANCHOR_WORDS
      && (literal.match(/\p{L}/gu) || []).length >= MIN_ANCHOR_LETTERS
    return { key, indices, literals, anchored, regex: new RegExp('^' + regex + '$') }
  })
  const translate = value => {
    const key = value.replace(/\s+/g, ' ').trim()
    let translated = Object.hasOwn(data, key) ? data[key] : undefined
    if (translated === undefined && !/[Ѐ-ӿ]/.test(key)) for (const pattern of patterns) {
      if (!pattern.literals.every(literal => !literal || key.includes(literal))) continue
      const match = pattern.regex.exec(key)
      if (!match) continue
      if (!pattern.anchored && !match.slice(1).every(valueLike)) continue
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
  translate.plural = other => Object.hasOwn(plurals, other) ? plurals[other] : undefined
  return translate
}
// The integer categories are required; `other` (fractions) is optional and `uiPlural` reads `few`.
export const PLURAL_FORMS = ['one', 'few', 'many', 'other']
export function validPluralForms(forms) {
  return !!forms && typeof forms === 'object' && !Array.isArray(forms)
    && ['one', 'few', 'many'].every(name => typeof forms[name] === 'string' && forms[name].trim())
    && Object.entries(forms).every(([name, form]) => PLURAL_FORMS.includes(name) && typeof form === 'string' && form.trim())
}
