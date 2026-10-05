import { uiText } from './uiLanguage.js'
import React from 'react'
import { effectiveUILanguage, languageLoadState, loadUILanguage, useUILanguage } from './uiLanguage.js'

export default function AssistantLanguagePicker({ language, onChange, disabled }) {
  useUILanguage()
  const ru = effectiveUILanguage(language) === 'ru'
  const state = languageLoadState()
  return <label className="asst-language">
    <span>{ru ? 'Язык' : 'Language / Язык'}</span>
    <select aria-label={((ru ? 'Язык интерфейса и новых текстов' : uiText('Interface and generated text language')))}
      title={((ru ? 'Язык всех экранов и новых текстов LoopLab' : uiText('Language of all screens and new LoopLab text')))}
      value={language} disabled={disabled} onChange={event => onChange(event.target.value)}>
      <option value="auto">{((ru ? 'Авто' : uiText('Auto')))}</option>
      <option value="en">{uiText("English")}</option><option value="ru">Русский</option>
    </select>
    {state.error && <button type="button" className="btn sm" onClick={() => loadUILanguage()}>{state.error}</button>}
  </label>
}
