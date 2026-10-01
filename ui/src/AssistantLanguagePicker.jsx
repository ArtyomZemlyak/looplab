import React from 'react'

export default function AssistantLanguagePicker({ language, onChange, disabled }) {
  const ru = language === 'ru'
  return <label className="asst-language">
    <span>{ru ? 'Язык' : 'Language / Язык'}</span>
    <select aria-label={ru ? 'Язык ответов ассистента' : 'Assistant response language'}
      title={ru ? 'Язык новых ответов и кратких итогов' : 'Language of new replies and completion briefs'}
      value={language} disabled={disabled} onChange={event => onChange(event.target.value)}>
      <option value="auto">{ru ? 'Авто' : 'Auto'}</option>
      <option value="en">English</option><option value="ru">Русский</option>
    </select>
  </label>
}
