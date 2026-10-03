import React, { useMemo } from 'react'
import CodeViewer from './CodeViewer.jsx'
import { diffLines } from './lineDiff.js'
import { nodeCodeModel } from './nodeCodeModel.js'
import { useInspectorDraftField } from './inspectorDraftStore.js'
import { useAssistantLanguage } from './useAssistantLanguage.js'

function FileEditCode({ row, comparing, language, draftStore, draftScope }) {
  // Live state refreshes must not recompute the LCS of every unchanged source file.
  const diff = useMemo(() => comparing ? diffLines(row.oldText, row.newText) : null,
    [comparing, row.oldText, row.newText])
  return <CodeViewer {...(comparing ? { diff, copyText: row.newText } : { code: row.newText })}
    label={row.path} language={language} allowCopy={row.newKind === 'override'}
    maxHeight={300} draftStore={draftStore} draftScope={draftScope} />
}

export default function NodeCode({ n, state, draftStore, draftScope }) {
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
  return <section aria-label={ru ? 'Код эксперимента' : 'Experiment code'}>
    <p className="muted">{ru
      ? `Сохранённые правки опыта #${n.id}, попытка ${n.attempt ?? '?'}. Унаследованные файлы базы здесь не включены.`
      : `Saved edits of experiment #${n.id}, attempt ${n.attempt ?? '?'}. Inherited base files are not included here.`}</p>
    {model.base && <p className="muted">{ru ? 'Записанная база' : 'Recorded base'}: <code title={model.base.digest}>{model.base.digest.slice(0, 12)}</code></p>}
    <div className="toolbar code-toolbar">
      {model.hasParent && <button className={'btn sm' + (comparing ? ' primary' : '')}
        disabled={!model.available} aria-pressed={comparing} onClick={() => setDiff(value => !value)}>
        {ru ? 'Сравнить правки с родителем' : 'Compare edits with parent'} #{model.parentId}
        {model.available ? ` · ${ru ? 'попытка' : 'attempt'} ${model.parentAttempt}` : ''}
      </button>}
    </div>
    {model.hasParent && !model.available && <p className="notice resource-warning" role="status">{ru
      ? 'Сравнение недоступно: исходные правки родителя не загружены или его попытка изменилась. Обновите детали; текущий код не подменяет прежний.'
      : 'Comparison unavailable: original parent edits are missing or its attempt changed. Refresh details; current code does not replace the original.'}</p>}
    {comparing && <p className="muted">{ru
      ? 'Сравниваются только правки узлов, не полные программы. Снятая правка возвращает файл базы, а не обязательно удаляет его.'
      : 'Only node edits are compared, not full programs. Removing an override restores the base file; it does not necessarily delete it.'}
      {model.parentBase && <> {ru ? 'База родителя' : 'Parent base'}: <code title={model.parentBase.digest}>{model.parentBase.digest.slice(0, 12)}</code>.</>}
      {model.base && model.parentBase && model.base.digest !== model.parentBase.digest
        ? (ru ? ' Базы различаются; этот diff не включает изменения унаследованного кода.' : ' Bases differ; this diff excludes inherited code changes.')
        : (!model.base || !model.parentBase) ? (ru ? ' Совпадение баз не установлено.' : ' Matching bases are not established.') : ''}
    </p>}
    {main && <>
      <div className="section-h">{ru ? 'Основной код' : 'Main code'}</div>
      <CodeViewer {...(comparing ? { diff: main, copyText: model.code } : { code: main })}
        label={ru ? `Основной код #${n.id}` : `Node ${n.id} main code`} language={language}
        allowCopy={Boolean(model.code)} draftStore={draftStore} draftScope={`${draftScope}:main`} />
    </>}
    {rows.length > 0 && <>
      <div className="section-h">{ru ? 'Файлы эксперимента' : 'Experiment files'} <span className="pill">{rows.length}</span></div>
      {rows.map(row => <div key={row.path}>
        <div className="muted helper-file-label">{row.path}{comparing ? ` · ${changeLabel(row)}`
          : row.newKind === 'deleted' ? ` · ${ru ? 'Удаление файла' : 'File deletion'}` : ''}</div>
        {(comparing ? row.oldKind === 'override' || row.newKind === 'override' : row.newKind === 'override') && <FileEditCode
          row={row} comparing={comparing} language={language}
          draftStore={draftStore} draftScope={`${draftScope}:file:${row.path}`} />}
      </div>)}
    </>}
    {!main && rows.length === 0 && <p className="notice compact" role="status">{comparing
      ? (ru ? 'Сохранённые правки не отличаются. Это не доказывает совпадение полных программ.' : 'Saved edits are unchanged. This does not establish that the full programs match.')
      : (ru ? 'Текст правок отсутствует. Это не означает, что у repo-задачи нет кода.' : 'No edit text recorded. This does not mean the repo task has no code.')}</p>}
  </section>
}
