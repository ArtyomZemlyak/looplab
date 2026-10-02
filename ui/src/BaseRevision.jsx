import React from 'react'
import './baseRevision.css'
import { nodeBase, capabilityOrigin } from './baseRevision.js'

export default function BaseRevision({ node, state, compact = false }) {
  const base = nodeBase(node)
  if (!base) return <span className="muted" title="No complete recorded seed archive for this result">Base unknown</span>
  const origin = capabilityOrigin(state, base.digest)
  const selection = base.selection
  return <div className={compact ? 'base-revision compact' : 'base-revision'}>
    <span className="muted">{compact ? 'base' : 'Evaluated code base'}</span>{' '}
    <code title={base.digest}>{base.digest.slice(0, 12)}</code>
    {origin && <span title={`Capability from experiment #${origin.source_node_id}`}> · {compact ? `#${origin.source_node_id}` : `capability from experiment #${origin.source_node_id}`}</span>}
    {!compact && <>
      <p className="muted">{origin?.summary || (selection ? `Recorded seed · ${(selection.run_dir || '').split(/[\\/]/).filter(Boolean).at(-1)} · event ${selection.event_seq}` : 'Run-owned seed captured before this experiment’s overlay')}.</p>
      {base.rebase?.status === 'conflict' && <p className="notice resource-warning">Overlay conflicts: {(base.rebase.conflicts || []).join(', ')}. This whole experiment kept its prior verified base.</p>}
      {base.rebase?.status === 'rebased' && <p className="muted">Overlay migrated from {base.rebase.from_digest?.slice(0, 12)}. Score was measured after migration.</p>}
      {base.rebase?.status !== 'conflict' && base.rebase?.absorbed_paths?.length > 0 && <p className="muted">Shared implementation replaced exact inherited copies: {base.rebase.absorbed_paths.join(', ')}. Scientific recipe remains separate.</p>}
      <details><summary>Recorded archive identity</summary><code className="base-digest">{base.digest}</code>
        <p>{base.file_count} files · {base.bytes} bytes · scorer boundary {base.scorer_boundary?.complete ? 'recorded' : 'unknown'}</p></details>
    </>}
  </div>
}
