import { uiText, uiMessage, useUILanguage } from './uiLanguage.js'
import React, { useEffect, useRef, useState } from 'react'
import { deadlineGet, runNodeApiPath } from './api.js'
import CodeViewer from './CodeViewer.jsx'
import { recordedFileOverlay } from './nodeCodeModel.js'
import { seedReadError } from './seedReadError.js'
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
      && file.bytes <= page.text_limit && file.text.length <= page.text_limit
      && !file.text.includes('\0')
      // In Unicode mode a paired surrogate is one astral code point, outside this range.
      // Refuse lone surrogates before TextEncoder silently hashes replacement characters.
      && !/[\uD800-\uDFFF]/u.test(file.text)
      && new TextEncoder().encode(file.text).length === file.bytes
      : file.text === null && (file.text_status === 'too_large'
        ? file.bytes > page.text_limit : file.bytes <= page.text_limit))
}

export default function RecordedSeedFiles({ runId, node, generation, baseDigest, language, draftStore, draftScope }) {
  useUILanguage()

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
      if (!validSeedFilesPage(result, { generation, nodeId: node.id, attempt: node.attempt, baseDigest }, offset, path)) throw { code: 'seed_page_invalid' }
      if (result.file?.text_status === 'utf8') {
        const bytes = new TextEncoder().encode(result.file.text)
        const hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))]
          .map(byte => byte.toString(16).padStart(2, '0')).join('')
        if (hash !== result.file.sha256) throw { code: 'seed_page_invalid' }
      }
      if (ticket !== serial.current) return
      setPage(result); setFile(result.file)
    } catch (failure) {
      if (ticket === serial.current) { setPage(null); setError(failure || {}) }
    } finally {
      if (ticket === serial.current) { setPending(false); request.current = null }
    }
  }
  const overlay = file ? recordedFileOverlay(node, file.path) : null
  const mainCode = overlay?.kind === 'main_code'
  const hasEdit = mainCode || overlay?.kind === 'override'
  const overlayLabel = kind => ({
    inherited: ru ? 'Отдельной правки нет' : 'No separate edit',
    override: ru ? 'Есть правка опыта' : 'Experiment edit recorded',
    main_code: ru ? 'Есть основной код опыта' : 'Main code recorded',
    path_ambiguity: ru ? 'Написание пути различается' : 'Path spelling differs',
    deleted: ru ? 'Удаление в правках' : 'Deletion recorded',
    unknown: ru ? 'Правки не проверены' : 'Edits unavailable',
  })[kind]
  return <section aria-label={((ru ? 'Записанные файлы базы' : uiText('Recorded base files')))}>
    <div className="section-h">{((ru ? 'Унаследованная база' : uiText('Inherited base')))}</div>
    <p className="muted">{((ru ? 'Файлы проверенного архива до правок узла. Не включает файлы и данные, подключённые при запуске, и окружение. Без вызова модели.' : uiText('Verified archive files before node edits. Data, mounts, task assets and environment are not added here. No model request.')))}</p>
    <button className="btn sm" disabled={pending} onClick={() => load()}>
      {((pending ? (ru ? 'Чтение…' : uiText('Reading…')) : (ru ? 'Открыть файлы базы' : uiText('Open base files'))))}</button>
    {error && <p className="notice resource-warning" role="status">{seedReadError(error, ru)}</p>}
    {page && <>
      <p className="muted">{((ru ? 'Файлы' : uiText('Files')))} {page.total ? page.offset + 1 : 0}–{page.offset + page.files.length} / {page.total}</p>
      <div className="toolbar">
        <button className="btn sm" disabled={pending || page.offset === 0} onClick={() => load(Math.max(0, page.offset - 100))}>{((ru ? 'Назад' : uiText('Previous')))}</button>
        <button className="btn sm" disabled={pending || page.next_offset === null} onClick={() => load(page.next_offset)}>{((ru ? 'Дальше' : uiText('Next')))}</button>
      </div>
    </>}
    {file && <>
      <div ref={preview} tabIndex={-1} className="section-h">{file.path}</div>
      <p className="muted">{(ru ? `Опыт #${node.id} · попытка ${node.attempt} · база ${baseDigest.slice(0, 12)}. Показаны версии исходных файлов, не снимок работающей программы.` : uiMessage("Experiment #{0} · attempt {1} · base {2}. Source file versions, not a running-program snapshot.", [node.id, node.attempt, baseDigest.slice(0, 12)]))}</p>
      {hasEdit && <>
        <p className="notice compact" role="status">{((mainCode ? (ru ? 'Для solution.py сохранён основной код опыта. Версия базы не включает его. Наличие основного кода не подтверждает, что команда оценивания repo-задачи его запускала.' : uiText('Main code is recorded separately for solution.py. The base version excludes it. Saved main code does not establish that the repo evaluation command executed it.')) : (ru ? 'Для этого файла есть отдельная правка опыта. Версия базы не включает её. Защищённые файлы и файлы задачи могут перекрывать правку при запуске.' : uiText('This file has a separate experiment edit. The base version excludes it. Protected files and task assets may override the edit at runtime.'))))}</p>
        <div className="toolbar" aria-label={((ru ? 'Версия файла' : uiText('File version')))}>
          <button className="btn sm" aria-pressed={version === 'base'} onClick={() => setVersion('base')}>{((ru ? 'Версия базы' : uiText('Base version')))}</button>
          <button className="btn sm" aria-pressed={version === 'edit'} onClick={() => setVersion('edit')}>{((mainCode ? (ru ? 'Основной код опыта' : uiText('Experiment main code')) : (ru ? 'Правка опыта' : uiText('Experiment edit'))))}</button>
        </div>
      </>}
      {overlay.kind === 'deleted' && <p className="notice compact" role="status">{((ru ? 'В сохранённых правках файл удалён. Ниже — прежний файл из архива базы, а не восстановленный файл опыта.' : uiText('Deletion is recorded in the saved edits. Below is the old base file, not a restored experiment file.')))}</p>}
      {overlay.kind === 'unknown' && <p className="notice compact" role="status">{((ru ? 'Правки не проверены. Версию базы нельзя считать итоговым файлом опыта.' : uiText('Edits are unavailable. The base version cannot establish the final experiment file.')))}</p>}
      {overlay.kind === 'path_ambiguity' && <p className="notice compact" role="status">{((ru ? 'В правках есть другое написание этого пути. На разных файловых системах оно может означать тот же файл. Ниже — только версия базы; итоговый файл опыта не установлен. Проверьте имена в сохранённых правках выше.' : uiText('Saved edits contain another spelling of this path. Depending on the filesystem it may name the same file. Below is the base version only; the final experiment file is not established. Check the names in the saved edits above.')))}</p>}
      {version === 'edit' && hasEdit
      ? <CodeViewer code={overlay.text} label={(mainCode ? (ru ? `Основной код #${node.id}: ${file.path}` : uiMessage("Main code #{0}: {1}", [node.id, file.path])) : (ru ? `Правка #${node.id}: ${file.path}` : uiMessage("Edit #{0}: {1}", [node.id, file.path])))}
          language={language} draftStore={draftStore} draftScope={mainCode ? `${draftScope}:main` : `${draftScope}:file:${file.path}`} />
      : file.text_status === 'utf8'
      ? <CodeViewer code={file.text} label={(ru ? `База: ${file.path}` : uiMessage("Base: {0}", [file.path]))} language={language}
          draftStore={draftStore} draftScope={`${draftScope}:base:${file.path}`} />
      : <p className="notice compact" role="status">{file.path}: {((file.text_status === 'too_large' ? (ru ? 'слишком большой файл для текстового просмотра (лимит 256 КиБ).' : uiText('too large for text preview (256 KiB limit).')) : (ru ? 'бинарный файл; текстовый просмотр недоступен.' : uiText('binary file; no text preview.'))))}</p>}
    </>}
    {page && <ul className="recorded-seed-list" aria-label={((ru ? 'Файлы записанной базы' : uiText('Recorded base file list')))}>
      {page.files.map(row => <li key={row.path}>
        <button className="btn sm ghost" disabled={pending} aria-pressed={file?.path === row.path}
          onClick={() => load(page.offset, row.path)}>{row.path}</button>
        <span className="muted"> · {uiText(overlayLabel(recordedFileOverlay(node, row.path).kind))}</span>
        <span className="muted" title={row.sha256}> {row.bytes} B · {row.sha256.slice(0, 12)}{((row.executable ? uiText(' · executable') : ''))}</span>
      </li>)}
    </ul>}
  </section>
}
