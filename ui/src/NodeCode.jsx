import { uiText, uiMessage, useUILanguage } from './uiLanguage.js'
import React, { useMemo } from 'react'
import CodeViewer from './CodeViewer.jsx'
import { diffLines } from './lineDiff.js'
import { nodeCodeModel } from './nodeCodeModel.js'
import { useInspectorDraftField } from './inspectorDraftStore.js'
import { useAssistantLanguage } from './useAssistantLanguage.js'
import { nodeBase } from './baseRevision.js'
import RecordedSeedFiles from './RecordedSeedFiles.jsx'

function FileEditCode({ row, comparing, language, draftStore, draftScope }) {
  useUILanguage()

  // Live state refreshes must not recompute the LCS of every unchanged source file.
  const diff = useMemo(() => comparing ? diffLines(row.oldText, row.newText) : null,
    [comparing, row.oldText, row.newText])
  return <CodeViewer {...(comparing ? { diff, copyText: row.newText } : { code: row.newText })}
    label={row.path} language={language} allowCopy={row.newKind === 'override'}
    maxHeight={300} draftStore={draftStore} draftScope={draftScope} />
}

export default function NodeCode({ n, state, runId, expectedGeneration, allowBaseRead = false, draftStore, draftScope }) {
  useUILanguage()

  const [language] = useAssistantLanguage()
  const ru = language === 'ru'
  const [diff, setDiff] = useInspectorDraftField(
    draftStore, draftScope, 'diff', false, { disposable: true })
  const model = useMemo(() => nodeCodeModel(n, state), [n, state])
  const comparing = diff && model.available
  const rows = comparing ? model.rows.filter(row => row.changed)
    : model.rows.filter(row => row.newKind !== 'inherited')
  const changeLabel = row => row.newKind === 'deleted'
    ? (ru ? 'Удаление файла' : 'File deletion')
    : row.newKind === 'inherited'
      ? (ru ? 'Возврат к файлу базы' : 'Return to base file')
      : row.oldKind !== 'override'
        ? (ru ? 'Добавлена правка' : 'Override added')
        : (ru ? 'Изменена правка' : 'Override changed')
  const mainDiff = useMemo(() => comparing && model.mainChanged
    ? diffLines(model.oldCode, model.code) : null, [comparing, model.mainChanged, model.oldCode, model.code])
  const main = comparing ? mainDiff : model.code
  const current = state?.nodes?.[n.id]
  const canReadBase = allowBaseRead && runId && expectedGeneration && model.base
    && n.status === 'evaluated' && current?.status === 'evaluated' && current.attempt === n.attempt
    && nodeBase(current)?.digest === model.base.digest
  return <section aria-label={((ru ? 'Код эксперимента' : uiText('Experiment code')))}>
    <p className="muted">{(ru ? `Сохранённые правки опыта #${n.id}, попытка ${n.attempt ?? '?'}. Унаследованные файлы базы в правки не включены.` : uiMessage("Saved edits of experiment #{0}, attempt {1}. Inherited base files are not included here.", [n.id, n.attempt ?? '?']))}</p>
    {model.base && <p className="muted">{((ru ? 'Записанная база' : uiText('Recorded base')))}: <code title={model.base.digest}>{model.base.digest.slice(0, 12)}</code></p>}
    <div className="toolbar code-toolbar">
      {model.hasParent && <button className={'btn sm' + (comparing ? ' primary' : '')}
        disabled={!model.available} aria-pressed={comparing} onClick={() => setDiff(value => !value)}>
        {((ru ? 'Сравнить правки с родителем' : uiText('Compare edits with parent')))} #{model.parentId}
        {model.available ? ` · ${ru ? 'попытка' : 'attempt'} ${model.parentAttempt}` : ''}
      </button>}
    </div>
    {model.hasParent && !model.available && <p className="notice resource-warning" role="status">{((ru ? 'Сравнение недоступно: исходные правки родителя не загружены или его попытка изменилась. Обновите детали; текущий код не подменяет прежний.' : uiText('Comparison unavailable: original parent edits are missing or its attempt changed. Refresh details; current code does not replace the original.')))}</p>}
    {comparing && <p className="muted">{((ru ? 'Сравниваются только правки узлов, не полные программы. Снятая правка возвращает файл базы, а не обязательно удаляет его.' : uiText('Only node edits are compared, not full programs. Removing an override restores the base file; it does not necessarily delete it.')))}
      {model.parentBase && <> {((ru ? 'База родителя' : uiText('Parent base')))}: <code title={model.parentBase.digest}>{model.parentBase.digest.slice(0, 12)}</code>.</>}
      {((model.base && model.parentBase && model.base.digest !== model.parentBase.digest ? (ru ? ' Базы различаются; этот diff не включает изменения унаследованного кода.' : uiText(' Bases differ; this diff excludes inherited code changes.')) : (!model.base || !model.parentBase ? (ru ? ' Совпадение баз не установлено.' : uiText(' Matching bases are not established.')) : '')))}
    </p>}
    {main && <>
      <div className="section-h">{((ru ? 'Основной код' : uiText('Main code')))}</div>
      <CodeViewer {...(comparing ? { diff: main, copyText: model.code } : { code: main })}
        label={(ru ? `Основной код #${n.id}` : uiMessage("Node {0} main code", [n.id]))} language={language}
        allowCopy={Boolean(model.code)} draftStore={draftStore} draftScope={`${draftScope}:main`} />
    </>}
    {rows.length > 0 && <>
      <div className="section-h">{((ru ? 'Файлы эксперимента' : uiText('Experiment files')))} <span className="pill">{rows.length}</span></div>
      {rows.map(row => <div key={row.path}>
        <div className="muted helper-file-label">{row.path}{comparing ? ` · ${changeLabel(row)}`
          : row.newKind === 'deleted' ? ` · ${ru ? 'Удаление файла' : 'File deletion'}` : ''}</div>
        {(comparing ? row.oldKind === 'override' || row.newKind === 'override' : row.newKind === 'override') && <FileEditCode
          row={row} comparing={comparing} language={language}
          draftStore={draftStore} draftScope={`${draftScope}:file:${row.path}`} />}
      </div>)}
    </>}
    {!main && rows.length === 0 && <p className="notice compact" role="status">{((comparing ? (ru ? 'Сохранённые правки не отличаются. Это не доказывает совпадение полных программ.' : uiText('Saved edits are unchanged. This does not establish that the full programs match.')) : (ru ? 'Текст правок отсутствует. Это не означает, что у repo-задачи нет кода.' : uiText('No edit text recorded. This does not mean the repo task has no code.'))))}</p>}
    {canReadBase && <RecordedSeedFiles key={`${runId}:${expectedGeneration}:${n.id}:${n.attempt}:${model.base.digest}`}
      runId={runId} node={n} generation={expectedGeneration} baseDigest={model.base.digest}
      language={language} draftStore={draftStore} draftScope={draftScope} />}
  </section>
}
