import { useEffect, useState } from 'react'
import { storageGet, storageSet } from './util.js'

const KEY = 'looplab.language'
const EVENT = 'looplab:language'
export const validLanguage = value => ['auto', 'en', 'ru'].includes(value)
const read = () => {
  const value = storageGet(KEY) ?? storageGet('looplab.resultLanguage')
  return validLanguage(value) ? value : 'auto'
}

// One preference for all three Assistant surfaces and the completion feed. A
// storage failure still permits changing this tab; no server/model request.
export function useAssistantLanguage() {
  const [language, setLanguage] = useState(read)
  useEffect(() => {
    const changed = event => { if (validLanguage(event.detail)) setLanguage(event.detail) }
    const stored = event => { if (event.key === KEY || event.key === null) setLanguage(read()) }
    window.addEventListener(EVENT, changed)
    window.addEventListener('storage', stored)
    return () => {
      window.removeEventListener(EVENT, changed)
      window.removeEventListener('storage', stored)
    }
  }, [])
  return [language, value => {
    if (!validLanguage(value)) return
    setLanguage(value)
    storageSet(KEY, value)
    window.dispatchEvent(new window.CustomEvent(EVENT, { detail: value }))
  }]
}
