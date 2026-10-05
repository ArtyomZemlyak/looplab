import React, { lazy } from 'react'
import LazyBoundary from './LazyBoundary.jsx'
import { useUILanguage } from './uiLanguage.js'

// Public and unauthenticated pages share display preference without owner writes.
const AssistantLanguagePicker = lazy(() => import('./AssistantLanguagePicker.jsx'))

export default function LanguageControl({ disabled = false }) {
  const [language, setLanguage] = useUILanguage()
  return <LazyBoundary label="Language" focusOnFailure={false}>
    <AssistantLanguagePicker language={language} onChange={setLanguage} disabled={disabled} />
  </LazyBoundary>
}
