import { uiText, uiMessage, useUILanguage } from './uiLanguage.js'
import React from 'react'
import { ASSISTANT_MODES } from './util.js'

export default function AssistantModePicker({ mode, language, disabled, disabledReason, onChange }) {
  useUILanguage()

  const ru = language === 'ru'
  const labels = { plan: 'Обсуждение', default: 'С подтверждением', acceptEdits: 'Правки файлов', auto: 'Автономно' }
  const hints = {
    plan: 'Обсуждение, чтение и предложения. Изменения и команды отключены.',
    default: 'Изменения и команды выполняются после вашего подтверждения.',
    acceptEdits: 'Правки файлов выполняются сразу; команды и запуск требуют подтверждения.',
    auto: 'Обычные правки и управление run выполняются сразу; shell и опасные действия требуют подтверждения.',
  }
  const items = ASSISTANT_MODES.map(item => ru ? { ...item, label: labels[item.id], hint: hints[item.id] } : item)
  const active = items.find(item => item.id === mode) || items[0]
  return <div className="asst-moderow">
    <details className="asst-modepicker">
      <summary aria-label={(ru ? `Права для следующего сообщения: ${active.label}. Изменить права.` : uiMessage("Assistant permissions for the next message: {0}. Change permissions.", [uiText(active.label)]))}
        aria-describedby="assistant-mode-hint">
        {((ru ? 'Права' : uiText('Permissions')))} · <strong>{uiText(active.label)}</strong>
      </summary>
      <div className="asst-modes" role="group" aria-label={((ru ? 'Права для следующего сообщения' : uiText('Assistant permissions for the next message')))}>
        {items.map(item => <button type="button" key={item.id}
          aria-pressed={item.id === mode} className={'asst-mode' + (item.id === mode ? ' on' : '')}
          disabled={disabled} title={disabled ? disabledReason : undefined}
          onClick={event => {
            if (disabled) return
            onChange(item.id)
            const picker = event.currentTarget.closest('details')
            picker.open = false
            picker.querySelector('summary').focus()
          }}>
          <strong>{uiText(item.label)}</strong><span>{uiText(item.hint)}</span>
        </button>)}
      </div>
    </details>
    <span id="assistant-mode-hint" className="asst-modehint muted" aria-live="polite">{uiText(active.hint)}</span>
  </div>
}
