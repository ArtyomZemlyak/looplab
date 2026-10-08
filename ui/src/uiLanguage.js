import { useSyncExternalStore } from 'react'
import { boundedRequest } from './requestDeadline.js'

// Translate authored UI sinks, never walk user content, code or measured evidence.
export const validLanguage = value => ['auto', 'en', 'ru'].includes(value)
const KEY = 'looplab.language', EVENT = 'looplab:language'
const listeners = new Set()
let preference, revision = 0, dictionary = null, loading = null, loadError = null
let attachedWindow
const read = () => {
  try {
    const value = window.localStorage.getItem(KEY) ?? window.localStorage.getItem('looplab.resultLanguage')
    return validLanguage(value) ? value : 'auto'
  } catch { return 'auto' }
}
export function effectiveUILanguage(value = listeners.size ? preference ?? read() : read()) {
  if (value !== 'auto') return value
  return typeof navigator !== 'undefined' && /^ru(?:-|$)/i.test(navigator.language) ? 'ru' : 'en'
}
function notify() { revision++; listeners.forEach(fn => fn()) }
export async function loadUILanguage() {
  if (dictionary || loading || effectiveUILanguage() !== 'ru') return loading
  loadError = null
  loading = boundedRequest(async signal => {
    const response = await fetch(new URL('./locales/ru.json', import.meta.url), { signal })
    if (!response.ok) throw new Error('Russian interface could not be loaded')
    const page = await response.json()
    const { decodeCatalogue } = await import('./localeCatalogue.js')
    return decodeCatalogue(page)
  }, 12000).then(translate => { dictionary = translate })
    .catch(() => { loadError = 'Не удалось загрузить русский интерфейс. Повторите загрузку.' })
    .finally(() => { loading = null; notify() })
  return loading
}
function initialize(load = false) {
  if (typeof window === 'undefined') return
  if (attachedWindow !== window) {
    attachedWindow = window; preference = read()
    window.addEventListener(EVENT, event => {
      if (validLanguage(event.detail)) apply(event.detail, false)
    })
    window.addEventListener('storage', event => {
      if (event.key === KEY || event.key === null) apply(read(), false)
    })
  }
  if (document.documentElement) document.documentElement.lang = effectiveUILanguage()
  if (load && effectiveUILanguage() === 'ru' && !dictionary && !loadError) void loadUILanguage()
}
function apply(value, broadcast) {
  preference = value
  try { window.localStorage.setItem(KEY, value) } catch { /* this tab still changes */ }
  if (typeof document !== 'undefined') document.documentElement.lang = effectiveUILanguage(value)
  if (broadcast && typeof window !== 'undefined') window.dispatchEvent(new window.CustomEvent(EVENT, { detail: value }))
  else { notify(); void loadUILanguage() }
}
export function setUILanguage(value) { initialize(); if (validLanguage(value)) apply(value, true) }
function subscribe(fn) {
  if (!listeners.size) { const next = read(); if (preference !== next) { preference = next; revision++ } }
  initialize(true); listeners.add(fn); return () => listeners.delete(fn)
}
export function useUILanguage() {
  useSyncExternalStore(subscribe, () => revision, () => 0)
  return [listeners.size ? preference ?? read() : read(), setUILanguage, revision]
}
export function languageLoadState() { return { loading: !!loading, error: loadError } }
export function uiText(value) {
  if (typeof value !== 'string' || effectiveUILanguage() !== 'ru' || !dictionary) return value
  return dictionary(value)
}
export function uiMessage(key, values = []) {
  return uiText(key).replace(/\{(\d+)\}/g, (_, index) => String(values[Number(index)] ?? ''))
}

// Plural copy. English source text says its two forms and picks `one` for exactly 1 and `other`
// for everything else — the `n === 1 ? '' : 's'` every call site used to glue on, byte for byte.
// Russian needs three integer forms (1 узел, 2 узла, 5 узлов; 21 узел, 11 узлов), which no suffix
// placeholder can carry, so the Russian forms live in the catalogue's `plurals` section keyed by
// the English `other` text: { one, few, many, other? } with CLDR's categories (`other` is the
// fractional form and defaults to `few`, "1,5 запуска"; the optional `exact1` is the text for
// exactly one, since CLDR `one` is also 21 and 101 — `localeCatalogue.js::PLURAL_FORMS`). `{N}`
// substitutes `values[N]` as in `uiMessage`; `values` defaults to `[count]`. A key with no Russian forms falls back to the
// English choice through `uiText`, so a missing entry reads English, never a glued fragment.
// `scripts/localize-copy.mjs --check` collects every `uiPlural(count, '<one>', '<other>', …)`
// call (both forms string literals carrying the same placeholders, the function called by its own
// imported name — an alias, `L.uiPlural` or a call the collector never reaches is refused) and
// refuses a missing, unused or ill-formed entry.
export function russianPluralCategory(count) {
  const n = Math.abs(Number(count))
  if (!Number.isInteger(n)) return 'other'
  const last = n % 10, lastTwo = n % 100
  if (last === 1 && lastTwo !== 11) return 'one'
  if (last >= 2 && last <= 4 && (lastTwo < 12 || lastTwo > 14)) return 'few'
  return 'many'
}
// The Russian form for `count`: `exact1` for exactly one when the entry has it, else the CLDR
// category's form (`other`, the fractional form, falls back to `few`).
export function russianPluralForm(forms, count) {
  if (Number(count) === 1 && forms.exact1) return forms.exact1
  return forms[russianPluralCategory(count)] ?? forms.few
}
export function uiPlural(count, one, other, values = [count]) {
  // Keyed like `uiText`: whitespace-normalized and trimmed, with the source's own edges kept.
  const key = typeof other === 'string' ? other.replace(/\s+/g, ' ').trim() : other
  const forms = effectiveUILanguage() === 'ru' && dictionary?.plural ? dictionary.plural(key) : undefined
  const template = forms
    ? other.match(/^\s*/)[0] + russianPluralForm(forms, count) + other.match(/\s*$/)[0]
    : uiText(Number(count) === 1 ? one : other)
  return template.replace(/\{(\d+)\}/g, (_, index) => String(values[Number(index)] ?? ''))
}
