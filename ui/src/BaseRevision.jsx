import { uiText, uiMessage, useUILanguage } from './uiLanguage.js'
import React from 'react'
import './baseRevision.css'
import { nodeBase, capabilityOrigin } from './baseRevision.js'

export default function BaseRevision({ node, state, compact = false }) {
  useUILanguage()

  const base = nodeBase(node)
  if (!base) return <span className="muted" title={uiText("No complete recorded seed archive for this result")}>{uiText("Base unknown")}</span>
  const origin = capabilityOrigin(state, base.digest)
  const selection = base.selection
  return <div className={compact ? 'base-revision compact' : 'base-revision'}>
    <span className="muted">{((compact ? uiText('base') : uiText('Evaluated code base')))}</span>{' '}
    <code title={base.digest}>{base.digest.slice(0, 12)}</code>
    {origin && <span title={uiMessage("Capability from experiment #{0}", [origin.source_node_id])}> · {(compact ? `#${origin.source_node_id}` : uiMessage("capability from experiment #{0}", [origin.source_node_id]))}</span>}
    {!compact && <>
      <p className="muted">{((origin?.summary || (selection ? uiMessage("Recorded seed · {0} · event {1}", [(selection.run_dir || '').split(/[\\/]/).filter(Boolean).at(-1), selection.event_seq]) : uiText('Run-owned seed captured before this experiment’s overlay'))))}.</p>
      {base.rebase?.status === 'conflict' && <p className="notice resource-warning">{uiText("Overlay conflicts: ")}{(base.rebase.conflicts || []).join(', ')}{uiText(". This whole experiment kept its prior verified base.")}</p>}
      {base.rebase?.status === 'rebased' && <p className="muted">{uiText("Overlay migrated from ")}{base.rebase.from_digest?.slice(0, 12)}{uiText(". Score was measured after migration.")}</p>}
      {base.rebase?.status !== 'conflict' && base.rebase?.absorbed_paths?.length > 0 && <p className="muted">{uiText("Shared implementation replaced exact inherited copies: ")}{base.rebase.absorbed_paths.join(', ')}{uiText(". Scientific recipe remains separate.")}</p>}
      <details><summary>{uiText("Recorded archive identity")}</summary><code className="base-digest">{base.digest}</code>
        <p>{base.file_count}{uiText(" files · ")}{base.bytes}{uiText(" bytes · scorer boundary ")}{((base.scorer_boundary?.complete ? uiText('recorded') : uiText('unknown')))}</p></details>
    </>}
  </div>
}
