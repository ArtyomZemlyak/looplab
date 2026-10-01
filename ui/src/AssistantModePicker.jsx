import React from 'react'
import { ASSISTANT_MODES } from './util.js'

export default function AssistantModePicker({ mode, language, disabled, disabledReason, onChange }) {
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
      <summary aria-label={ru ? `Права для следующего сообщения: ${active.label}. Изменить права.`
        : `Assistant permissions for the next message: ${active.label}. Change permissions.`}
        aria-describedby="assistant-mode-hint">
        {ru ? 'Права' : 'Permissions'} · <strong>{active.label}</strong>
      </summary>
      <div className="asst-modes" role="group" aria-label={ru ? 'Права для следующего сообщения' : 'Assistant permissions for the next message'}>
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
          <strong>{item.label}</strong><span>{item.hint}</span>
        </button>)}
      </div>
    </details>
    <span id="assistant-mode-hint" className="asst-modehint muted" aria-live="polite">{active.hint}</span>
  </div>
}
