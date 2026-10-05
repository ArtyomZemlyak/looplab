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
