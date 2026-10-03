import React, { useEffect, useRef, useState } from 'react'
import { deadlineGet, runNodeApiPath } from './api.js'
import CodeViewer from './CodeViewer.jsx'

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
  return fileRow(file) && listed && file.path === path
    && ['bytes', 'executable', 'sha256'].every(key => file[key] === listed[key])
    && ['utf8', 'binary', 'too_large'].includes(file.text_status)
    && (file.text_status === 'utf8' ? typeof file.text === 'string'
      && new TextEncoder().encode(file.text).length === file.bytes && file.bytes <= page.text_limit
      : file.text === null)
}

export default function RecordedSeedFiles({ runId, node, generation, baseDigest, language, draftStore, draftScope }) {
  const ru = language === 'ru'
  const [page, setPage] = useState(null)
  const [file, setFile] = useState(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState(false)
  const request = useRef(null)
  const serial = useRef(0)
  useEffect(() => () => { serial.current += 1; request.current?.controller.abort() }, [])
  const load = async (offset = 0, path = null) => {
    const ticket = ++serial.current
    request.current?.controller.abort()
    setPending(true); setError(false); setFile(null)
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
      <ul>{page.files.map(row => <li key={row.path}>
        <button className="btn sm ghost" disabled={pending} onClick={() => load(page.offset, row.path)}>{row.path}</button>
        <span className="muted" title={row.sha256}> {row.bytes} B · {row.sha256.slice(0, 12)}{row.executable ? ' · executable' : ''}</span>
      </li>)}</ul>
    </>}
    {file && (file.text_status === 'utf8'
      ? <CodeViewer code={file.text} label={file.path} language={language}
          draftStore={draftStore} draftScope={`${draftScope}:base:${file.path}`} />
      : <p className="notice compact" role="status">{file.path}: {file.text_status === 'too_large'
        ? (ru ? 'слишком большой файл для текстового просмотра (лимит 256 КиБ).' : 'too large for text preview (256 KiB limit).')
        : (ru ? 'бинарный файл; текстовый просмотр недоступен.' : 'binary file; no text preview.')}</p>)}
  </section>
}
