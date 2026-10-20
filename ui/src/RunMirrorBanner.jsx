import React from 'react'
import { get } from './api.js'
import { useScopedResource } from './useScopedResource.js'
import { uiText } from './uiLanguage.js'

// The parent owns language changes. Only source-local status is read, never the mirror target.
export default function RunMirrorBanner() {
  const { data } = useScopedResource(signal => get('/api/run-mirror', { signal }), {
    scope: 'run-mirror', pollMs: 30_000,
  })
  if (!data?.volatile) return null
  return <div className="notice warn" role="status">
    {uiText('Runs are in memory and may be lost on restart.')}{' '}
    {data.last_pass_finished_at && <>{uiText('Last mirror:')} {new Date(data.last_pass_finished_at * 1000).toLocaleString()} </>}
    {data.dst_root ? (data.errors || data.stuck ? uiText('Mirror needs attention.') : null) :
      <>{uiText('Choose a durable folder with')} <code>LOOPLAB_RUN_MIRROR_DST</code>.</>}
  </div>
}
