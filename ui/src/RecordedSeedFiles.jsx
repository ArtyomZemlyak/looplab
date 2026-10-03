import React, { useEffect, useRef, useState } from 'react'
import { deadlineGet, runNodeApiPath } from './api.js'
import CodeViewer from './CodeViewer.jsx'
import { recordedFileOverlay } from './nodeCodeModel.js'
import './RecordedSeedFiles.css'

const digest = value => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value)
const index = value => Number.isSafeInteger(value) && value >= 0
const fileRow = row => row && typeof row.path === 'string' && row.path.length > 0
  && index(row.bytes) && typeof row.executable === 'boolean' && digest(row.sha256)

export function validSeedFilesPage(page, identity, offset, path) {
  if (page?.version !== 1 || page.scope !== 'recorded_seed_before_mounts_and_overlay'
      || page.run_generation !== identity.generation || page.node_id !== identity.nodeId
      || page.attempt !== identity.attempt || page.base_digest !== identity.baseDigest
      || page.offset !== offset || page.limit !== 100
      || !index(page.total) || !Array.isArray(page.files) || !page.files.every(fileRow)
      || page.files.length !== Math.min(page.limit, page.total - offset)
      || new Set(page.files.map(row => row.path)).size !== page.files.length
      || page.text_limit !== 256 * 1024) return false
  const next = offset + page.files.length
  if (page.next_offset !== (next < page.total ? next : null)
      || (next < page.total && page.files.length === 0)) return false
  if (path == null) return page.file === null
  const file = page.file
  const listed = page.files.find(row => row.path === path)
  if (!fileRow(file) || !listed) return false
  return file.path === path
    && ['bytes', 'executable', 'sha256'].every(key => file[key] === listed[key])
    && ['utf8', 'binary', 'too_large'].includes(file.text_status)
    && (file.text_status === 'utf8' ? typeof file.text === 'string'
      && !file.text.includes('\0')
      && new TextEncoder().encode(file.text).length === file.bytes && file.bytes <= page.text_limit
      : file.text === null && (file.text_status === 'too_large'
        ? file.bytes > page.text_limit : file.bytes <= page.text_limit))
}

