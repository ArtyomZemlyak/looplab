import React, { useEffect, useState } from 'react'
import './tracking-panels.css'
import { uiMessage, uiText, useUILanguage } from './uiLanguage.js'
import { get, getBlob, runApiPath } from './api.js'
import Markdown from './markdown.jsx'
import { extensionOf, parseDelimited, prettyJson, previewKind } from './artifactPreviewModel.js'

function artifactQuery(sel, expectedGeneration) {
  const query = new URLSearchParams({ expected_generation: expectedGeneration, root: sel.root, path: sel.path })
  if (sel.nodeId != null) { query.set('node_id', String(sel.nodeId)); query.set('attempt', String(sel.attempt)) }
  return query
}

function ImagePreview({ runId, sel, expectedGeneration }) {
  useUILanguage()
  const [url, setUrl] = useState(null), [failed, setFailed] = useState(false)
  useEffect(() => {
    let alive = true, objectUrl = null
    const controller = new AbortController()
    getBlob(runApiPath(runId, `/artifact/raw?${artifactQuery(sel, expectedGeneration)}`), { signal: controller.signal, cache: 'no-store' })
      .then(blob => { if (alive) { objectUrl = URL.createObjectURL(blob); setUrl(objectUrl) } })
      .catch(() => { if (alive) setFailed(true) })
    return () => { alive = false; controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl) }
  }, [runId, sel.root, sel.path, sel.nodeId, sel.attempt, expectedGeneration])
  if (failed) return <div className="notice">{uiText('The image could not be previewed (unsupported or too large).')}</div>
  if (!url) return <div className="muted">{uiText('Loading…')}</div>
  return <img className="art-image" src={url} alt={uiMessage('Image {0}', [sel.path])} />
}

function MetaPreview({ runId, sel, expectedGeneration, size }) {
  useUILanguage()
  const [meta, setMeta] = useState(null), [failed, setFailed] = useState(false)
  useEffect(() => {
    let alive = true
    const controller = new AbortController()
    get(runApiPath(runId, `/artifact/meta?${artifactQuery(sel, expectedGeneration)}`), { signal: controller.signal, cache: 'no-store' })
      .then(value => { if (alive) setMeta(value) }).catch(() => { if (alive) setFailed(true) })
    return () => { alive = false; controller.abort() }
  }, [runId, sel.root, sel.path, sel.nodeId, sel.attempt, expectedGeneration])
  if (failed) return <div className="notice">{uiText('Binary file — not shown inline.')}</div>
  if (!meta) return <div className="muted">{uiText('Loading…')}</div>
  const st = meta.safetensors, zip = meta.zip, npy = meta.npy
  return <div className="art-meta">
    <div className="notice">{uiText('Binary file — described, not executed.')}</div>
    <dl className="kv">
      <dt>{uiText('kind')}</dt><dd>{meta.kind || uiText('unknown')}</dd>
      <dt>{uiText('size')}</dt><dd>{meta.size ?? size}</dd>
      <dt>{uiText('sha256')}</dt><dd><code>{meta.sha256 || uiText('not computed (file too large)')}</code></dd>
      {st && <><dt>{uiText('tensors')}</dt><dd>{st.tensor_count} · {uiText('parameters')} {st.parameter_count} · {Object.entries(st.dtypes || {}).map(([k, v]) => `${k}×${v}`).join(', ')}</dd></>}
      {npy && <><dt>{uiText('npy')}</dt><dd>{npy.dtype} · [{(npy.shape || []).join(', ')}]</dd></>}
      {zip && <><dt>{uiText('archive members')}</dt><dd>{zip.members}{zip.pickle_inside ? ` · ${uiText('contains a pickle (not loaded)')}` : ''}</dd></>}
    </dl>
    {st?.tensors_sample?.length > 0 && <details><summary>{uiText('Tensor list')}</summary>
      <ul className="bul">{st.tensors_sample.map(t => <li key={t.name}><code>{t.name}</code> {t.dtype} [{t.shape.join(', ')}]</li>)}</ul></details>}
  </div>
}

// The file viewer's body (ArtifactsPanel): one representation per file kind, all from fenced reads.
export default function ArtifactPreview({ runId, sel, content, expectedGeneration }) {
  useUILanguage()
  const kind = previewKind(sel?.path, content)
  if (kind === 'image') return <ImagePreview runId={runId} sel={sel} expectedGeneration={expectedGeneration} />
  if (kind === 'meta') return <MetaPreview runId={runId} sel={sel} expectedGeneration={expectedGeneration} size={content.size} />
  if (kind === 'none') return null
  if (kind === 'json' || kind === 'jsonl') {
    const pretty = prettyJson(content.content, kind === 'jsonl')
    if (pretty != null) return <pre className="art-pre" role="region" aria-label={uiMessage('File {0} contents', [sel.path])} tabIndex={0}>{pretty}</pre>
  }
  if (kind === 'table') {
    const table = parseDelimited(content.content, extensionOf(sel.path) === 'tsv' ? '\t' : ',')
    if (table.header.length > 1) return <div className="md-table-wrap" role="region" aria-label={uiMessage('File {0} contents', [sel.path])} tabIndex={0}>
      <table className="tbl md-table"><thead><tr>{table.header.map((h, i) => <th key={i} scope="col">{h}</th>)}</tr></thead>
        <tbody>{table.rows.map((row, r) => <tr key={r}>{row.map((cell, c) => <td key={c}>{cell}</td>)}</tr>)}</tbody></table>
      {table.truncated && <div className="muted">{uiText('Only the first 200 rows and 32 columns are shown.')}</div>}
    </div>
  }
  if (extensionOf(sel.path) === 'md') return <div className="art-md"><Markdown text={content.content} externalOnly /></div>
  return <pre className="art-pre" role="region" aria-label={uiMessage('File {0} contents', [sel.path])} tabIndex={0}>{content.content}</pre>
}
