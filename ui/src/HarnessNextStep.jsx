import React from 'react'
import './harness-next-step.css'
import { useAssistantLanguage } from './useAssistantLanguage.js'

const codes = new Set(['inspect_sources', 'answer_checkpoint', 'inspect_lifecycle',
  'inspect_pending', 'choose_direction'])

// Older servers omit this optional field. A malformed new summary is a failed read,
// never a reason to replace a missing obligation with optimistic client advice.
export function validHarnessNextStep(value, language = '') {
  return value == null || (typeof value === 'object' && !Array.isArray(value)
    && codes.has(value.code) && value.owner === 'external_agent'
    && typeof value.title === 'string' && !!value.title && typeof value.detail === 'string' && !!value.detail
    && (!Object.hasOwn(value, 'language') || ['en', 'ru'].includes(value.language)
      && (!language || value.language === language))
    && Array.isArray(value.reads) && value.reads.every(ref => typeof ref === 'string')
    && (value.action === null || typeof value.action === 'string')
    && (value.phase_id === null || typeof value.phase_id === 'string'))
}

export default function HarnessNextStep({ step, runId, fresh }) {
  const [language] = useAssistantLanguage()
  const ru = language === 'ru'
  if (!step) return null
  if (!fresh) return <p role="status" className="report-inline-state">
    {ru ? 'Следующий шаг недоступен до успешного обновления Agent cycle. Ниже показаны журналы последнего чтения.'
      : 'Next step unavailable until Agent cycle refresh succeeds. The journals below are the last read.'}
  </p>
  const ref = value => value.replaceAll('{run_id}', runId)
  return <section className="harness-next-step" aria-label={ru ? 'Следующий шаг внешнего агента' : 'External agent next step'}>
    <h3>{ru ? 'Следующий шаг' : 'Next step'} · {step.title}</h3>
    <p>{step.detail}</p>
    <p className="muted">{ru ? 'Ответственный: внешний агент.' : 'Responsible: external agent.'}</p>
    {ru && !step.language && <p className="muted">Подсказка старого сервера приведена на исходном языке.</p>}
    <details>
      <summary>{ru ? 'Что прочитать и где ответить' : 'Read and response references'}</summary>
      <ul>{step.reads.map((value, index) => <li key={index} style={{ overflowWrap: 'anywhere' }}>
        {ref(value)}</li>)}</ul>
      {step.phase_id && <p>{ru ? 'Прочитайте MCP phase_info' : 'Read MCP phase_info'}: {step.phase_id}</p>}
      {step.action && <p style={{ overflowWrap: 'anywhere' }}>{ru ? 'Отправьте явный ответ через' : 'Submit an explicit response via'} {ref(step.action)}.</p>}
      <p className="muted">{ru ? 'Используйте текущую generation запуска. Обновляйте данные после события или ответа.' : 'Use the current run generation. Refresh after an event or response.'}</p>
    </details>
  </section>
}
