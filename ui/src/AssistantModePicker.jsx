import React from 'react'
import { ASSISTANT_MODES } from './util.js'

export default function AssistantModePicker({ mode, disabled, disabledReason, onChange }) {
  const active = ASSISTANT_MODES.find(item => item.id === mode) || ASSISTANT_MODES[0]
  return <div className="asst-moderow">
    <details className="asst-modepicker">
      <summary aria-label={`Assistant permissions for the next message: ${active.label}. Change permissions.`}
        aria-describedby="assistant-mode-hint">
        Permissions · <strong>{active.label}</strong>
      </summary>
      <div className="asst-modes" role="group" aria-label="Assistant permissions for the next message">
        {ASSISTANT_MODES.map(item => <button type="button" key={item.id}
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
