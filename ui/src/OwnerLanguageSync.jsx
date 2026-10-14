import { useEffect, useRef } from 'react'
import { get } from './api.js'
import { boundedRequest } from './requestDeadline.js'
import { setUILanguage, useUILanguage } from './uiLanguage.js'

// The interface language is a PER-VIEWER preference (localStorage). Until 2026-10-14 every change
// also PUT the server's `output_language`, so one viewer switching the UI to Russian changed the
// language of every later generated text and the default of every other viewer of a shared
// server (review 2026-10-14 section 10.5). Now this only READS: a viewer who never chose a language
// ('auto') adopts the saved generation language once; the server setting itself changes only
// through Settings → output language, an explicit action.
export default function OwnerLanguageSync() {
  const [language] = useUILanguage()
  const adopted = useRef(false)
  useEffect(() => {
    if (adopted.current || language !== 'auto') return
    adopted.current = true
    let alive = true
    boundedRequest(signal => get('/api/settings', { signal }), 8000).then(snapshot => {
      const saved = snapshot?.settings?.output_language
      if (alive && ['en', 'ru'].includes(saved)) setUILanguage(saved)
    }).catch(() => {})   // an unreadable default leaves the viewer's own 'auto' in place
    return () => { alive = false }
  }, [language])
  return null
}