export default function RecordedSeedFiles({ runId, node, generation, baseDigest, language, draftStore, draftScope }) {
  const ru = language === 'ru'
  const [page, setPage] = useState(null)
  const [file, setFile] = useState(null)
  const [version, setVersion] = useState('base')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState(false)
  const request = useRef(null)
  const serial = useRef(0)
  const preview = useRef(null)
  const focusOrigin = useRef(null)
  useEffect(() => () => { serial.current += 1; request.current?.controller.abort() }, [])
  useEffect(() => {
    if (!file || !focusOrigin.current || !preview.current) return
    // The read was explicitly requested. Do not steal focus from a chat/input used while it waited.
    if (document.activeElement === focusOrigin.current || document.activeElement === document.body) {
      preview.current.focus({ preventScroll: true })
      preview.current.scrollIntoView?.({ block: 'nearest' })
    }
  }, [file])
  const load = async (offset = 0, path = null) => {
    const ticket = ++serial.current
    focusOrigin.current = path == null ? null : document.activeElement
    request.current?.controller.abort()
    setPending(true); setError(false); setFile(null); setVersion('base')
    const query = new URLSearchParams({ expected_generation: generation, attempt: String(node.attempt),
      offset: String(offset), limit: '100' })
    if (path != null) query.set('path', path)
    const read = deadlineGet(runNodeApiPath(runId, node.id, `/seed-files?${query}`))
    request.current = read
    try {
      const result = await read.promise
      if (ticket !== serial.current) return
      if (!validSeedFilesPage(result, { generation, nodeId: node.id, attempt: node.attempt, baseDigest }, offset, path)) throw 0
      if (result.file?.text_status === 'utf8') {
        const bytes = new TextEncoder().encode(result.file.text)
        const hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))]
          .map(byte => byte.toString(16).padStart(2, '0')).join('')
        if (hash !== result.file.sha256) throw 0
      }
      if (ticket !== serial.current) return
      setPage(result); setFile(result.file)
    } catch {
      if (ticket === serial.current) { setPage(null); setError(true) }
    } finally {
      if (ticket === serial.current) { setPending(false); request.current = null }
    }
  }
  const overlay = file ? recordedFileOverlay(node, file.path) : null
  const overlayLabel = kind => ({
    inherited: ru ? 'Отдельной правки нет' : 'No separate edit',
    override: ru ? 'Есть правка опыта' : 'Experiment edit recorded',
    deleted: ru ? 'Удаление в правках' : 'Deletion recorded',
    unknown: ru ? 'Правки не проверены' : 'Edits unavailable',
  })[kind]
  return <section aria-label={ru ? 'Записанные файлы базы' : 'Recorded base files'}>
    <div className="section-h">{ru ? 'Унаследованная база' : 'Inherited base'}</div>
    <p className="muted">{ru
      ? 'Файлы проверенного архива до правок узла. Не включает файлы и данные, подключённые при запуске, и окружение. Без вызова модели.'
      : 'Verified archive files before node edits. Data, mounts, task assets and environment are not added here. No model request.'}</p>
    <button className="btn sm" disabled={pending} onClick={() => load()}>
      {pending ? (ru ? 'Чтение…' : 'Reading…') : ru ? 'Открыть файлы базы' : 'Open base files'}</button>
    {error && <p className="notice resource-warning" role="status">{ru
      ? 'Архив или его квитанция недоступны. Обновите состояние и повторите чтение; код из текущего репозитория не подставляется.'
      : 'Archive or its evidence is unavailable. Refresh state and retry; the current repository is not substituted.'}</p>}
    {page && <>
      <p className="muted">{ru ? 'Файлы' : 'Files'} {page.total ? page.offset + 1 : 0}–{page.offset + page.files.length} / {page.total}</p>
      <div className="toolbar">
        <button className="btn sm" disabled={pending || page.offset === 0} onClick={() => load(Math.max(0, page.offset - 100))}>{ru ? 'Назад' : 'Previous'}</button>
        <button className="btn sm" disabled={pending || page.next_offset === null} onClick={() => load(page.next_offset)}>{ru ? 'Дальше' : 'Next'}</button>
      </div>
    </>}
    {file && <>
      <div ref={preview} tabIndex={-1} className="section-h">{file.path}</div>
      <p className="muted">{ru
        ? `Опыт #${node.id} · попытка ${node.attempt} · база ${baseDigest.slice(0, 12)}. Показаны версии исходных файлов, не снимок работающей программы.`
        : `Experiment #${node.id} · attempt ${node.attempt} · base ${baseDigest.slice(0, 12)}. Source file versions, not a running-program snapshot.`}</p>
      {overlay.kind === 'override' && <>
        <p className="notice compact" role="status">{ru
          ? 'Для этого файла есть отдельная правка опыта. Версия базы не включает её. Защищённые файлы и файлы задачи могут перекрывать правку при запуске.'
          : 'This file has a separate experiment edit. The base version excludes it. Protected files and task assets may override the edit at runtime.'}</p>
        <div className="toolbar" aria-label={ru ? 'Версия файла' : 'File version'}>
          <button className="btn sm" aria-pressed={version === 'base'} onClick={() => setVersion('base')}>{ru ? 'Версия базы' : 'Base version'}</button>
          <button className="btn sm" aria-pressed={version === 'edit'} onClick={() => setVersion('edit')}>{ru ? 'Правка опыта' : 'Experiment edit'}</button>
        </div>
      </>}
      {overlay.kind === 'deleted' && <p className="notice compact" role="status">{ru
        ? 'В сохранённых правках файл удалён. Ниже — прежний файл из архива базы, а не восстановленный файл опыта.'
        : 'Deletion is recorded in the saved edits. Below is the old base file, not a restored experiment file.'}</p>}
      {overlay.kind === 'unknown' && <p className="notice compact" role="status">{ru
        ? 'Правки не проверены. Версию базы нельзя считать итоговым файлом опыта.'
        : 'Edits are unavailable. The base version cannot establish the final experiment file.'}</p>}
      {version === 'edit' && overlay.kind === 'override'
      ? <CodeViewer code={overlay.text} label={ru ? `Правка #${node.id}: ${file.path}` : `Edit #${node.id}: ${file.path}`}
          language={language} draftStore={draftStore} draftScope={`${draftScope}:file:${file.path}`} />
      : file.text_status === 'utf8'
      ? <CodeViewer code={file.text} label={ru ? `База: ${file.path}` : `Base: ${file.path}`} language={language}
          draftStore={draftStore} draftScope={`${draftScope}:base:${file.path}`} />
      : <p className="notice compact" role="status">{file.path}: {file.text_status === 'too_large'
        ? (ru ? 'слишком большой файл для текстового просмотра (лимит 256 КиБ).' : 'too large for text preview (256 KiB limit).')
        : (ru ? 'бинарный файл; текстовый просмотр недоступен.' : 'binary file; no text preview.')}</p>}
    </>}
    {page && <ul className="recorded-seed-list" aria-label={ru ? 'Файлы записанной базы' : 'Recorded base file list'}>
      {page.files.map(row => <li key={row.path}>
        <button className="btn sm ghost" disabled={pending} aria-pressed={file?.path === row.path}
          onClick={() => load(page.offset, row.path)}>{row.path}</button>
        <span className="muted"> · {overlayLabel(recordedFileOverlay(node, row.path).kind)}</span>
        <span className="muted" title={row.sha256}> {row.bytes} B · {row.sha256.slice(0, 12)}{row.executable ? ' · executable' : ''}</span>
      </li>)}
    </ul>}
  </section>
}
