import { uiText, useUILanguage } from './uiLanguage.js'
import React from 'react'
import { OpIcon } from './icons.jsx'

export function PanelResourceNotice({ resource, label, onRetry, language = 'en' }) {
  useUILanguage()

  const ru = language === 'ru'
  const text = (en, russian) => ru ? russian : en
  if (resource.status === 'ready') return null
  if (resource.status === 'loading') return <div className="muted" role="status">{text('Loading', 'Загружаем')} {uiText(label)}…</div>
  const stale = resource.status === 'stale'
  return <div className={'report-inline-state' + (stale ? '' : ' error')} role={stale ? 'status' : 'alert'}>
    <OpIcon name="alert" size={14} />
    <span>{uiText(label)}: {resource.pending
      ? `${resource.pending === 'retry' ? text('Retrying', 'Повторяем чтение') : text('Refreshing', 'Обновляем')}…${stale ? text(' Last loaded data remains visible.', ' Показаны данные последнего чтения.') : ''}`
      : stale ? text('Last loaded data; refresh failed.', 'Данные последнего чтения; обновление не удалось.') : text('Unavailable.', 'Недоступно.')}</span>
    <button className="btn sm" disabled={!!resource.pending} onClick={onRetry}>
      {resource.pending === 'retry' ? text('Retrying…', 'Повторяем чтение…') : resource.pending ? text('Refreshing…', 'Обновляем…') : text('Retry', 'Повторить чтение')}</button>
  </div>
}
