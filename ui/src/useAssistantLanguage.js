// Shared preference for the whole application and newly generated prose.
export { validLanguage, useUILanguage as useAssistantLanguage } from './uiLanguage.js'

import { effectiveUILanguage, useUILanguage } from './uiLanguage.js'

// Display language is resolved separately from the saved/model preference "auto".
export function useAssistantUILanguage() {
  const [preference] = useUILanguage()
  return effectiveUILanguage(preference)
}
