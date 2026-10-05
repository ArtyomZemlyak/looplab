import React, { useEffect, useRef, useState } from 'react'
import { get, saveSettings } from './api.js'
import { boundedRequest } from './requestDeadline.js'
import { setUILanguage, useUILanguage, uiText } from './uiLanguage.js'

// Owner-only persistence; public reviews never save global generation settings.
export default function OwnerLanguageSync() {
  const [language] = useUILanguage()
  const [error, setError] = useState(false), [retry, setRetry] = useState(0)
  const wanted = useRef(language), initialized = useRef(false)
  const alive = useRef(true), worker = useRef(null)
  wanted.current = language
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  useEffect(() => {
    if (worker.current) return
    const work = async () => {
      try {
        const read = () => boundedRequest(signal => get('/api/settings', { signal }), 8000)
        let snapshot = await read()
        const valid = value => value && typeof value.settings_revision === 'string'
          && value.settings_revision && ['auto', 'en', 'ru'].includes(value.settings?.output_language)
        if (!valid(snapshot)) throw new Error('Language settings unavailable')
        if (!alive.current) return
        if (!initialized.current) {
          initialized.current = true
          const saved = snapshot.settings?.output_language
          if (wanted.current === 'auto' && ['en', 'ru'].includes(saved)) {
            wanted.current = saved; setUILanguage(saved)
          }
          else if (wanted.current === 'auto') return
        }
        while (alive.current) {
          const choice = wanted.current, output = choice
          if (snapshot.settings?.output_language !== output) {
            snapshot = await boundedRequest(signal => saveSettings({ output_language: output },
              { expectedRevision: snapshot.settings_revision, signal }), 8000)
            if (!valid(snapshot) || snapshot.ok !== true || snapshot.settings.output_language !== output) {
              throw new Error('Language save unavailable')
            }
          }
          if (choice === wanted.current) break
          snapshot = await read()
          if (!valid(snapshot)) throw new Error('Language settings unavailable')
        }
        if (alive.current) setError(false)
      } catch { if (alive.current) setError(true) }
    }
    worker.current = work().finally(() => { worker.current = null })
  }, [language, retry])
  return error ? <div className="notice resource-error" role="alert">
    {uiText('The interface language changed, but the language for new generated text was not saved.')}
    {' '}<button className="btn sm" onClick={() => setRetry(value => value + 1)}>{uiText('Retry saving language')}</button>
  </div> : null
}
